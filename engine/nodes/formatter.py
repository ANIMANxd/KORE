"""
LangGraph formatter node.

Pure formatting — no LLM calls.
Converts the extraction state into a validated Pydantic BusinessRule.
"""

from engine.state import ExtractionState
from output.schema import BusinessRule, SourceRef, ExtractionMeta
from storage.vector_store import get_chunks_by_ids


def format_output(state: ExtractionState) -> ExtractionState:
    """Format the extraction state into a validated BusinessRule.

    Fetches full chunk metadata from the vector store to build complete
    SourceRef objects. Sets state.final_rule to the validated dict.
    """
    # Collect chunk IDs from source_refs
    chunk_ids = [ref.get("chunk_id", "") for ref in state.get("source_refs", [])]

    # Fetch full metadata from vector store
    stored_chunks = get_chunks_by_ids(chunk_ids) if chunk_ids else []
    id_to_chunk = {c.get("chunk_id", ""): c for c in stored_chunks}

    # Build SourceRef list with enriched metadata
    source_refs: list[SourceRef] = []
    for ref in state.get("source_refs", []):
        cid = ref.get("chunk_id", "")
        chunk = id_to_chunk.get(cid, {})
        meta = chunk.get("metadata", {}) if isinstance(chunk, dict) else {}

        source_refs.append(
            SourceRef(
                chunk_id=cid,
                source_type=meta.get("source_type", ref.get("source_type", "unknown")),
                channel=meta.get("channel", ref.get("channel", "")),
                timestamp=meta.get("timestamp", ref.get("timestamp", "")),
                excerpt=ref.get("excerpt", chunk.get("content", "")[:200]),
            )
        )

    # Build ExtractionMeta from state
    meta_dict = state.get("extraction_meta", {})
    extraction_meta = ExtractionMeta(
        provider=meta_dict.get("provider", ""),
        model=meta_dict.get("model", ""),
        deployment_mode=meta_dict.get("deployment_mode", ""),
        json_repair_attempts=state.get("json_repair_attempts", meta_dict.get("json_repair_attempts", 0)),
    )

    # Normalize rule_category to valid literal
    _valid_categories = {
        "pricing", "operations", "escalation", "compliance",
        "customer_success", "engineering", "hr", "other",
    }
    raw_category = state.get("rule_category", "other")
    normalized_category = raw_category if raw_category in _valid_categories else "other"

    # Build BusinessRule
    rule = BusinessRule(
        rule_text=state.get("candidate_rule", ""),
        rule_category=normalized_category,
        confidence=state.get("rule_confidence", 0.0),
        verification_status=state.get("verification_status", "rejected"),
        source_refs=source_refs,
        approved_by=None,
        ambiguity_notes=state.get("ambiguity_notes"),
        extraction_meta=extraction_meta,
    )

    state["final_rule"] = rule.model_dump(mode="json")
    return state
