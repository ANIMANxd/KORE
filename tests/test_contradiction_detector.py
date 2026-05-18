"""Tests for engine/contradiction_detector.py

All LLM calls are mocked — no real network requests.
"""

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

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

from engine.contradiction_detector import (
    ContradictionReport,
    find_contradictions,
    save_contradiction_report,
    load_contradiction_reports,
)
from output.schema import BusinessRule, ExtractionMeta, SourceRef
from providers.llm import JSONRepairFailed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_rule(
    rule_id: str = "test-id-1234",
    rule_text: str = "All refunds must be processed within 7 days.",
    rule_category: str = "operations",
    confidence: float = 0.85,
    verification_status: str = "verified",
) -> BusinessRule:
    return BusinessRule(
        rule_id=rule_id,
        rule_text=rule_text,
        rule_category=rule_category,
        confidence=confidence,
        verification_status=verification_status,
        source_refs=[
            SourceRef(
                chunk_id="chunk_1",
                source_type="slack",
                channel="#test",
                timestamp="2024-01-01T00:00:00Z",
                excerpt="test excerpt",
            )
        ],
        extraction_meta=ExtractionMeta(
            provider="test",
            model="test-model",
            deployment_mode="local",
        ),
    )


# ---------------------------------------------------------------------------
# ContradictionReport dataclass
# ---------------------------------------------------------------------------

class TestContradictionReport:
    def test_creation(self):
        r = ContradictionReport(
            rule_id_a="a",
            rule_id_b="b",
            rule_text_a="Rule A text",
            rule_text_b="Rule B text",
            contradiction_description="They conflict",
            severity="high",
        )
        assert r.rule_id_a == "a"
        assert r.severity == "high"
        assert isinstance(r.detected_at, datetime)

    def test_to_dict_round_trip(self):
        now = datetime.now(timezone.utc)
        r = ContradictionReport(
            rule_id_a="a",
            rule_id_b="b",
            rule_text_a="Text A",
            rule_text_b="Text B",
            contradiction_description="Desc",
            severity="medium",
            detected_at=now,
        )
        d = r.to_dict()
        restored = ContradictionReport.from_dict(d)
        assert restored.rule_id_a == "a"
        assert restored.severity == "medium"
        assert restored.detected_at == now


# ---------------------------------------------------------------------------
# find_contradictions
# ---------------------------------------------------------------------------

