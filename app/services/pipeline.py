from __future__ import annotations
 
import logging
from pathlib import Path
from typing import Any
 
from pydantic import ValidationError
 
from app.config import Settings, get_settings
from app.core.security import detect_prompt_injection
from app.models.schemas import LeadCard, PipelineResult, SecurityVerdict, TaskStatus
from app.services.chunking import split_into_chunks
from app.services.extraction import DocumentParsingError, extract_text
from app.services.injection_judge import judge_for_injection
from app.services.llm_adapter import LLMAdapter, LLMError, get_llm_adapter
from app.services.output_guard import find_leaked_system_prompt_fragments
from app.services.prompts import EXTRACTION_SYSTEM_PROMPT, GENERATION_SYSTEM_PROMPT
 
logger = logging.getLogger(__name__)
 
_SCALAR_FIELDS = ("company", "industry", "task", "problem", "expected_result", "timeline", "budget")
_LIST_FIELDS = ("integrations", "risks", "missing_data")
 

_VERDICT_SEVERITY = {
    SecurityVerdict.CLEAN: 0,
    SecurityVerdict.NEEDS_REVIEW: 1,
    SecurityVerdict.BLOCKED: 2,
}
 
 
def _merge_partials(partials: list[dict[str, Any]]) -> dict[str, Any]:
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
            LeadCard.model_validate({**raw, "task": raw.get("task") or "placeholder"})
            return raw
        except (LLMError, ValidationError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "llm_extraction_attempt_failed",
                extra={"attempt": attempt, "error_type": type(exc).__name__},
            )
    logger.error(
        "llm_extraction_failed_after_retries",
        extra={"error_type": type(last_error).__name__ if last_error else None},
    )
    raise LLMError("Structured lead extraction failed after retries") from last_error
 
 
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
    except ValidationError as exc:
        logger.error("lead_card_final_validation_failed")
        raise LLMError("Final lead card validation failed") from exc
 
 
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
    if not brief.strip() or not proposal.strip():
        raise LLMError("LLM returned an empty Markdown result")

    required_sections = (
        "ценность для клиента",
        "этапы пилота",
        "вопросы для уточнения",
    )
    proposal_lower = proposal.lower()
    if not all(section in proposal_lower for section in required_sections):
        raise LLMError("Proposal is missing required sections")

    return brief, proposal
 
 
def _blocked_result(verdict: SecurityVerdict, reasons: list[str]) -> PipelineResult:
    status = TaskStatus.BLOCKED if verdict == SecurityVerdict.BLOCKED else TaskStatus.NEEDS_REVIEW
    logger.warning("pipeline_halted_by_security", extra={"task_verdict": verdict.value, "status": status.value})
    return PipelineResult(
        status=status,
        security_verdict=verdict,
        security_reasons=reasons,
    )
 
 
async def run_pipeline(file_path: Path, settings: Settings | None = None) -> PipelineResult:
    settings = settings or get_settings()
 
    try:
        text = extract_text(file_path)
    except DocumentParsingError as exc:
        logger.warning("document_parsing_failed", extra={"error_type": type(exc).__name__})
        return PipelineResult(
            status=TaskStatus.FAILED,
            security_verdict=SecurityVerdict.CLEAN,
            error="Document could not be parsed",
        )

    logger.info("document_text_extracted", extra={"chars": len(text)})
    detection = detect_prompt_injection(text)
    logger.info(
        "prompt_injection_scan_completed",
        extra={"verdict": detection.verdict.value, "matched_rules": detection.matched_rules},
    )
 
    verdict = detection.verdict
    reasons = list(detection.matched_rules)
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
            logger.warning("injection_judge_unavailable", extra={"error_type": type(exc).__name__})
            if _VERDICT_SEVERITY[SecurityVerdict.NEEDS_REVIEW] > _VERDICT_SEVERITY[verdict]:
                verdict = SecurityVerdict.NEEDS_REVIEW
                reasons.append("llm_judge_unavailable")
 
    if verdict != SecurityVerdict.CLEAN:
        return _blocked_result(verdict, reasons)
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