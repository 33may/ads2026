"""Run repeated Phase 3 experiments for all load-balancing policies.

Cache off and cache on are each measured after 0, 100, and 500 successful
warm-up requests by default.

Each measured run is open-loop. The load balancer is recreated before every
repetition so response-time measurements cannot carry over.
Results are resumable: an existing CSV is skipped unless --overwrite is used.
"""

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterable

# Make `python experiments/phase3.py` work from the repository root.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client.api import Client
from client import plot
from client.loadgen import (
    KEYWORDS, _sleep_until, make_workload, one_user, summary, write_csv,
)


POLICIES = ("least-connections", "lrt", "combined")
REQUIRED_POLICIES = ("least-connections", "lrt")
CACHE_MODES = ("off", "on")
DEFAULT_WARMUP_COUNTS = (0, 100, 500)
WARMUP_MAX_ATTEMPT_MULTIPLIER = 3


def validate_rates(rates: list[int]) -> None:
    """Enforce the Phase 2/3 report's five-rate workload requirement."""
    ordered = sorted(rates)
    if len(ordered) < 5 or len(set(ordered)) != len(ordered):
        raise SystemExit("provide at least five distinct request rates")
    if ordered[0] < 10:
        raise SystemExit("the lowest request rate must be at least 10 requests/s")
    if any(right - left < 10 for left, right in zip(ordered, ordered[1:])):
        raise SystemExit("adjacent request rates must differ by at least 10 requests/s")


def experiment_conditions(
    warmup_counts: list[int], cache_modes: Iterable[str] = CACHE_MODES,
) -> list[tuple[str, str, int]]:
    """Return condition name, cache mode, and warm-up count combinations."""
    return [
        (f"cache-{cache}-warm{count}", cache, count)
        for cache in cache_modes
        for count in warmup_counts
    ]


