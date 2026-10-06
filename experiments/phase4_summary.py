"""Derive Phase 4 tables and figures only from recorded observations."""
from collections import Counter
import csv
import json
import math
from pathlib import Path


def json_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def analyze_run(directory):
    run = json.loads((directory / "run.json").read_text())
    with (directory / "requests.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for key in ("started_at", "completed_at", "elapsed_s", "latency_ms", "schedule_lag_ms"):
            row[key] = float(row[key])
        row["success"] = row["success"] == "True"
    events = json_lines(directory / "events.jsonl")
    samples = json_lines(directory / "metrics.jsonl")
    final = json.loads((directory / "final-metrics.json").read_text())
    times = {e["kind"]: e["time"] for e in events if e["kind"] in
             ("kill_begin", "kill_end", "restart_begin", "restart_end")}
    kill, restart = times.get("kill_begin"), times.get("restart_begin")

    def first_observation(after, state):
        if after is None or run["ft"] == "off":
            return None
        return next((s["observed_at"] for s in samples if s["time"] >= after
                     and any(b["host"] == run["target"] and b["health"] == state
                             for b in s["backends"])), None)

    detected = first_observation(kill, "unhealthy")
    recovered = first_observation(restart, "healthy")
    stable = [r for r in rows if detected is not None and restart is not None
              and detected <= r["started_at"] < restart]
    violations = [e for e in final["selection_events"] if run["ft"] == "on"
                  and any(b["host"] == e["selected_server"] and b["health"] != "healthy"
                          for b in e["connections"])]
    returned = [r for r in rows if restart is not None and r["started_at"] >= restart
                and r["backend"] == run["target"] and r["success"]]
    summary = dict(run=directory.name, policy=run["policy"], ft=run["ft"],
                   repetition=run["repetition"], complete=run["complete"], target=run["target"],
                   requests=len(rows), expected_requests=run["expected_requests"],
                   successes=sum(r["success"] for r in rows),
                   failures=sum(not r["success"] for r in rows),
                   wrong_answers=sum(r["error"] == "WrongCount" for r in rows),
                   uncorrelated_successes=sum(r["success"] and not r["backend"] for r in rows),
                   p95_latency_ms=percentile([r["latency_ms"] for r in rows], .95),
                   p95_schedule_lag_ms=percentile([r["schedule_lag_ms"] for r in rows], .95),
                   observed_detection_s=None if detected is None else detected - kill,
                   observed_recovery_s=None if recovered is None else recovered - restart,
                   unhealthy_selection_violations=len(violations),
                   stable_down_requests=len(stable),
                   stable_down_failures=sum(not r["success"] for r in stable),
                   successful_requests_on_returned_target=len(returned),
                   sample_interval_s=run["sample_interval_s"])
    phases = []
    for label, lower, upper in (("normal", run["epoch"], kill),
                                 ("fault", kill, restart),
                                 ("recovery", restart, float("inf"))):
        selected = [r for r in rows if lower is not None and upper is not None
                    and lower <= r["started_at"] < upper]
        phases.append(dict(run=directory.name, phase=label, requests=len(selected),
                           failures=sum(not r["success"] for r in selected),
                           p95_latency_ms=percentile([r["latency_ms"] for r in selected], .95)))
    # Acceptance requires evidence, not just zero observed errors in an empty set.
    summary["acceptance"] = "baseline" if run["ft"] == "off" else (
        "pass" if run["complete"] and len(rows) == run["expected_requests"]
        and not violations and stable and not any(not r["success"] for r in stable)
        and returned and recovered is not None and not summary["wrong_answers"]
        and not summary["uncorrelated_successes"] else "needs-review")
    if not run["complete"] or len(rows) != run["expected_requests"]:
        summary["acceptance"] = "incomplete"
    return summary, phases, rows, run, times, samples


def plot_timeline(directory, rows, run, times, samples):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    seconds = range(math.ceil(run["stage_seconds"] * 3))
    ok = Counter(int(r["elapsed_s"]) for r in rows if r["success"])
    failed = Counter(int(r["elapsed_s"]) for r in rows if not r["success"])
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True, layout="constrained")
    axes[0].bar(seconds, [ok[s] for s in seconds], label="Correct replies", color="#27856c")
    axes[0].bar(seconds, [failed[s] for s in seconds], bottom=[ok[s] for s in seconds],
                label="Errors / wrong answers", color="#ce554c")
    axes[0].set_ylabel("Requests / second")
    axes[0].legend(loc="lower left", bbox_to_anchor=(0, 1.01), ncol=2, frameon=False)
    axes[0].set_ylim(0, max((ok[s] + failed[s] for s in seconds), default=1) * 1.18)
    colors = dict(zip(("server-1", "server-2", "server-3"), ("#246eaf", "#b58425", "#884b9e")))
    for name, color in colors.items():
        counts = Counter(int(r["elapsed_s"]) for r in rows if r["backend"] == name and r["success"])
        axes[1].plot(list(seconds), [counts[s] for s in seconds], label=name, color=color)
    axes[1].set_ylabel("Correct replies / server / s")
    axes[1].legend(loc="lower left", bbox_to_anchor=(0, 1.01), ncol=3, frameon=False)
    health = {"unknown": 0, "unhealthy": 1, "healthy": 2}
    for name in (run["target"],):
        values = [(s["observed_at"] - run["epoch"], health[b["health"]]) for s in samples
                  for b in s["backends"] if b["host"] == name]
        if values:
            axes[2].step([v[0] for v in values], [v[1] for v in values], where="post",
                         label=name, color=colors.get(name), linewidth=2)
    axes[2].set_yticks([0, 1, 2], ["unknown", "unhealthy", "healthy"])
    axes[2].set_ylabel(f"Observed target health ({run['target']})")
    if run["ft"] == "off":
        axes[2].set_yticks([2], ["eligible\n(unverified)"])
        axes[2].set_ylabel(f"Target eligibility ({run['target']})")
    axes[2].set_ylim(-.15, 2.15)
    axes[2].set_xlabel("Seconds since load start (request start time for counts)")
    for ax in axes:
        for key, color in (("kill_begin", "#ce554c"), ("restart_begin", "#27856c")):
            if key in times:
                ax.axvline(times[key] - run["epoch"], linestyle="--", color=color)
        ax.grid(axis="y", alpha=.2)
    for key, label in (("kill_begin", "SIGKILL"), ("restart_begin", "Start")):
        if key in times:
            axes[0].annotate(label, (times[key] - run["epoch"], .94),
                             xycoords=("data", "axes fraction"), xytext=(4, 0),
                             textcoords="offset points", fontsize=9)
    fig.suptitle(f"{run['policy']} · FT {run['ft']} · repetition {run['repetition']} · target {run['target']}"
                 + ("\nFT off: 'healthy' is eligibility, not a health measurement" if run["ft"] == "off" else ""))
    fig.savefig(directory / "timeline.png", dpi=170)
    plt.close(fig)


