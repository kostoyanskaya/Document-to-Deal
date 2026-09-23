from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    UploadFile,
    status,
)

from app.config import Settings, get_settings
from app.core.hashing import sha256_file
from app.deps import get_task_store
from app.models.schemas import (
    ApproveResponse,
    TaskRecord,
    TaskStatus,
    UploadResponse,
)
from app.storage.task_store import TaskStore
from app.worker.tasks import process_document

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["documents"])


@router.post(
    "/documents",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    store: TaskStore = Depends(get_task_store),
) -> UploadResponse:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{suffix}'. "
                f"Allowed: {list(settings.allowed_extensions)}"
            ),
        )

    max_bytes = settings.max_file_size_mb * 1024 * 1024
    task_id = str(uuid.uuid4())
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest_path = upload_dir / f"{task_id}{suffix}"

    total_bytes = 0
    try:
        with dest_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"File exceeds {settings.max_file_size_mb} MB limit",
                    )
                output.write(chunk)

        if total_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Empty file",
            )

        file_hash = sha256_file(dest_path)
        existing_task_id = await store.get_task_id_for_hash(file_hash)
        if existing_task_id:
            dest_path.unlink(missing_ok=True)
            record = await store.get_task(existing_task_id)
            logger.info(
                "idempotent_upload_hit",
                extra={"hash_prefix": file_hash[:12], "task_id": existing_task_id},
            )
            return UploadResponse(
                task_id=existing_task_id,
                status=record.status if record else TaskStatus.QUEUED,
                idempotent=True,
            )

        reserved = await store.create_task_if_hash_absent(
            file_hash, task_id, file.filename or "document"
        )
        if not reserved:
            existing_task_id = await store.get_task_id_for_hash(file_hash)
            dest_path.unlink(missing_ok=True)
            if not existing_task_id:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Could not resolve idempotent task",
                )
            record = await store.get_task(existing_task_id)
            return UploadResponse(
                task_id=existing_task_id,
                status=record.status if record else TaskStatus.QUEUED,
                idempotent=True,
            )

        logger.info(
            "document_uploaded",
            extra={
                "task_id": task_id,
                "size_bytes": total_bytes,
                "hash_prefix": file_hash[:12],
            },
        )
    except HTTPException:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception:
        dest_path.unlink(missing_ok=True)
        logger.exception("document_upload_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not store document",
        )

    process_document.delay(task_id, str(dest_path))

    return UploadResponse(
        task_id=task_id,
        status=TaskStatus.QUEUED,
        idempotent=False,
    )


@router.get("/tasks/{task_id}", response_model=TaskRecord)
async def get_task(
    task_id: str,
    store: TaskStore = Depends(get_task_store),
) -> TaskRecord:
    record = await store.get_task(task_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    return record


@router.post("/tasks/{task_id}/approve", response_model=ApproveResponse)
async def approve_task(
    task_id: str,
    store: TaskStore = Depends(get_task_store),
) -> ApproveResponse:
    record = await store.get_task(task_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    if record.status != TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Task must be '{TaskStatus.COMPLETED.value}' to "
                f"approve, currently '{record.status.value}'"
            ),
        )

    record.status = TaskStatus.APPROVED
    await store.save_task(record)
    logger.info("task_approved", extra={"task_id": task_id})

    return ApproveResponse(
        task_id=task_id,
        status=record.status,
        message=(
            "Approved manually. Nothing was sent to any external "
            "CRM or to the client."
        ),
    )


@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
