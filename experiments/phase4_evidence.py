"""Display a compact, traceable view of recorded experiment evidence in a terminal.

This does not rerun Docker or manufacture screenshots. Capture the terminal
yourself when the screen-capture tool cannot access your terminal application.
"""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path


def view(session, policy, scenario, repetition=1):
    ft = 'off' if scenario == 'off-failure' else 'on'
    policy = 'least-connections' if policy == 'lc' else 'lrt'
    directory = session / f'{policy}-ft-{ft}-r{repetition}'
    run = json.loads((directory / 'run.json').read_text())
    events = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines()]
    samples = [json.loads(line) for line in (directory / 'metrics.jsonl').read_text().splitlines()]
    final = json.loads((directory / 'final-metrics.json').read_text())
    with (directory / 'requests.csv').open() as stream:
        rows = list(csv.DictReader(stream))
    control = {e['kind']: e for e in events if e['kind'] in ('kill_begin', 'restart_begin')}
    kill, restart = control['kill_begin']['time'], control['restart_begin']['time']
    recovery = scenario == 'on-recovery'
    lower, upper = (restart, float('inf')) if recovery else (kill, restart)
    period = [r for r in rows if lower <= float(r['started_at']) < upper]
    utc = lambda value: datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds')
    lines = [f'PHASE 4 | {policy} | FT={ft} | repetition={repetition} | {scenario}',
             f'Source: {directory}',
             'Recorded experiment evidence; this command does not run a new live test.',
             f'Target: {run["target"]} | Query: the / mansfield-park | Expected: 6208',
             f'SIGKILL command started: {utc(kill)}', f'Start command began:     {utc(restart)}',
             f'Entire run: {len(rows)} requests, {sum(r["success"] != "True" for r in rows)} errors',
             f'This phase: {len(period)} requests, {sum(r["success"] != "True" for r in period)} errors', '']
    label = 'final' if recovery else 'failed'
    status_event = next(e for e in events if e['kind'] == 'docker_status' and e['label'] == label)
    containers = [json.loads(line) for line in status_event['output'].splitlines()]
    target = next(c for c in containers if c['Service'] == run['target'])
    lines.append(f'Docker recorded {label} state: {target["Service"]} {target["State"]}, exit code {target["ExitCode"]}')
    state = 'healthy' if recovery else 'unhealthy'
    observation = next((s for s in samples if s['time'] >= lower and
                        (ft == 'off' or any(b['host'] == run['target'] and b['health'] == state for b in s['backends']))), None)
    if observation:
        lines += [f'Balancer snapshot at {utc(observation["observed_at"])}',
                  f'{"SERVER":12} {"HEALTH":10} {"ACTIVE":>6} {"ATTEMPTS":>9}']
        for b in observation['backends']:
            health = b['health'] if ft == 'on' else 'disabled'
            lines.append(f'{b["host"]:12} {health:10} {b["active"]:6} {b["selections"]:9}')
    if ft == 'on':
        lines.append('Recorded target health transitions:')
        for e in final['health_events']:
            if e['server'] == run['target']:
                lines.append(f'  {utc(e["time"])} {e["previous"]} -> {e["health"]} ({e["reason"]})')
    if scenario == 'off-failure':
        selected = [r for r in period if r['success'] != 'True'][:6]
        lines.append('First six failed requests during the outage:')
    elif recovery:
        selected = [r for r in period if r['success'] == 'True' and r['backend'] == run['target']][:6]
        lines.append('First correct requests on the returned target:')
    else:
        selected = [r for r in period if observation and float(r['started_at']) >= observation['observed_at']][:6]
        lines.append('First six requests after observed exclusion:')
    lines.append(f'{"ELAPSED":>8} {"RESULT":>8} {"BACKEND / ATTEMPTS":24} ERROR')
    for r in selected:
        attempts = json.loads(r['attempts'])
        backend = r['backend'] or ','.join(e['selected_server'] for e in attempts) or 'none'
        lines.append(f'{float(r["elapsed_s"]):7.3f}s {(r["count"] or "ERROR"):>8} {backend:24} {r["error"].splitlines()[0] if r["error"] else "-"}')
    if not selected:
        lines.append('No matching requests: inspect the raw data before claiming this scenario passed.')
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path)
    parser.add_argument('--policy', choices=('lc', 'lrt'), default='lc')
    parser.add_argument('--scenario', choices=('off-failure', 'on-failure', 'on-recovery'), default='off-failure')
    parser.add_argument('--repetition', type=int, default=1)
    parser.add_argument('--export-all', action='store_true')
    args = parser.parse_args()
    if args.export_all:
        folder = args.session / 'terminal-evidence'
        folder.mkdir(exist_ok=True)
        manifest = []
        for policy in ('lc', 'lrt'):
            for scenario in ('off-failure', 'on-failure', 'on-recovery'):
                repetition = args.repetition
                # Illustrate a real failure; all repetitions remain in the analysis.
                # Selection is explicit, not a claim that this is an average run.
                if scenario == 'off-failure':
                    name = 'least-connections' if policy == 'lc' else policy
                    with (args.session / 'phases.csv').open() as stream:
                        candidates = [int(r['run'].rsplit('-r', 1)[1]) for r in csv.DictReader(stream)
                                      if r['run'].startswith(f'{name}-ft-off-')
                                      and r['phase'] == 'fault' and int(r['failures']) > 0]
                    if candidates:
                        repetition = min(candidates)
                filename = f'{policy}-{scenario}.txt'
                (folder / filename).write_text(view(args.session, policy, scenario, repetition))
                manifest.append(dict(file=filename, policy=policy, scenario=scenario, repetition=repetition))
        (folder / 'selection.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(folder)
    else:
        print(view(args.session, args.policy, args.scenario, args.repetition), end='')


if __name__ == '__main__':
    main()
