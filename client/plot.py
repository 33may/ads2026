"""Latency-vs-rate figures from loadgen CSVs.

Usage: python -m client.plot results/phase2            -> results/phase2/{avg,p99}.png, {avg,p99}-log.png, summary.tsv
Groups files by label (scenario), one line per label, x = rate, y = total_ms (connect + call).
"""
import csv
import glob
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ORDER = ["cache-off", "cache-on"]                  # legend order when these labels exist


def load(directory):
    """{label: {rate: sorted list of rpc_ms}} over every *.csv in the directory."""
    data = defaultdict(dict)
    for path in glob.glob(os.path.join(directory, "*.csv")):
        with open(path) as f:
            rows = [r for r in csv.DictReader(f) if r["ok"] == "1"]
        if not rows:
            continue
        label, rate = rows[0]["label"], float(rows[0]["rate"])
        data[label][rate] = sorted(float(r["total_ms"]) for r in rows)
    return data


def p99(xs):
    return xs[min(len(xs) - 1, int(0.99 * len(xs)))]


def figure(data, stat, ylabel, out, log=False):
    plt.figure(figsize=(5, 3.4))
    labels = sorted(data, key=lambda l: (ORDER.index(l) if l in ORDER else 99, l))
    for label in labels:
        rates = sorted(data[label])
        plt.plot(rates, [stat(data[label][r]) for r in rates], marker="o", label=label)
    plt.xlabel("keyword requests per second")
    plt.ylabel(ylabel)
    if log:
        plt.yscale("log")
    plt.grid(alpha=0.3, which="both")
    plt.legend()
    plt.tight_layout()
    plt.savefig(out, dpi=200)
    plt.close()


def main(directory):
    data = load(directory)
    if not data:
        raise SystemExit(f"no CSVs in {directory}")
    avg = lambda xs: sum(xs) / len(xs)
    figure(data, avg, "average latency (ms)", os.path.join(directory, "avg.png"))
    figure(data, p99, "99th-percentile latency (ms)", os.path.join(directory, "p99.png"))
    figure(data, avg, "average latency (ms, log)", os.path.join(directory, "avg-log.png"), log=True)
    figure(data, p99, "99th-percentile latency (ms, log)", os.path.join(directory, "p99-log.png"), log=True)

    lines = ["label\trate\tn\tavg_ms\tp50_ms\tp99_ms"]
    for label in sorted(data, key=lambda l: (ORDER.index(l) if l in ORDER else 99, l)):
        for rate in sorted(data[label]):
            xs = data[label][rate]
            lines.append(f"{label}\t{rate:g}\t{len(xs)}\t{sum(xs)/len(xs):.1f}\t{xs[len(xs)//2]:.1f}\t{p99(xs):.1f}")
    with open(os.path.join(directory, "summary.tsv"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "results/phase2")
