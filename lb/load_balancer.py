"""Transparent asyncio TCP load balancer for the RPyC word-count service.

Connections stay pinned to one backend for their whole lifetime.  The load balancer
never parses RPyC messages; response time is measured from a client write to
the first following bytes received from that backend.
"""

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import math
import os
import time
from typing import Iterable


LOG = logging.getLogger("load-balancer")
CORRELATION_PREFIX = b"ADS-CORRELATION "
POLICY_ALIASES = {
    "combined": "combined",
    "lc-lrt": "combined",
    "lc": "least-connections",
    "least-connections": "least-connections",
    "lrt": "least-response-time",
    "least-response-time": "least-response-time",
}


@dataclass(frozen=True)
class HealthConfig:
    enabled: bool = False
    port: int = 18862
    interval: float = 1.0
    timeout: float = 0.5
    fall: int = 2
    rise: int = 2
    connect_timeout: float = 0.5

    def __post_init__(self):
        if not 1 <= self.port <= 65535:
            raise ValueError("health port must be between 1 and 65535")
        if any(not math.isfinite(v) or v <= 0 for v in
               (self.interval, self.timeout, self.connect_timeout)):
            raise ValueError("health intervals and timeouts must be finite and positive")
        if self.fall < 1 or self.rise < 1:
            raise ValueError("health rise/fall thresholds must be positive")


class NoHealthyBackends(ConnectionError):
    """No eligible replica remains for this connection."""


