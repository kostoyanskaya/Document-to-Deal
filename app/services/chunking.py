"""Simple char-based chunking for the map-reduce extraction path.

No vector DB, no embeddings: for large documents we just split the text into
overlapping windows, run the extraction LLM call on each ("map"), then merge
the partial results into one LeadCard ("reduce") in `app.services.pipeline`.
"""
from __future__ import annotations


def split_into_chunks(text: str, chunk_size: int, overlap: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = min(start + chunk_size, length)
        chunks.append(text[start:end])
        if end == length:
            break
        start = end - overlap
    return chunks
