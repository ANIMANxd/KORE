"""Unit tests for graph/store.py"""

import json
from pathlib import Path

import pytest

from graph.models import Entity, KnowledgeGraph, Relationship
from graph.store import (
    load_graph,
    save_graph,
    get_entity_by_name,
    get_related_entities,
    get_rules_for_entity,
    search_graph,
    get_most_connected_entities,
)
from output.schema import BusinessRule, ExtractionMeta, SourceRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_graph() -> KnowledgeGraph:
    return KnowledgeGraph()


def _make_rule(rule_id: str = "aaaaaaaa-1111-2222-3333-444444444444") -> BusinessRule:
    return BusinessRule(
        rule_id=rule_id,
        rule_text="Deploys to production must be approved by the Engineering manager.",
        rule_category="engineering",
        confidence=0.92,
        verification_status="verified",
        source_refs=[
            SourceRef(
                chunk_id="slack_eng_123",
                source_type="slack",
                channel="#engineering",
                timestamp="2024-01-01T10:00:00Z",
                excerpt="All production deploys need manager approval.",
            )
        ],
        extraction_meta=ExtractionMeta(
            provider="lmstudio",
            model="test-model",
            deployment_mode="local",
            json_repair_attempts=0,
        ),
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_load_nonexistent_returns_fresh_graph(self):
        graph = load_graph("graph/nonexistent_xyz.json")
        assert isinstance(graph, KnowledgeGraph)
        assert len(graph.entities) == 0

    def test_save_and_load_roundtrip(self, tmp_path):
        graph = _make_graph()
        ent = Entity(entity_type="team", name="Engineering")
        graph.add_entity(ent)

        path = str(tmp_path / "kg.json")
        save_graph(graph, path)
        loaded = load_graph(path)

        assert len(loaded.entities) == 1
        assert loaded.entities[ent.entity_id].name == "Engineering"

    def test_load_corrupt_returns_fresh_graph(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json", encoding="utf-8")
        graph = load_graph(str(path))
        assert len(graph.entities) == 0

    def test_save_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "kg.json")
        graph = _make_graph()
        save_graph(graph, path)
        assert Path(path).exists()


# ---------------------------------------------------------------------------
# get_entity_by_name
# ---------------------------------------------------------------------------


class TestGetEntityByName:
    def test_exact_match(self):
        graph = _make_graph()
        ent = Entity(entity_type="team", name="Engineering")
        graph.add_entity(ent)
        found = get_entity_by_name("Engineering", graph)
        assert found is not None
        assert found.entity_id == ent.entity_id

    def test_case_insensitive(self):
        graph = _make_graph()
        ent = Entity(entity_type="team", name="Engineering")
        graph.add_entity(ent)
        found = get_entity_by_name("engineering", graph)
        assert found is not None
        assert found.entity_id == ent.entity_id

    def test_fuzzy_match_above_threshold(self):
        graph = _make_graph()
        ent = Entity(entity_type="team", name="Engineering")
        graph.add_entity(ent)
        found = get_entity_by_name("enginering", graph)  # typo
        # "enginering" vs "Engineering" is ~0.95
        assert found is not None
        assert found.entity_id == ent.entity_id

    def test_no_match_returns_none(self):
        graph = _make_graph()
        graph.add_entity(Entity(entity_type="team", name="Engineering"))
        found = get_entity_by_name("Sales", graph)
        assert found is None


# ---------------------------------------------------------------------------
# get_related_entities
# ---------------------------------------------------------------------------


class TestGetRelatedEntities:
    def test_direct_neighbor(self):
        graph = _make_graph()
        e1 = Entity(entity_type="team", name="Engineering")
        e2 = Entity(entity_type="person_role", name="Manager")
        graph.add_entity(e1)
        graph.add_entity(e2)
        graph.add_relationship(
            Relationship(
                from_entity_id=e1.entity_id,
                to_entity_id=e2.entity_id,
                relationship_type="owned_by",
                confidence=0.9,
            )
        )
        related = get_related_entities(e1.entity_id, graph, max_hops=1)
        assert len(related) == 1
        assert related[0].entity_id == e2.entity_id

    def test_two_hops(self):
        graph = _make_graph()
        e1 = Entity(entity_type="team", name="A")
        e2 = Entity(entity_type="team", name="B")
        e3 = Entity(entity_type="team", name="C")
        for e in (e1, e2, e3):
            graph.add_entity(e)
        graph.add_relationship(
            Relationship(from_entity_id=e1.entity_id, to_entity_id=e2.entity_id, relationship_type="involves", confidence=0.9)
        )
        graph.add_relationship(
            Relationship(from_entity_id=e2.entity_id, to_entity_id=e3.entity_id, relationship_type="involves", confidence=0.9)
        )

        related = get_related_entities(e1.entity_id, graph, max_hops=2)
        ids = {r.entity_id for r in related}
        assert e2.entity_id in ids
        assert e3.entity_id in ids

    def test_max_hops_zero(self):
        graph = _make_graph()
        e1 = Entity(entity_type="team", name="A")
        e2 = Entity(entity_type="team", name="B")
        graph.add_entity(e1)
        graph.add_entity(e2)
        graph.add_relationship(
            Relationship(from_entity_id=e1.entity_id, to_entity_id=e2.entity_id, relationship_type="involves", confidence=0.9)
        )
        assert get_related_entities(e1.entity_id, graph, max_hops=0) == []

    def test_filter_by_relationship_type(self):
        graph = _make_graph()
        e1 = Entity(entity_type="team", name="A")
        e2 = Entity(entity_type="team", name="B")
        e3 = Entity(entity_type="team", name="C")
        for e in (e1, e2, e3):
            graph.add_entity(e)
        graph.add_relationship(
            Relationship(from_entity_id=e1.entity_id, to_entity_id=e2.entity_id, relationship_type="governs", confidence=0.9)
        )
        graph.add_relationship(
            Relationship(from_entity_id=e1.entity_id, to_entity_id=e3.entity_id, relationship_type="involves", confidence=0.9)
        )

        related = get_related_entities(e1.entity_id, graph, relationship_types=["governs"], max_hops=1)
        assert len(related) == 1
        assert related[0].entity_id == e2.entity_id

    def test_excludes_start_entity(self):
        graph = _make_graph()
        e1 = Entity(entity_type="team", name="A")
        graph.add_entity(e1)
        graph.add_relationship(
            Relationship(from_entity_id=e1.entity_id, to_entity_id=e1.entity_id, relationship_type="involves", confidence=0.9)
        )
        related = get_related_entities(e1.entity_id, graph, max_hops=1)
        assert e1.entity_id not in {r.entity_id for r in related}


# ---------------------------------------------------------------------------
# get_rules_for_entity
# ---------------------------------------------------------------------------


class TestGetRulesForEntity:
    def test_returns_linked_rules(self, tmp_path):
        from output.writer import save_rule

        rules_dir = str(tmp_path / "rules")
        rule = _make_rule()
        save_rule(rule, rules_dir)

        graph = _make_graph()
        ent = Entity(entity_type="team", name="Engineering", source_rule_ids=[rule.rule_id])
        graph.add_entity(ent)

        found = get_rules_for_entity(ent.entity_id, graph, rules_dir=rules_dir)
        assert len(found) == 1
        assert found[0].rule_id == rule.rule_id

    def test_no_source_rule_ids_returns_empty(self):
        graph = _make_graph()
        ent = Entity(entity_type="team", name="Engineering")
        graph.add_entity(ent)
        assert get_rules_for_entity(ent.entity_id, graph) == []

    def test_missing_entity_returns_empty(self):
        graph = _make_graph()
        assert get_rules_for_entity("does-not-exist", graph) == []


# ---------------------------------------------------------------------------
# search_graph
# ---------------------------------------------------------------------------


class TestSearchGraph:
    def test_exact_name_match_first(self):
        graph = _make_graph()
        graph.add_entity(Entity(entity_type="team", name="Engineering"))
        results = search_graph("Engineering", graph)
        assert len(results) == 1
        assert results[0].name == "Engineering"

    def test_keyword_in_description(self):
        graph = _make_graph()
        graph.add_entity(Entity(entity_type="process", name="Deploy", description="Production deployment pipeline"))
        results = search_graph("pipeline", graph)
        assert len(results) == 1
        assert results[0].name == "Deploy"

    def test_fuzzy_match(self):
        graph = _make_graph()
        graph.add_entity(Entity(entity_type="team", name="Engineering"))
        results = search_graph("Enginering", graph)  # typo
        assert len(results) == 1

    def test_empty_query_returns_empty(self):
        graph = _make_graph()
        assert search_graph("", graph) == []

    def test_top_k_limits_results(self):
        graph = _make_graph()
        for i in range(20):
            graph.add_entity(Entity(entity_type="team", name=f"Team {i}"))
        results = search_graph("Team", graph, top_k=5)
        assert len(results) == 5


# ---------------------------------------------------------------------------
# get_most_connected_entities
# ---------------------------------------------------------------------------


class TestGetMostConnectedEntities:
    def test_ranks_by_degree(self):
        graph = _make_graph()
        e1 = Entity(entity_type="team", name="Hub")
        e2 = Entity(entity_type="team", name="Leaf1")
        e3 = Entity(entity_type="team", name="Leaf2")
        for e in (e1, e2, e3):
            graph.add_entity(e)
        graph.add_relationship(Relationship(from_entity_id=e1.entity_id, to_entity_id=e2.entity_id, relationship_type="involves", confidence=0.9))
        graph.add_relationship(Relationship(from_entity_id=e1.entity_id, to_entity_id=e3.entity_id, relationship_type="involves", confidence=0.9))

        ranked = get_most_connected_entities(graph, top_n=2)
        assert len(ranked) == 2
        assert ranked[0][0].name == "Hub"
        assert ranked[0][1] == 2

    def test_empty_graph(self):
        graph = _make_graph()
        assert get_most_connected_entities(graph) == []
