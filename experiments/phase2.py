"""Run repeated single-server Phase 2 cache/warm-up experiments.

Both cache modes are measured after 0, 100, and 500 successful warm-up
requests by default. Every cache/warm-up/rate combination is repeated at least
three times and the plotting step averages the per-repetition statistics.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from client import plot
from client.loadgen import ActiveConnectionTracker, KEYWORDS, summary, write_csv
from experiments.phase3 import (
    WARMUP_MAX_ATTEMPT_MULTIPLIER,
    docker_prefix,
    ensure_corpus,
    fixed_warmup,
    has_per_request_connection_schema,
    open_loop_requests,
    validate_rates,
    wait_for_service,
)


MODES = ("off", "on")
DEFAULT_WARMUP_COUNTS = (0, 100, 500)


def compose_command(
    docker: list[str], compose_file: Path, env_file: Path, cache: str,
    *arguments: str,
) -> None:
    compose_args = [
        "compose", "--env-file", str(env_file), "-f", str(compose_file),
        *arguments,
    ]
    if docker[0] == "sudo":
        command = ["sudo", "env", f"CACHE={cache}", "docker", *compose_args]
        environment = None
    else:
        command = ["docker", *compose_args]
        environment = os.environ.copy()
        environment["CACHE"] = cache
    subprocess.run(command, check=True, env=environment)


def write_metadata(path: Path, metadata: dict) -> None:
    path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    rate_group = parser.add_mutually_exclusive_group()
    rate_group.add_argument("--rates", type=int, nargs="+")
    rate_group.add_argument(
        "--rate", type=int, dest="single_rate",
        help="run one request rate instead of the complete five-rate sweep",
    )
    parser.add_argument(
        "--cache-mode", choices=MODES,
        help="run only one cache mode instead of both",
    )
    parser.add_argument("--duration", type=float, default=15)
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
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--mix", choices=["uniform", "zipf"], default="uniform")
    parser.add_argument("--warmup-seed", type=int, default=1)
    parser.add_argument("--measurement-seed", type=int, default=2)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18861)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "results/Phase2+3/controlled/warmup-0-100-500/phase2"
        ),
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        help="combined Phase 2+3 report directory (default: parent of --out)",
    )
    parser.add_argument("--compose-file", type=Path, default=Path("compose.yml"))
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
    args.cache_modes = [args.cache_mode] if args.cache_mode else list(MODES)
    args.targeted = any((
        args.single_rate is not None,
        args.cache_mode is not None,
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
        or args.warmup_rate <= 0
        or args.request_timeout <= 0
        or any(count < 0 for count in args.warmup_counts)
    ):
        raise SystemExit(
            "duration, warm-up rate, and request timeout must be positive; "
            "warm-up counts cannot be negative"
        )
    if len(set(args.warmup_counts)) != len(args.warmup_counts):
        raise SystemExit("warm-up counts must be unique")
    if args.repetitions < 3:
        raise SystemExit("--repetitions must be at least 3")
    if args.single_rate is not None:
        if args.single_rate < 10:
            raise SystemExit("the request rate must be at least 10 requests/s")
    else:
        validate_rates(args.rates)

    args.out.mkdir(parents=True, exist_ok=True)
    report_out = args.report_out or args.out.parent
    report_out.mkdir(parents=True, exist_ok=True)
    metadata_path = args.out / "experiment.json"
    metadata = {
        "status": "running",
        "rates": args.rates,
        "measured_duration_seconds": args.duration,
        "warmup_requests": args.warmup_counts,
        "warmup_max_attempts": {
            str(count): count * WARMUP_MAX_ATTEMPT_MULTIPLIER
            for count in args.warmup_counts
        },
        "warmup_rate": args.warmup_rate,
        "request_timeout_seconds": args.request_timeout,
        "latency_metric": "rpc_ms",
        "latency_definition": "client get_count send-to-response time",
        "failed_requests": "retained in CSV and excluded only from latency percentiles",
        "request_model": "one fresh RPyC connection per keyword request",
        "schedule": "open-loop at the offered rate",
        "server_count": 1,
        "redis_flushed_before_each_repetition": True,
        "processes_recreated_before_each_repetition": ["server-1"],
        "keyword_pool": KEYWORDS,
        "measurement_seed": args.measurement_seed,
        "repetitions": args.repetitions,
        "mix": args.mix,
        "modes": args.cache_modes,
        "completed_runs": [],
    }
    if not args.targeted:
        write_metadata(metadata_path, metadata)

    docker = docker_prefix()
    references = None
    first_configuration = True

    for cache in args.cache_modes:
        print(f"\n=== Phase 2 | cache {cache} ===", flush=True)
        up_arguments = ["up", "-d", "--wait", "--remove-orphans"]
        if first_configuration:
            up_arguments.insert(2, "--build")
            first_configuration = False
        compose_command(
            docker, args.compose_file, args.env_file, cache, *up_arguments
        )
        wait_for_service(args.host, args.port)
        if references is None:
            references = ensure_corpus(args.host, args.port, args.corpus)
            metadata["reference_pool"] = references
            if not args.targeted:
                write_metadata(metadata_path, metadata)

        for warmup_count in args.warmup_counts:
            condition_name = f"cache-{cache}-warm{warmup_count}"
            condition_dir = args.out / condition_name
            condition_dir.mkdir(parents=True, exist_ok=True)
            for rate in args.rates:
                for repetition in range(1, args.repetitions + 1):
                    output = condition_dir / f"{rate}-run-{repetition}.csv"
                    if output.exists() and not args.overwrite:
                        if not has_per_request_connection_schema(output):
                            raise SystemExit(
                                f"obsolete benchmark schema in {output}; "
                                "rerun with --overwrite or choose a new --out directory"
                            )
                        print(f"skip existing {output}", flush=True)
                        metadata["completed_runs"].append({
                            "cache": cache, "warmup": warmup_count,
                            "rate": rate, "run": repetition,
                        })
                        if not args.targeted:
                            write_metadata(metadata_path, metadata)
                        continue

                    # A client-side timeout closes its connection but the RPyC
                    # worker may still be finishing the abandoned request. Start
                    # every repetition with a fresh server so overload from the
                    # preceding run cannot contaminate its warm-up or measurement.
                    compose_command(
                        docker, args.compose_file, args.env_file, cache,
                        "up", "-d", "--force-recreate", "--no-deps", "--wait",
                        "server-1",
                    )
                    wait_for_service(args.host, args.port)
                    compose_command(
                        docker, args.compose_file, args.env_file, cache,
                        "exec", "-T", "redis", "redis-cli", "FLUSHDB",
                    )
                    print(
                        f"run {repetition}/{args.repetitions} warm-up: "
                        f"{warmup_count} requests at {args.warmup_rate:g} req/s",
                        flush=True,
                    )
                    fixed_warmup(
                        args.host, args.port, references, warmup_count,
                        args.warmup_rate, args.mix,
                        args.warmup_seed + repetition - 1,
                        args.request_timeout,
                    )

                    request_count = round(rate * args.duration)
                    print(
                        f"run {repetition}/{args.repetitions} measure: "
                        f"{rate} req/s for {args.duration:g}s",
                        flush=True,
                    )
                    tracker = ActiveConnectionTracker(("server-1",))
                    rows, started = open_loop_requests(
                        args.host, args.port, references, request_count, rate,
                        args.mix, args.measurement_seed + repetition - 1,
                        connection_tracker=tracker,
                        request_timeout=args.request_timeout,
                    )
                    label = condition_name
                    write_csv(
                        output, rows, started, label, rate,
                        repetition=repetition,
                    )
                    print(
                        f"{label} @ {rate}, run {repetition}: {summary(rows)} "
                        f"-> {output}",
                        flush=True,
                    )
                    metadata["completed_runs"].append({
                        "cache": cache, "warmup": warmup_count,
                        "rate": rate, "run": repetition,
                    })
                    if not args.targeted:
                        write_metadata(metadata_path, metadata)

    plot.main(str(args.out))
    # Recursive CSV loading includes Phase 3 whenever it exists beside phase2.
    plot.main(str(report_out))
    if not args.targeted:
        metadata["status"] = "complete"
        write_metadata(metadata_path, metadata)
        print(f"\nAll Phase 2 results saved under {args.out}", flush=True)
    else:
        print(f"\nSelected Phase 2 results refreshed under {args.out}", flush=True)
    print(f"Combined report saved under {report_out}", flush=True)


if __name__ == "__main__":
    main()
