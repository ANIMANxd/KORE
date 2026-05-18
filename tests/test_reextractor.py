"""Tests for engine/reextractor.py"""

import sys
import types
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

# Mock heavy optional dependencies before any storage imports happen
_pyarrow = types.ModuleType("pyarrow")
_pyarrow.schema = MagicMock(return_value=MagicMock())
_pyarrow.field = MagicMock(return_value=MagicMock())
_pyarrow.string = MagicMock()
_pyarrow.float32 = MagicMock()
_pyarrow.list_ = MagicMock(return_value=MagicMock())
_pyarrow.__version__ = "15.0.0"
sys.modules["pyarrow"] = _pyarrow
sys.modules["lancedb"] = types.ModuleType("lancedb")
sys.modules["psycopg2"] = types.ModuleType("psycopg2")
_psycopg2_extras = types.ModuleType("psycopg2.extras")
_psycopg2_extras.Json = lambda x: x
_psycopg2_extras.connection = MagicMock()
sys.modules["psycopg2.extras"] = _psycopg2_extras
_psycopg2_ext = types.ModuleType("psycopg2.extensions")
_psycopg2_ext.new_type = MagicMock()
_psycopg2_ext.new_array_type = MagicMock()
_psycopg2_ext.register_type = MagicMock()
_psycopg2_ext.connection = MagicMock()
sys.modules["psycopg2.extensions"] = _psycopg2_ext
sys.modules["psycopg2"].extensions = _psycopg2_ext
sys.modules["pgvector"] = types.ModuleType("pgvector")
sys.modules["pgvector.psycopg2"] = types.ModuleType("pgvector.psycopg2")

from engine.reextractor import find_affected_rules, reextract_rule
from output.schema import BusinessRule, ExtractionMeta, SourceRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rule(
    rule_id: str = "test-id-1234",
    chunk_ids: list[str] | None = None,
) -> BusinessRule:
    refs = []
    if chunk_ids:
        for cid in chunk_ids:
            refs.append(
                SourceRef(
                    chunk_id=cid,
                    source_type="slack",
                    channel="#test",
                    timestamp="2024-01-01T00:00:00Z",
                    excerpt="excerpt",
                )
            )
    return BusinessRule(
        rule_id=rule_id,
        rule_text="All refunds must be processed within 7 days.",
        rule_category="operations",
        confidence=0.85,
        verification_status="verified",
        source_refs=refs,
        extraction_meta=ExtractionMeta(
            provider="test",
            model="test-model",
            deployment_mode="local",
        ),
    )


# ---------------------------------------------------------------------------
# find_affected_rules
# ---------------------------------------------------------------------------

class TestFindAffectedRules:
    def test_exact_overlap_above_threshold(self):
        new_ids = ["chunk_a", "chunk_b", "chunk_c"]
        existing = [_make_rule(rule_id="r1", chunk_ids=["chunk_a", "chunk_b", "chunk_c"])]
        affected = find_affected_rules(new_ids, existing, similarity_threshold=0.85)
        assert affected == ["r1"]

    def test_no_overlap_returns_empty(self):
        new_ids = ["chunk_x", "chunk_y"]
        existing = [_make_rule(rule_id="r1", chunk_ids=["chunk_a", "chunk_b"])]
        affected = find_affected_rules(new_ids, existing)
        assert affected == []

    def test_partial_overlap_below_threshold(self):
        new_ids = ["chunk_a"]
        existing = [_make_rule(rule_id="r1", chunk_ids=["chunk_a", "chunk_b", "chunk_c", "chunk_d"])]
        affected = find_affected_rules(new_ids, existing, similarity_threshold=0.85)
        # 1/4 = 0.25 < 0.85
        assert affected == []

    def test_partial_overlap_above_threshold(self):
        new_ids = ["chunk_a", "chunk_b", "chunk_c"]
        existing = [_make_rule(rule_id="r1", chunk_ids=["chunk_a", "chunk_b", "chunk_c"])]
        affected = find_affected_rules(new_ids, existing, similarity_threshold=0.85)
        # 3/3 = 1.0 >= 0.85
        assert affected == ["r1"]

    def test_multiple_rules_some_affected(self):
        new_ids = ["chunk_a"]
        existing = [
            _make_rule(rule_id="r1", chunk_ids=["chunk_a"]),           # 1/1 = 1.0
            _make_rule(rule_id="r2", chunk_ids=["chunk_b", "chunk_c"]), # 0/2 = 0.0
        ]
        affected = find_affected_rules(new_ids, existing, similarity_threshold=0.85)
        assert affected == ["r1"]

    def test_empty_new_chunks_returns_empty(self):
        existing = [_make_rule(rule_id="r1", chunk_ids=["chunk_a"])]
        assert find_affected_rules([], existing) == []

    def test_rule_with_no_source_refs_skipped(self):
        new_ids = ["chunk_a"]
        rule = _make_rule(rule_id="r1", chunk_ids=["chunk_x"])
        # Clear source_refs after creation to simulate a rule with no refs
        rule.source_refs = []
        existing = [rule]
        assert find_affected_rules(new_ids, existing) == []

    def test_custom_threshold(self):
        new_ids = ["chunk_a"]
        existing = [_make_rule(rule_id="r1", chunk_ids=["chunk_a", "chunk_b"])]
        # 1/2 = 0.5 — passes with threshold 0.4, fails with 0.6
        assert find_affected_rules(new_ids, existing, similarity_threshold=0.4) == ["r1"]
        assert find_affected_rules(new_ids, existing, similarity_threshold=0.6) == []


# ---------------------------------------------------------------------------
# reextract_rule
# ---------------------------------------------------------------------------

class TestReextractRule:
    @patch("engine.reextractor.run_extraction")
    def test_uses_rule_text_as_default_query(self, mock_run_extraction):
        mock_run_extraction.return_value = {
            "query": "All refunds must be processed within 7 days.",
            "candidate_rule": "All refunds within 5 days.",
            "rule_confidence": 0.9,
            "verification_status": "verified",
            "source_refs": [],
        }
        rule = _make_rule(chunk_ids=["chunk_1"])
        result = reextract_rule(rule)
        mock_run_extraction.assert_called_once_with("All refunds must be processed within 7 days.")
        assert result["candidate_rule"] == "All refunds within 5 days."

    @patch("engine.reextractor.run_extraction")
    def test_uses_custom_query_when_provided(self, mock_run_extraction):
        mock_run_extraction.return_value = {
            "query": "new query",
            "candidate_rule": "New rule text.",
            "rule_confidence": 0.8,
            "verification_status": "verified",
            "source_refs": [],
        }
        rule = _make_rule(chunk_ids=["chunk_1"])
        result = reextract_rule(rule, query="new query")
        mock_run_extraction.assert_called_once_with("new query")
        assert result["candidate_rule"] == "New rule text."
