"""
Graph persistence and query helpers.

Single JSON file storage with atomic writes, plus fuzzy
search and BFS traversal utilities.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from graph.models import Entity, KnowledgeGraph
    from output.schema import BusinessRule

_DEFAULT_PATH = "graph/knowledge_graph.json"


def load_graph(path: str = _DEFAULT_PATH) -> "KnowledgeGraph":
    """Load a KnowledgeGraph from a JSON file.

    Returns a fresh graph if the file does not exist or is corrupt.
    """
    from graph.models import KnowledgeGraph

    file_path = Path(path)
    if not file_path.exists():
        return KnowledgeGraph()

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return KnowledgeGraph.model_validate(data)
    except Exception:
        return KnowledgeGraph()


def save_graph(graph: "KnowledgeGraph", path: str = _DEFAULT_PATH) -> None:
    """Persist a KnowledgeGraph to a JSON file atomically."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(".tmp")
    tmp_path.write_text(graph.to_json(), encoding="utf-8")
    os.replace(str(tmp_path), str(file_path))


def _fuzzy_name_match(a: str, b: str) -> float:
    """Return similarity ratio between two strings (case-insensitive)."""
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def get_entity_by_name(name: str, graph: "KnowledgeGraph") -> "Entity | None":
    """Find an entity by exact or fuzzy name match (> 85% threshold).

    Case-insensitive. Prefers exact matches, then falls back to fuzzy.
    """
    clean_name = name.lower().strip()

    # Exact match first
    for entity in graph.entities.values():
        if entity.name.lower().strip() == clean_name:
            return entity

    # Fuzzy fallback
    best_match: "Entity | None" = None
    best_score = 0.0
    for entity in graph.entities.values():
        score = _fuzzy_name_match(name, entity.name)
        if score > best_score:
            best_score = score
            best_match = entity

    if best_match and best_score >= 0.85:
        return best_match
    return None


def get_related_entities(
    entity_id: str,
    graph: "KnowledgeGraph",
    relationship_types: list[str] | None = None,
    max_hops: int = 2,
) -> list["Entity"]:
    """BFS traversal from entity_id up to max_hops.

    Parameters
    ----------
    entity_id : str
        Starting entity.
    graph : KnowledgeGraph
    relationship_types : list[str] | None
        If provided, only follow relationships of these types.
    max_hops : int
        Maximum depth to traverse (default 2).

    Returns
    -------
    list[Entity]
        All reachable entities, excluding the start entity itself.
    """
    if max_hops < 1:
        return []

    allowed_types = set(relationship_types or [])
    visited: set[str] = {entity_id}
    queue: deque[tuple[str, int]] = deque([(entity_id, 0)])
    results: list["Entity"] = []

    while queue:
        current_id, depth = queue.popleft()
        if depth >= max_hops:
            continue

        for rel in graph.relationships:
            # Determine if this relationship involves the current entity
            other_id: str | None = None
            if rel.from_entity_id == current_id:
                other_id = rel.to_entity_id
            elif rel.to_entity_id == current_id:
                other_id = rel.from_entity_id

            if other_id is None or other_id in visited:
                continue

            if allowed_types and rel.relationship_type not in allowed_types:
                continue

            visited.add(other_id)
            other = graph.entities.get(other_id)
            if other is not None:
                results.append(other)
                queue.append((other_id, depth + 1))

    return results


def get_rules_for_entity(
    entity_id: str,
    graph: "KnowledgeGraph",
    rules_dir: str = "rules/extracted/",
) -> list["BusinessRule"]:
    """Load BusinessRule objects that are linked to the given entity.

    Links are determined by matching entity.source_rule_ids against
    rule.rule_id values loaded from the rules directory.
    """
    from output.writer import load_rules

    entity = graph.entities.get(entity_id)
    if entity is None:
        return []

    source_rule_ids = set(entity.source_rule_ids)
    if not source_rule_ids:
        return []

    all_rules = load_rules(directory=rules_dir)
    return [r for r in all_rules if r.rule_id in source_rule_ids]


def search_graph(
    query: str,
    graph: "KnowledgeGraph",
    top_k: int = 10,
) -> list["Entity"]:
    """Keyword + fuzzy search across entity names and descriptions.

    Ranks by: exact name match > name contains query > description
    contains query > fuzzy name match. Returns up to top_k results.
    """
    clean_query = query.lower().strip()
    if not clean_query:
        return []

    scored: list[tuple[float, "Entity"]] = []

    for entity in graph.entities.values():
        score = 0.0
        name_lower = entity.name.lower().strip()
        desc_lower = (entity.description or "").lower().strip()

        # Exact name match
        if name_lower == clean_query:
            score = 100.0
        # Name contains query
        elif clean_query in name_lower:
            score = 50.0 + len(clean_query) / len(name_lower) * 10
        # Description contains query
        elif clean_query in desc_lower:
            score = 20.0
        else:
            # Fuzzy name match
            fuzzy = _fuzzy_name_match(query, entity.name)
            if fuzzy >= 0.70:
                score = fuzzy * 30.0

        if score > 0:
            # Boost by mention count (popularity signal)
            score += min(entity.mention_count * 0.5, 5.0)
            scored.append((score, entity))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [e for _, e in scored[:top_k]]


def get_most_connected_entities(
    graph: "KnowledgeGraph",
    top_n: int = 5,
) -> list[tuple["Entity", int]]:
    """Return the top-N entities with the most relationships."""
    degree: dict[str, int] = {}
    for rel in graph.relationships:
        degree[rel.from_entity_id] = degree.get(rel.from_entity_id, 0) + 1
        degree[rel.to_entity_id] = degree.get(rel.to_entity_id, 0) + 1

    ranked = sorted(
        ((eid, cnt) for eid, cnt in degree.items() if eid in graph.entities),
        key=lambda x: x[1],
        reverse=True,
    )[:top_n]

    return [(graph.entities[eid], cnt) for eid, cnt in ranked]
