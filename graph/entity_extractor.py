"""
LLM-based entity extraction from business rules.

Given a verified BusinessRule, calls the LLM to extract named entities
and relationships, then merges them into an existing KnowledgeGraph.
"""

from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

from providers.llm import complete_json

if TYPE_CHECKING:
    from graph.models import Entity, KnowledgeGraph, Relationship
    from output.schema import BusinessRule


_SYSTEM_PROMPT = (
    "You are a knowledge graph builder. Given a business rule, extract "
    "named entities and their relationships. Be conservative — only extract "
    "what is clearly present. Respond in JSON only."
)

_EXPECTED_SCHEMA = {
    "entities": "list of entities with name, entity_type, description",
    "relationships": "list of relationships with from_name, to_name, relationship_type, confidence",
}

_NAME_MATCH_THRESHOLD = 0.85


def _fuzzy_name_match(name: str, existing_name: str) -> float:
    """Return similarity ratio between two entity names (case-insensitive)."""
    return difflib.SequenceMatcher(
        None, name.lower().strip(), existing_name.lower().strip()
    ).ratio()


def _find_existing_entity(
    entity_type: str,
    name: str,
    graph: "KnowledgeGraph",
) -> "Entity | None":
    """Find an existing entity by fuzzy-matching name and exact type."""
    for entity in graph.entities.values():
        if entity.entity_type != entity_type:
            continue
        if _fuzzy_name_match(name, entity.name) >= _NAME_MATCH_THRESHOLD:
            return entity
    return None


def extract_entities(
    rule: BusinessRule,
    existing_graph: KnowledgeGraph,
) -> tuple[list[Entity], list[Relationship]]:
    """Extract entities and relationships from a business rule.

    Parameters
    ----------
    rule : BusinessRule
        The verified rule to analyse.
    existing_graph : KnowledgeGraph
        The current graph to merge against.

    Returns
    -------
    tuple[list[Entity], list[Relationship]]
        New entities and relationships that were added (or updated in-place).
    """
    prompt = (
        f"Business rule:\n{rule.rule_text}\n\n"
        f"Category: {rule.rule_category}\n"
        f"Confidence: {rule.confidence:.2f}\n"
        "Extract named entities and relationships."
    )

    raw_result, _ = complete_json(
        messages=[{"role": "user", "content": prompt}],
        system_prompt=_SYSTEM_PROMPT,
        expected_schema=_EXPECTED_SCHEMA,
    )

    raw_entities = raw_result.get("entities", [])
    raw_relationships = raw_result.get("relationships", [])

    new_entities: list[Entity] = []
    entity_name_map: dict[str, Entity] = {}  # lower_name -> Entity

    for raw_ent in raw_entities:
        name = str(raw_ent.get("name", "")).strip()
        ent_type = str(raw_ent.get("entity_type", "")).strip()
        description = raw_ent.get("description")
        if not name or not ent_type:
            continue

        existing = _find_existing_entity(ent_type, name, existing_graph)
        if existing is not None:
            existing.bump_mention()
            if rule.rule_id not in existing.source_rule_ids:
                existing.source_rule_ids.append(rule.rule_id)
            if description and not existing.description:
                existing.description = description
            new_entities.append(existing)
            entity_name_map[name.lower()] = existing
        else:
            from graph.models import Entity

            entity = Entity(
                entity_type=ent_type,  # type: ignore[arg-type]
                name=name,
                description=description,
                source_rule_ids=[rule.rule_id],
            )
            existing_graph.add_entity(entity)
            new_entities.append(entity)
            entity_name_map[name.lower()] = entity

    new_relationships: list[Relationship] = []

    for raw_rel in raw_relationships:
        from_name = str(raw_rel.get("from_name", "")).strip()
        to_name = str(raw_rel.get("to_name", "")).strip()
        rel_type = str(raw_rel.get("relationship_type", "")).strip()
        confidence = float(raw_rel.get("confidence", 0.5))
        if not from_name or not to_name or not rel_type:
            continue

        from_ent = entity_name_map.get(from_name.lower())
        to_ent = entity_name_map.get(to_name.lower())
        if from_ent is None or to_ent is None:
            continue

        from graph.models import Relationship

        relationship = Relationship(
            from_entity_id=from_ent.entity_id,
            to_entity_id=to_ent.entity_id,
            relationship_type=rel_type,  # type: ignore[arg-type]
            confidence=confidence,
            source_rule_ids=[rule.rule_id],
        )
        existing_graph.add_relationship(relationship)
        new_relationships.append(relationship)

    return new_entities, new_relationships
