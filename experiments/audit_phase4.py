"""Independently reconcile recorded Phase 4 requests, events and summaries.

Read-only with respect to Docker. Writes validation.json and condition-summary.csv
beside the input session. Does not import the summary calculation helpers.
"""
import argparse
from collections import Counter, defaultdict
import csv
import json
import math
from pathlib import Path


def read_csv(path):
    with path.open() as stream:
        return list(csv.DictReader(stream))


def read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def audit(root):
    session = json.loads((root / "session.json").read_text())
    expected_runs = {f"{p}-ft-{ft}-r{r}" for p, ft, r in session["matrix"]}
    summaries = {r["run"]: r for r in read_csv(root / "summary.csv")}
    phase_summaries = {(r["run"], r["phase"]): r for r in read_csv(root / "phases.csv")}
    failures, reviewed, groups, image_sets = [], [], defaultdict(list), []
    def check(condition, message):
        if not condition:
            failures.append(message)
    check(set(summaries) == expected_runs, "summary run coverage differs from the planned matrix")
    for name in sorted(expected_runs):
        folder = root / name
        if not (folder / "run.json").exists():
            failures.append(f"{name}: missing run.json")
            continue
        run = json.loads((folder / "run.json").read_text())
        rows = read_csv(folder / "requests.csv")
        events = read_lines(folder / "events.jsonl")
        samples = read_lines(folder / "metrics.jsonl")
        final = json.loads((folder / "final-metrics.json").read_text())
        summary = summaries.get(name, {})
        check(final['fault_tolerance'] == (run['ft'] == 'on'), f"{name}: recorded FT mode mismatch")
        image_lines = [line for line in (folder / 'commands.txt').read_text().splitlines()
                       if line.startswith('[{') and 'ContainerName' in line]
        check(len(image_lines) == 1, f"{name}: missing or ambiguous image inventory")
        if len(image_lines) == 1:
            image_sets.append({image['ContainerName']: image['ID'] for image in json.loads(image_lines[0])})
        planned = math.ceil(session["rate"] * session["stage_seconds"] * 3)
        check(run["complete"] and len(rows) == planned, f"{name}: incomplete request population")
        check(len({r['request_id'] for r in rows}) == len(rows), f"{name}: duplicate request IDs")
        check({r['request_id'] for r in rows} == {f"{name}-{i}" for i in range(planned)},
              f"{name}: missing/unexpected request IDs")
        counts = Counter(r['success'] for r in rows)
        check(counts['False'] == int(summary.get('failures', -1)), f"{name}: failure total mismatch")
        check(counts['True'] == int(summary.get('successes', -1)), f"{name}: success total mismatch")
        bad = [r for r in rows if r['success'] == 'True' and
               (r['count'] != str(session['expected']) or r['error'])]
        check(not bad, f"{name}: success flags disagree with answers/errors")
        by_id = defaultdict(list)
        assignments = final['selection_events']
        for event in assignments:
            by_id[event.get('correlation_id')].append(event)
        check(len({e['sequence'] for e in assignments}) == len(assignments), f"{name}: duplicate attempt sequence")
        check(sum(b['selections'] for b in final['backends']) == len(assignments),
              f"{name}: cumulative selection counts do not match attempts")
        check(all(b['active'] == 0 for b in final['backends']), f"{name}: sessions still active in final snapshot")
        for row in rows:
            attempts = by_id[row['request_id']]
            encoded = json.loads(row['attempts'])
            check([e['sequence'] for e in attempts] == [e['sequence'] for e in encoded],
                  f"{name}/{row['request_id']}: attempt join mismatch")
            connected = [e for e in attempts if e['outcome'] == 'connected']
            check(len(connected) <= 1, f"{name}/{row['request_id']}: multiple connected backends")
            if row['success'] == 'True':
                check(len(connected) == 1 and row['backend'] == connected[0]['selected_server'],
                      f"{name}/{row['request_id']}: success missing a connected backend")
            check(float(row['completed_at']) >= float(row['started_at']) >= float(row['scheduled_at']) - .01,
                  f"{name}/{row['request_id']}: inconsistent timestamps")
        keyed = {e['kind']: e for e in events if e['kind'] in
                 ('kill_begin', 'kill_end', 'restart_begin', 'restart_end', 'load_start', 'load_end')}
        check(len(keyed) == 6, f"{name}: incomplete lifecycle boundaries")
        if len(keyed) != 6:
            continue
        kill, restart = keyed['kill_begin']['time'], keyed['restart_begin']['time']
        selected = keyed['kill_begin']['window_counts']
        expected_target = min((f'server-{i}' for i in (1, 2, 3)), key=lambda n: (-selected.get(n, 0), n))
        check(run['target'] == expected_target, f"{name}: target does not match the declared selection rule")
        check(not [e for e in events if e['kind'].endswith('_error') or
                   (e['kind'] == 'command' and e['returncode'] != 0)], f"{name}: controller/evidence error")
        for phase, lower, upper in [('normal', run['epoch'], kill), ('fault', kill, restart), ('recovery', restart, math.inf)]:
            part = [r for r in rows if lower <= float(r['started_at']) < upper]
            stored = phase_summaries.get((name, phase), {})
            check(len(part) == int(stored.get('requests', -1)), f"{name}/{phase}: denominator mismatch")
            check(sum(r['success'] == 'False' for r in part) == int(stored.get('failures', -1)),
                  f"{name}/{phase}: error total mismatch")
        gaps = [b['time'] - a['time'] for a, b in zip(samples, samples[1:])]
        detection, recovery = None, None
        if run['ft'] == 'on':
            check(all(next(b['health'] for b in e['connections'] if b['host'] == e['selected_server'])
                      == 'healthy' for e in assignments), f"{name}: unhealthy backend selected")
            for kind, after, state in [('detection', kill, 'unhealthy'), ('recovery', restart, 'healthy')]:
                observed = [s['observed_at'] for s in samples if s['time'] >= after and
                            any(b['host'] == run['target'] and b['health'] == state for b in s['backends'])]
                check(bool(observed), f"{name}: {kind} not observed")
                if observed:
                    value = min(observed) - after
                    check(abs(value - float(summary[f'observed_{kind}_s'])) < 1e-6,
                          f"{name}: {kind} timing mismatch")
                    if kind == 'detection':
                        detection = value
                    else:
                        recovery = value
            check(summary.get('acceptance') == 'pass', f"{name}: FT acceptance needs review")
            if detection is not None:
                stable = [r for r in rows if kill + detection <= float(r['started_at']) < restart]
                check(bool(stable) and all(r['success'] == 'True' for r in stable),
                      f"{name}: stable-down requests missing or failed")
                check(len(stable) == int(summary['stable_down_requests']), f"{name}: stable-down count mismatch")
        returned = [r for r in rows if float(r['started_at']) >= restart
                    and r['success'] == 'True' and r['backend'] == run['target']]
        check(len(returned) == int(summary['successful_requests_on_returned_target']),
              f"{name}: returned-target count mismatch")
        if run['ft'] == 'on':
            check(bool(returned), f"{name}: returned target did not serve a correct reply")
        statuses = []
        for e in events:
            if e['kind'] == 'docker_status' and e['label'] in ('ready', 'failed', 'final'):
                containers = [json.loads(line) for line in e['output'].splitlines()]
                target = next(c for c in containers if c['Service'] == run['target'])
                statuses.append(dict(label=e['label'], state=target['State'], exit_code=target['ExitCode']))
        check(any(s['label'] == 'failed' and s['state'] == 'exited' and s['exit_code'] == 137 for s in statuses),
              f"{name}: SIGKILL exit status not confirmed")
        check(any(s['label'] == 'final' and s['state'] == 'running' for s in statuses),
              f"{name}: target not restored")
        latencies = sorted(float(r['latency_ms']) for r in rows)
        p95 = latencies[math.ceil(.95 * len(latencies)) - 1]
        check(abs(p95 - float(summary.get('p95_latency_ms', -1))) < 1e-6, f"{name}: p95 mismatch")
        item = dict(run=name, requests=len(rows), failures=counts['False'],
                    error_types=dict(Counter(r['error'].split(':')[0] for r in rows if r['success'] == 'False')),
                    sample_count=len(samples), max_sample_gap_s=max(gaps, default=None),
                    max_schedule_lag_ms=max(float(r['schedule_lag_ms']) for r in rows),
                    p95_latency_ms=p95, observed_detection_s=detection, observed_recovery_s=recovery,
                    kill_command_s=keyed['kill_end']['time']-kill,
                    restart_command_s=keyed['restart_end']['time']-restart,
                    target=run['target'], target_statuses=statuses,
                    detection_reasons=[e['reason'] for e in final['health_events']
                                       if e['server'] == run['target'] and e['health'] == 'unhealthy' and e['time'] >= kill],
                    longest_backend_connect_s=max((e.get('connected_at', e.get('completed_at', e['time'])) - e['time']
                                                   for e in assignments), default=0),
                    returned_target_successes=int(summary['successful_requests_on_returned_target']),
                    retry_requests=sum(len(json.loads(r['attempts'])) > 1 for r in rows))
        reviewed.append(item)
        groups[(run['policy'], run['ft'])].append(item)
    check(bool(image_sets) and all(images == image_sets[0] for images in image_sets),
          "container images differ across runs or inventory is missing")
    result = dict(status='pass' if not failures else 'needs-review', source_session=str(root),
                  expected_runs=len(expected_runs), reviewed_runs=len(reviewed),
                  image_ids=image_sets[0] if image_sets else {},
                  checks='All request IDs, answers, attempt joins, counts, phase totals, lifecycle events and timing calculations; no reruns omitted.',
                  findings=failures, runs=reviewed)
    (root / 'validation.json').write_text(json.dumps(result, indent=2) + '\n')
    conditions = []
    for (policy, ft), runs in sorted(groups.items()):
        requests = sum(r['requests'] for r in runs)
        errors = sum(r['failures'] for r in runs)
        delays = [r['observed_detection_s'] for r in runs if r['observed_detection_s'] is not None]
        recovery = [r['observed_recovery_s'] for r in runs if r['observed_recovery_s'] is not None]
        conditions.append(dict(policy=policy, ft=ft, runs=len(runs), requests=requests, failures=errors,
                               failure_percent=100*errors/requests, errors_per_run='/'.join(str(r['failures']) for r in runs),
                               observed_detection_s_range='' if not delays else f'{min(delays):.3f}–{max(delays):.3f}',
                               observed_recovery_s_range='' if not recovery else f'{min(recovery):.3f}–{max(recovery):.3f}',
                               returned_target_successes=sum(r['returned_target_successes'] for r in runs)))
    if conditions:
        with (root / 'condition-summary.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(conditions[0]))
            writer.writeheader()
            writer.writerows(conditions)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path)
    args = parser.parse_args()
    result = audit(args.session)
    print(json.dumps({k: v for k, v in result.items() if k != 'runs'}, indent=2))
    raise SystemExit(bool(result['findings']))
