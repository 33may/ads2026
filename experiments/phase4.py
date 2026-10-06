"""User-run, open-loop replica-failure experiment. See docs/phase4.md.

This controller recreates the cluster, kills one replica, then restarts it.
Nothing touches Docker on import or with --dry-run.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

SERVERS = [f"server-{i}" for i in range(1, 4)]
REFERENCE = "mansfield-park"
KEYWORD = "the"
EXPECTED = 6208  # checked against the repository's corpus before any Docker action


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def metrics(port):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.settimeout(2)
        with sock.makefile("rb") as stream:
            return json.loads(stream.readline())


def choose_target(events, now, window=5):
    counts = Counter(e["selected_server"] for e in events if now - window <= e["time"] <= now)
    if not counts:
        raise RuntimeError("no selections in the target window; cannot inject a meaningful failure")
    return min(SERVERS, key=lambda name: (-counts[name], name)), dict(counts)


def join_attempts(rows, events):
    by_id = defaultdict(list)
    for event in events:
        by_id[event.get("correlation_id")].append(event)
    for row in rows:
        attempts = by_id[row["request_id"]]
        connected = [e for e in attempts if e.get("outcome") == "connected"]
        row["backend"] = connected[-1]["selected_server"] if connected else ""
        row["attempts"] = json.dumps([
            {k: e.get(k) for k in ("sequence", "time", "selected_server", "outcome", "error", "stream_error")}
            for e in attempts
        ])
    return rows


class Controller:
    def __init__(self, args, directory, policy, ft):
        self.args, self.directory = args, directory
        self.env = dict(os.environ, CACHE="on", LB_ALGORITHM=policy, FAULT_TOLERANCE=ft)
        self.compose = ["docker", "compose", "--env-file", str(args.env_file),
                        "-f", str(ROOT / "compose.cluster.yml")]
        self.events = []

    def event(self, kind, **fields):
        event = dict(kind=kind, time=time.time(), **fields)
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(event) + "\n")
        return event

    def command(self, *args, check=True, timeout=120, record=True):
        command = self.compose + list(args)
        started = time.time()
        try:
            result = subprocess.run(command, cwd=ROOT, env=self.env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if record:
                self.event("command_error", command=args, started_at=started, error=str(exc))
            raise
        if record:
            self.event("command", command=args, started_at=started, returncode=result.returncode)
            with (self.directory / "commands.txt").open("a") as stream:
                stream.write(f"\n$ {' '.join(command)}\n[{started:.6f} .. {time.time():.6f}]\n{result.stdout}\n")
        if check and result.returncode:
            raise RuntimeError(f"Docker command failed: {' '.join(args)}; see commands.txt")
        return result.stdout

    def status(self, label):
        output = self.command("ps", "--all", "--format", "json", timeout=10)
        self.event("docker_status", label=label, output=output)


def one_request(client_type, port, request_id, scheduled, epoch):
    started = time.time()
    timer = time.perf_counter()
    row = dict(request_id=request_id, scheduled_at=scheduled, started_at=started,
               elapsed_s=started - epoch, schedule_lag_ms=(started - scheduled) * 1000,
               completed_at=None, latency_ms=None, rpc_ms=None,
               success=False, count=None, expected=EXPECTED, error="")
    try:
        with client_type("127.0.0.1", port, timeout=2, correlation_id=request_id) as client:
            row["count"] = int(client.get_count(KEYWORD, REFERENCE))
            row["rpc_ms"] = client.last_ms
            row["success"] = row["count"] == EXPECTED
            if not row["success"]:
                row["error"] = "WrongCount"
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["latency_ms"] = (time.perf_counter() - timer) * 1000
    row["completed_at"] = time.time()
    return row


def produce_load(client_type, port, run_id, args, epoch, start, stop, rows, errors):
    # The scheduler runs independently of blocking Docker kill/start commands.
    # Queueing is visible as schedule_lag_ms, rather than silently dropping load.
    futures = []
    try:
        with ThreadPoolExecutor(max_workers=128) as pool:
            for index in range(math.ceil(args.rate * args.stage_seconds * 3)):
                delay = start + index / args.rate - time.monotonic()
                if stop.wait(max(0, delay)):
                    break
                futures.append(pool.submit(one_request, client_type, port,
                                           f"{run_id}-{index}", epoch + index / args.rate, epoch))
            for future in futures:
                rows.append(future.result())
    except BaseException as exc:
        errors.append(f"{type(exc).__name__}: {exc}")


def ready(controller, port, metrics_port, ft, client_type):
    deadline = time.monotonic() + 60
    pending = set(SERVERS)
    code = ("import os,rpyc; c=rpyc.connect('localhost',int(os.environ['SERVER_PORT']),"
            "config={'sync_request_timeout':2}); print(c.root.ping()); c.close()")
    while time.monotonic() < deadline:
        for server in sorted(pending):
            # Direct RPC readiness also works when FT is off. These connections
            # do not pass through the balancer and do not train its policy.
            output = controller.command("exec", "-T", server, "python", "-c", code,
                                        check=False, timeout=10)
            if f"pong from {server}" in output:
                pending.remove(server)
        try:
            snapshot = metrics(metrics_port)
            health_ready = ft == "off" or all(b["health"] == "healthy" for b in snapshot["backends"])
            if not pending and health_ready:
                with client_type("127.0.0.1", port, timeout=3) as client:
                    # Upload the canonical source each run, retaining the volume
                    # and all other books while fixing accidental demo edits.
                    text = (ROOT / "texts/gutenberg/mansfield-park.txt").read_text()
                    client.upload_text(REFERENCE, text)
                    client.cache_flush()
                    for _ in range(9):
                        if client.get_count(KEYWORD, REFERENCE) != EXPECTED:
                            raise ValueError("canonical word-count oracle mismatch")
                controller.event("ready", expected=EXPECTED, health=snapshot["backends"])
                return
        except (OSError, EOFError, TimeoutError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"cluster did not become ready; pending replicas: {sorted(pending)}")


def run_condition(args, directory, policy, ft, repetition, client_type):
    directory.mkdir()
    controller = Controller(args, directory, policy, ft)
    # Resolve Compose variables without persisting credentials from its output.
    config = json.loads(controller.command("config", "--format", "json", record=False))
    port = int(config["services"]["lb"]["environment"]["SERVER_PORT"])
    metrics_port = int(config["services"]["lb"]["environment"]["LB_METRICS_PORT"])
    controller.command("up", "-d", "--no-build", "--wait", "redis", "minio")
    # Previous measured sessions have drained. Recreate only the application
    # processes, using the same images built once for the entire matrix.
    controller.command("up", "-d", "--no-deps", "--no-build", "--force-recreate",
                       "--timeout", "1", *SERVERS, "lb", "client")
    ready(controller, port, metrics_port, ft, client_type)
    controller.command("images", "--format", "json")
    controller.status("ready")
    epoch, start = time.time(), time.monotonic()
    controller.event("load_start", epoch=epoch, rate=args.rate)
    stopped = None
    rows, errors, observations = [], [], []
    stop = threading.Event()
    producer = threading.Thread(target=produce_load,
                                args=(client_type, port, directory.name, args, epoch, start, stop, rows, errors))
    producer.start()
    next_sample, killed, restarted = start, False, False
    target = None
    completed = False
    try:
        while time.monotonic() < start + args.stage_seconds * 3:
            elapsed = time.monotonic() - start
            if elapsed >= args.stage_seconds and not killed:
                snapshot = metrics(metrics_port)
                target, counts = choose_target(snapshot["selection_events"], snapshot["time"])
                stopped = target  # set BEFORE kill, so interruption still attempts restoration
                controller.event("kill_begin", server=target, window_counts=counts)
                controller.command("kill", "-s", "SIGKILL", target, timeout=15)
                controller.event("kill_end", server=target)
                killed = True
                controller.status("failed")
                print(f"{directory.name}: SIGKILL {target}; recent selections={counts}", flush=True)
            if elapsed >= args.stage_seconds * 2 and killed and not restarted:
                controller.event("restart_begin", server=target)
                controller.command("start", target, timeout=15)
                controller.event("restart_end", server=target)
                restarted = True
                stopped = None
                controller.status("restarting")
            if time.monotonic() >= next_sample:
                sampled_at = time.time()
                try:
                    snapshot = metrics(metrics_port)
                    observation = dict(time=sampled_at, observed_at=time.time(),
                                       backends=snapshot["backends"],
                                       last_selection_sequence=snapshot["last_selection_sequence"])
                    observations.append(observation)
                    with (directory / "metrics.jsonl").open("a") as stream:
                        stream.write(json.dumps(observation) + "\n")
                    # Record Docker lifecycle independently of balancer health.
                    controller.status("sample")
                except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    controller.event("sample_error", error=str(exc))
                next_sample += 1
                if next_sample < time.monotonic():
                    next_sample = time.monotonic() + 1
            time.sleep(0.01)
        completed = True
    finally:
        stop.set()
        if stopped:
            try:
                controller.command("start", stopped, timeout=30)
                controller.event("cleanup_restored", server=stopped)
            except Exception as exc:
                controller.event("cleanup_failed", server=stopped, error=str(exc))
                print(f"Restore manually: make replica-start N={stopped[-1]}", file=sys.stderr)
        producer.join()
        controller.event("load_end", complete=completed, producer_errors=errors)
        try:
            final = metrics(metrics_port)
        except (OSError, ValueError) as exc:
            controller.event("final_metrics_error", error=str(exc))
            final = {"selection_events": [], "health_events": [], "backends": []}
            completed = False
        join_attempts(rows, final["selection_events"])
        write_csv(directory / "requests.csv", sorted(rows, key=lambda r: r["scheduled_at"]))
        write_json(directory / "final-metrics.json", final)
        write_json(directory / "run.json", dict(policy=policy, ft=ft, repetition=repetition,
                   epoch=epoch, target=target, stage_seconds=args.stage_seconds,
                   rate=args.rate, complete=completed and not errors,
                   expected_requests=math.ceil(args.rate * args.stage_seconds * 3),
                   recorded_requests=len(rows), sample_interval_s=1,
                   health_config=final.get("health_config"), producer_errors=errors))
        try:
            logs = controller.command("logs", "--no-color", "--timestamps", "lb", *SERVERS, timeout=30)
            (directory / "logs.txt").write_text(logs)
            controller.status("final")
        except Exception as exc:
            controller.event("evidence_error", error=str(exc))
    if errors:
        raise RuntimeError(f"load producer failed: {errors}")


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policies", nargs="+", choices=("least-connections", "lrt"),
                        default=["least-connections", "lrt"])
    parser.add_argument("--ft", nargs="+", choices=("off", "on"), default=["off", "on"])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--rate", type=float, default=20)
    parser.add_argument("--stage-seconds", type=float, default=20)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="print matrix; do not touch Docker or files")
    parser.add_argument("--summarize", type=Path, help="regenerate tables/figures from existing run files only")
    args = parser.parse_args(argv)
    if (args.repetitions < 1 or not math.isfinite(args.rate) or args.rate <= 0
            or not math.isfinite(args.stage_seconds) or args.stage_seconds < 6):
        parser.error("positive repetitions/rate and stage-seconds >= 6 are required")
    if len(set(args.policies)) != len(args.policies) or len(set(args.ft)) != len(args.ft):
        parser.error("duplicate policies or FT modes are not allowed")
    args.env_file = args.env_file.resolve()
    return args


def main(argv=None):
    args = arguments(argv)
    if args.summarize:
        from experiments.phase4_summary import summarize
        summarize(args.summarize)
        return
    matrix = [(policy, ft, repeat) for policy in args.policies for ft in args.ft
              for repeat in range(1, args.repetitions + 1)]
    if args.dry_run:
        print(json.dumps(dict(matrix=matrix, rate=args.rate, stage_seconds=args.stage_seconds,
                              duration_per_run=args.stage_seconds * 3), indent=2))
        return
    if not args.env_file.is_file():
        raise SystemExit("copy .env.example to .env and configure it first")
    import re
    corpus = (ROOT / "texts/gutenberg/mansfield-park.txt").read_text()
    if sum(word == KEYWORD for word in re.findall(r"\w+", corpus.lower())) != EXPECTED:
        raise SystemExit("the canonical corpus changed; verify the expected count before measuring")
    from client.api import Client
    from experiments.phase4_summary import summarize
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    output = args.output or ROOT / "results/phase4" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=False)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    hashes = {}
    for folder in ("server", "lb", "client", "experiments"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            hashes[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("compose.cluster.yml", ".env.example", "Makefile", "texts/gutenberg/mansfield-park.txt"):
        hashes[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    write_json(output / "session.json", dict(source_commit=commit, source_dirty=bool(dirty),
               source_hashes=hashes, matrix=matrix, python=sys.version, platform=platform.platform(),
               dependencies={name: version(name) for name in ("rpyc", "matplotlib")},
               cpu_count=os.cpu_count(), max_workers=128,
               rate=args.rate, stage_seconds=args.stage_seconds, keyword=KEYWORD,
               reference=REFERENCE, expected=EXPECTED, sample_interval_s=1))
    print(f"Recording {len(matrix)} runs under {output}", flush=True)
    build = Controller(args, output, matrix[0][0], matrix[0][1])
    build.command("build", timeout=300)
    try:
        for policy, ft, repeat in matrix:
            name = f"{policy}-ft-{ft}-r{repeat}"
            print(f"Starting {name}", flush=True)
            run_condition(args, output / name, policy, ft, repeat, Client)
            summarize(output)
    finally:
        summarize(output)


if __name__ == "__main__":
    main()