@dataclass
class Backend:
    host: str
    port: int
    active: int = 0
    selections: int = 0
    response_ewma_ms: float | None = None
    response_samples: int = 0
    health_port: int | None = None
    health: str = "healthy"
    health_version: int = 0
    successes: int = 0
    failures: int = 0
    last_check_at: float | None = None
    last_error: str | None = None

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class Balancer:

    def __init__(self, backends: Iterable[Backend], policy: str, ewma_alpha: float = 0.2,
                 health: HealthConfig | None = None):
        self.backends = list(backends)
        if not self.backends:
            raise ValueError("at least one backend server is required")
        try:
            self.policy = POLICY_ALIASES[policy]
        except KeyError:
            choices = ", ".join(sorted(POLICY_ALIASES))
            raise ValueError(f"unknown algorithm {policy!r}; choose one of: {choices}") from None
        if not 0 < ewma_alpha <= 1:
            raise ValueError("EWMA alpha must be greater than 0 and at most 1")
        self.ewma_alpha = ewma_alpha
        self.health_config = health or HealthConfig()
        for backend in self.backends:
            backend.health = "unknown" if self.health_config.enabled else "healthy"
        self._health_events: list[dict] = []
        self._tie_cursor = 0
        self._selection_sequence = 0
        self._selection_events: list[dict] = []

    def select(self, exclude: set[str] | None = None) -> Backend:
        pool = [b for b in self.backends
                if b.address not in (exclude or set())
                and (not self.health_config.enabled or b.health == "healthy")]
        if not pool:
            raise NoHealthyBackends("no healthy, untried backend")
        if self.policy == "least-connections":
            lowest = min(backend.active for backend in pool)
            candidates = [backend for backend in pool if backend.active == lowest]
        else:
            if self.health_config.enabled:
                # Bootstrap a recovered/unsampled replica when it has no sample
                # already in flight. A session that never replies can be retried.
                candidates = [b for b in pool if b.response_ewma_ms is None and b.active == 0]
            else:
                candidates = [b for b in pool if b.selections == 0]
            if not candidates:
                measured = [backend for backend in pool if backend.response_ewma_ms is not None]
                if measured:
                    if self.policy == "combined":
                        score = lambda backend: (
                            backend.response_ewma_ms * (backend.active + 1)
                        )
                    else:
                        score = lambda backend: backend.response_ewma_ms
                    fastest = min(score(backend) for backend in measured)
                    candidates = [
                        backend for backend in measured
                        if score(backend) == fastest
                    ]
                else:
                    lowest = min(backend.active for backend in pool)
                    candidates = [backend for backend in pool if backend.active == lowest]

        backend = self._tie_resolve(candidates)
        backend.active += 1
        backend.selections += 1
        return backend

    def _tie_resolve(self, candidates: list[Backend]) -> Backend:
        candidate_ids = {id(backend) for backend in candidates}
        for offset in range(len(self.backends)):
            index = (self._tie_cursor + offset) % len(self.backends)
            if id(self.backends[index]) in candidate_ids:
                self._tie_cursor = (index + 1) % len(self.backends)
                return self.backends[index]
        raise RuntimeError("candidate is not in the backend pool")

    def release(self, backend: Backend) -> None:
        if backend.active <= 0:
            raise RuntimeError(f"connection count underflow for {backend.address}")
        backend.active -= 1

    def record_selection(self, peer, backend: Backend, correlation_id=None) -> dict:
        """Capture active counts at the instant a client is assigned."""
        self._selection_sequence += 1
        event = {
            "sequence": self._selection_sequence,
            "time": time.time(),
            "client_host": peer[0] if peer else None,
            "client_port": peer[1] if peer else None,
            "correlation_id": correlation_id,
            "selected_server": backend.host,
            "outcome": "connecting",
            "error": None,
            "connections": [
                {
                    "host": item.host,
                    "port": item.port,
                    "active": item.active,
                    "response_ewma_ms": item.response_ewma_ms,
                    "health": item.health,
                }
                for item in self.backends
            ],
        }
        self._selection_events.append(event)
        return event

    def observe_response(self, backend: Backend, response_ms: float,
                         version: int | None = None) -> None:
        if self.health_config.enabled and (
            backend.health != "healthy"
            or (version is not None and version != backend.health_version)
        ):
            return
        previous = backend.response_ewma_ms
        backend.response_ewma_ms = (
            response_ms
            if previous is None
            else self.ewma_alpha * response_ms + (1 - self.ewma_alpha) * previous
        )
        backend.response_samples += 1
        LOG.info(
            "response backend=%s sample_ms=%.3f ewma_ms=%.3f samples=%d",
            backend.host,
            response_ms,
            backend.response_ewma_ms,
            backend.response_samples,
        )

    def _transition(self, backend: Backend, state: str, reason: str) -> None:
        previous = backend.health
        if previous == state:
            return
        backend.health = state
        backend.health_version += 1
        if state == "healthy":
            backend.response_ewma_ms = None
        event = {
            "sequence": len(self._health_events) + 1, "time": time.time(),
            "server": backend.host, "previous": previous, "health": state,
            "reason": reason, "version": backend.health_version,
        }
        self._health_events.append(event)
        LOG.info("health backend=%s %s->%s reason=%s",
                 backend.host, previous, state, reason)

    def probe_result(self, backend: Backend, version: int, error: str | None) -> None:
        # A probe started before a passive failure must not resurrect a replica.
        if version != backend.health_version:
            return
        backend.last_check_at = time.time()
        backend.last_error = error
        if error is None:
            backend.failures = 0
            backend.successes += 1
            if backend.successes >= self.health_config.rise:
                self._transition(backend, "healthy", "ping/pong")
        else:
            backend.successes = 0
            backend.failures += 1
            if backend.failures >= self.health_config.fall:
                self._transition(backend, "unhealthy", error)

    def connection_failed(self, backend: Backend, version: int, error: str) -> None:
        if not self.health_config.enabled or version != backend.health_version:
            return
        backend.successes = 0
        backend.failures = self.health_config.fall
        backend.last_error = error
        self._transition(backend, "unhealthy", "connect: " + error)

    def metrics(self) -> str:
        active = ",".join(f"{backend.host}={backend.active}" for backend in self.backends)
        response = ",".join(
            f"{backend.host}="
            + ("unknown" if backend.response_ewma_ms is None else f"{backend.response_ewma_ms:.3f}")
            for backend in self.backends
        )
        return f"active=[{active}] response_ewma_ms=[{response}]"

    def snapshot(self) -> dict:
        """Return machine-readable counters without affecting policy state."""
        return {
            "time": time.time(),
            "policy": self.policy,
            "fault_tolerance": self.health_config.enabled,
            "health_config": vars(self.health_config),
            "health_events": list(self._health_events),
            "last_selection_sequence": self._selection_sequence,
            "selection_events": list(self._selection_events),
            "backends": [
                {
                    "host": backend.host,
                    "port": backend.port,
                    "address": backend.address,
                    "active": backend.active,
                    "selections": backend.selections,
                    "response_ewma_ms": backend.response_ewma_ms,
                    "response_samples": backend.response_samples,
                    "health": backend.health,
                    "health_port": backend.health_port or self.health_config.port,
                    "health_version": backend.health_version,
                    "consecutive_successes": backend.successes,
                    "consecutive_failures": backend.failures,
                    "last_check_at": backend.last_check_at,
                    "last_error": backend.last_error,
                }
                for backend in self.backends
            ],
        }


