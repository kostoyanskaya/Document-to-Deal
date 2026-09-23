from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised for any provider failure: network error, timeout, invalid JSON, etc."""


class LLMAdapter(ABC):
    @abstractmethod
    async def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Return a parsed JSON object. Raises LLMError if the call or parsing fails."""

    @abstractmethod
    async def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        """Return raw text output (used for Markdown generation)."""


def _strip_code_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else ""
        if raw.endswith("```"):
            raw = raw[:-3]
        if raw.lower().startswith("json"):
            raw = raw[4:]
    return raw.strip()


# --------------------------------------------------------------------------- #
# Mock adapter — no network, deterministic, used in dev/CI by default.
# --------------------------------------------------------------------------- #
class MockLLMAdapter(LLMAdapter):
    """Rule-based stand-in for a real model.

    It does a small heuristic "extraction" (looks for RU/EN field labels and
    an email address) so the pipeline produces plausible-looking output and
    can be exercised end to end without any API key.
    """

    _FIELD_MARKERS: dict[str, list[str]] = {
        "company": ["компания:", "клиент:", "company:", "client:"],
        "industry": ["отрасль:", "industry:"],
        "contact": ["контакт:", "contact:"],
        "timeline": ["срок:", "сроки:", "deadline:", "timeline:"],
        "budget": ["бюджет:", "budget:"],
        "task": ["задача:", "task:"],
        "problem": ["проблема:", "problem:"],
        "expected_result": ["результат:", "ожидаемый результат:", "expected result:"],
        "integrations": ["необходимые интеграции:", "интеграции:", "integrations:"],
        "risks": ["риски:", "risks:"],
    }
    _EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    _PHONE_RE = re.compile(r"(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")

    # BUGFIX: раньше поле обрезалось по первому "\n" (snippet.split("\n")[0]),
    # что теряло "хвост" значения, если исходный текст был просто перенесён
    # на новую строку по ширине (как в normal_brief.txt: "...входящие заявки\n
    # клиентов ..." — одно предложение, разорванное переносом строки). Теперь
    # значение читается до следующей ПУСТОЙ строки (границы абзаца/поля) или
    # до начала следующего известного маркера — какая граница ближе, — а
    # переносы внутри значения схлопываются в пробел, чтобы не склеивать
    # слова ("заявки" + "клиентов" -> "заявки клиентов", а не "заявкиклиентов").
    def _all_marker_strings(self) -> list[str]:
        return [m for markers in self._FIELD_MARKERS.values() for m in markers]

    def _find_after(self, text: str, lower: str, markers: list[str]) -> str | None:
        all_markers = self._all_marker_strings()
        for marker in markers:
            idx = lower.find(marker)
            if idx == -1:
                continue
            start = idx + len(marker)
            window = text[start: start + 500]
            window_lower = window.lower()

            # граница 1: пустая строка (конец абзаца/поля)
            blank_line_pos = window_lower.find("\n\n")
            end = blank_line_pos if blank_line_pos != -1 else len(window)

            # граница 2: начало следующего маркера (на случай, если поля идут
            # без пустой строки между ними)
            for other in all_markers:
                other_pos = window_lower.find(other)
                if other_pos != -1 and other_pos > 0:
                    end = min(end, other_pos)

            value = window[:end]
            value = " ".join(value.split())  # схлопнуть \n/пробелы в один пробел
            value = value.strip(" :-\t")
            if value:
                return value
        return None

    def _extract_contact(self, text: str, lower: str, fallback_email: str | None) -> dict[str, Any] | None:
        raw = self._find_after(text, lower, self._FIELD_MARKERS["contact"])
        email = fallback_email or (self._EMAIL_RE.search(raw) if raw else None) or None
        if isinstance(email, re.Match):
            email = email.group(0)
        if not raw:
            return {"email": email} if email else None

        phone_match = self._PHONE_RE.search(raw)
        phone = phone_match.group(0) if phone_match else None

        # "Иван Петров, руководитель отдела цифровизации, ivan.petrov@..." ->
        # первый фрагмент до запятой обычно имя, следующий — должность.
        # Убираем email/телефон из каждого фрагмента, пустые после этого
        # (т.е. фрагменты, целиком состоявшие из email/телефона) отбрасываем.
        raw_parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
        text_parts: list[str] = []
        for part in raw_parts:
            cleaned = self._EMAIL_RE.sub("", part)
            cleaned = self._PHONE_RE.sub("", cleaned).strip()
            if cleaned:
                text_parts.append(cleaned)

        name = text_parts[0] if text_parts else None
        position = text_parts[1] if len(text_parts) > 1 else None

        if not any([name, email, phone, position]):
            return None
        return {"name": name, "email": email, "phone": phone, "position": position}

    @staticmethod
    def _split_list_value(value: str | None) -> list[str]:
        if not value:
            return []
        items = re.split(r"[,;•]|(?:\s-\s)", value)
        return [item.strip(" .") for item in items if item.strip(" .")]

    async def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        text = user_prompt
        lower = text.lower()

        # Скалярные поля, для которых достаточно "найти после маркера до границы"
        scalar_keys = ("company", "industry", "timeline", "budget", "task", "problem", "expected_result")
        fields = {key: self._find_after(text, lower, self._FIELD_MARKERS[key]) for key in scalar_keys}

        email_match = self._EMAIL_RE.search(text)
        global_email = email_match.group(0) if email_match else None

        # BUGFIX: integrations/risks раньше были захардкожены как [] и никогда
        # не заполнялись, хотя ТЗ требует извлекать "необходимые интеграции".
        integrations = self._split_list_value(self._find_after(text, lower, self._FIELD_MARKERS["integrations"]))
        risks = self._split_list_value(self._find_after(text, lower, self._FIELD_MARKERS["risks"]))

        contact = self._extract_contact(text, lower, fallback_email=global_email)

        task = fields.get("task") or (text.strip().split("\n")[0][:200] if text.strip() else None) or (
            "Не удалось однозначно определить задачу клиента"
        )

        missing = [
            label
            for label, value in {
                "контакт": contact and contact.get("email"),
                "бюджет": fields.get("budget"),
                "сроки": fields.get("timeline"),
            }.items()
            if not value
        ]

        return {
            "company": fields.get("company"),
            "industry": fields.get("industry"),
            "contact": contact,
            "task": task,
            "problem": fields.get("problem"),
            "expected_result": fields.get("expected_result"),
            "timeline": fields.get("timeline"),
            "budget": fields.get("budget"),
            "integrations": integrations,
            "risks": risks,
            "missing_data": missing,
        }

    async def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        # BUGFIX: раньше этот метод игнорировал user_prompt и всегда возвращал
        # один и тот же статический текст — из-за этого brief_markdown и
        # proposal_markdown в mock-режиме были ИДЕНТИЧНЫ (см. _generate_brief_and_proposal
        # в pipeline.py, который вызывает complete_text дважды с разными промптами:
        # один раз просит бриф, другой раз — КП с разделами "Ценность для клиента",
        # "Этапы пилота", "Вопросы для уточнения"). Теперь ветвимся по содержимому
        # user_prompt, как и должна вести себя настоящая модель, получившая разные
        # инструкции.
        lower_prompt = user_prompt.lower()
        if "коммерческого предложения" in lower_prompt or "кп" in lower_prompt.split():
            return self._mock_proposal()
        return self._mock_brief()

    @staticmethod
    def _mock_brief() -> str:
        return (
            "# Аналитический бриф (mock-режим, черновик)\n\n"
            "_Сгенерировано без обращения к реальной LLM (`LLM_PROVIDER=mock`)._\n\n"
            "## Резюме\n"
            "Документ обработан демонстрационным конвейером AthenAI Document-to-Deal Pilot.\n\n"
            "## Ключевые наблюдения\n"
            "- Задача клиента и доступные структурированные поля перечислены в карточке лида\n"
            "- Недостающие данные (бюджет, сроки, контакт) отмечены отдельно и требуют уточнения\n\n"
            "## Риски\n"
            "- См. поле `risks` в карточке лида, если были обнаружены подозрительные фрагменты\n"
        )

    @staticmethod
    def _mock_proposal() -> str:
        return (
            "# Черновик коммерческого предложения (draft)\n\n"
            "_Сгенерировано без обращения к реальной LLM (`LLM_PROVIDER=mock`). "
            "Статус: **draft** — не отправлено клиенту и не передано в CRM._\n\n"
            "## Ценность для клиента\n"
            "- Быстрая первичная квалификация входящих заявок\n"
            "- Структурированные данные для менеджера по продажам вместо ручного разбора\n\n"
            "## Этапы пилота\n"
            "1. Приём документа и проверка на попытки prompt injection\n"
            "2. Извлечение структурированной карточки лида\n"
            "3. Генерация брифа и черновика КП\n"
            "4. Ручное одобрение (без автоматической отправки клиенту или в CRM)\n\n"
            "## Вопросы для уточнения\n"
            "- Требуется уточнить бюджет и точные сроки проекта\n"
            "- Нужны ли дополнительные интеграции, не указанные в документе\n"
        )


