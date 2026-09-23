"""Open-loop load generator for the word-count service.

Every 1/rate seconds a new independent user appears (own thread): connect, one
get_count, disconnect. Users never wait for each other, so all queueing happens on
the server side and shows up in the measured latency instead of being hidden.

Usage: python -m client.loadgen --rate 50 --duration 20 --label both --out results/both-50.csv
Every request is a fresh client (connect, one call, close), so connection cost is part of
what a user sees and the load balancer in Phase 3 gets to balance every request.
Reusable across phases: point --host/--port at the load balancer.
"""
import argparse
import csv
import os
import random
import threading
import time

from client.api import Client, DEFAULT_HOST, DEFAULT_PORT

# 20 English words spanning frequency in the Gutenberg corpus: from "the" (in every
# book, ~600k hits) down to "whale" (in 20 books, 92 hits), so counts range 0..thousands.
KEYWORDS = ["the", "and", "of", "was", "he", "she", "you",
            "house", "night", "heart", "love", "death", "king", "sea",
            "money", "letter", "london", "horse", "castle", "whale"]


# --- workload -------------------------------------------------------------

def make_workload(references, mix, seed):
    """Return a zero-arg function that yields the next (keyword, reference) pair."""
    rng = random.Random(seed)
    pairs = [(k, r) for k in KEYWORDS for r in references]
    if mix == "uniform":
        return lambda: rng.choice(pairs)
    # zipf: a few pairs are hot, most are cold (weight 1/rank)
    weights = [1.0 / (i + 1) for i in range(len(pairs))]
    return lambda: rng.choices(pairs, weights)[0]


# --- engine ---------------------------------------------------------------

def one_user(t_sched, keyword, reference, host, port, rows):
    """One independent user: connect, ask once, disconnect. Runs in its own thread."""
    t_start = time.perf_counter()
    ok, conn_ms, rpc_ms = 1, float("nan"), float("nan")
    try:
        client = Client(host, port)
        conn_ms = (time.perf_counter() - t_start) * 1000
        try:
            client.get_count(keyword, reference)
            rpc_ms = client.last_ms
        finally:
            client.close()
    except Exception:
        ok = 0
    rows.append((t_sched, conn_ms, rpc_ms, keyword, reference, ok))


def _sleep_until(t):
    """time.sleep overshoots by ms on macOS; sleep coarsely, then finish in 1 ms steps."""
    while (d := t - time.perf_counter()) > 0:
        time.sleep(d - 0.002 if d > 0.003 else 0.001)


def run(host, port, rate, duration, mix, warmup, seed):
    with Client(host, port) as c:
        references = c.list_references()
    if not references:
        raise SystemExit("no references seeded — run `make seed` first")
    next_pair = make_workload(references, mix, seed)

    rows, users = [], []
    # open loop: a new user every 1/rate seconds, on the clock, regardless of replies
    period = 1.0 / rate
    t0 = time.perf_counter()
    n = 0
    while True:
        t_sched = t0 + n * period
        if t_sched - t0 >= duration:
            break
        _sleep_until(t_sched)
        u = threading.Thread(target=one_user, args=(t_sched, *next_pair(), host, port, rows), daemon=True)
        u.start()
        users.append(u)
        n += 1
    for u in users:
        u.join()

    rows.sort()
    cutoff = t0 + warmup
    return [r for r in rows if r[0] >= cutoff], t0


def write_csv(path, rows, t0, label, rate):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["label", "rate", "t_s", "conn_ms", "rpc_ms", "total_ms", "keyword", "reference", "ok"])
        for t_sched, conn, rpc, kw, ref, ok in rows:
            w.writerow([label, rate, f"{t_sched - t0:.4f}", f"{conn:.3f}", f"{rpc:.3f}", f"{conn + rpc:.3f}", kw, ref, ok])


def summary(rows):
    ok = [r for r in rows if r[5]]
    p = lambda xs, f: xs[min(len(xs) - 1, int(f * len(xs)))] if xs else float("nan")
    conn = sorted(r[1] for r in ok)
    rpc = sorted(r[2] for r in ok)
    tot = sorted(r[1] + r[2] for r in ok)
    return (f"n={len(rows)} ok={len(ok)}  "
            f"conn avg={sum(conn)/len(conn):.1f}  rpc avg={sum(rpc)/len(rpc):.1f}  "
            f"total avg={sum(tot)/len(tot):.1f} p50={p(tot,.5):.1f} p99={p(tot,.99):.1f} ms")


def main():
    ap = argparse.ArgumentParser(prog="loadgen", description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--rate", type=float, required=True, help="requests per second")
    ap.add_argument("--duration", type=float, default=20, help="seconds, including warm-up")
    ap.add_argument("--mix", choices=["uniform", "zipf"], default="uniform")
    ap.add_argument("--warmup", type=float, default=0, help="seconds discarded from the start")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--label", default="run", help="tag stored in the CSV, e.g. cache mode")
    ap.add_argument("--out", help="CSV path; default results/<label>-<rate>.csv")
    a = ap.parse_args()

    rows, t0 = run(a.host, a.port, a.rate, a.duration, a.mix, a.warmup, a.seed)
    out = a.out or f"results/{a.label}-{int(a.rate)}.csv"
    write_csv(out, rows, t0, a.label, a.rate)
    print(f"{a.label} @ {a.rate:g} req/s  {summary(rows)}  -> {out}")


if __name__ == "__main__":
    main()
