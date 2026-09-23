"""Small Redis wrapper used for rate limiting and short-lived caches.

If Redis is unreachable in development we fall back to an in-process store so
the platform still runs; production should always have Redis.
"""

from __future__ import annotations

import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class Cache:
    def __init__(self, redis: Redis | None) -> None:
        self._redis = redis
        self._memory: dict[str, tuple[float, int]] = {}

    async def incr_window(self, key: str, window_seconds: int) -> int:
        """Increment a fixed-window counter and return the new count."""
        if self._redis is not None:
            try:
                async with self._redis.pipeline(transaction=True) as pipe:
                    pipe.incr(key)
                    pipe.expire(key, window_seconds, nx=True)
                    count, _ = await pipe.execute()
                return int(count)
            except RedisError as exc:
                logger.warning("Redis unavailable, using in-memory counter: %s", exc)
        now = time.monotonic()
        expires, count = self._memory.get(key, (now + window_seconds, 0))
        if expires < now:
            expires, count = now + window_seconds, 0
        self._memory[key] = (expires, count + 1)
        return count + 1

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            return bool(await self._redis.ping())
        except RedisError:
            return False

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()


_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        url = get_settings().redis_url
        _cache = Cache(Redis.from_url(url, socket_connect_timeout=2) if url else None)
    return _cache


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.close()
        _cache = None
