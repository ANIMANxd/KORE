"""
Re-extraction engine.

Triggered when new data is ingested or a rule is explicitly flagged for
re-extraction. Finds rules affected by new chunks and re-runs the full
LangGraph extraction pipeline.
"""

from output.schema import BusinessRule
from engine.graph import run_extraction
from engine.state import ExtractionState


def find_affected_rules(
    new_chunk_ids: list[str],
    existing_rules: list[BusinessRule],
    similarity_threshold: float = 0.85,
) -> list[str]:
    """Find rule IDs whose source chunks overlap with newly ingested chunks.

    For each existing rule, we compute a simple overlap ratio between the
    rule's source chunk IDs and the new chunk IDs. If the overlap is above
    ``similarity_threshold`` (default 0.85), the rule is flagged as affected.

    Parameters
    ----------
    new_chunk_ids : list[str]
        Chunk IDs from the most recent ingestion batch.
    existing_rules : list[BusinessRule]
        All previously extracted rules.
    similarity_threshold : float
        Minimum fraction of a rule's source chunks that must overlap with
        ``new_chunk_ids`` for the rule to be considered affected.

    Returns
    -------
    list[str]
        ``rule_id`` values of affected rules.
    """
    if not new_chunk_ids:
        return []

    new_set = set(new_chunk_ids)
    affected: list[str] = []

    for rule in existing_rules:
        if not rule.source_refs:
            continue

        rule_chunk_ids = {ref.chunk_id for ref in rule.source_refs}
        if not rule_chunk_ids:
            continue

        overlap = rule_chunk_ids & new_set
        similarity = len(overlap) / len(rule_chunk_ids)

        if similarity >= similarity_threshold:
            affected.append(rule.rule_id)

    return affected


def reextract_rule(
    rule: BusinessRule,
    query: str | None = None,
) -> ExtractionState:
    """Re-run the full extraction pipeline for an existing rule.

    Parameters
    ----------
    rule : BusinessRule
        The existing rule to re-extract.
    query : str | None
        Optional custom query. Defaults to the rule's existing text.

    Returns
    -------
    ExtractionState
        The final state from the new extraction run.
    """
    extraction_query = query if query else rule.rule_text
    return run_extraction(extraction_query)
