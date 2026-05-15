"""
LangGraph retriever node.

Pure retrieval — no LLM calls.
"""

from engine.state import ExtractionState
from storage.vector_store import similarity_search

_MIN_CONTEXT_CHUNKS = 3


def retrieve_context(state: ExtractionState) -> ExtractionState:
    """Retrieve relevant chunks from the vector store for the current query.

    Deduplicates by content, requires at least 3 chunks for sufficient context.
    """
    state["iteration_count"] = state["iteration_count"] + 1

    results = similarity_search(state["query"], top_k=15)

    # Deduplicate by content (simple exact string match)
    seen: set[str] = set()
    deduplicated: list[dict] = []
    for r in results:
        content = r.get("content", "").strip()
        if content and content not in seen:
            seen.add(content)
            deduplicated.append(r)

    if len(deduplicated) < _MIN_CONTEXT_CHUNKS:
        state["error"] = (
            f"insufficient_context: only {len(deduplicated)} unique chunks found "
            f"(minimum {_MIN_CONTEXT_CHUNKS} required)."
        )
        return state

    state["retrieved_chunks"] = deduplicated
    return state
