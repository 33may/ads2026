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
import math
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


class ActiveConnectionTracker:
    """Thread-safe per-server active counts for direct-server experiments."""

    def __init__(self, servers):
        self.counts = {server: 0 for server in servers}
        self._lock = threading.Lock()

    def connected(self, server):
        with self._lock:
            self.counts[server] += 1
            return dict(self.counts)

    def disconnected(self, server):
        with self._lock:
            self.counts[server] -= 1


def one_user(
    t_sched, keyword, reference, host, port, rows, connection_tracker=None,
    correlation_id=None, timeout=30,
):
    """One independent user: connect, ask once, disconnect. Runs in its own thread."""
    t_start = time.perf_counter()
    ok, conn_ms, rpc_ms = 1, float("nan"), float("nan")
    local_port = None
    active_connections = None
    error_type = ""
    tracked = False
    if connection_tracker is not None:
        # Count the request at admission, including time spent connecting and
        # resolving the RPyC root object under overload.
        active_connections = connection_tracker.connected("server-1")
        tracked = True
    try:
        client = Client(
            host, port, timeout=timeout, correlation_id=correlation_id
        )
        local_port = client.local_port
        conn_ms = (time.perf_counter() - t_start) * 1000
        try:
            client.get_count(keyword, reference)
        finally:
            if client.last_ms is not None:
                rpc_ms = client.last_ms
            client.close()
    except Exception as exc:
        ok = 0
        error_type = type(exc).__name__
    finally:
        if tracked:
            connection_tracker.disconnected("server-1")
    rows.append((
        t_sched, conn_ms, rpc_ms, keyword, reference, ok, local_port,
        active_connections, correlation_id, error_type,
    ))


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


def write_csv(path, rows, t0, label, rate, repetition=1, connection_states=None):
    """Write each response with active counts at connection assignment time."""
    connection_states = connection_states or {}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "label", "rate", "run", "t_s", "conn_ms", "rpc_ms", "total_ms",
            "keyword", "reference", "ok",
            "error_type", "selected_server",
            "server_1_connections",
            "server_2_connections", "server_3_connections",
            "server_1_response_ewma_ms",
            "server_2_response_ewma_ms", "server_3_response_ewma_ms",
        ])
        for row in rows:
            t_sched, conn, rpc, kw, ref, ok = row[:6]
            local_port = row[6] if len(row) > 6 else None
            direct_counts = row[7] if len(row) > 7 else None
            correlation_id = row[8] if len(row) > 8 else None
            error_type = row[9] if len(row) > 9 else ""
            state = connection_states.get(
                correlation_id, connection_states.get(local_port, {})
            )
            counts = direct_counts or state.get("connections", {})
            has_counts = direct_counts is not None or "connections" in state
            response_ewma = state.get("response_ewma_ms", {})
            selected_server = state.get(
                "selected_server", "server-1" if direct_counts else ""
            )
            w.writerow([
                label, rate, repetition, f"{t_sched - t0:.4f}",
                f"{conn:.3f}", f"{rpc:.3f}", f"{conn + rpc:.3f}",
                kw, ref, ok, error_type, selected_server,
                counts.get("server-1", 0) if has_counts else "",
                counts.get("server-2", 0) if has_counts else "",
                counts.get("server-3", 0) if has_counts else "",
                *("" if response_ewma.get(f"server-{number}") is None else
                  f"{response_ewma[f'server-{number}']:.3f}"
                  for number in (1, 2, 3)),
            ])


def summary(rows):
    ok = [r for r in rows if r[5]]
    p = lambda xs, f: xs[min(len(xs) - 1, math.ceil(f * len(xs)) - 1)] if xs else float("nan")
    conn = sorted(r[1] for r in ok)
    rpc = sorted(r[2] for r in ok)
    success_pct = 100 * len(ok) / len(rows) if rows else float("nan")
    if not ok:
        return f"attempted={len(rows)} successful=0 success={success_pct:.1f}%"
    return (
        f"attempted={len(rows)} successful={len(ok)} success={success_pct:.1f}%  "
        f"conn avg={sum(conn)/len(conn):.1f} ms  "
        f"rpc avg={sum(rpc)/len(rpc):.1f} p50={p(rpc, .5):.1f} "
        f"p99={p(rpc, .99):.1f} ms"
    )


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
