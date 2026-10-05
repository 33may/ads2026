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
import os
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


@dataclass
class Backend:
    host: str
    port: int
    active: int = 0
    selections: int = 0
    response_ewma_ms: float | None = None
    response_samples: int = 0

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class Balancer:

    def __init__(self, backends: Iterable[Backend], policy: str, ewma_alpha: float = 0.2):
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
        self._tie_cursor = 0
        self._selection_sequence = 0
        self._selection_events: list[dict] = []

    def select(self) -> Backend:
        if self.policy == "least-connections":
            lowest = min(backend.active for backend in self.backends)
            candidates = [backend for backend in self.backends if backend.active == lowest]
        else:
            candidates = [backend for backend in self.backends if backend.selections == 0]
            if not candidates:
                measured = [backend for backend in self.backends if backend.response_ewma_ms is not None]
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
                    lowest = min(backend.active for backend in self.backends)
                    candidates = [backend for backend in self.backends if backend.active == lowest]

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

    def record_selection(self, peer, backend: Backend, correlation_id=None) -> None:
        """Capture active counts at the instant a client is assigned."""
        self._selection_sequence += 1
        self._selection_events.append({
            "sequence": self._selection_sequence,
            "client_host": peer[0] if peer else None,
            "client_port": peer[1] if peer else None,
            "correlation_id": correlation_id,
            "selected_server": backend.host,
            "connections": [
                {
                    "host": item.host,
                    "port": item.port,
                    "active": item.active,
                    "response_ewma_ms": item.response_ewma_ms,
                }
                for item in self.backends
            ],
        })

    def observe_response(self, backend: Backend, response_ms: float) -> None:
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
            "policy": self.policy,
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
                }
                for backend in self.backends
            ],
        }


class Proxy:
    def __init__(self, balancer: Balancer):
        self.balancer = balancer

    async def handle_client(
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
        except (UnicodeDecodeError, ValueError, asyncio.LimitOverrunError) as exc:
            LOG.warning("invalid correlation preamble client=%s error=%s", peer, exc)
            client_writer.close()
            with suppress(ConnectionError, OSError):
                await client_writer.wait_closed()
            return

        backend_writer = None
        tasks: list[asyncio.Task] = []
        try:
            backend = self.balancer.select()
            self.balancer.record_selection(peer, backend, correlation_id)
            LOG.info(
                "selected client=%s backend=%s algorithm=%s %s",
                peer,
                backend.host,
                self.balancer.policy,
                self.balancer.metrics(),
            )
            backend_reader, backend_writer = await asyncio.open_connection(
                backend.host, backend.port
            )
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
                        self.balancer.observe_response(backend, elapsed_ms)
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
            await asyncio.gather(*tasks)
        except (ConnectionError, OSError) as exc:
            LOG.warning("proxy error client=%s backend=%s error=%s", peer, backend.host, exc)
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
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    balancer = Balancer(args.backends, args.algorithm, args.ewma_alpha)
    proxy = Proxy(balancer)
    server = await asyncio.start_server(proxy.handle_client, args.host, args.port)

    async def send_metrics(
        _reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writer.write(json.dumps(balancer.snapshot()).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()
        with suppress(ConnectionError, OSError):
            await writer.wait_closed()

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
    async with server, metrics_server:
        await asyncio.gather(server.serve_forever(), metrics_server.serve_forever())


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
