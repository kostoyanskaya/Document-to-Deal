"""Shared pytest fixtures.

Uses `fakeredis` so the whole suite runs with no real Redis server and
`LLM_PROVIDER=mock` (set in .env / CI) so it runs with no API key either —
per the task's requirement that the project "runs and passes tests without
an API key".
"""
from __future__ import annotations

import os
from pathlib import Path

import fakeredis.aioredis
import pytest
import pytest_asyncio

os.environ.setdefault("LLM_PROVIDER", "mock")
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")

from app.config import Settings  # noqa: E402
from app.storage.task_store import TaskStore  # noqa: E402

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        llm_provider="mock",
        redis_url="redis://localhost:6379/0",
        upload_dir=str(tmp_path / "uploads"),
        celery_task_always_eager=True,
    )


@pytest_asyncio.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def task_store(fake_redis) -> TaskStore:
    return TaskStore(fake_redis, idempotency_ttl_seconds=86400)


@pytest.fixture
def normal_doc_path() -> Path:
    return EXAMPLES_DIR / "normal_brief.txt"


@pytest.fixture
def injection_doc_path() -> Path:
    return EXAMPLES_DIR / "injection_attempt.txt"