def docker_prefix() -> list[str]:
    """Ensure Docker is running; use sudo when the socket is not accessible."""
    probe = subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if probe.returncode == 0:
        return ["docker"]
    if shutil.which("sudo"):
        if shutil.which("systemctl"):
            try:
                subprocess.run(
                    ["sudo", "systemctl", "start", "docker"], check=True
                )
            except subprocess.CalledProcessError as exc:
                raise SystemExit("could not start the Docker service") from exc
        try:
            subprocess.run(
                ["sudo", "docker", "info"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            raise SystemExit(
                "Docker is unavailable; start the daemon and rerun the experiment"
            ) from exc
        return ["sudo", "docker"]
    raise SystemExit("Docker is not accessible and sudo is unavailable")


def compose_command(
    docker: list[str], compose_file: Path, env_file: Path, cache: str, policy: str,
    *arguments: str,
) -> None:
    """Run Compose with cache/policy overrides, preserving them through sudo."""
    compose_args = [
        "compose", "--env-file", str(env_file), "-f", str(compose_file), *arguments
    ]
    if docker[0] == "sudo":
        command = [
            "sudo", "env", f"CACHE={cache}", f"LB_ALGORITHM={policy}",
            f"LB_METRICS_PORT={os.environ.get('LB_METRICS_PORT', '18860')}",
            "docker", *compose_args,
        ]
        environment = None
    else:
        command = ["docker", *compose_args]
        environment = os.environ.copy()
        environment.update(CACHE=cache, LB_ALGORITHM=policy)
    subprocess.run(command, check=True, env=environment)


def wait_for_service(host: str, port: int, timeout: float = 60) -> None:
    """Wait until an RPyC request succeeds through the recreated balancer."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            # A broken backend must not consume the entire readiness deadline.
            # Short attempts also let least-connections rotate across all backends.
            with Client(host, port, timeout=2) as client:
                client.ping()
            return
        except Exception as exc:  # The socket may be between container restarts.
            last_error = exc
            time.sleep(0.25)
    raise RuntimeError(
        f"service did not become ready within {timeout:g}s; "
        f"last RPC error was {type(last_error).__name__}: {last_error}"
    )


def balancer_snapshot(host: str, port: int, timeout: float = 3) -> dict:
    """Read one JSON metrics snapshot from the balancer's side-channel."""
    with socket.create_connection((host, port), timeout=timeout) as connection:
        with connection.makefile("r", encoding="utf-8") as stream:
            line = stream.readline()
    if not line:
        raise RuntimeError("load-balancer metrics socket returned no data")
    return json.loads(line)


def wait_for_balancer_metrics(
    host: str, port: int, timeout: float = 30
) -> dict:
    """Wait for a fresh balancer without creating a proxied connection."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            return balancer_snapshot(host, port)
        except (ConnectionError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(
        f"load-balancer metrics did not become ready within {timeout:g}s: "
        f"{last_error}"
    )


def connection_states(before: dict, after: dict) -> dict[int, dict]:
    """Map explicit experiment IDs to LB state captured at selection time."""
    baseline = int(before["last_selection_sequence"])
    states = {}
    for event in after["selection_events"]:
        if int(event["sequence"]) <= baseline or event.get("correlation_id") is None:
            continue
        state = dict(event)
        state["connections"] = {
            backend["host"]: int(backend["active"])
            for backend in event["connections"]
        }
        state["response_ewma_ms"] = {
            backend["host"]: backend.get("response_ewma_ms")
            for backend in event["connections"]
        }
        states[int(event["correlation_id"])] = state
    return states


def policy_decision_violations(policy: str, states: dict[int, dict]) -> list[int]:
    """Return request IDs whose recorded assignment contradicts the policy.

    The selected backend's connection count already includes the new
    connection, so Least Connections is checked against its count immediately
    before admission. LRT bootstrap assignments are skipped until every
    backend has a response sample.
    """
    violations = []
    for request_id, state in states.items():
        selected = state["selected_server"]
        connections = state["connections"]
        before = {
            server: count - (1 if server == selected else 0)
            for server, count in connections.items()
        }
        if policy == "least-connections":
            if before[selected] != min(before.values()):
                violations.append(request_id)
            continue

        response = state["response_ewma_ms"]
        if any(value is None for value in response.values()):
            continue
        if policy == "lrt":
            scores = response
        else:
            scores = {
                server: response[server] * (before[server] + 1)
                for server in response
            }
        if scores[selected] > min(scores.values()) + 1e-9:
            violations.append(request_id)
    return violations


def has_per_request_connection_schema(path: Path) -> bool:
    """Prevent old run-total CSVs from being mistaken for per-request data."""
    with path.open(newline="") as stream:
        fields = csv.DictReader(stream).fieldnames or []
    basic_schema = {
        "selected_server", "rpc_ms", "error_type",
        "server_1_response_ewma_ms",
        "server_2_response_ewma_ms", "server_3_response_ewma_ms",
    }.issubset(fields)
    return basic_schema


def completed_run_is_valid(path: Path, expected_rows: int) -> bool:
    """Reject partial files so a resumed sweep cannot silently skip bad data."""
    if not has_per_request_connection_schema(path):
        return False
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if len(rows) != expected_rows:
        return False
    return True


def print_cluster_diagnostics(
    docker: list[str], compose_file: Path, env_file: Path,
) -> None:
    """Print bounded diagnostics while the caller's sudo authorization is warm."""
    print("\nCluster readiness failed; current container state:", file=sys.stderr)
    subprocess.run(
        [*docker, "compose", "--env-file", str(env_file), "-f", str(compose_file),
         "ps"],
        check=False,
    )
    for container in ("lb", "server-1", "server-2", "server-3"):
        print(f"\n--- {container}: last 40 log lines ---", file=sys.stderr)
        subprocess.run(
            [*docker, "logs", "--tail", "40", container],
            check=False,
        )


def save_distribution_evidence(
    docker: list[str], compose_file: Path, env_file: Path, output: Path,
) -> None:
    """Save representative Docker logs suitable for the required screenshot."""
    sections = [
        "Docker Compose request-distribution evidence",
        "Captured from the current lb/server containers after one measured run.",
    ]
    missing_evidence = []
    base = [
        *docker, "compose", "--env-file", str(env_file), "-f", str(compose_file),
        "logs", "--no-color",
    ]
    for service in ("lb", "server-1", "server-2", "server-3"):
        result = subprocess.run(
            [*base, service], check=False, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        lines = result.stdout.splitlines()
        if service == "lb":
            request_lines = [line for line in lines if "selected client=" in line]
            representative = []
            for backend in ("server-1", "server-2", "server-3"):
                backend_lines = [
                    line for line in request_lines
                    if f"backend={backend} " in line
                ][:4]
                if not backend_lines:
                    missing_evidence.append(f"lb assignment to {backend}")
                # Keep at most four readable examples for each backend.
                representative.extend(backend_lines)
        else:
            representative = [line for line in lines if "get_count(" in line][:6]
            if not representative:
                missing_evidence.append(f"{service} get_count request")
        sections.append(
            f"\n=== {service}: representative request lines ===\n"
            + "\n".join(representative)
        )
    if missing_evidence:
        raise RuntimeError(
            "could not produce complete Docker distribution evidence: "
            + ", ".join(missing_evidence)
        )
    output.write_text("\n".join(sections), encoding="utf-8")


def ensure_corpus(host: str, port: int, corpus: Path) -> list[str]:
    """Upload missing corpus files and return the complete reference list."""
    paths = sorted(corpus.glob("*.txt"))
    if not paths:
        raise RuntimeError(f"no corpus files found in {corpus}")
    with Client(host, port) as client:
        existing = set(client.list_references())
        missing = [path for path in paths if path.stem not in existing]
        for path in missing:
            client.upload_text(path.stem, path.read_text(encoding="utf-8"))
            print(f"seeded {path.stem}", flush=True)
        references = sorted(client.list_references())
    print(
        f"corpus: {len(references)} references ({len(missing)} uploaded)", flush=True
    )
    return references


def open_loop_requests(
    host: str, port: int, references: list[str], count: int, rate: float,
    mix: str, seed: int, connection_tracker=None, correlate: bool = False,
    request_timeout: float = 30,
) -> tuple[list[tuple], float]:
    """Schedule exactly count independent requests and wait for every result."""
    next_pair = make_workload(references, mix, seed)
    rows = []
    users = []
    period = 1.0 / rate
    started = time.perf_counter()
    for number in range(count):
        scheduled = started + number * period
        _sleep_until(scheduled)
        user = threading.Thread(
            target=one_user,
            args=(
                scheduled, *next_pair(), host, port, rows, connection_tracker,
                number if correlate else None, request_timeout,
            ),
            daemon=True,
        )
        user.start()
        users.append(user)
    for user in users:
        user.join()
    rows.sort()
    return rows, started


def fixed_warmup(
    host: str, port: int, references: list[str], count: int, rate: float,
    mix: str, seed: int, request_timeout: float = 30,
) -> None:
    """Complete count successful requests, retrying failures within a hard cap."""
    if count == 0:
        return

    successful = 0
    attempted = 0
    round_number = 0
    max_attempts = count * WARMUP_MAX_ATTEMPT_MULTIPLIER

    while successful < count and attempted < max_attempts:
        missing = count - successful
        batch_size = min(missing, max_attempts - attempted)
        rows, _ = open_loop_requests(
            host, port, references, batch_size, rate, mix, seed + round_number,
            request_timeout=request_timeout,
        )
        batch_successes = sum(row[5] for row in rows)
        # Every scheduled request consumes one attempt, even if a worker exits
        # before it can append a result row.
        batch_failures = batch_size - batch_successes
        successful += batch_successes
        attempted += batch_size
        round_number += 1

        if batch_failures:
            print(
                "warm-up retry: "
                f"successful={successful}/{count}, "
                f"failures_this_batch={batch_failures}, "
                f"attempted={attempted}/{max_attempts}",
                flush=True,
            )
            # Give timed-out server work a brief chance to drain before retrying.
            time.sleep(0.5)

    if successful < count:
        raise RuntimeError(
            f"warm-up failed after {attempted}/{max_attempts} attempts: "
            f"successful={successful}/{count}, failures={attempted - successful}"
        )


def write_metadata(path: Path, metadata: dict) -> None:
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def validate_required_artifacts(
    phase3_out: Path, report_out: Path, conditions: list[str],
    rates: list[int], repetitions: int,
) -> None:
    """Refuse to mark the sweep complete without every Phase 3 rubric artifact."""
    missing = []
    for condition in conditions:
        condition_dir = phase3_out / condition
        for policy in REQUIRED_POLICIES:
            for rate in rates:
                for repetition in range(1, repetitions + 1):
                    path = condition_dir / f"{policy}-{rate}-run-{repetition}.csv"
                    if not path.is_file():
                        missing.append(path)
            evidence = condition_dir / f"evidence-{policy}.log"
            if not evidence.is_file():
                missing.append(evidence)
        for statistic in ("avg", "p99"):
            comparison = report_out / f"comparison-{condition}-{statistic}.png"
            if not comparison.is_file():
                missing.append(comparison)
    if missing:
        preview = "\n".join(f"- {path}" for path in missing[:20])
        remainder = len(missing) - min(len(missing), 20)
        if remainder:
            preview += f"\n- ... and {remainder} more"
        raise RuntimeError(
            "Phase 3 cannot be marked complete; required artifacts are missing:\n"
            + preview
        )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    rate_group = parser.add_mutually_exclusive_group()
    rate_group.add_argument("--rates", type=int, nargs="+")
    rate_group.add_argument(
        "--rate", type=int, dest="single_rate",
        help="run one request rate instead of the complete five-rate sweep",
    )
    parser.add_argument(
        "--cache-mode", choices=CACHE_MODES,
        help="run only one cache mode instead of both",
    )
    parser.add_argument(
        "--policy", choices=POLICIES,
        help="run only one load-balancing policy",
    )
    parser.add_argument("--duration", type=float, default=15, help="measured seconds per run")
    warmup_group = parser.add_mutually_exclusive_group()
    warmup_group.add_argument(
        "--warmup-counts", type=int, nargs="+",
        default=list(DEFAULT_WARMUP_COUNTS),
        help="successful warm-up requests per condition (default: 0 100 500)",
    )
    warmup_group.add_argument(
        "--warmup-count", type=int, dest="single_warmup_count",
        help="run one warm-up count (backward-compatible shortcut)",
    )
    parser.add_argument("--warmup-rate", type=float, default=100)
    parser.add_argument(
        "--request-timeout", type=float, default=30,
        help="maximum seconds for an individual RPC (default: 30)",
    )
    parser.add_argument("--lb-settle", type=float, default=1)
    parser.add_argument("--mix", choices=["uniform", "zipf"], default="uniform")
    parser.add_argument("--warmup-seed", type=int, default=1)
    parser.add_argument("--measurement-seed", type=int, default=2)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18861)
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.environ.get("LB_METRICS_PORT", "18860")),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "results/Phase2+3/controlled/warmup-0-100-500/phase3"
        ),
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        help="combined Phase 2+3 report directory (default: parent of --out)",
    )
    parser.add_argument("--compose-file", type=Path, default=Path("compose.cluster.yml"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--corpus", type=Path, default=Path("texts/gutenberg"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.single_rate is not None:
        args.rates = [args.single_rate]
    elif args.rates is None:
        args.rates = [50, 70, 90, 110, 130]
    if args.single_warmup_count is not None:
        args.warmup_counts = [args.single_warmup_count]
    args.cache_modes = (
        [args.cache_mode] if args.cache_mode else list(CACHE_MODES)
    )
    args.policies = [args.policy] if args.policy else list(POLICIES)
    args.targeted = any((
        args.single_rate is not None,
        args.cache_mode is not None,
        args.policy is not None,
        args.single_warmup_count is not None,
    ))
    return args


def main() -> None:
    args = parse_arguments()
    if not args.env_file.exists():
        fallback = Path(".env.example")
        if fallback.exists():
            args.env_file = fallback
        else:
            raise SystemExit(f"environment file not found: {args.env_file}")
    if (
        args.duration <= 0
        or any(count < 0 for count in args.warmup_counts)
        or args.warmup_rate <= 0
        or args.request_timeout <= 0
        or args.lb_settle < 0
    ):
        raise SystemExit(
            "duration, warm-up rate, and request timeout must be positive; "
            "counts/settle cannot be negative"
        )
    if len(set(args.warmup_counts)) != len(args.warmup_counts):
        raise SystemExit("warm-up counts must be unique")
    if args.single_rate is not None:
        if args.single_rate < 10:
            raise SystemExit("the request rate must be at least 10 requests/s")
    else:
        validate_rates(args.rates)
    if args.repetitions < 3:
        raise SystemExit("--repetitions must be at least 3")

    args.out.mkdir(parents=True, exist_ok=True)
    report_out = args.report_out or args.out.parent
    report_out.mkdir(parents=True, exist_ok=True)
    os.environ["LB_METRICS_PORT"] = str(args.metrics_port)
    metadata_path = args.out / "experiment.json"
    condition_names = [
        condition[0]
        for condition in experiment_conditions(args.warmup_counts, args.cache_modes)
    ]
    metadata = {
        "status": "running",
        "rates": args.rates,
        "measured_duration_seconds": args.duration,
        "mix": args.mix,
        "measurement_seed": args.measurement_seed,
        "repetitions": args.repetitions,
        "balancer_settle_seconds": args.lb_settle,
        "warmup": {
            "requests": args.warmup_counts,
            "max_attempts": {
                str(count): count * WARMUP_MAX_ATTEMPT_MULTIPLIER
                for count in args.warmup_counts
            },
            "rate": args.warmup_rate,
            "seed": args.warmup_seed,
        },
        "request_timeout_seconds": args.request_timeout,
        "latency_metric": "rpc_ms",
        "latency_definition": "client get_count send-to-response time",
        "failed_requests": "retained in CSV and excluded only from latency percentiles",
        "request_model": "one fresh RPyC connection per keyword request",
        "schedule": "open-loop at the offered rate",
        "server_count": 3,
        "redis_flushed_before_each_repetition": True,
        "processes_recreated_before_each_repetition": [
            "server-1", "server-2", "server-3", "lb",
        ],
        "same_requests_as_phase2": (
            "identical rates, duration, mix, keyword/reference pools, and "
            "measurement seed per repetition"
        ),
        "keyword_pool": KEYWORDS,
        "policies": args.policies,
        "required_policies": list(REQUIRED_POLICIES),
        "optional_policies": [
            policy for policy in POLICIES if policy not in REQUIRED_POLICIES
        ],
        "cache_modes": args.cache_modes,
        "conditions": condition_names,
        "completed_runs": [],
    }
    if not args.targeted:
        write_metadata(metadata_path, metadata)

    docker = docker_prefix()
    # Recreate the Compose network at the beginning of a resumed/new sweep.
    # This removes stale containers and network attachments that can leave the
    # LB unable to resolve server-1..3. Named volumes are intentionally kept.
    compose_command(
        docker, args.compose_file, args.env_file,
        args.cache_modes[0], args.policies[0],
        "down", "--remove-orphans",
    )
    references = None
    first_configuration = True

    for condition_name, cache, warmup_count in experiment_conditions(
        args.warmup_counts, args.cache_modes
    ):
        condition_dir = args.out / condition_name
        condition_dir.mkdir(parents=True, exist_ok=True)
        for policy in args.policies:
            print(f"\n=== {condition_name} | {policy} ===", flush=True)
            up_arguments = ["up", "-d", "--wait"]
            if first_configuration:
                up_arguments.insert(2, "--build")
                first_configuration = False
            compose_command(
                docker, args.compose_file, args.env_file, cache, policy, *up_arguments
            )
            # Recreate even when Compose considers the old LB configuration
            # unchanged. After a Docker daemon/network restart, an old
            # container can retain a published port that accepts connections
            # without forwarding them into the container.
            compose_command(
                docker, args.compose_file, args.env_file, cache, policy,
                "up", "-d", "--force-recreate", "--no-deps", "lb",
            )
            time.sleep(args.lb_settle)
            try:
                wait_for_service(args.host, args.port)
            except RuntimeError:
                print_cluster_diagnostics(
                    docker, args.compose_file, args.env_file
                )
                raise
            if references is None:
                references = ensure_corpus(args.host, args.port, args.corpus)
                metadata["reference_pool"] = references
                if not args.targeted:
                    write_metadata(metadata_path, metadata)

            for rate in args.rates:
                for repetition in range(1, args.repetitions + 1):
                    request_count = round(rate * args.duration)
                    output = condition_dir / (
                        f"{policy}-{rate}-run-{repetition}.csv"
                    )
                    if output.exists() and not args.overwrite:
                        if not completed_run_is_valid(output, request_count):
                            raise SystemExit(
                                f"obsolete or incomplete Phase 3 run in {output}; "
                                "rerun with --overwrite or choose a new --out directory"
                            )
                        print(f"skip existing {output}", flush=True)
                        metadata["completed_runs"].append({
                            "condition": condition_name, "policy": policy,
                            "cache": cache, "warmup": warmup_count,
                            "rate": rate, "run": repetition,
                        })
                        if not args.targeted:
                            write_metadata(metadata_path, metadata)
                        continue

                    # Every repetition starts with the same empty cache, fresh
                    # server processes, and fresh policy state. This prevents
                    # timed-out work from contaminating the following sample.
                    compose_command(
                        docker, args.compose_file, args.env_file, cache, policy,
                        "up", "-d", "--force-recreate", "--no-deps", "--wait",
                        "server-1", "server-2", "server-3", "lb",
                    )
                    # Flush only after the old workers are gone; otherwise a
                    # timed-out old request could repopulate Redis after FLUSHDB.
                    compose_command(
                        docker, args.compose_file, args.env_file, cache, policy,
                        "exec", "-T", "redis", "redis-cli", "FLUSHDB",
                    )
                    time.sleep(args.lb_settle)
                    # Metrics readiness does not select a backend, so every
                    # repetition begins with pristine balancing state.
                    wait_for_balancer_metrics(args.host, args.metrics_port)

                    print(
                        f"run {repetition}/{args.repetitions} warm-up: "
                        f"{warmup_count} requests at "
                        f"{args.warmup_rate:g} req/s",
                        flush=True,
                    )
                    fixed_warmup(
                        args.host, args.port, references, warmup_count,
                        args.warmup_rate, args.mix,
                        args.warmup_seed + repetition - 1,
                        args.request_timeout,
                    )

                    policy_label = {
                        "lrt": "least-response-time",
                        "combined": "combined-lc-lrt",
                    }.get(policy, policy)
                    label = f"{policy_label}-{condition_name}"
                    print(
                        f"run {repetition}/{args.repetitions} measure: "
                        f"{rate} req/s for {args.duration:g}s",
                        flush=True,
                    )
                    before = balancer_snapshot(args.host, args.metrics_port)
                    rows, started = open_loop_requests(
                        args.host, args.port, references, request_count, rate,
                        args.mix, args.measurement_seed + repetition - 1,
                        correlate=True,
                        request_timeout=args.request_timeout,
                    )
                    if len(rows) != request_count:
                        raise RuntimeError(
                            f"incomplete measured run: got {len(rows)}/"
                            f"{request_count} result rows"
                        )
                    after = balancer_snapshot(args.host, args.metrics_port)
                    states = connection_states(before, after)
                    expected_ids = {
                        row[8] for row in rows if len(row) > 8 and row[8] is not None
                    }
                    missing_ids = expected_ids - states.keys()
                    if missing_ids:
                        print(
                            f"warning: {len(missing_ids)} requests failed before "
                            "a load-balancer assignment; their server/connection "
                            "fields will be blank",
                            file=sys.stderr,
                            flush=True,
                        )
                    violations = policy_decision_violations(policy, states)
                    if violations:
                        raise RuntimeError(
                            f"{policy} contradicted its recorded state for "
                            f"{len(violations)} assignments; first request IDs: "
                            + ", ".join(map(str, violations[:10]))
                        )
                    distribution = {
                        server: sum(
                            state["selected_server"] == server
                            for state in states.values()
                        )
                        for server in ("server-1", "server-2", "server-3")
                    }
                    write_csv(
                        output, rows, started, label, rate,
                        repetition=repetition,
                        connection_states=states,
                    )
                    if rate == args.rates[0] and repetition == 1:
                        evidence = condition_dir / f"evidence-{policy}.log"
                        save_distribution_evidence(
                            docker, args.compose_file, args.env_file, evidence
                        )
                        print(f"Docker distribution evidence -> {evidence}", flush=True)
                    print(
                        f"{label} @ {rate}, run {repetition}: {summary(rows)} "
                        f"distribution={distribution} -> {output}",
                        flush=True,
                    )
                    metadata["completed_runs"].append({
                        "condition": condition_name, "policy": policy,
                        "cache": cache, "warmup": warmup_count,
                        "rate": rate, "run": repetition,
                        "connection_distribution": distribution,
                        "uncorrelated_requests": len(missing_ids),
                        "policy_decision_violations": 0,
                    })
                    if not args.targeted:
                        write_metadata(metadata_path, metadata)

        plot.main(str(condition_dir))

    # Recursive loading produces one line per policy/condition combination.
    plot.main(str(args.out))
    # Include sibling Phase 2 CSVs in the total report when they are present.
    plot.main(str(report_out))
    if not args.targeted:
        validate_required_artifacts(
            args.out, report_out, condition_names, args.rates, args.repetitions
        )
        metadata["required_artifacts_validated"] = True
        metadata["status"] = "complete"
        write_metadata(metadata_path, metadata)
        print(f"\nAll Phase 3 results saved under {args.out}", flush=True)
    else:
        print(f"\nSelected Phase 3 results refreshed under {args.out}", flush=True)
    print(f"Combined report saved under {report_out}", flush=True)


if __name__ == "__main__":
    main()
