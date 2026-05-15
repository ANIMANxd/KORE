"""
Pydantic v2 output schema for extracted business rules.

Every rule passes schema validation before writing to YAML.
"""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class SourceRef(BaseModel):
    chunk_id: str
    source_type: str
    channel: str = ""
    timestamp: str = ""
    excerpt: str = ""

    @field_validator("excerpt", mode="before")
    @classmethod
    def _truncate_excerpt(cls, v: str) -> str:
        if isinstance(v, str) and len(v) > 100:
            return v[:100]
        return v or ""


class ExtractionMeta(BaseModel):
    provider: str
    model: str
    deployment_mode: str
    json_repair_attempts: int = 0


class BusinessRule(BaseModel):
    rule_id: str = Field(default_factory=lambda: str(uuid4()))
    rule_text: str = Field(min_length=10)
    rule_category: Literal[
        "pricing",
        "operations",
        "escalation",
        "compliance",
        "customer_success",
        "engineering",
        "hr",
        "other",
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    verification_status: Literal[
        "verified", "needs_review", "rejected", "parse_failed"
    ]
    source_refs: list[SourceRef]
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    approved_by: str | None = None
    version: int = 1
    ambiguity_notes: str | None = None
    extraction_meta: ExtractionMeta

    @model_validator(mode="after")
    def _check_verified_confidence(self) -> "BusinessRule":
        if self.verification_status == "verified" and self.confidence < 0.5:
            raise ValueError(
                f"verified rule must have confidence >= 0.5, got {self.confidence}"
            )
        return self

    @model_validator(mode="after")
    def _check_verified_sources(self) -> "BusinessRule":
        if self.verification_status == "verified" and len(self.source_refs) == 0:
            raise ValueError(
                "verified rule must have at least one source reference"
            )
        return self


class RulesFile(BaseModel):
    company_id: str
    extraction_date: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    rules: list[BusinessRule]

    @property
    def total_rules(self) -> int:
        return len(self.rules)

    def to_yaml(self) -> str:
        """Serialize the rules file to a YAML string."""
        data = self.model_dump(mode="json")
        data["total_rules"] = self.total_rules
        return yaml.safe_dump(
            data,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )
