"""Tests for engine/expiry_detector.py"""

import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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

from engine.expiry_detector import (
    ExpiryReport,
    _parse_source_timestamp,
    check_rule_expiry,
    scan_all_rules,
)
from output.schema import BusinessRule, ExtractionMeta, SourceRef


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rule(
    rule_id: str = "test-id-1234",
    rule_text: str = "All refunds must be processed within 7 days.",
    rule_category: str = "operations",
    timestamps: list[str] | None = None,
) -> BusinessRule:
    refs = []
    if timestamps:
        for i, ts in enumerate(timestamps):
            refs.append(
                SourceRef(
                    chunk_id=f"chunk_{i}",
                    source_type="slack",
                    channel="#test",
                    timestamp=ts,
                    excerpt="excerpt",
                )
            )
    return BusinessRule(
        rule_id=rule_id,
        rule_text=rule_text,
        rule_category=rule_category,
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
# _parse_source_timestamp
# ---------------------------------------------------------------------------

class TestParseSourceTimestamp:
    def test_iso_8601_with_z(self):
        dt = _parse_source_timestamp("2024-01-15T10:30:00Z")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15
        assert dt.hour == 10
        assert dt.tzinfo is not None

    def test_iso_8601_with_offset(self):
        dt = _parse_source_timestamp("2024-01-15T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2024

    def test_date_only(self):
        dt = _parse_source_timestamp("2024-01-15")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15

    def test_empty_string_returns_none(self):
        assert _parse_source_timestamp("") is None
        assert _parse_source_timestamp("   ") is None

    def test_unparseable_returns_none(self):
        assert _parse_source_timestamp("not a date") is None


# ---------------------------------------------------------------------------
# check_rule_expiry
# ---------------------------------------------------------------------------

class TestCheckRuleExpiry:
    def test_fresh_rule_returns_none(self):
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        rule = _make_rule(timestamps=["2024-05-01T00:00:00Z"])  # 31 days old
        assert check_rule_expiry(rule, days_threshold=90, reference_date=now) is None

    def test_stale_rule_returns_report(self):
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        rule = _make_rule(timestamps=["2024-01-01T00:00:00Z"])  # 152 days old
        report = check_rule_expiry(rule, days_threshold=90, reference_date=now)
        assert report is not None
        assert report.days_since_referenced == 152
        assert "re-extracting" in report.recommendation.lower() or "deprecated" in report.recommendation.lower()

    def test_uses_most_recent_timestamp(self):
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        rule = _make_rule(
            timestamps=[
                "2023-01-01T00:00:00Z",  # very old
                "2024-05-20T00:00:00Z",  # 12 days old — most recent
            ]
        )
        assert check_rule_expiry(rule, days_threshold=90, reference_date=now) is None

    def test_no_source_refs_returns_none(self):
        rule = _make_rule(timestamps=["2024-01-01T00:00:00Z"])
        # Clear source_refs after creation to bypass validator
        rule.source_refs = []
        assert check_rule_expiry(rule, days_threshold=90) is None

    def test_unparseable_timestamps_returns_none(self):
        rule = _make_rule(timestamps=["not-a-date", "also-not"])
        assert check_rule_expiry(rule, days_threshold=90) is None

    def test_report_contains_rule_category(self):
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        rule = _make_rule(
            rule_category="pricing",
            timestamps=["2024-01-01T00:00:00Z"],
        )
        report = check_rule_expiry(rule, days_threshold=90, reference_date=now)
        assert report is not None
        assert report.rule_category == "pricing"


# ---------------------------------------------------------------------------
# scan_all_rules
# ---------------------------------------------------------------------------

class TestScanAllRules:
    @patch("engine.expiry_detector.load_rules")
    def test_scan_finds_expired_rules(self, mock_load_rules):
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_load_rules.return_value = [
            _make_rule(rule_id="fresh", timestamps=["2024-05-20T00:00:00Z"]),
            _make_rule(rule_id="stale", timestamps=["2024-01-01T00:00:00Z"]),
        ]
        reports = scan_all_rules(days_threshold=90, reference_date=now)
        assert len(reports) == 1
        assert reports[0].rule_id == "stale"

    @patch("engine.expiry_detector.load_rules")
    def test_scan_returns_empty_when_all_fresh(self, mock_load_rules):
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_load_rules.return_value = [
            _make_rule(rule_id="fresh-1", timestamps=["2024-05-20T00:00:00Z"]),
            _make_rule(rule_id="fresh-2", timestamps=["2024-04-01T00:00:00Z"]),
        ]
        reports = scan_all_rules(days_threshold=90, reference_date=now)
        assert reports == []

    @patch("engine.expiry_detector.load_rules")
    def test_scan_sorts_by_days_descending(self, mock_load_rules):
        now = datetime(2024, 6, 1, tzinfo=timezone.utc)
        mock_load_rules.return_value = [
            _make_rule(rule_id="medium", timestamps=["2024-02-01T00:00:00Z"]),   # 121 days
            _make_rule(rule_id="oldest", timestamps=["2023-06-01T00:00:00Z"]),  # 366 days
        ]
        reports = scan_all_rules(days_threshold=90, reference_date=now)
        assert len(reports) == 2
        assert reports[0].rule_id == "oldest"
        assert reports[1].rule_id == "medium"
        assert reports[0].days_since_referenced > reports[1].days_since_referenced

    @patch("engine.expiry_detector.load_rules")
    def test_scan_with_empty_directory(self, mock_load_rules):
        mock_load_rules.return_value = []
        reports = scan_all_rules()
        assert reports == []
