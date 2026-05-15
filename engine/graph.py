"""
LangGraph pipeline wiring.

Full extraction pipeline: retrieve → extract → verify → format.
Conditional edges route based on state.
Loop guard prevents infinite iteration.
"""

from langgraph.graph import StateGraph, END

from engine.state import ExtractionState, create_initial_state
from engine.nodes.retriever import retrieve_context
from engine.nodes.extractor import extract_rule
from engine.nodes.verifier import verify_rule
from engine.nodes.formatter import format_output

_MAX_ITERATIONS = 5


def _loop_guard_node(state: ExtractionState) -> ExtractionState:
    """Check iteration count and set error if exceeded."""
    if state["iteration_count"] > _MAX_ITERATIONS:
        state["error"] = f"loop_guard: exceeded max iterations ({_MAX_ITERATIONS})"
    return state


def _loop_guard_router(state: ExtractionState) -> str:
    """Route based on loop guard check."""
    if state.get("error") and state["error"].startswith("loop_guard:"):
        return "end_error"
    return "continue"


def after_retrieval(state: ExtractionState) -> str:
    if state.get("error"):
        return "end_error"
    return "extract_rule"


def after_extraction(state: ExtractionState) -> str:
    status = state.get("verification_status", "")
    confidence = state.get("rule_confidence", 0.0)

    if status == "parse_failed":
        return "end_rejected"
    if confidence < 0.3:
        state["rejection_reason"] = state.get("rejection_reason") or "confidence_below_threshold"
        return "end_rejected"
    return "verify_rule"


def after_verification(state: ExtractionState) -> str:
    status = state.get("verification_status", "")
    if status == "verified":
        return "format_output"
    return "end_rejected"


def _build_graph() -> StateGraph:
    builder = StateGraph(ExtractionState)

    # Nodes
    builder.add_node("loop_guard", _loop_guard_node)
    builder.add_node("retrieve_context", retrieve_context)
    builder.add_node("extract_rule", extract_rule)
    builder.add_node("verify_rule", verify_rule)
    builder.add_node("format_output", format_output)

    # Entry point
    builder.set_entry_point("loop_guard")

    # Loop guard → retrieve or END
    builder.add_conditional_edges(
        "loop_guard",
        _loop_guard_router,
        {
            "continue": "retrieve_context",
            "end_error": END,
        },
    )

    # After retrieval
    builder.add_conditional_edges(
        "retrieve_context",
        after_retrieval,
        {
            "extract_rule": "extract_rule",
            "end_error": END,
        },
    )

    # After extraction
    builder.add_conditional_edges(
        "extract_rule",
        after_extraction,
        {
            "verify_rule": "verify_rule",
            "end_rejected": END,
        },
    )

    # After verification
    builder.add_conditional_edges(
        "verify_rule",
        after_verification,
        {
            "format_output": "format_output",
            "end_rejected": END,
        },
    )

    # Format always ends
    builder.add_edge("format_output", END)

    return builder


# Compile once and reuse
_graph = _build_graph().compile()


def run_extraction(query: str) -> ExtractionState:
    """Run the full extraction pipeline for a given query.

    Parameters
    ----------
    query : str
        Natural language query describing the rule to extract.

    Returns
    -------
    ExtractionState
        Final state containing the extracted rule or error/rejection info.
    """
    initial_state = create_initial_state(query)
    final_state = _graph.invoke(initial_state)
    return final_state
