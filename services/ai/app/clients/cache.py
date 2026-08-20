"""Scrape cache behind a tiny interface.

Scraping the same posting twice is wasteful and, on rate-limited boards, risky.
But a hard Redis dependency would mean unit tests need a running server, so the
cache is an interface with an in-memory implementation and a null object.

Failures never propagate. A cache miss and a cache outage should look identical
to the caller -- an unavailable Redis must not take down the pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60 * 60 * 6  # 6h: postings change slowly, but they do change


def cache_key(prefix: str, value: str) -> str:
    """Hash rather than embed: URLs carry tracking params and can exceed key limits."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"resumeforge:{prefix}:{digest}"


class Cache(ABC):
    @abstractmethod
    async def get(self, key: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def set(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL_SECONDS) -> None: ...


class NullCache(Cache):
    """Always misses. The default, so nothing depends on Redis existing."""

    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def set(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL_SECONDS) -> None:
        return None


class MemoryCache(Cache):
    """Process-local. Used by tests and single-instance development."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        return self._store.get(key)

    async def set(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL_SECONDS) -> None:
        self._store[key] = value


class RedisCache(Cache):
    """Shared across replicas -- the only implementation that helps at scale."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self._client.get(key)
        except Exception:
            logger.warning("Cache read failed for %s; treating as a miss", key, exc_info=True)
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Discarding corrupt cache entry %s", key)
            return None

    async def set(self, key: str, value: dict[str, Any], ttl: int = DEFAULT_TTL_SECONDS) -> None:
        try:
            await self._client.set(key, json.dumps(value), ex=ttl)
        except Exception:
            # A write failure costs a future cache hit, nothing more.
            logger.warning("Cache write failed for %s", key, exc_info=True)


def get_cache() -> Cache:
    from app.config import get_settings

    settings = get_settings()
    if not settings.redis_url:
        return NullCache()
    try:
        return RedisCache(settings.redis_url)
    except Exception:
        logger.warning("Redis unavailable; running without a scrape cache", exc_info=True)
        return NullCache()
