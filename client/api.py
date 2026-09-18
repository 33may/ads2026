"""Client-side view of the word-count service. The only module that imports rpyc."""
import os
import time

import rpyc

DEFAULT_HOST = os.environ.get("ADS_HOST", "localhost")
DEFAULT_PORT = int(os.environ.get("ADS_PORT", "18861"))


class NoSuchReference(Exception):
    """Raised when the service reports an unknown reference."""


def _translate(exc):
    """Map the service's remote exception onto the local class of the same name."""
    if type(exc).__name__.endswith("NoSuchReference"):
        return NoSuchReference(*exc.args)
    return exc


class Client:
    """One connection to the service; each method is one remote call."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self._conn = rpyc.connect(host, port)
        self._svc = self._conn.root
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
