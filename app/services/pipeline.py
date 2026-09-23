from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.core.security import detect_prompt_injection
from app.models.schemas import LeadCard, PipelineResult, SecurityVerdict, TaskStatus
from app.services.chunking import split_into_chunks
from app.services.extraction import extract_text
from app.services.injection_judge import judge_for_injection
from app.services.llm_adapter import LLMAdapter, LLMError, get_llm_adapter
from app.services.output_guard import find_leaked_system_prompt_fragments
from app.services.prompts import EXTRACTION_SYSTEM_PROMPT, GENERATION_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

_SCALAR_FIELDS = ("company", "industry", "task", "problem", "expected_result", "timeline", "budget")
_LIST_FIELDS = ("integrations", "risks", "missing_data")

# Итоговый вердикт хуже (строже) побеждает: clean < needs_review < blocked.
_VERDICT_SEVERITY = {
    SecurityVerdict.CLEAN: 0,
    SecurityVerdict.NEEDS_REVIEW: 1,
    SecurityVerdict.BLOCKED: 2,
}


def _merge_partials(partials: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce step: merge partial LeadCard dicts extracted from each chunk."""
    merged: dict[str, Any] = {}

    for field_name in _SCALAR_FIELDS:
        for partial in partials:
            value = partial.get(field_name)
            if value:
                merged[field_name] = value
                break

    contact = next((p["contact"] for p in partials if p.get("contact")), None)
    merged["contact"] = contact

    for field_name in _LIST_FIELDS:
        deduped: list[str] = []
        for partial in partials:
            for item in partial.get(field_name) or []:
                if item not in deduped:
                    deduped.append(item)
        merged[field_name] = deduped

    return merged


async def _call_extraction_with_retries(adapter: LLMAdapter, chunk: str, settings: Settings) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(settings.llm_max_retries + 1):
        try:
            raw = await adapter.complete_json(EXTRACTION_SYSTEM_PROMPT, chunk)
            # Validate early (with a placeholder task if missing) so a chunk that
            # produced schema-invalid output is retried, not just JSON-invalid output.
            LeadCard.model_validate({**raw, "task": raw.get("task") or "placeholder"})
            return raw
        except (LLMError, ValidationError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "llm_extraction_attempt_failed",
                extra={"attempt": attempt, "error_type": type(exc).__name__},
            )
    logger.error("llm_extraction_failed_after_retries", extra={"error_type": type(last_error).__name__ if last_error else None})
    return {"task": "Не удалось извлечь данные из документа (ошибка LLM после повторных попыток)"}


async def _extract_lead_card(adapter: LLMAdapter, text: str, settings: Settings) -> LeadCard:
    needs_chunking = len(text) > settings.chunk_char_threshold
    chunks = (
        split_into_chunks(text, settings.chunk_size_chars, settings.chunk_overlap_chars)
        if needs_chunking
        else [text]
    )
    if needs_chunking:
        logger.info("map_reduce_chunking_engaged", extra={"chunks": len(chunks)})

    partials = [await _call_extraction_with_retries(adapter, chunk, settings) for chunk in chunks]
    merged = _merge_partials(partials) if len(partials) > 1 else partials[0]

    try:
        return LeadCard.model_validate(merged)
    except ValidationError:
        logger.warning("lead_card_final_validation_failed_using_fallback")
        merged.setdefault("task", "Не удалось однозначно определить задачу клиента")
        return LeadCard.model_validate(merged)


async def _generate_brief_and_proposal(adapter: LLMAdapter, lead_card: LeadCard) -> tuple[str, str]:
    payload = lead_card.model_dump_json(indent=2)

    brief = await adapter.complete_text(
        GENERATION_SYSTEM_PROMPT,
        f"Сформируй краткий аналитический бриф в Markdown на основе следующей карточки лида (JSON):\n{payload}",
    )
    proposal = await adapter.complete_text(
        GENERATION_SYSTEM_PROMPT,
        (
            "Сформируй черновик коммерческого предложения (статус draft) в Markdown на основе "
            "следующей карточки лида (JSON). Обязательно включи разделы 'Ценность для клиента', "
            f"'Этапы пилота' и 'Вопросы для уточнения':\n{payload}"
        ),
    )
    return brief, proposal


def _blocked_result(verdict: SecurityVerdict, reasons: list[str]) -> PipelineResult:
    status = TaskStatus.BLOCKED if verdict == SecurityVerdict.BLOCKED else TaskStatus.NEEDS_REVIEW
    logger.warning("pipeline_halted_by_security", extra={"task_verdict": verdict.value, "status": status.value})
    return PipelineResult(
        status=status,
        security_verdict=verdict,
        security_reasons=reasons,
        # lead_card / brief_markdown / proposal_markdown намеренно None
    )


async def run_pipeline(file_path: Path, settings: Settings | None = None) -> PipelineResult:
    settings = settings or get_settings()

    text = extract_text(file_path)
    # Safe logging: length only, never the document content itself.
    logger.info("document_text_extracted", extra={"chars": len(text)})

    # --- СЛОЙ 1: детерминированный regex/keyword фильтр ---------------------
    detection = detect_prompt_injection(text)
    logger.info(
        "prompt_injection_scan_completed",
        extra={"verdict": detection.verdict.value, "matched_rules": detection.matched_rules},
    )

    verdict = detection.verdict
    reasons = list(detection.matched_rules)

    # --- СЛОЙ 2: LLM-as-judge (семантическая проверка) -----------------------
    # Regex ловит только дословные формулировки. Судья запускается ТОЛЬКО когда
    # настроен реальный провайдер (в mock-режиме его просто нет и проверять
    # нечем — задача "проект должен запускаться без API-ключа" в ТЗ выполняется
    # тем, что слой 1 продолжает работать сам по себе). Судью гоняем даже если
    # слой 1 уже сказал CLEAN — это и есть его смысл: ловить то, что regex
    # пропустил. Если слой 1 уже дал BLOCKED, второй проход не нужен — хуже
    # вердикт уже некуда.
    if settings.llm_provider != "mock" and verdict != SecurityVerdict.BLOCKED:
        try:
            judge_adapter = get_llm_adapter(settings)
            judge_result = await judge_for_injection(judge_adapter, text, enabled=True)
            if judge_result.ran and _VERDICT_SEVERITY[judge_result.verdict] > _VERDICT_SEVERITY[verdict]:
                verdict = judge_result.verdict
                reasons.append(f"llm_judge:{judge_result.reason or judge_result.verdict.value}")
            logger.info(
                "injection_judge_completed",
                extra={"ran": judge_result.ran, "verdict": judge_result.verdict.value},
            )
        except LLMError as exc:
            # Судья недоступен (например, невалидный ключ) — fail closed, не тихо
            # пропускаем документ дальше как CLEAN.
            logger.warning("injection_judge_unavailable", extra={"error_type": type(exc).__name__})
            if _VERDICT_SEVERITY[SecurityVerdict.NEEDS_REVIEW] > _VERDICT_SEVERITY[verdict]:
                verdict = SecurityVerdict.NEEDS_REVIEW
                reasons.append("llm_judge_unavailable")

    # ГЛАВНОЕ ПРАВИЛО: любая инъекция — стоп. LLM для генерации не вызывается,
    # lead_card / brief / proposal не создаются.
    if verdict != SecurityVerdict.CLEAN:
        return _blocked_result(verdict, reasons)

    # Сюда попадаем только при итоговом verdict == CLEAN (оба слоя согласны)
    adapter = get_llm_adapter(settings)

    try:
        lead_card = await _extract_lead_card(adapter, text, settings)
        brief_md, proposal_md = await _generate_brief_and_proposal(adapter, lead_card)
    except LLMError:
        logger.exception("pipeline_llm_error")
        return PipelineResult(
            status=TaskStatus.FAILED,
            security_verdict=verdict,
            security_reasons=reasons,
            error="LLM processing failed",
        )

    # --- СЛОЙ 3: output filtering ---------------------------------------------
    # Даже "чистый" вход не гарантирует безопасный выход: сама генеративная
    # модель может процитировать собственный системный промпт (OWASP LLM07:
    # System Prompt Leakage), если её об этом как-то удалось убедить на этапе
    # генерации. Сканируем результат на известные фрагменты системных
    # промптов перед тем, как отдать его наружу.
    leak_hits = find_leaked_system_prompt_fragments(
        [brief_md, proposal_md],
        system_prompts=[EXTRACTION_SYSTEM_PROMPT, GENERATION_SYSTEM_PROMPT],
    )
    if leak_hits:
        logger.warning("output_guard_blocked_system_prompt_leak", extra={"fragments": leak_hits})
        return PipelineResult(
            status=TaskStatus.NEEDS_REVIEW,
            security_verdict=SecurityVerdict.NEEDS_REVIEW,
            security_reasons=reasons + ["output_system_prompt_leak"],
        )

    return PipelineResult(
        status=TaskStatus.COMPLETED,
        security_verdict=verdict,
        security_reasons=reasons,
        lead_card=lead_card,
        brief_markdown=brief_md,
        proposal_markdown=proposal_md,
    )
