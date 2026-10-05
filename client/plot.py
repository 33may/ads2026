"""Build assignment-ready latency, reliability, and distribution reports.

Latency figures use ``rpc_ms``: the client-side time from sending ``get_count``
until its response arrives. Connection setup remains in the raw CSV as
``conn_ms`` but is not part of the assignment's execution-latency metric.
"""

import csv
import glob
import math
import os
import statistics
import sys
from collections import Counter, defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


PHASE3_POLICIES = (
    ("least-connections", "3 servers — Least Connections"),
    ("least-response-time", "3 servers — Least Response Time"),
    ("combined-lc-lrt", "3 servers — Combined LC + LRT"),
)

# Every model/cache combination has its own color. Markers identify the model,
# while line styles identify the warm-up count. This avoids assigning the same
# color to six cache/warm-up variants in the combined reports.
MODEL_STYLES = {
    "direct": {"marker": "o"},
    "least-connections": {"marker": "s"},
    "least-response-time": {"marker": "^"},
    "combined-lc-lrt": {"marker": "D"},
}
MODEL_CACHE_COLORS = {
    ("direct", "off"): "#1F4E79",
    ("direct", "on"): "#56B4E9",
    ("least-connections", "off"): "#D55E00",
    ("least-connections", "on"): "#E69F00",
    ("least-response-time", "off"): "#006D5B",
    ("least-response-time", "on"): "#2CA02C",
    ("combined-lc-lrt", "off"): "#6A3D9A",
    ("combined-lc-lrt", "on"): "#E377C2",
}
WARMUP_LINESTYLES = {
    "0": "-",
    "100": "--",
    "500": ":",
}
CONDITIONS = tuple(
    f"cache-{cache}-warm{warmup}"
    for cache in ("off", "on")
    for warmup in (0, 100, 500)
)
ORDER = [
    *CONDITIONS,
    *[
        f"{policy}-{condition}"
        for condition in CONDITIONS
        for policy, _ in PHASE3_POLICIES
    ],
]


def p99(values):
    """Nearest-rank 99th percentile for an already sorted non-empty list."""
    return values[min(len(values) - 1, math.ceil(0.99 * len(values)) - 1)]


def _metric(function, values):
    return function(values) if values else float("nan")


def _series_style(label):
    """Return a stable visual identity for one model and test condition."""
    model = "direct"
    condition = label
    for policy, _description in PHASE3_POLICIES:
        prefix = f"{policy}-"
        if label.startswith(prefix):
            model = policy
            condition = label[len(prefix):]
            break
    cache = "on" if condition.startswith("cache-on-") else "off"
    warmup = condition.rsplit("warm", 1)[-1]
    return {
        **MODEL_STYLES[model],
        "color": MODEL_CACHE_COLORS[(model, cache)],
        "linestyle": WARMUP_LINESTYLES.get(warmup, "-"),
        "linewidth": 2,
    }


def load(directory):
    """Return every raw repetition grouped by scenario label and offered rate."""
    data = defaultdict(lambda: defaultdict(list))
    for path in glob.glob(os.path.join(directory, "**", "*.csv"), recursive=True):
        with open(path, newline="") as stream:
            rows = list(csv.DictReader(stream))
        if not rows or "ok" not in rows[0]:
            continue

        label = rows[0]["label"]
        rate = float(rows[0]["rate"])
        successful = [row for row in rows if row["ok"] == "1"]
        latencies = sorted(
            float(row["rpc_ms"])
            for row in successful
            if row.get("rpc_ms") not in (None, "", "nan")
        )
        connections = {
            server: [
                int(row[f"server_{server}_connections"])
                for row in rows
                if row.get(f"server_{server}_connections") not in (None, "")
            ]
            for server in (1, 2, 3)
        }
        response_ewma = {
            server: [
                float(row[f"server_{server}_response_ewma_ms"])
                for row in rows
                if row.get(f"server_{server}_response_ewma_ms")
                not in (None, "", "nan")
            ]
            for server in (1, 2, 3)
        }
        selections = Counter(
            row.get("selected_server", "") for row in rows
            if row.get("selected_server", "")
        )
        errors = Counter(
            row.get("error_type") or "unspecified"
            for row in rows if row["ok"] != "1"
        )
        # The generator schedules exactly rate * configured-duration attempts.
        # This remains reproducible for legacy CSVs without an explicit duration.
        duration = len(rows) / rate if rate else float("nan")
        data[label][rate].append({
            "path": path,
            "run": int(rows[0].get("run") or 1),
            "latencies": latencies,
            "attempted": len(rows),
            "successful": len(successful),
            "failed": len(rows) - len(successful),
            "duration": duration,
            "connections": connections,
            "response_ewma": response_ewma,
            "selections": selections,
            "errors": errors,
        })
    return data


