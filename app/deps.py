"""FastAPI dependency wiring (Redis connection, TaskStore)."""
from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import Depends

from app.config import Settings, get_settings
from app.storage.task_store import TaskStore

_redis_client: aioredis.Redis | None = None


def get_redis(settings: Settings = Depends(get_settings)) -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def get_task_store(
    settings: Settings = Depends(get_settings),
    redis: aioredis.Redis = Depends(get_redis),
) -> TaskStore:
    return TaskStore(redis, settings.idempotency_ttl_seconds)
