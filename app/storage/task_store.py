from __future__ import annotations

from datetime import datetime, timezone

from redis.asyncio import Redis

from app.models.schemas import TaskRecord, TaskStatus

_TASK_PREFIX = "task:"
_HASH_PREFIX = "hash:"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    def __init__(self, redis: Redis, idempotency_ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl = idempotency_ttl_seconds

    async def create_task(self, task_id: str, filename: str) -> TaskRecord:
        record = TaskRecord(
            task_id=task_id,
            status=TaskStatus.QUEUED,
            filename=filename,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        await self._redis.set(_TASK_PREFIX + task_id, record.model_dump_json())
        return record

    async def get_task(self, task_id: str) -> TaskRecord | None:
        raw = await self._redis.get(_TASK_PREFIX + task_id)
        if raw is None:
            return None
        return TaskRecord.model_validate_json(raw)

    async def save_task(self, record: TaskRecord) -> None:
        record.updated_at = _now_iso()
        await self._redis.set(_TASK_PREFIX + record.task_id, record.model_dump_json())

    async def get_task_id_for_hash(self, file_hash: str) -> str | None:
        return await self._redis.get(_HASH_PREFIX + file_hash)

    async def link_hash_to_task(self, file_hash: str, task_id: str) -> None:
        await self._redis.set(_HASH_PREFIX + file_hash, task_id, ex=self._ttl)
