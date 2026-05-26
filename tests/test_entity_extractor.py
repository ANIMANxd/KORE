"""Unit tests for graph/entity_extractor.py"""

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from output.schema import BusinessRule, ExtractionMeta, SourceRef
from graph.models import Entity, KnowledgeGraph, Relationship
from graph.entity_extractor import extract_entities, _fuzzy_name_match, _find_existing_entity


def _make_rule(rule_text: str = "Deploys to production must be approved by the Engineering manager.") -> BusinessRule:
    return BusinessRule(
        rule_text=rule_text,
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


class TestFuzzyMatch:
    def test_exact_match(self):
        assert _fuzzy_name_match("Engineering", "Engineering") == 1.0

    def test_case_insensitive(self):
        assert _fuzzy_name_match("engineering", "Engineering") == 1.0

    def test_partial_match_below_threshold(self):
        assert _fuzzy_name_match("Engineering", "Sales") < 0.85

    def test_close_match_above_threshold(self):
        assert _fuzzy_name_match("Engineering Team", "Engineering team") == 1.0


class TestFindExistingEntity:
    def test_no_match_returns_none(self):
        graph = KnowledgeGraph()
        assert _find_existing_entity("team", "Sales", graph) is None

    def test_exact_match(self):
        graph = KnowledgeGraph()
        ent = Entity(entity_type="team", name="Engineering")
        graph.add_entity(ent)
        found = _find_existing_entity("team", "Engineering", graph)
        assert found is not None
        assert found.entity_id == ent.entity_id

    def test_type_must_match(self):
        graph = KnowledgeGraph()
        ent = Entity(entity_type="team", name="Engineering")
        graph.add_entity(ent)
        assert _find_existing_entity("process", "Engineering", graph) is None


class TestExtractEntities:
    def _mock_llm_response(self, entities=None, relationships=None):
        return {
            "entities": entities or [],
            "relationships": relationships or [],
        }

    def test_creates_new_entities_and_relationships(self):
        graph = KnowledgeGraph()
        rule = _make_rule()

        mock_resp = self._mock_llm_response(
            entities=[
                {"name": "Engineering", "entity_type": "team", "description": "Engineering team"},
                {"name": "Production Deploy", "entity_type": "process", "description": None},
            ],
            relationships=[
                {"from_name": "Production Deploy", "to_name": "Engineering", "relationship_type": "owned_by", "confidence": 0.9},
            ],
        )

        with patch("graph.entity_extractor.complete_json", return_value=(mock_resp, 0)):
            new_ents, new_rels = extract_entities(rule, graph)

        assert len(new_ents) == 2
        assert len(new_rels) == 1
        assert graph.total_rules_processed == 0  # run.py increments this
        assert len(graph.entities) == 2
        assert len(graph.relationships) == 1

        # Verify entity attributes
        team = next(e for e in new_ents if e.name == "Engineering")
        assert team.entity_type == "team"
        assert rule.rule_id in team.source_rule_ids

        # Verify relationship
        rel = new_rels[0]
        assert rel.relationship_type == "owned_by"
        assert rel.confidence == pytest.approx(0.9)
        assert rel.from_entity_id != rel.to_entity_id

    def test_fuzzy_merge_existing_entity(self):
        graph = KnowledgeGraph()
        existing = Entity(entity_type="team", name="Engineering", mention_count=2)
        graph.add_entity(existing)

        rule = _make_rule()
        mock_resp = self._mock_llm_response(
            entities=[
                {"name": "engineering", "entity_type": "team", "description": "The dev team"},
            ],
            relationships=[],
        )

        with patch("graph.entity_extractor.complete_json", return_value=(mock_resp, 0)):
            new_ents, new_rels = extract_entities(rule, graph)

        assert len(new_ents) == 1
        assert new_ents[0].entity_id == existing.entity_id
        assert existing.mention_count == 3  # bumped
        assert rule.rule_id in existing.source_rule_ids
        assert existing.description == "The dev team"  # filled in
        assert len(graph.entities) == 1

    def test_skips_empty_names(self):
        graph = KnowledgeGraph()
        rule = _make_rule()
        mock_resp = self._mock_llm_response(
            entities=[
                {"name": "", "entity_type": "team", "description": None},
                {"name": "Valid", "entity_type": "concept", "description": None},
            ],
            relationships=[],
        )

        with patch("graph.entity_extractor.complete_json", return_value=(mock_resp, 0)):
            new_ents, new_rels = extract_entities(rule, graph)

        assert len(new_ents) == 1
        assert new_ents[0].name == "Valid"

    def test_skips_relationship_with_missing_entity(self):
        graph = KnowledgeGraph()
        rule = _make_rule()
        mock_resp = self._mock_llm_response(
            entities=[
                {"name": "Engineering", "entity_type": "team", "description": None},
            ],
            relationships=[
                {"from_name": "Engineering", "to_name": "Missing", "relationship_type": "governs", "confidence": 0.8},
            ],
        )

        with patch("graph.entity_extractor.complete_json", return_value=(mock_resp, 0)):
            new_ents, new_rels = extract_entities(rule, graph)

        assert len(new_ents) == 1
        assert len(new_rels) == 0

    def test_no_entities_returns_empty(self):
        graph = KnowledgeGraph()
        rule = _make_rule()
        mock_resp = self._mock_llm_response()

        with patch("graph.entity_extractor.complete_json", return_value=(mock_resp, 0)):
            new_ents, new_rels = extract_entities(rule, graph)

        assert new_ents == []
        assert new_rels == []
