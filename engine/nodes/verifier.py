"""
LangGraph verifier node.

Cross-checks an extracted business rule against its source chunks.
All LLM calls go through providers/llm.py — never litellm directly.
"""

from engine.state import ExtractionState
from providers.llm import complete_json, JSONRepairFailed

_SYSTEM_PROMPT = (
    "You are a verification agent for an enterprise compliance system.\n"
    "You will receive a proposed business rule and the source chunks it was extracted from.\n"
    "Verify the rule is accurately and fairly represented.\n"
    "Check: does the rule match the source, is it too broad or narrow, are there contradictions.\n"
    "Respond in valid JSON only. No preamble. No explanation. No markdown fences. No trailing commas."
)

_EXPECTED_SCHEMA = {
    "verified": "boolean",
    "adjusted_confidence": "float",
    "adjusted_rule_text": "string or null",
    "rejection_reason": "string or null",
    "has_contradictions": "boolean",
    "contradiction_notes": "string or null",
}


def verify_rule(state: ExtractionState) -> ExtractionState:
    """Verify an extracted business rule against its source context.

    Updates verification_status based on LLM verification output.
    """
    chunks = state.get("retrieved_chunks", [])
    candidate = state.get("candidate_rule", "")
    source_refs = state.get("source_refs", [])

    if not candidate or not chunks:
        state["verification_status"] = "rejected"
        state["rejection_reason"] = "verifier: missing candidate rule or context"
        return state

    # Build context block
    context_lines: list[str] = []
    for c in chunks:
        chunk_id = c.get("chunk_id", "unknown")
        content = c.get("content", "").strip()
        if content:
            context_lines.append(f"[chunk_id: {chunk_id}]\n{content}")
    context_block = "\n\n---\n\n".join(context_lines)

    # Build source reference summary
    ref_lines: list[str] = []
    for ref in source_refs:
        ref_lines.append(
            f"  - {ref.get('chunk_id', 'unknown')} "
            f"({ref.get('source_type', 'unknown')}, {ref.get('channel', '')})"
        )
    refs_block = "\n".join(ref_lines) if ref_lines else "(none)"

    user_message = (
        f"Proposed rule:\n{state['candidate_rule']}\n\n"
        f"Confidence: {state['rule_confidence']}\n"
        f"Category: {state['rule_category']}\n\n"
        f"Source references:\n{refs_block}\n\n"
        f"Full context chunks:\n\n{context_block}\n\n"
        "Verify this rule. Return valid JSON with the required fields."
    )

    try:
        parsed, repair_attempts = complete_json(
            messages=[{"role": "user", "content": user_message}],
            system_prompt=_SYSTEM_PROMPT,
            expected_schema=_EXPECTED_SCHEMA,
        )
    except JSONRepairFailed as exc:
        state["verification_status"] = "parse_failed"
        state["json_repair_attempts"] = state.get("json_repair_attempts", 0) + exc.attempts
        state["error"] = f"verifier: JSON repair failed after {exc.attempts} attempts"
        return state

    # Accumulate repair attempts from verifier
    state["json_repair_attempts"] = state.get("json_repair_attempts", 0) + repair_attempts

    # Apply adjusted rule text if provided
    adjusted_text = parsed.get("adjusted_rule_text")
    if adjusted_text is not None and isinstance(adjusted_text, str) and adjusted_text.strip():
        state["candidate_rule"] = adjusted_text.strip()

    # Update confidence with adjusted value
    adjusted_confidence = float(parsed.get("adjusted_confidence", state["rule_confidence"]))
    state["rule_confidence"] = max(0.0, min(1.0, adjusted_confidence))

    # Determine verification status
    verified = bool(parsed.get("verified", False))
    has_contradictions = bool(parsed.get("has_contradictions", False))

    if not verified:
        state["verification_status"] = "rejected"
        state["rejection_reason"] = parsed.get("rejection_reason") or "verifier_rejected"
    elif has_contradictions:
        state["verification_status"] = "needs_review"
        state["ambiguity_notes"] = parsed.get("contradiction_notes")
    else:
        state["verification_status"] = "verified"
        state["rejection_reason"] = None

    return state
