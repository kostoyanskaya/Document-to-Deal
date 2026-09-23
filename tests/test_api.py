from __future__ import annotations

import io

import fakeredis.aioredis
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import app.api.routes as routes_module
import app.deps as deps_module
from app.main import app
from app.models.schemas import TaskStatus


@pytest_asyncio.fixture
async def client(monkeypatch, tmp_path):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _get_fake_redis():
        return fake

    from app.storage.task_store import TaskStore

    def _override_task_store():
        return TaskStore(fake, idempotency_ttl_seconds=86400)

    app.dependency_overrides[deps_module.get_task_store] = _override_task_store

    monkeypatch.setattr(routes_module, "process_document", _NoopDelay())

    settings = routes_module.get_settings()
    settings.upload_dir = str(tmp_path / "uploads")

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    await fake.aclose()


class _NoopDelay:

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def delay(self, task_id: str, file_path: str) -> None:
        self.calls.append((task_id, file_path))


def test_health_check(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_upload_rejects_unsupported_extension(client):
    resp = client.post(
        "/api/v1/documents",
        files={
            "file": (
                "doc.exe",
                io.BytesIO(b"binary"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 415


def test_upload_rejects_empty_file(client):
    resp = client.post(
        "/api/v1/documents",
        files={"file": ("doc.txt", io.BytesIO(b""), "text/plain")},
    )
    assert resp.status_code == 400


def test_upload_rejects_oversized_file(client, monkeypatch):
    settings = routes_module.get_settings()
    monkeypatch.setattr(
        settings,
        "max_file_size_mb",
        0,
    )  # anything is "too big"
    resp = client.post(
        "/api/v1/documents",
        files={
            "file": (
                "doc.txt",
                io.BytesIO(b"not empty"),
                "text/plain",
            )
        },
    )
    assert resp.status_code == 413


def test_upload_accepts_txt_and_returns_queued_status(client):
    resp = client.post(
        "/api/v1/documents",
        files={
            "file": (
                "brief.txt",
                io.BytesIO(
                    b"Company: Acme\nTask: build a pilot"
                ),
                "text/plain",
            )
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == TaskStatus.QUEUED.value
    assert body["idempotent"] is False
    assert body["task_id"]


def test_repeated_upload_is_idempotent(client):
    content = b"Company: Acme\nTask: build a pilot, same bytes twice"
    first = client.post(
        "/api/v1/documents",
        files={"file": ("a.txt", io.BytesIO(content), "text/plain")},
    )
    second = client.post(
        "/api/v1/documents",
        files={"file": ("b.txt", io.BytesIO(content), "text/plain")},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["task_id"] == second.json()["task_id"]
    assert second.json()["idempotent"] is True


def test_get_unknown_task_returns_404(client):
    resp = client.get("/api/v1/tasks/does-not-exist")
    assert resp.status_code == 404


def test_approve_before_completion_returns_conflict(client):
    upload = client.post(
        "/api/v1/documents",
        files={
            "file": (
                "c.txt",
                io.BytesIO(b"Task: something"),
                "text/plain",
            )
        },
    )
    task_id = upload.json()["task_id"]
    resp = client.post(f"/api/v1/tasks/{task_id}/approve")
    assert resp.status_code == 409