async def ping_backend(backend: Backend, config: HealthConfig) -> str | None:
    """One bounded TCP PING/PONG exchange, separate from user connections."""
    writer = None

    async def exchange():
        nonlocal writer
        reader, writer = await asyncio.open_connection(
            backend.host, backend.health_port or config.port, limit=16,
        )
        writer.write(b"PING\n")
        await writer.drain()
        if await reader.readuntil(b"\n") != b"PONG\n":
            raise ValueError("invalid PONG")

    try:
        await asyncio.wait_for(exchange(), config.timeout)
        return None
    except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError) as exc:
        return type(exc).__name__
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), config.timeout)


class HealthMonitor:
    def __init__(self, balancer: Balancer):
        self.balancer = balancer
        self.tasks: list[asyncio.Task] = []

    async def _watch(self, backend: Backend):
        config = self.balancer.health_config
        while True:
            started = asyncio.get_running_loop().time()
            version = backend.health_version
            error = await ping_backend(backend, config)
            self.balancer.probe_result(backend, version, error)
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.01, config.interval - elapsed))

    def start(self):
        if self.balancer.health_config.enabled:
            self.tasks = [asyncio.create_task(self._watch(b)) for b in self.balancer.backends]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()


class Proxy:
    def __init__(self, balancer: Balancer):
        self.balancer = balancer
        self.clients: set[asyncio.Task] = set()

    async def handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            await self._handle_client(client_reader, client_writer)
        finally:
            client_writer.close()
            with suppress(OSError):
                await client_writer.wait_closed()
            self.clients.discard(task)

    async def close(self):
        tasks = tuple(self.clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        peer = client_writer.get_extra_info("peername")
        backend = None
        correlation_id = None
        initial_data = b""
        try:
            # A normal RPyC frame can be shorter than the instrumentation
            # prefix and then wait for its response. `readexactly` would
            # deadlock in that case. Read whatever is available, and only wait
            # for more while the bytes can still be a fragmented preamble.
            initial_data = await client_reader.read(len(CORRELATION_PREFIX))
            if not initial_data:
                client_writer.close()
                with suppress(OSError):
                    await client_writer.wait_closed()
                return
            while (
                initial_data
                and len(initial_data) < len(CORRELATION_PREFIX)
                and CORRELATION_PREFIX.startswith(initial_data)
            ):
                more = await client_reader.read(
                    len(CORRELATION_PREFIX) - len(initial_data)
                )
                if not more:
                    break
                initial_data += more
            if initial_data == CORRELATION_PREFIX:
                raw_id = await client_reader.readuntil(b"\n")
                if len(raw_id) > 65:
                    raise ValueError("correlation ID exceeds 64 characters")
                correlation_id = raw_id[:-1].decode("ascii")
                if not correlation_id:
                    raise ValueError("correlation ID is empty")
                initial_data = b""
        except (UnicodeDecodeError, ValueError, asyncio.LimitOverrunError,
                asyncio.IncompleteReadError, OSError) as exc:
            LOG.warning("invalid correlation preamble client=%s error=%s", peer, exc)
            client_writer.close()
            with suppress(ConnectionError, OSError):
                await client_writer.wait_closed()
            return

        backend_writer = None
        event = None
        tasks: list[asyncio.Task] = []
        try:
            tried: set[str] = set()
            config = self.balancer.health_config
            while True:
                backend = self.balancer.select(tried)
                tried.add(backend.address)
                version = backend.health_version
                event = self.balancer.record_selection(peer, backend, correlation_id)
                LOG.info(
                    "selected client=%s backend=%s algorithm=%s %s",
                    peer, backend.host, self.balancer.policy, self.balancer.metrics(),
                )
                try:
                    opening = asyncio.open_connection(backend.host, backend.port)
                    if config.enabled:
                        backend_reader, backend_writer = await asyncio.wait_for(
                            opening, config.connect_timeout,
                        )
                    else:
                        backend_reader, backend_writer = await opening
                except (OSError, TimeoutError) as exc:
                    event.update(outcome="connect_failed", error=type(exc).__name__,
                                 completed_at=time.time())
                    self.balancer.connection_failed(backend, version, type(exc).__name__)
                    self.balancer.release(backend)
                    backend = None
                    if not config.enabled:
                        raise
                    continue
                if config.enabled and (
                    backend.health != "healthy" or version != backend.health_version
                ):
                    # A health transition raced the connect. No bytes were sent.
                    event.update(outcome="health_changed", completed_at=time.time())
                    backend_writer.close()
                    with suppress(OSError):
                        await backend_writer.wait_closed()
                    backend_writer = None
                    self.balancer.release(backend)
                    backend = None
                    continue
                event.update(outcome="connected", connected_at=time.time())
                break
            loop = asyncio.get_running_loop()
            pending_request_at: list[float | None] = [None]

            async def client_to_backend() -> None:
                if initial_data:
                    pending_request_at[0] = loop.time()
                    backend_writer.write(initial_data)
                    await backend_writer.drain()
                while data := await client_reader.read(64 * 1024):
                    if pending_request_at[0] is None:
                        pending_request_at[0] = loop.time()
                    backend_writer.write(data)
                    await backend_writer.drain()
                if backend_writer.can_write_eof():
                    backend_writer.write_eof()
                    await backend_writer.drain()

            async def backend_to_client() -> None:
                while data := await backend_reader.read(64 * 1024):
                    if pending_request_at[0] is not None:
                        elapsed_ms = (loop.time() - pending_request_at[0]) * 1000
                        self.balancer.observe_response(backend, elapsed_ms, version)
                        pending_request_at[0] = None
                    client_writer.write(data)
                    await client_writer.drain()
                if client_writer.can_write_eof():
                    client_writer.write_eof()
                    await client_writer.drain()

            tasks = [
                asyncio.create_task(client_to_backend()),
                asyncio.create_task(backend_to_client()),
            ]
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[1] in done:
                # The backend closed: stop reading this client's old session.
                tasks[0].cancel()
            else:
                # Client half-closed after sending its payload; drain the reply.
                await tasks[1]
        except (ConnectionError, OSError) as exc:
            if event is not None and event["outcome"] == "connected":
                event["stream_error"] = type(exc).__name__
            LOG.warning("proxy error client=%s backend=%s error=%s",
                        peer, backend.host if backend else "none", exc)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if backend_writer is not None:
                backend_writer.close()
                with suppress(ConnectionError, OSError):
                    await backend_writer.wait_closed()
            client_writer.close()
            with suppress(ConnectionError, OSError):
                await client_writer.wait_closed()
            if backend is not None:
                if event is not None:
                    event["closed_at"] = time.time()
                self.balancer.release(backend)
                LOG.info(
                    "released client=%s backend=%s %s",
                    peer, backend.host, self.balancer.metrics(),
                )


def parse_backends(value: str) -> list[Backend]:
    backends = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            host, port_text = item.rsplit(":", 1)
            port = int(port_text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid backend {item!r}; expected HOST:PORT") from None
        if not host or not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError(f"invalid backend {item!r}; expected HOST:PORT")
        backends.append(Backend(host, port))
    if not backends:
        raise argparse.ArgumentTypeError("at least one backend is required")
    return backends


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transparent TCP load balancer")
    parser.add_argument("--host", default=os.environ.get("LB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SERVER_PORT", "18861")))
    parser.add_argument(
        "--backends",
        type=parse_backends,
        default=parse_backends(os.environ.get("BACKENDS", "localhost:18862")),
        help="comma-separated HOST:PORT list (default: BACKENDS environment variable)",
    )
    parser.add_argument(
        "--algorithm",
        choices=sorted(POLICY_ALIASES),
        default=os.environ.get("LB_ALGORITHM", "least-connections"),
        help="least-connections/lc (default), least-response-time/lrt, or combined/lc-lrt",
    )
    parser.add_argument(
        "--ewma-alpha",
        type=float,
        default=float(os.environ.get("LRT_EWMA_ALPHA", "0.2")),
        help="weight of the newest LRT sample (default: 0.2)",
    )
    parser.add_argument(
        "--metrics-host", default=os.environ.get("LB_METRICS_HOST", "0.0.0.0")
    )
    parser.add_argument(
        "--metrics-port",
        type=int,
        default=int(os.environ.get("LB_METRICS_PORT", "18860")),
        help="read-only JSON metrics socket (default: 18860)",
    )
    parser.add_argument("--fault-tolerance", choices=("off", "on"),
                        default=os.environ.get("FAULT_TOLERANCE", "off"))
    for flag, env, kind, default in (
        ("health-port", "SERVER_HEALTH_PORT", int, 18862),
        ("health-interval", "HEALTH_INTERVAL", float, 1.0),
        ("health-timeout", "HEALTH_TIMEOUT", float, 0.5),
        ("health-fall", "HEALTH_FALL", int, 2),
        ("health-rise", "HEALTH_RISE", int, 2),
        ("connect-timeout", "BACKEND_CONNECT_TIMEOUT", float, 0.5),
    ):
        parser.add_argument("--" + flag, type=kind, default=os.environ.get(env, str(default)))
    args = parser.parse_args(argv)
    try:
        args.health_config = HealthConfig(
            enabled=args.fault_tolerance == "on", port=args.health_port,
            interval=args.health_interval, timeout=args.health_timeout,
            fall=args.health_fall, rise=args.health_rise,
            connect_timeout=args.connect_timeout,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return args


async def run(args: argparse.Namespace) -> None:
    balancer = Balancer(args.backends, args.algorithm, args.ewma_alpha, args.health_config)
    monitor = HealthMonitor(balancer)
    proxy = Proxy(balancer)
    server = await asyncio.start_server(proxy.handle_client, args.host, args.port)

    async def send_metrics(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            writer.write(json.dumps(balancer.snapshot()).encode("utf-8") + b"\n")
            await asyncio.wait_for(writer.drain(), 2)
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)

    metrics_server = await asyncio.start_server(
        send_metrics, args.metrics_host, args.metrics_port
    )
    addresses = ", ".join(str(sock.getsockname()) for sock in server.sockets or [])
    metrics_addresses = ", ".join(
        str(sock.getsockname()) for sock in metrics_server.sockets or []
    )
    LOG.info(
        "listening=%s metrics=%s algorithm=%s backends=%s ewma_alpha=%g",
        addresses,
        metrics_addresses,
        balancer.policy,
        ",".join(backend.host for backend in balancer.backends),
        balancer.ewma_alpha,
    )
    monitor.start()
    try:
        # start_server already accepts connections. Close client handlers before
        # wait_closed(), which also waits for open transports on Python 3.12+.
        await asyncio.Event().wait()
    finally:
        server.close()
        metrics_server.close()
        await monitor.close()
        await proxy.close()
        await server.wait_closed()
        await metrics_server.wait_closed()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)-5s | lb | %(message)s",
        datefmt="%H:%M:%S",
    )
    args = arguments()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