class TestFindContradictions:
    @patch("engine.contradiction_detector.complete_json")
    def test_no_contradiction(self, mock_complete_json):
        mock_complete_json.return_value = (
            {"contradicts": False, "description": None, "severity": "low"},
            0,
        )
        new = _make_rule(rule_id="new-1")
        existing = [_make_rule(rule_id="existing-1")]
        reports = find_contradictions(new, existing)
        assert reports == []
        mock_complete_json.assert_called_once()

    @patch("engine.contradiction_detector.complete_json")
    def test_contradiction_found(self, mock_complete_json):
        mock_complete_json.return_value = (
            {
                "contradicts": True,
                "description": "One says 7 days, the other says 14 days.",
                "severity": "high",
            },
            0,
        )
        new = _make_rule(rule_id="new-1", rule_text="Refunds within 7 days.")
        existing = [_make_rule(rule_id="existing-1", rule_text="Refunds within 14 days.")]
        reports = find_contradictions(new, existing)
        assert len(reports) == 1
        assert reports[0].rule_id_a == "new-1"
        assert reports[0].rule_id_b == "existing-1"
        assert reports[0].severity == "high"
        assert "7 days" in reports[0].contradiction_description

    @patch("engine.contradiction_detector.complete_json")
    def test_only_same_category_checked(self, mock_complete_json):
        """Rules in different categories are never compared."""
        new = _make_rule(rule_id="new-1", rule_category="pricing")
        existing = [_make_rule(rule_id="existing-1", rule_category="operations")]
        reports = find_contradictions(new, existing)
        assert reports == []
        mock_complete_json.assert_not_called()

    @patch("engine.contradiction_detector.complete_json")
    def test_skips_self(self, mock_complete_json):
        """A rule is not compared against itself."""
        new = _make_rule(rule_id="same-id")
        existing = [_make_rule(rule_id="same-id")]
        reports = find_contradictions(new, existing)
        assert reports == []
        mock_complete_json.assert_not_called()

    @patch("engine.contradiction_detector.complete_json")
    def test_graceful_on_json_repair_failed(self, mock_complete_json):
        """If the LLM returns unparseable JSON, assume no contradiction."""
        mock_complete_json.side_effect = JSONRepairFailed(
            message="failed",
            raw_output="bad json",
            attempts=3,
        )
        new = _make_rule(rule_id="new-1")
        existing = [_make_rule(rule_id="existing-1")]
        reports = find_contradictions(new, existing)
        assert reports == []

    @patch("engine.contradiction_detector.complete_json")
    def test_multiple_existing_rules(self, mock_complete_json):
        """Checks all existing rules in the same category."""
        mock_complete_json.return_value = (
            {"contradicts": True, "description": "Conflict", "severity": "medium"},
            0,
        )
        new = _make_rule(rule_id="new-1")
        existing = [
            _make_rule(rule_id="existing-1"),
            _make_rule(rule_id="existing-2"),
        ]
        reports = find_contradictions(new, existing)
        assert len(reports) == 2
        assert mock_complete_json.call_count == 2

    @patch("engine.contradiction_detector.complete_json")
    def test_invalid_severity_defaults_to_medium(self, mock_complete_json):
        mock_complete_json.return_value = (
            {"contradicts": True, "description": "Conflict", "severity": "critical"},
            0,
        )
        new = _make_rule(rule_id="new-1")
        existing = [_make_rule(rule_id="existing-1")]
        reports = find_contradictions(new, existing)
        assert reports[0].severity == "medium"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestSaveAndLoad:
    def test_save_creates_yaml(self, tmp_path: Path):
        report = ContradictionReport(
            rule_id_a="aaaaaaaa-1111-2222-3333-444444444444",
            rule_id_b="bbbbbbbb-1111-2222-3333-444444444444",
            rule_text_a="Text A",
            rule_text_b="Text B",
            contradiction_description="Desc",
            severity="high",
        )
        out_dir = tmp_path / "contradictions"
        path = save_contradiction_report(report, output_dir=str(out_dir))
        assert Path(path).exists()
        assert "contradiction" in Path(path).name
        assert "aaaaaaaa" in Path(path).name
        assert "vs" in Path(path).name
        assert "bbbbbbbb" in Path(path).name

    def test_load_round_trip(self, tmp_path: Path):
        out_dir = tmp_path / "contradictions"
        report = ContradictionReport(
            rule_id_a="a",
            rule_id_b="b",
            rule_text_a="A",
            rule_text_b="B",
            contradiction_description="Desc",
            severity="low",
        )
        save_contradiction_report(report, output_dir=str(out_dir))
        loaded = load_contradiction_reports(directory=str(out_dir))
        assert len(loaded) == 1
        assert loaded[0].rule_id_a == "a"
        assert loaded[0].severity == "low"

    def test_load_skips_malformed(self, tmp_path: Path):
        out_dir = tmp_path / "contradictions"
        out_dir.mkdir()
        # Valid file
        valid = {"rule_id_a": "a", "rule_id_b": "b", "rule_text_a": "A", "rule_text_b": "B",
                 "contradiction_description": "Desc", "severity": "low", "detected_at": "2024-01-01T00:00:00+00:00"}
        (out_dir / "valid.yaml").write_text(yaml.safe_dump(valid))
        # Invalid file (missing required field)
        invalid = {"rule_id_a": "a"}
        (out_dir / "invalid.yaml").write_text(yaml.safe_dump(invalid))
        loaded = load_contradiction_reports(directory=str(out_dir))
        assert len(loaded) == 1
        assert loaded[0].rule_id_a == "a"

    def test_load_empty_directory(self, tmp_path: Path):
        loaded = load_contradiction_reports(directory=str(tmp_path / "nonexistent"))
        assert loaded == []
