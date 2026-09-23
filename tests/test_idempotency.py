from __future__ import annotations

import pytest

from app.core.hashing import sha256_bytes
from app.models.schemas import TaskStatus


def test_sha256_is_deterministic():
    data = b"hello world"
    assert sha256_bytes(data) == sha256_bytes(data)


def test_sha256_differs_for_different_content():
    assert sha256_bytes(b"a") != sha256_bytes(b"b")


@pytest.mark.asyncio
async def test_repeated_upload_of_same_hash_reuses_task_id(task_store):
    file_hash = sha256_bytes(b"identical content")

    assert await task_store.get_task_id_for_hash(file_hash) is None

    reserved = await task_store.create_task_if_hash_absent(
        file_hash, "task-1", filename="doc.txt"
    )
    assert reserved is True

    # Simulate the second, identical upload: it must resolve to the same task_id
    # instead of a new one being created.
    resolved = await task_store.get_task_id_for_hash(file_hash)
    assert resolved == "task-1"


@pytest.mark.asyncio
async def test_task_status_lifecycle_is_persisted(task_store):
    record = await task_store.create_task("task-2", filename="doc.txt")
    assert record.status == TaskStatus.QUEUED

    record.status = TaskStatus.PROCESSING
    await task_store.save_task(record)

    fetched = await task_store.get_task("task-2")
    assert fetched is not None
    assert fetched.status == TaskStatus.PROCESSING

    fetched.status = TaskStatus.COMPLETED
    await task_store.save_task(fetched)

    final = await task_store.get_task("task-2")
    assert final.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_atomic_hash_reservation_allows_only_one_task(task_store):
    file_hash = sha256_bytes(b"same content")
    first = await task_store.create_task_if_hash_absent(
        file_hash, "task-a", "a.txt"
    )
    second = await task_store.create_task_if_hash_absent(
        file_hash, "task-b", "b.txt"
    )
    assert first is True
    assert second is False
    assert await task_store.get_task_id_for_hash(file_hash) == "task-a"
