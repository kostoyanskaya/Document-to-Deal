from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.schemas import LeadCard


def test_lead_card_requires_task():
    with pytest.raises(ValidationError):
        LeadCard.model_validate({"company": "Acme"})


def test_lead_card_rejects_blank_task():
    with pytest.raises(ValidationError):
        LeadCard.model_validate({"task": "   "})


def test_lead_card_minimal_valid():
    card = LeadCard.model_validate({"task": "Разобрать заявку клиента"})
    assert card.task == "Разобрать заявку клиента"
    assert card.integrations == []
    assert card.risks == []
    assert card.contact is None


def test_lead_card_full_valid():
    payload = {
        "company": "Acme",
        "industry": "retail",
        "contact": {"name": "Ivan", "email": "ivan@example.com"},
        "task": "Build a pilot",
        "problem": "Manual processing",
        "expected_result": "Faster onboarding",
        "timeline": "4 weeks",
        "budget": "$10k",
        "integrations": ["CRM"],
        "risks": [],
        "missing_data": [],
    }
    card = LeadCard.model_validate(payload)
    assert card.contact.email == "ivan@example.com"
    assert card.integrations == ["CRM"]
