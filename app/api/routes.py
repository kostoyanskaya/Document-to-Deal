from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.config import Settings, get_settings
from app.core.hashing import sha256_bytes
from app.deps import get_task_store
from app.models.schemas import ApproveResponse, TaskRecord, TaskStatus, UploadResponse
from app.storage.task_store import TaskStore
from app.worker.tasks import process_document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["documents"])


@router.post("/documents", response_model=UploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    store: TaskStore = Depends(get_task_store),
) -> UploadResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '{suffix}'. Allowed: {list(settings.allowed_extensions)}",
        )

    data = await file.read()
    max_bytes = settings.max_file_size_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.max_file_size_mb} MB limit",
        )
    if len(data) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    file_hash = sha256_bytes(data)

    existing_task_id = await store.get_task_id_for_hash(file_hash)
    if existing_task_id:
        record = await store.get_task(existing_task_id)
        logger.info("idempotent_upload_hit", extra={"hash_prefix": file_hash[:12], "task_id": existing_task_id})
        return UploadResponse(
            task_id=existing_task_id,
            status=record.status if record else TaskStatus.QUEUED,
            idempotent=True,
        )

    task_id = str(uuid.uuid4())
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest_path = upload_dir / f"{task_id}{suffix}"
    dest_path.write_bytes(data)

    await store.create_task(task_id, filename=file.filename or "document")
    await store.link_hash_to_task(file_hash, task_id)

    logger.info(
        "document_uploaded",
        extra={"task_id": task_id, "size_bytes": len(data), "hash_prefix": file_hash[:12]},
    )

    process_document.delay(task_id, str(dest_path))

    return UploadResponse(task_id=task_id, status=TaskStatus.QUEUED, idempotent=False)


@router.get("/tasks/{task_id}", response_model=TaskRecord)
async def get_task(task_id: str, store: TaskStore = Depends(get_task_store)) -> TaskRecord:
    record = await store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return record


@router.post("/tasks/{task_id}/approve", response_model=ApproveResponse)
async def approve_task(task_id: str, store: TaskStore = Depends(get_task_store)) -> ApproveResponse:
    """Manual-approval stub.

    This is a demo endpoint only: it flips the task to `approved` and logs
    the action. It never sends anything to a CRM or to the client — there is
    no such integration in this pilot.
    """
    record = await store.get_task(task_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    if record.status != TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task must be '{TaskStatus.COMPLETED.value}' to approve, currently '{record.status.value}'",
        )

    record.status = TaskStatus.APPROVED
    await store.save_task(record)
    logger.info("task_approved", extra={"task_id": task_id})

    return ApproveResponse(
        task_id=task_id,
        status=record.status,
        message="Approved manually. Nothing was sent to any external CRM or to the client.",
    )


@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