def summarize(root):
    from experiments.phase4 import write_csv
    root = Path(root)
    summaries, phases = [], []
    for path in sorted(root.glob("*/run.json")):
        directory = path.parent
        if not (directory / "requests.csv").exists():
            continue
        summary, phase_rows, rows, run, times, samples = analyze_run(directory)
        summaries.append(summary)
        phases.extend(phase_rows)
        plot_timeline(directory, rows, run, times, samples)
    if not summaries:
        return
    write_csv(root / "summary.csv", summaries)
    write_csv(root / "phases.csv", phases)
    lines = ["# Recorded Phase 4 results", "",
             "Generated from requests.csv and recorded control/health observations. "
             "Detection/recovery are first-observed delays from the corresponding command start; "
             "polling is nominally 1 Hz. Inspect metrics.jsonl for actual gaps and command times "
             "in events.jsonl. This is not millisecond-accurate failure detection latency.", "",
             "All request errors, including transitions, are included. Latency includes connection setup, "
              "GETROOT, the query and connection cleanup. LRT's internal first-byte EWMA is a different metric.", "",
             "| Run | Target | Requests | Errors | Detection (s) | Recovery (s) | Acceptance |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    session_path = root / "session.json"
    if session_path.exists():
        expected_runs = len(json.loads(session_path.read_text())["matrix"])
        lines[2:2] = [f"Recorded runs: {len(summaries)} / {expected_runs}. "
                      "Only a complete matrix supports the planned comparison.", ""]
    for s in summaries:
        formatted = lambda key: "n/a" if s[key] is None else f"{s[key]:.3f}"
        lines.append(f"| {s['run']} | {s['target']} | {s['requests']} | {s['failures']} | "
                     f"{formatted('observed_detection_s')} | {formatted('observed_recovery_s')} | {s['acceptance']} |")
    lines += ["", "The stable-down interval starts at the first observed unhealthy state and ends "
              "at restart command start. Empty intervals never pass acceptance. Successful requests "
              "on the returned target are required, but equal LRT distribution is not required. "
              "Consult phases.csv, summary.csv and each timeline.png before drawing conclusions.", ""]
    (root / "results.md").write_text("\n".join(lines))
