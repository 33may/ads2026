"""Docker-free tests using real loopback sockets for health and forwarding."""
import asyncio
from contextlib import suppress
import logging
import socket
import unittest
from unittest.mock import patch

from lb.load_balancer import (
    Backend, Balancer, HealthConfig, HealthMonitor, NoHealthyBackends,
    Proxy, arguments, ping_backend,
)
from server.health import HealthServer


logging.getLogger("load-balancer").setLevel(logging.CRITICAL)


def healthy(balancer, backend):
    for _ in range(balancer.health_config.rise):
        balancer.probe_result(backend, backend.health_version, None)


class StateTests(unittest.TestCase):
    def make(self, policy="lc"):
        backends = [Backend(f"server-{i}", 18861) for i in range(1, 4)]
        return Balancer(backends, policy, health=HealthConfig(enabled=True))

    def test_thresholds_and_streak_reset(self):
        balancer = self.make()
        backend = balancer.backends[0]
        with self.assertRaises(NoHealthyBackends):
            balancer.select()
        balancer.probe_result(backend, 0, None)
        self.assertEqual(backend.health, "unknown")
        balancer.probe_result(backend, 0, "TimeoutError")
        balancer.probe_result(backend, 0, None)
        self.assertEqual(backend.health, "unknown")
        balancer.probe_result(backend, 0, None)
        self.assertEqual(backend.health, "healthy")
        version = backend.health_version
        balancer.probe_result(backend, version, "TimeoutError")
        self.assertEqual(backend.health, "healthy")
        balancer.probe_result(backend, version, "TimeoutError")
        self.assertEqual(backend.health, "unhealthy")
        healthy(balancer, backend)
        self.assertEqual(backend.health, "healthy")

    def test_old_probe_and_response_cannot_resurrect_or_reseed(self):
        balancer = self.make("lrt")
        backend = balancer.backends[0]
        healthy(balancer, backend)
        version = backend.health_version
        balancer.connection_failed(backend, version, "ConnectionRefusedError")
        balancer.probe_result(backend, version, None)
        self.assertEqual(backend.successes, 0)
        self.assertEqual(backend.health, "unhealthy")
        healthy(balancer, backend)
        balancer.observe_response(backend, 1, version)
        self.assertIsNone(backend.response_ewma_ms)

    def test_all_policies_filter_unknown_unhealthy_and_excluded(self):
        for policy in ("lc", "lrt", "combined"):
            with self.subTest(policy=policy):
                balancer = self.make(policy)
                a, b, c = balancer.backends
                healthy(balancer, b)
                chosen = balancer.select()
                self.assertIs(chosen, b)
                balancer.release(chosen)
                with self.assertRaises(NoHealthyBackends):
                    balancer.select({b.address})
                balancer.connection_failed(b, b.health_version, "refused")
                with self.assertRaises(NoHealthyBackends):
                    balancer.select()

    def test_lrt_recovery_bootstrap_preserves_counters_and_can_retry(self):
        balancer = self.make("lrt")
        a, b, _ = balancer.backends
        for backend in (a, b):
            healthy(balancer, backend)
            backend.selections = 9
            backend.response_samples = 7
            backend.response_ewma_ms = 2 if backend is a else 100
        old = balancer.select()
        self.assertIs(old, a)
        balancer.connection_failed(a, a.health_version, "refused")
        healthy(balancer, a)
        self.assertEqual(a.active, 1)  # old session must still release itself
        balancer.release(old)
        self.assertEqual(a.selections, 10)  # cumulative evidence is retained
        self.assertEqual(a.response_samples, 7)
        self.assertIs(balancer.select(), a)
        balancer.release(a)  # no response: should not strand the recovered node
        self.assertIs(balancer.select(), a)
        balancer.observe_response(a, 30, a.health_version)
        balancer.release(a)
        self.assertIs(balancer.select(), a)
        balancer.release(a)

    def test_phase3_lc_and_lrt_behavior_when_disabled(self):
        backends = [Backend(str(i), 1) for i in range(3)]
        balancer = Balancer(backends, "lc")
        self.assertEqual([balancer.select().host for _ in range(3)], ["0", "1", "2"])
        balancer.release(backends[1])
        self.assertIs(balancer.select(), backends[1])
        for policy in ("lrt", "combined"):
            nodes = [Backend(str(i), 1) for i in range(3)]
            balancer = Balancer(nodes, policy)
            for backend, ms in zip(nodes, (30, 10, 20)):
                self.assertIs(balancer.select(), backend)
                balancer.observe_response(backend, ms)
                balancer.release(backend)
            self.assertIs(balancer.select(), nodes[1])

    def test_configuration_validation(self):
        for kwargs in ({"timeout": 0}, {"interval": float("nan")},
                       {"connect_timeout": -1}, {"rise": 0}, {"fall": 0},
                       {"port": 65536}):
            with self.assertRaises(ValueError):
                HealthConfig(**kwargs)
        args = arguments(["--fault-tolerance", "on", "--health-rise", "3"])
        self.assertTrue(args.health_config.enabled)
        self.assertEqual(args.health_config.rise, 3)


