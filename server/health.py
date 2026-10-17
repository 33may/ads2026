"""Small application-level PING/PONG endpoint, owned by the service process.

This checks the health handler and RPC listener readiness, not Redis/MinIO or
the correctness of every business operation. No user RPC is sent by a probe.
"""
import socket
import math
import socketserver
import threading
import time


class _HealthServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        deadline = time.monotonic() + self.server.request_timeout
        data = bytearray()
        try:
            while len(data) < 5:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.request.settimeout(remaining)
                chunk = self.request.recv(5 - len(data))
                if not chunk:
                    return
                data.extend(chunk)
                if not b"PING\n".startswith(data):
                    return
            remaining = deadline - time.monotonic()
            if data == b"PING\n" and remaining > 0 and self.server.is_ready():
                self.request.settimeout(remaining)
                self.request.sendall(b"PONG\n")
        except (OSError, socket.timeout):
            pass


class HealthServer:
    def __init__(self, host="0.0.0.0", port=18862, is_ready=lambda: True,
                 request_timeout=0.5):
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("health request timeout must be positive")
        self.server = _HealthServer((host, port), _Handler)
        self.server.is_ready = is_ready
        self.server.request_timeout = request_timeout
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
            name="health-listener", daemon=True,
        )

    @property
    def address(self):
        return self.server.server_address

    def start(self):
        self.thread.start()

    def close(self):
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=1)
        self.server.server_close()