def figure(
    data, stat, ylabel, output, log=False, labels=None,
    display_labels=None, title=None,
):
    fig, axis = plt.subplots(figsize=(8, 3.8))
    labels = labels or sorted(
        data, key=lambda label: (ORDER.index(label) if label in ORDER else 99, label)
    )
    plotted = False
    for label in labels:
        if label not in data:
            continue
        rates = sorted(data[label])
        values = []
        for rate in rates:
            per_run = [
                _metric(stat, run["latencies"])
                for run in data[label][rate]
                if run["latencies"]
            ]
            values.append(statistics.mean(per_run) if per_run else float("nan"))
        axis.plot(
            rates, values,
            label=(display_labels or {}).get(label, label),
            **_series_style(label),
        )
        plotted = True
    axis.set_xlabel("offered keyword requests per second")
    axis.set_ylabel(ylabel)
    if title:
        axis.set_title(title)
    if log:
        axis.set_yscale("log")
    axis.grid(alpha=0.3, which="both")
    if plotted:
        axis.legend(loc="center left", bbox_to_anchor=(1.02, 0.5))
    fig.tight_layout()
    fig.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(fig)


def operational_figure(data, value, ylabel, output, labels=None, title=None):
    """Plot a per-run operational metric without treating it as latency."""
    fig, axis = plt.subplots(figsize=(8, 3.8))
    labels = labels or sorted(
        data, key=lambda label: (ORDER.index(label) if label in ORDER else 99, label)
    )
    plotted = False
    for label in labels:
        if label not in data:
            continue
        rates = sorted(data[label])
        values = [
            statistics.mean(value(run) for run in data[label][rate])
            for rate in rates
        ]
        axis.plot(rates, values, label=label, **_series_style(label))
        plotted = True
    axis.set_xlabel("offered keyword requests per second")
    axis.set_ylabel(ylabel)
    if title:
        axis.set_title(title)
    axis.grid(alpha=0.3)
    if plotted:
        axis.legend(loc="center left", bbox_to_anchor=(1.02, 0.5))
    fig.tight_layout()
    fig.savefig(output, dpi=200, bbox_inches="tight")
    plt.close(fig)


def _connection_statistics(runs, server):
    per_run = [
        statistics.mean(run["connections"][server])
        for run in runs if run["connections"][server]
    ]
    average = statistics.mean(per_run) if per_run else 0
    maximum = max(
        (max(run["connections"][server]) for run in runs
         if run["connections"][server]),
        default=0,
    )
    return average, maximum


def _average_samples(runs, field, server):
    """Average a sampled per-server field, weighting repetitions equally."""
    per_run = [
        statistics.mean(run[field][server])
        for run in runs if run[field][server]
    ]
    return statistics.mean(per_run) if per_run else float("nan")


def _run_row(label, rate, run):
    attempted = run["attempted"]
    successful = run["successful"]
    selected = [run["selections"].get(f"server-{server}", 0) for server in (1, 2, 3)]
    selected_total = sum(selected)
    latency = run["latencies"]
    connection_stats = [
        _connection_statistics([run], server) for server in (1, 2, 3)
    ]
    response_ewma = [
        _average_samples([run], "response_ewma", server)
        for server in (1, 2, 3)
    ]
    return [
        label, f"{rate:g}", str(run["run"]), str(attempted), str(successful),
        str(run["failed"]), f"{100 * successful / attempted:.2f}" if attempted else "nan",
        f"{successful / run['duration']:.2f}" if run["duration"] else "nan",
        f"{_metric(statistics.mean, latency):.3f}",
        f"{_metric(statistics.median, latency):.3f}",
        f"{_metric(p99, latency):.3f}",
        *map(str, selected),
        *(
            f"{100 * count / selected_total:.2f}" if selected_total else "0.00"
            for count in selected
        ),
        *(f"{average:.3f}" for average, _ in connection_stats),
        *(str(maximum) for _, maximum in connection_stats),
        *(f"{value:.3f}" for value in response_ewma),
        ";".join(f"{name}:{count}" for name, count in sorted(run["errors"].items())),
        run["path"],
    ]


RUN_HEADER = [
    "label", "rate", "run", "attempted", "successful", "failed",
    "success_pct", "achieved_success_rps", "avg_rpc_ms", "p50_rpc_ms",
    "p99_rpc_ms", "server_1_selections", "server_2_selections",
    "server_3_selections", "server_1_share_pct", "server_2_share_pct",
    "server_3_share_pct", "avg_server_1_connections",
    "avg_server_2_connections", "avg_server_3_connections",
    "max_server_1_connections", "max_server_2_connections",
    "max_server_3_connections", "avg_server_1_response_ewma_ms",
    "avg_server_2_response_ewma_ms", "avg_server_3_response_ewma_ms",
    "errors", "source_csv",
]