class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.writers = []
        self.handlers = set()

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
        for writer in self.writers:
            writer.close()
        for writer in self.writers:
            with suppress(OSError):
                await writer.wait_closed()
        for task in tuple(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        for server in self.servers:
            await asyncio.wait_for(server.wait_closed(), 1)

    async def serve(self, handler):
        async def tracked(reader, writer):
            task = asyncio.current_task()
            self.handlers.add(task)
            self.writers.append(writer)
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
                self.handlers.discard(task)
        server = await asyncio.start_server(tracked, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def echo(self, reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def wait_until(self, condition, timeout=1):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.005)

    async def send(self, port, payload=b"hello", correlation=None):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.writers.append(writer)
        prefix = b"" if correlation is None else b"ADS-CORRELATION " + correlation + b"\n"
        writer.write(prefix + payload)
        await writer.drain()
        writer.write_eof()
        return await asyncio.wait_for(reader.read(), 1)

    async def test_health_listener_readiness_fragmented_and_malformed(self):
        ready = False
        endpoint = HealthServer("127.0.0.1", 0, lambda: ready, request_timeout=0.08)
        endpoint.start()
        try:
            backend = Backend("127.0.0.1", 1, health_port=endpoint.address[1])
            config = HealthConfig(enabled=True, timeout=0.1)
            self.assertIsNotNone(await ping_backend(backend, config))
            ready = True
            self.assertIsNone(await ping_backend(backend, config))
            reader, writer = await asyncio.open_connection(*endpoint.address)
            self.writers.append(writer)
            writer.write(b"PI")
            await writer.drain()
            writer.write(b"NG\n")
            await writer.drain()
            self.assertEqual(await reader.read(), b"PONG\n")
            self.assertEqual(await self.send(endpoint.address[1], b"WRONG\n"), b"")
            reader, writer = await asyncio.open_connection(*endpoint.address)
            self.writers.append(writer)
            writer.write(b"PI")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 0.5), b"")
        finally:
            await asyncio.to_thread(endpoint.close)

    async def test_probe_wrong_reply_eof_and_whole_exchange_timeout(self):
        for response in (b"NOPE\n", b"PO", b""):
            async def responder(reader, writer, reply=response):
                await reader.read(5)
                writer.write(reply)
                await writer.drain()
            port = await self.serve(responder)
            error = await ping_backend(
                Backend("127.0.0.1", 1, health_port=port),
                HealthConfig(timeout=0.05),
            )
            self.assertIsNotNone(error)
        async def hanging(reader, writer):
            await reader.read(5)
            await asyncio.Event().wait()
        port = await self.serve(hanging)
        error = await ping_backend(Backend("127.0.0.1", 1, health_port=port),
                                   HealthConfig(timeout=0.03))
        self.assertEqual(error, "TimeoutError")

    async def test_monitor_detects_failure_and_recovery_without_user_traffic(self):
        ready = True
        async def responder(reader, writer):
            await reader.read(5)
            if ready:
                writer.write(b"PONG\n")
                await writer.drain()
        port = await self.serve(responder)
        backend = Backend("127.0.0.1", 1, health_port=port)
        balancer = Balancer([backend], "lc",
                            health=HealthConfig(enabled=True, interval=0.02, timeout=0.02))
        monitor = HealthMonitor(balancer)
        monitor.start()
        try:
            await self.wait_until(lambda: backend.health == "healthy")
            ready = False
            await self.wait_until(lambda: backend.health == "unhealthy")
            ready = True
            await self.wait_until(lambda: backend.health == "healthy")
            self.assertEqual((backend.active, backend.selections, backend.response_samples), (0, 0, 0))
        finally:
            await monitor.close()
        self.assertEqual(monitor.tasks, [])

    async def test_disabled_monitor_opens_no_probe(self):
        balancer = Balancer([Backend("127.0.0.1", 1)], "lc")
        monitor = HealthMonitor(balancer)
        monitor.start()
        self.assertEqual(monitor.tasks, [])
        await monitor.close()

    async def test_large_payload_and_concurrent_sessions_with_ft_on_and_off(self):
        port = await self.serve(self.echo)
        for enabled in (False, True):
            backend = Backend("127.0.0.1", port)
            balancer = Balancer([backend], "lc", health=HealthConfig(enabled=enabled))
            if enabled:
                healthy(balancer, backend)
            proxy_port = await self.serve(Proxy(balancer).handle_client)
            payload = bytes(range(256)) * 769
            responses = await asyncio.gather(*[
                self.send(proxy_port, payload, str(i).encode()) for i in range(8)
            ])
            self.assertTrue(all(response == payload for response in responses))
            await self.wait_until(lambda: backend.active == 0)
            self.assertEqual(backend.selections, 8)
            self.assertEqual(len(balancer.snapshot()["selection_events"]), 8)

    async def test_failover_before_forwarding_records_both_attempts(self):
        # Reserve a bound, non-listening port so no other process can take it.
        with socket.socket() as refused:
            refused.bind(("127.0.0.1", 0))
            port = await self.serve(self.echo)
            a = Backend("127.0.0.1", refused.getsockname()[1])
            b = Backend("127.0.0.1", port)
            balancer = Balancer([a, b], "lc", health=HealthConfig(enabled=True))
            for backend in (a, b):
                healthy(balancer, backend)
            proxy_port = await self.serve(Proxy(balancer).handle_client)
            self.assertEqual(await self.send(proxy_port, correlation=b"demo"), b"hello")
            await self.wait_until(lambda: b.active == 0)
            self.assertEqual((a.health, a.active), ("unhealthy", 0))
            events = balancer.snapshot()["selection_events"]
            self.assertEqual([e["outcome"] for e in events], ["connect_failed", "connected"])
            self.assertEqual([e["correlation_id"] for e in events], ["demo", "demo"])

    async def test_phase3_does_not_retry_and_all_down_can_recover(self):
        with socket.socket() as refused:
            refused.bind(("127.0.0.1", 0))
            port = await self.serve(self.echo)
            nodes = [Backend("127.0.0.1", refused.getsockname()[1]), Backend("127.0.0.1", port)]
            balancer = Balancer(nodes, "lc")
            proxy_port = await self.serve(Proxy(balancer).handle_client)
            connect = asyncio.open_connection
            async def refused_connect(host, target_port, *args, **kwargs):
                if target_port == nodes[0].port:
                    raise ConnectionRefusedError("test refusal")
                return await connect(host, target_port, *args, **kwargs)
            with patch("lb.load_balancer.asyncio.open_connection", refused_connect):
                self.assertEqual(await self.send(proxy_port), b"")
            self.assertEqual(nodes[1].selections, 0)
            balancer = Balancer([nodes[1]], "lc", health=HealthConfig(enabled=True))
            proxy_port = await self.serve(Proxy(balancer).handle_client)
            self.assertEqual(await self.send(proxy_port), b"")
            healthy(balancer, nodes[1])
            self.assertEqual(await self.send(proxy_port), b"hello")

    async def test_no_replay_after_backend_received_bytes(self):
        received = []
        async def crash(reader, writer):
            received.append(await reader.read(65536))
        crash_port = await self.serve(crash)
        echo_port = await self.serve(self.echo)
        a, b = Backend("127.0.0.1", crash_port), Backend("127.0.0.1", echo_port)
        balancer = Balancer([a, b], "lc", health=HealthConfig(enabled=True))
        for backend in (a, b):
            healthy(balancer, backend)
        proxy_port = await self.serve(Proxy(balancer).handle_client)
        self.assertEqual(await self.send(proxy_port, b"do-not-replay"), b"")
        await self.wait_until(lambda: a.active == 0)
        self.assertTrue(received)
        self.assertEqual(b.selections, 0)

    async def test_proxy_shutdown_closes_idle_and_active_sessions(self):
        port = await self.serve(self.echo)
        backend = Backend("127.0.0.1", port)
        balancer = Balancer([backend], "lc", health=HealthConfig(enabled=True))
        healthy(balancer, backend)
        proxy = Proxy(balancer)
        proxy_port = await self.serve(proxy.handle_client)
        for data in (b"", b"hello"):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            self.writers.append(writer)
            if data:
                writer.write(data)
                await writer.drain()
                self.assertEqual(await reader.readexactly(len(data)), data)
        await asyncio.wait_for(proxy.close(), 1)
        self.assertEqual(backend.active, 0)
        self.assertEqual(proxy.clients, set())

    async def test_connect_timeout_is_bounded_and_does_not_leak_active_count(self):
        port = await self.serve(self.echo)
        a, b = Backend("stalled.invalid", 1), Backend("127.0.0.1", port)
        balancer = Balancer([a, b], "lc", health=HealthConfig(enabled=True, connect_timeout=0.02))
        for backend in (a, b):
            healthy(balancer, backend)
        proxy_port = await self.serve(Proxy(balancer).handle_client)
        connect = asyncio.open_connection
        async def stalled(host, target_port, *args, **kwargs):
            if host == a.host:
                await asyncio.Event().wait()
            return await connect(host, target_port, *args, **kwargs)
        with patch("lb.load_balancer.asyncio.open_connection", stalled):
            self.assertEqual(await self.send(proxy_port), b"hello")
        await self.wait_until(lambda: b.active == 0)
        self.assertEqual(a.active, 0)
        self.assertEqual(a.last_error, "TimeoutError")

    async def test_health_transition_during_connect_discards_old_socket(self):
        port = await self.serve(self.echo)
        a, b = Backend("127.0.0.1", port), Backend("localhost", port)
        balancer = Balancer([a, b], "lrt", health=HealthConfig(enabled=True))
        for backend in (a, b):
            healthy(balancer, backend)
        proxy_port = await self.serve(Proxy(balancer).handle_client)
        connect = asyncio.open_connection
        async def transition(host, target_port, *args, **kwargs):
            result = await connect(host, target_port, *args, **kwargs)
            if host == a.host and target_port == a.port:
                balancer.connection_failed(a, a.health_version, "concurrent failure")
                healthy(balancer, a)
            return result
        with patch("lb.load_balancer.asyncio.open_connection", transition):
            self.assertEqual(await self.send(proxy_port), b"hello")
        await self.wait_until(lambda: b.active == 0)
        self.assertEqual((a.active, a.response_samples), (0, 0))
        self.assertEqual([e["outcome"] for e in balancer.snapshot()["selection_events"]],
                         ["health_changed", "connected"])


if __name__ == "__main__":
    unittest.main()
