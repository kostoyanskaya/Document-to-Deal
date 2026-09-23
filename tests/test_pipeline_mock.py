from __future__ import annotations

import pytest

from app.models.schemas import SecurityVerdict, TaskStatus
from app.services.chunking import split_into_chunks
from app.services.pipeline import run_pipeline


@pytest.mark.asyncio
async def test_pipeline_completes_for_normal_document(normal_doc_path, settings):
    result = await run_pipeline(normal_doc_path, settings)
    assert result.status == TaskStatus.COMPLETED
    assert result.security_verdict == SecurityVerdict.CLEAN
    assert result.lead_card is not None
    assert result.lead_card.task
    assert result.brief_markdown and "бриф" in result.brief_markdown.lower()
    assert result.proposal_markdown
    assert "этапы пилота" in result.proposal_markdown.lower() or "Этапы пилота" in result.proposal_markdown


@pytest.mark.asyncio
async def test_pipeline_blocks_injection_document_without_producing_output(injection_doc_path, settings):
    result = await run_pipeline(injection_doc_path, settings)
    assert result.status in (TaskStatus.BLOCKED, TaskStatus.NEEDS_REVIEW)
    assert result.lead_card is None
    assert result.brief_markdown is None
    assert result.proposal_markdown is None
    assert result.security_reasons


@pytest.mark.asyncio
async def test_pipeline_never_leaks_system_prompt_in_output(injection_doc_path, settings):
    result = await run_pipeline(injection_doc_path, settings)
    combined = " ".join(filter(None, [result.brief_markdown, result.proposal_markdown, result.error]))
    assert "Жёсткие правила безопасности" not in combined


def test_chunking_splits_large_text():
    text = "A" * 10_000
    chunks = split_into_chunks(text, chunk_size=4000, overlap=200)
    assert len(chunks) > 1
    assert all(len(c) <= 4000 for c in chunks)


def test_chunking_keeps_small_text_as_one_chunk():
    text = "short text"
    chunks = split_into_chunks(text, chunk_size=4000, overlap=200)
    assert chunks == [text]


@pytest.mark.asyncio
async def test_pipeline_map_reduce_on_large_document(tmp_path, settings):
    settings.chunk_char_threshold = 500
    settings.chunk_size_chars = 300
    settings.chunk_overlap_chars = 50

    big_doc = tmp_path / "big.txt"
    content = (
        "Компания: BigCorp\n" + ("Общий контекст запроса клиента. " * 200) +
        "\nЗадача: построить пилотный модуль извлечения данных.\n" +
        ("Дополнительные детали проекта и контекста. " * 200)
    )
    big_doc.write_text(content, encoding="utf-8")

    result = await run_pipeline(big_doc, settings)
    assert result.status == TaskStatus.COMPLETED
    assert result.lead_card is not None