def write_reports(data, directory):
    run_rows = []
    summary_rows = []
    labels = sorted(
        data, key=lambda label: (ORDER.index(label) if label in ORDER else 99, label)
    )
    for label in labels:
        for rate in sorted(data[label]):
            runs = sorted(data[label][rate], key=lambda run: run["run"])
            run_rows.extend(_run_row(label, rate, run) for run in runs)

            attempted = sum(run["attempted"] for run in runs)
            successful = sum(run["successful"] for run in runs)
            latencies = [run["latencies"] for run in runs if run["latencies"]]
            averages = [statistics.mean(values) for values in latencies]
            medians = [statistics.median(values) for values in latencies]
            p99s = [p99(values) for values in latencies]
            selected = [
                sum(run["selections"].get(f"server-{server}", 0) for run in runs)
                for server in (1, 2, 3)
            ]
            selected_total = sum(selected)
            connection_stats = [
                _connection_statistics(runs, server) for server in (1, 2, 3)
            ]
            response_ewma = [
                _average_samples(runs, "response_ewma", server)
                for server in (1, 2, 3)
            ]
            errors = sum((run["errors"] for run in runs), Counter())
            summary_rows.append([
                label, f"{rate:g}", str(len(runs)), str(attempted), str(successful),
                str(attempted - successful),
                f"{100 * successful / attempted:.2f}" if attempted else "nan",
                f"{statistics.mean(run['successful'] / run['duration'] for run in runs):.2f}",
                f"{_metric(statistics.mean, averages):.3f}",
                f"{statistics.pstdev(averages):.3f}" if averages else "nan",
                f"{_metric(statistics.mean, medians):.3f}",
                f"{_metric(statistics.mean, p99s):.3f}",
                *map(str, selected),
                *(
                    f"{100 * count / selected_total:.2f}" if selected_total else "0.00"
                    for count in selected
                ),
                *(f"{average:.3f}" for average, _ in connection_stats),
                *(str(maximum) for _, maximum in connection_stats),
                *(f"{value:.3f}" for value in response_ewma),
                ";".join(
                    f"{name}:{count}" for name, count in sorted(errors.items())
                ),
            ])

    summary_header = RUN_HEADER[:-1]
    summary_header[2] = "runs"
    summary_header.insert(9, "avg_rpc_stddev_ms")
    with open(os.path.join(directory, "runs.tsv"), "w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(RUN_HEADER)
        writer.writerows(run_rows)
    with open(os.path.join(directory, "summary.tsv"), "w", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        writer.writerow(summary_header)
        writer.writerows(summary_rows)
    print("\t".join(summary_header))
    for row in summary_rows:
        print("\t".join(row))


def comparison_figures(data, directory):
    """Create the rubric comparison: one server versus every cluster policy."""
    for condition in CONDITIONS:
        required = [
            condition,
            f"least-connections-{condition}",
            f"least-response-time-{condition}",
        ]
        if not all(label in data for label in required):
            continue
        labels = [condition, *[
            f"{policy}-{condition}" for policy, _ in PHASE3_POLICIES
            if f"{policy}-{condition}" in data
        ]]
        display = {condition: "1 server — direct"}
        display.update({
            f"{policy}-{condition}": description
            for policy, description in PHASE3_POLICIES
        })
        title = condition.replace("-", " ")
        figure(
            data, statistics.mean,
            "average successful-request RPC latency (ms)",
            os.path.join(directory, f"comparison-{condition}-avg.png"),
            labels=labels, display_labels=display, title=title,
        )
        figure(
            data, p99,
            "99th-percentile successful-request RPC latency (ms)",
            os.path.join(directory, f"comparison-{condition}-p99.png"),
            labels=labels, display_labels=display, title=title,
        )


def main(directory):
    data = load(directory)
    if not data:
        raise SystemExit(f"no CSVs in {directory}")
    figure(
        data, statistics.mean,
        "average successful-request RPC latency (ms)",
        os.path.join(directory, "avg.png"),
    )
    figure(
        data, p99,
        "99th-percentile successful-request RPC latency (ms)",
        os.path.join(directory, "p99.png"),
    )
    figure(
        data, statistics.mean,
        "average successful-request RPC latency (ms, log)",
        os.path.join(directory, "avg-log.png"), log=True,
    )
    figure(
        data, p99,
        "99th-percentile successful-request RPC latency (ms, log)",
        os.path.join(directory, "p99-log.png"), log=True,
    )
    operational_figure(
        data,
        lambda run: 100 * run["successful"] / run["attempted"]
        if run["attempted"] else float("nan"),
        "successful requests (%)",
        os.path.join(directory, "success-rate.png"),
    )
    operational_figure(
        data,
        lambda run: run["successful"] / run["duration"],
        "successful responses per second",
        os.path.join(directory, "achieved-throughput.png"),
    )
    write_reports(data, directory)
    comparison_figures(data, directory)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/phase2")
