"""Client-side view of the word-count service. The only module that imports rpyc."""
import os
import time

import rpyc
from rpyc.core.stream import SocketStream

DEFAULT_HOST = os.environ.get("ADS_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("ADS_PORT", "18861"))
CORRELATION_PREFIX = b"ADS-CORRELATION "


class NoSuchReference(Exception):
    """Raised when the service reports an unknown reference."""


def _translate(exc):
    """Map the service's remote exception onto the local class of the same name."""
    if type(exc).__name__.endswith("NoSuchReference"):
        return NoSuchReference(*exc.args)
    return exc


class Client:
    """One connection to the service; each method is one remote call."""

    def __init__(
        self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=None,
        correlation_id=None,
    ):
        config = {} if timeout is None else {"sync_request_timeout": timeout}
        if correlation_id is None:
            self._conn = rpyc.connect(host, port, config=config)
        else:
            try:
                encoded_id = str(correlation_id).encode("ascii")
            except UnicodeEncodeError:
                raise ValueError(
                    "correlation_id must be 1-64 ASCII characters"
                ) from None
            if not encoded_id or len(encoded_id) > 64 or b"\n" in encoded_id:
                raise ValueError("correlation_id must be 1-64 ASCII characters")
            stream = SocketStream.connect(host, port)
            try:
                stream.write(CORRELATION_PREFIX + encoded_id + b"\n")
                self._conn = rpyc.connect_stream(stream, config=config)
            except Exception:
                stream.close()
                raise
        try:
            self.local_port = self._conn._channel.stream.sock.getsockname()[1]
            self._svc = self._conn.root
        except BaseException:
            # A backend can disappear during GETROOT, before the caller gets
            # a Client/context manager. Do not leak that half-open session.
            self._conn.close()
            raise
        self.last_ms = None          # client-observed latency of the last get_count

    # --- client API (C1) ---

    def ping(self):
        return self._svc.ping()

    def get_count(self, keyword, reference):
        """Count of keyword in reference. Latency (send -> reply, on the client) lands in self.last_ms."""
        t0 = time.perf_counter()
        try:
            return self._svc.get_count(keyword, reference)
        except Exception as e:
            raise _translate(e) from None
        finally:
            self.last_ms = (time.perf_counter() - t0) * 1000

    def list_references(self, name_query=""):
        return list(self._svc.list_references(name_query))

    # --- developer API (C4) ---

    def upload_text(self, reference, text):
        self._svc.upload_text(reference, text)

    def delete_text(self, reference):
        self._svc.delete_text(reference)

    def get_text(self, reference):
        return self._svc.get_text(reference)

    def hot_keywords(self, n=10):
        return [(kw, int(c)) for kw, c in self._svc.hot_keywords(n)]

    def cache_flush(self):
        self._svc.cache_flush()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def connect(host=DEFAULT_HOST, port=DEFAULT_PORT):
    return Client(host, port)
