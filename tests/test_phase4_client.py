"""Client initialization must release its connection on a failed GETROOT."""
import asyncio
import threading
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

try:
    from client.api import Client
except ImportError:
    Client = None


@unittest.skipIf(Client is None, "install client/requirements.txt for RPyC tests")
class ClientCleanupTests(unittest.TestCase):
    def test_getroot_failure_closes_connection_before_context_manager_exists(self):
        connection = MagicMock()
        connection._channel.stream.sock.getsockname.return_value = ("localhost", 1234)
        type(connection).root = PropertyMock(side_effect=EOFError("connection closed by peer"))
        with patch("client.api.rpyc.connect", return_value=connection):
            with self.assertRaises(EOFError):
                Client()
        connection.close.assert_called_once()

    def test_instrumented_getroot_failure_also_closes_connection(self):
        connection = MagicMock()
        type(connection).root = PropertyMock(side_effect=EOFError("closed"))
        with patch("client.api.SocketStream.connect"), patch(
            "client.api.rpyc.connect_stream", return_value=connection
        ):
            with self.assertRaises(EOFError):
                Client(correlation_id="phase4-1")
        connection.close.assert_called_once()


@unittest.skipIf(Client is None, "install client/requirements.txt for RPyC tests")
class RPyCProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_rpyc_initialization_and_query_through_proxy(self):
        import rpyc
        from rpyc.utils.server import ThreadedServer
        from lb.load_balancer import Backend, Balancer, HealthConfig, Proxy
        class TestService(rpyc.Service):
            def exposed_get_count(self, keyword, reference):
                return 6208 if (keyword, reference) == ("the", "mansfield-park") else 0
        rpc = ThreadedServer(TestService, hostname="127.0.0.1", port=0, auto_register=False)
        worker = threading.Thread(target=rpc.start, daemon=True)
        worker.start()
        node = Backend("127.0.0.1", rpc.port)
        balancer = Balancer([node], "lrt", health=HealthConfig(enabled=True))
        for _ in range(2):
            balancer.probe_result(node, node.health_version, None)
        proxy = Proxy(balancer)
        listener = await asyncio.start_server(proxy.handle_client, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        def query():
            with Client("127.0.0.1", port, timeout=1, correlation_id="real-rpyc") as client:
                return client.get_count("the", "mansfield-park")
        try:
            async with asyncio.timeout(2):
                while not rpc.active:
                    await asyncio.sleep(.005)
                self.assertEqual(await asyncio.to_thread(query), 6208)
                while node.active:
                    await asyncio.sleep(.005)
            self.assertGreater(node.response_samples, 0)
            self.assertEqual(balancer.snapshot()["selection_events"][0]["correlation_id"], "real-rpyc")
        finally:
            listener.close()
            await proxy.close()
            await listener.wait_closed()
            await asyncio.to_thread(rpc.close)
            await asyncio.to_thread(worker.join, 1)
