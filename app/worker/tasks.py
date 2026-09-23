from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import redis.asyncio as aioredis

from app.config import get_settings
from app.models.schemas import TaskStatus
from app.services.pipeline import run_pipeline
from app.storage.task_store import TaskStore
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="process_document", bind=True, max_retries=0)
def process_document(self, task_id: str, file_path: str) -> None:  # noqa: ANN001
    asyncio.run(_process_document_async(task_id, file_path))


async def _process_document_async(task_id: str, file_path: str) -> None:
    settings = get_settings()
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    store = TaskStore(redis, settings.idempotency_ttl_seconds)

    try:
        record = await store.get_task(task_id)
        if record is None:
            logger.error("task_not_found_for_processing", extra={"task_id": task_id})
            return

        record.status = TaskStatus.PROCESSING
        await store.save_task(record)

        try:
            result = await run_pipeline(Path(file_path), settings)
        except Exception:  # noqa: BLE001
            logger.exception("pipeline_unexpected_error", extra={"task_id": task_id})
            record.status = TaskStatus.FAILED
            record.error = "Internal processing error"
            await store.save_task(record)
            return

        record.status = result.status
        record.security_verdict = result.security_verdict
        record.security_reasons = result.security_reasons
        record.lead_card = result.lead_card
        record.brief_markdown = result.brief_markdown
        record.proposal_markdown = result.proposal_markdown
        record.error = result.error
        await store.save_task(record)
        logger.info("task_processing_finished", extra={"task_id": task_id, "status": record.status.value})
    finally:
        await redis.aclose()
