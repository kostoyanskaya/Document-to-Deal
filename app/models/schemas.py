from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class TaskStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    BLOCKED = "blocked"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    APPROVED = "approved"
    FAILED = "failed"


class SecurityVerdict(str, Enum):
    CLEAN = "clean"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"


class ContactInfo(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    position: Optional[str] = None


class LeadCard(BaseModel):
    company: Optional[str] = Field(
        None, description="Client company name"
    )
    industry: Optional[str] = Field(
        None, description="Client industry / domain"
    )
    contact: Optional[ContactInfo] = Field(
        None, description="Contact person, if mentioned"
    )
    task: str = Field(
        ..., description="What the client is asking to be done"
    )
    problem: Optional[str] = Field(
        None, description="Underlying business problem"
    )
    expected_result: Optional[str] = Field(
        None, description="Desired outcome / success criteria"
    )
    timeline: Optional[str] = Field(
        None, description="Deadlines or timeframes mentioned"
    )
    budget: Optional[str] = Field(
        None, description="Budget figure or signals of a budget"
    )
    integrations: list[str] = Field(
        default_factory=list, description="Systems/integrations required"
    )
    risks: list[str] = Field(
        default_factory=list,
        description="Risks, incl. suspicious content found in the doc",
    )
    missing_data: list[str] = Field(
        default_factory=list,
        description="Fields that could not be determined",
    )

    @field_validator("task")
    @classmethod
    def task_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("task must not be empty")
        return v.strip()


class PipelineResult(BaseModel):

    status: TaskStatus
    security_verdict: SecurityVerdict
    security_reasons: list[str] = Field(default_factory=list)
    lead_card: Optional[LeadCard] = None
    brief_markdown: Optional[str] = None
    proposal_markdown: Optional[str] = None
    error: Optional[str] = None


class TaskRecord(BaseModel):

    task_id: str
    status: TaskStatus
    filename: str
    created_at: str
    updated_at: str
    security_verdict: Optional[SecurityVerdict] = None
    security_reasons: list[str] = Field(default_factory=list)
    lead_card: Optional[LeadCard] = None
    brief_markdown: Optional[str] = None
    proposal_markdown: Optional[str] = None
    error: Optional[str] = None


class UploadResponse(BaseModel):
    task_id: str
    status: TaskStatus
    idempotent: bool = Field(
        description="True if an identical file (by SHA-256) "
        "was already submitted",
    )


class ApproveResponse(BaseModel):
    task_id: str
    status: TaskStatus
    message: str
