import os
import re

import rpyc
from rpyc.utils.server import ThreadedServer

from cache import Cache
from log import logger, SERVER_NAME
from store import Store

PORT = int(os.environ.get("SERVER_PORT", "18861"))
CACHE = os.environ.get("CACHE", "on") == "on"            # off = always fetch and count (experiment baseline)

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

        if CACHE:
            n = cache.get_count(reference, keyword)
            if n is not None:
                logger.info("get_count({!r}, {!r}) = {}  hit", keyword, reference, n)
                return n

        try:
            text = store.get(reference)
        except KeyError:
            logger.warning("get_count({!r}, {!r}) no such reference", keyword, reference)
            raise NoSuchReference(reference) from None

        n = count_word(text, keyword)
        if CACHE:
            cache.set_count(reference, keyword, n)
        logger.info("get_count({!r}, {!r}) = {}  miss", keyword, reference, n)
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
    logger.info("cache {} | listening on :{}", "on" if CACHE else "off", PORT)
    ThreadedServer(
        WordCountService,
        port=PORT,
        protocol_config={"allow_public_attrs": True},
    ).start()
