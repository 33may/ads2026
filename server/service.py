import os
import re

import rpyc
from rpyc.utils.server import ThreadedServer

from cache import Cache
from log import logger, SERVER_NAME
from store import Store

PORT = int(os.environ.get("SERVER_PORT", "18861"))
CACHE_MODE = os.environ.get("CACHE_MODE", "both")        # none | text | count | both
CACHE_TEXT = CACHE_MODE in ("text", "both")
CACHE_COUNT = CACHE_MODE in ("count", "both")

WORD = re.compile(r"\w+")


class NoSuchReference(Exception):
    """The requested reference does not exist in the file store."""


def count_word(text, keyword):
    """Case-insensitive whole-word count; punctuation is not part of a word."""
    keyword = keyword.lower()
    return sum(1 for w in WORD.findall(text.lower()) if w == keyword)


class WordCountService(rpyc.Service):
    """Word-count service: client API + developer API on one port."""

    def exposed_ping(self):
        logger.info("ping")
        return f"pong from {SERVER_NAME}"

    # --- client API ---

    def exposed_get_count(self, keyword, reference):
        keyword = keyword.lower()
        cache.bump_hot(keyword)

        if CACHE_COUNT:
            n = cache.get_count(reference, keyword)
            if n is not None:
                logger.info("get_count({!r}, {!r}) = {}  count-hit", keyword, reference, n)
                return n

        text = cache.get_text(reference) if CACHE_TEXT else None
        outcome = "text-hit"
        if text is None:
            outcome = "miss"
            try:
                text = store.get(reference)
            except KeyError:
                logger.warning("get_count({!r}, {!r}) no such reference", keyword, reference)
                raise NoSuchReference(reference) from None
            if CACHE_TEXT:
                cache.set_text(reference, text)

        n = count_word(text, keyword)
        if CACHE_COUNT:
            cache.set_count(reference, keyword, n)
        logger.info("get_count({!r}, {!r}) = {}  {}", keyword, reference, n, outcome)
        return n

    # --- developer API ---

    def exposed_upload_text(self, reference, text):
        store.put(reference, text)
        cache.invalidate(reference)
        logger.info("upload_text({!r}, {} chars)", reference, len(text))

    def exposed_delete_text(self, reference):
        store.delete(reference)
        cache.invalidate(reference)
        logger.info("delete_text({!r})", reference)

    def exposed_get_text(self, reference):
        return store.get(reference)

    def exposed_list_references(self, name_query=""):
        return store.list(prefix=name_query)

    def exposed_hot_keywords(self, n=10):
        return cache.hot(n)

    def exposed_cache_flush(self):
        cache.flush()
        logger.info("cache flushed")


if __name__ == "__main__":
    store = Store()
    cache = Cache()
    logger.info("cache mode {} | listening on :{}", CACHE_MODE, PORT)
    ThreadedServer(
        WordCountService,
        port=PORT,
        protocol_config={"allow_public_attrs": True},
    ).start()
