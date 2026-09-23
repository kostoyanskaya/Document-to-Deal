"""File hashing used for idempotency (SHA-256 of raw bytes -> task_id in Redis)."""
from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
