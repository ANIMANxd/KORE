"""
LangGraph extractor node.

Calls the LLM to extract a business rule from retrieved context.
All LLM calls go through providers/llm.py — never litellm directly.
"""

from engine.state import ExtractionState
from providers.config import get_config
from providers.llm import complete_json, JSONRepairFailed

_SYSTEM_PROMPT = (
    "You are a business rule extractor for an enterprise AI system.\n"
    "Identify explicit or implicit business rules from company communications.\n"
    "A business rule is a specific, actionable policy governing how the company operates.\n"
    "Only extract rules clearly supported by the provided context.\n"
    "Do not infer — only extract what is explicitly stated or strongly implied.\n"
    "If no clear rule exists, return confidence 0.0.\n"
    "Always cite the exact source chunk IDs supporting the rule.\n"
    "Respond in valid JSON only. No preamble. No explanation. No markdown fences. No trailing commas."
)

_EXPECTED_SCHEMA = {
    "rule_text": "string",
    "confidence": "float 0.0-1.0",
    "rule_category": "pricing|operations|escalation|compliance|customer_success|engineering|hr|other",
    "source_chunk_ids": "list of strings",
    "ambiguity_notes": "string or null",
}


def extract_rule(state: ExtractionState) -> ExtractionState:
    """Extract a candidate business rule from retrieved chunks via LLM.

    Updates state with the extracted rule, confidence, category, and status.
    """
    chunks = state.get("retrieved_chunks", [])
    if not chunks:
        state["error"] = "extractor: no chunks available"
        state["verification_status"] = "rejected"
        state["rejection_reason"] = "no_context"
        return state

    # Build context block
    context_lines: list[str] = []
    for c in chunks:
        chunk_id = c.get("chunk_id", "unknown")
        content = c.get("content", "").strip()
        if content:
            context_lines.append(f"[chunk_id: {chunk_id}]\n{content}")

    context_block = "\n\n---\n\n".join(context_lines)

    user_message = (
        f"Query: {state['query']}\n\n"
        f"Context chunks:\n\n{context_block}\n\n"
        "Extract a business rule from the above context. "
        "Return valid JSON with the required fields."
    )

    try:
        parsed, repair_attempts = complete_json(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=_SYSTEM_PROMPT,
            expected_schema=_EXPECTED_SCHEMA,
        )
    except JSONRepairFailed as exc:
        state["verification_status"] = "parse_failed"
        state["json_repair_attempts"] = exc.attempts
        state["error"] = f"extractor: JSON repair failed after {exc.attempts} attempts"
        state["candidate_rule"] = exc.raw_output[:500]
        return state

    # Update repair counter
    state["json_repair_attempts"] = repair_attempts

    # Populate extraction fields
    state["candidate_rule"] = parsed.get("rule_text", "")
    state["rule_confidence"] = float(parsed.get("confidence", 0.0))
    state["rule_category"] = parsed.get("rule_category", "other")
    state["ambiguity_notes"] = parsed.get("ambiguity_notes")

    # Build source_refs from cited chunk IDs
    cited_ids = parsed.get("source_chunk_ids", [])
    source_refs = []
    id_to_chunk = {c.get("chunk_id", ""): c for c in chunks}
    for cid in cited_ids:
        chunk = id_to_chunk.get(cid)
        if chunk:
            meta = chunk.get("metadata", {})
            source_refs.append({
                "chunk_id": cid,
                "source_type": meta.get("source_type", "unknown"),
                "channel": meta.get("channel", ""),
                "timestamp": meta.get("timestamp", ""),
                "excerpt": chunk.get("content", "")[:200],
            })
    state["source_refs"] = source_refs

    # Set verification status based on confidence
    confidence = state["rule_confidence"]
    if confidence < 0.3:
        state["verification_status"] = "rejected"
        state["rejection_reason"] = f"confidence_too_low ({confidence:.2f})"
    elif confidence < 0.6:
        state["verification_status"] = "needs_review"
    else:
        state["verification_status"] = "pending"  # verifier will decide

    # Populate extraction_meta from runtime config
    config = get_config()
    state["extraction_meta"] = {
        "provider": config.llm_provider,
        "model": config.llm_model,
        "deployment_mode": config.deployment_mode,
        "json_repair_attempts": repair_attempts,
    }

    return state
