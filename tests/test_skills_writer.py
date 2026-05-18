"""Tests for output/skills_writer.py"""

import sys
import types
import json
from datetime import datetime, timezone
from pathlib import Path
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

from output.skills_writer import export_skills_json, _build_source_string, _rule_to_skill
from output.schema import BusinessRule, ExtractionMeta, SourceRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rule(
    rule_id: str = "aaaaaaaa-1111-2222-3333-444444444444",
    verification_status: str = "verified",
    approved_by: str | None = "human_review",
    ambiguity_notes: str | None = None,
) -> BusinessRule:
    return BusinessRule(
        rule_id=rule_id,
        rule_text="All refunds must be processed within 7 days.",
        rule_category="operations",
        confidence=0.91,
        verification_status=verification_status,
        source_refs=[
            SourceRef(
                chunk_id="slack_general_123",
                source_type="slack",
                channel="#general",
                timestamp="2024-01-15T10:00:00Z",
                excerpt="Refund policy discussion",
            )
        ],
        approved_by=approved_by,
        version=2,
        ambiguity_notes=ambiguity_notes,
        extraction_meta=ExtractionMeta(
            provider="test",
            model="test-model",
            deployment_mode="local",
        ),
    )


# ---------------------------------------------------------------------------
# _build_source_string
# ---------------------------------------------------------------------------

class TestBuildSourceString:
    def test_channel_and_timestamp(self):
        ref = {"channel": "general", "timestamp": "2024-01-15T10:00:00Z", "chunk_id": "c1"}
        assert _build_source_string(ref) == "#general @ 2024-01-15T10:00:00Z"

    def test_channel_only(self):
        ref = {"channel": "ops", "timestamp": "", "chunk_id": "c1"}
        assert _build_source_string(ref) == "#ops"

    def test_timestamp_only(self):
        ref = {"channel": "", "timestamp": "2024-01-15T10:00:00Z", "chunk_id": "c1"}
        assert _build_source_string(ref) == "@ 2024-01-15T10:00:00Z"

    def test_neither_falls_back_to_chunk_id(self):
        ref = {"channel": "", "timestamp": "", "chunk_id": "c1"}
        assert _build_source_string(ref) == "c1"


# ---------------------------------------------------------------------------
# _rule_to_skill
# ---------------------------------------------------------------------------

class TestRuleToSkill:
    def test_basic_conversion(self):
        rule = _make_rule()
        skill = _rule_to_skill(rule)
        assert skill["id"] == rule.rule_id
        assert skill["name"] == f"operations_{rule.rule_id[:6]}"
        assert skill["description"] == rule.rule_text
        assert skill["category"] == "operations"
        assert skill["confidence"] == 0.91
        assert skill["approved"] is True
        assert skill["version"] == 2
        assert skill["sources"] == ["#general @ 2024-01-15T10:00:00Z"]

    def test_constraints_when_ambiguity_notes(self):
        rule = _make_rule(ambiguity_notes="Only for enterprise customers")
        skill = _rule_to_skill(rule)
        assert skill["constraints"] == ["Only applies when: Only for enterprise customers"]

    def test_constraints_empty_when_no_notes(self):
        rule = _make_rule(ambiguity_notes=None)
        skill = _rule_to_skill(rule)
        assert skill["constraints"] == []


# ---------------------------------------------------------------------------
# export_skills_json
# ---------------------------------------------------------------------------

class TestExportSkillsJson:
    @patch("output.skills_writer.load_rules")
    def test_export_includes_only_verified_approved(self, mock_load_rules, tmp_path: Path):
        mock_load_rules.return_value = [
            _make_rule(rule_id="r1", verification_status="verified", approved_by="human"),
            _make_rule(rule_id="r2", verification_status="verified", approved_by=None),
            _make_rule(rule_id="r3", verification_status="needs_review", approved_by="human"),
            _make_rule(rule_id="r4", verification_status="verified", approved_by="bot"),
        ]
        output_file = tmp_path / "skills.json"
        path = export_skills_json(rules_dir="rules/extracted/", output_path=str(output_file))

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["version"] == "1.0"
        assert data["source"] == "KORE"
        assert data["total_skills"] == 2
        assert len(data["skills"]) == 2
        ids = {s["id"] for s in data["skills"]}
        assert ids == {"r1", "r4"}

    @patch("output.skills_writer.load_rules")
    def test_export_empty_when_no_approved_rules(self, mock_load_rules, tmp_path: Path):
        mock_load_rules.return_value = [
            _make_rule(rule_id="r1", verification_status="verified", approved_by=None),
        ]
        output_file = tmp_path / "skills.json"
        path = export_skills_json(rules_dir="rules/extracted/", output_path=str(output_file))

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data["total_skills"] == 0
        assert data["skills"] == []

    @patch("output.skills_writer.load_rules")
    def test_generated_at_is_iso_timestamp(self, mock_load_rules, tmp_path: Path):
        mock_load_rules.return_value = [
            _make_rule(rule_id="r1", verification_status="verified", approved_by="human"),
        ]
        output_file = tmp_path / "skills.json"
        path = export_skills_json(output_path=str(output_file))

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Should be a valid ISO timestamp
        ts = data["generated_at"]
        assert isinstance(ts, str)
        assert "T" in ts

    @patch("output.skills_writer.load_rules")
    def test_creates_parent_directories(self, mock_load_rules, tmp_path: Path):
        mock_load_rules.return_value = []
        nested = tmp_path / "nested" / "dir" / "skills.json"
        path = export_skills_json(output_path=str(nested))
        assert Path(path).exists()
