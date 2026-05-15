from typing import Literal, TypedDict


class SourceRef(TypedDict):
    chunk_id: str
    source_type: str
    channel: str
    timestamp: str
    excerpt: str


class ExtractionMeta(TypedDict):
    provider: str
    model: str
    deployment_mode: str
    json_repair_attempts: int


class ExtractionState(TypedDict):
    query: str
    retrieved_chunks: list[dict]
    candidate_rule: str
    rule_confidence: float
    rule_category: str
    source_refs: list[SourceRef]
    ambiguity_notes: str | None
    verification_status: Literal["pending", "verified", "rejected", "needs_review", "parse_failed"]
    rejection_reason: str | None
    final_rule: dict | None
    iteration_count: int
    error: str | None
    json_repair_attempts: int
    extraction_meta: ExtractionMeta


def create_initial_state(query: str) -> ExtractionState:
    return {
        "query": query,
        "retrieved_chunks": [],
        "candidate_rule": "",
        "rule_confidence": 0.0,
        "rule_category": "",
        "source_refs": [],
        "ambiguity_notes": None,
        "verification_status": "pending",
        "rejection_reason": None,
        "final_rule": None,
        "iteration_count": 0,
        "error": None,
        "json_repair_attempts": 0,
        "extraction_meta": {
            "provider": "",
            "model": "",
            "deployment_mode": "",
            "json_repair_attempts": 0,
        },
    }
