"""Cache access: counts and texts as plain Redis keys, hot keywords as a sorted set."""
import os

import redis


class Cache:
    def __init__(self):
        self._r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

    # --- count cache: count:<reference>:<keyword> -> int ---

    def get_count(self, reference, keyword):
        v = self._r.get(f"count:{reference}:{keyword}")
        return None if v is None else int(v)

    def set_count(self, reference, keyword, n):
        self._r.set(f"count:{reference}:{keyword}", n)

    # --- text cache: text:<reference> -> str ---

    def get_text(self, reference):
        return self._r.get(f"text:{reference}")

    def set_text(self, reference, text):
        self._r.set(f"text:{reference}", text)

    # --- invalidation: a text changed or vanished, drop everything derived from it ---

    def invalidate(self, reference):
        keys = list(self._r.scan_iter(f"count:{reference}:*"))
        keys.append(f"text:{reference}")
        self._r.delete(*keys)

    def flush(self):
        self._r.flushdb()

    # --- hot keywords: sorted set, score = request count ---

    def bump_hot(self, keyword):
        self._r.zincrby("hot_keywords", 1, keyword)

    def hot(self, n):
        return [(kw, int(score)) for kw, score in self._r.zrevrange("hot_keywords", 0, n - 1, withscores=True)]