# --------------------------------------------------------------------------- #
# Real providers — OpenAI-compatible.
# --------------------------------------------------------------------------- #
class OpenAIAdapter(LLMAdapter):
    def __init__(self, api_key: str, model: str, timeout: int, base_url: str | None = None) -> None:
        from openai import AsyncOpenAI  # imported lazily: mock mode has no hard dependency on it

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self._model = model

    async def complete_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM call failed: {type(exc).__name__}") from exc

        raw = resp.choices[0].message.content or "{}"
        try:
            return json.loads(_strip_code_fences(raw))
        except json.JSONDecodeError as exc:
            raise LLMError(f"Model returned invalid JSON: {exc}") from exc

    async def complete_text(self, system_prompt: str, user_prompt: str) -> str:
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"LLM call failed: {type(exc).__name__}") from exc
        return resp.choices[0].message.content or ""


class DeepSeekAdapter(OpenAIAdapter):
    """DeepSeek exposes an OpenAI-compatible Chat Completions API."""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: int) -> None:
        super().__init__(api_key=api_key, model=model, timeout=timeout, base_url=base_url)


def get_llm_adapter(settings: Settings) -> LLMAdapter:
    if settings.llm_provider == "mock":
        return MockLLMAdapter()
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set but LLM_PROVIDER=openai")
        return OpenAIAdapter(settings.openai_api_key, settings.openai_model, settings.llm_timeout_seconds)
    if settings.llm_provider == "deepseek":
        if not settings.deepseek_api_key:
            raise LLMError("DEEPSEEK_API_KEY is not set but LLM_PROVIDER=deepseek")
        return DeepSeekAdapter(
            settings.deepseek_api_key,
            settings.deepseek_model,
            settings.deepseek_base_url,
            settings.llm_timeout_seconds,
        )
    raise LLMError(f"Unknown LLM provider: {settings.llm_provider}")
