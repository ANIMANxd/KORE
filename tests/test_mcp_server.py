"""Unit tests for mcp/server.py"""

import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import importlib.util


def _load_mcp_server():
    """Load mcp/server.py via importlib to avoid package conflicts."""
    spec = importlib.util.spec_from_file_location("kore_mcp_test", "mcp/server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["kore_mcp_test"] = mod
    spec.loader.exec_module(mod)
    return mod


_mcp_server = _load_mcp_server()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(
    rule_id: str = "aaaaaaaa-1111-2222-3333-444444444444",
    verification_status: str = "verified",
    approved_by: str | None = "human_review",
    confidence: float = 0.92,
    rule_text: str = "Deploys to production must be approved by the Engineering manager.",
    category: str = "engineering",
) -> "BusinessRule":
    from output.schema import BusinessRule, ExtractionMeta, SourceRef

    return BusinessRule(
        rule_id=rule_id,
        rule_text=rule_text,
        rule_category=category,
        confidence=confidence,
        verification_status=verification_status,  # type: ignore[arg-type]
        source_refs=[
            SourceRef(
                chunk_id="slack_eng_123",
                source_type="slack",
                channel="#engineering",
                timestamp="2024-01-01T10:00:00Z",
                excerpt="All production deploys need manager approval.",
            )
        ],
        approved_by=approved_by,
        extraction_meta=ExtractionMeta(
            provider="lmstudio",
            model="test-model",
            deployment_mode="local",
            json_repair_attempts=0,
        ),
    )


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------


class TestQueryCompanyKnowledge:
    @patch("memory.store.load_memory_store")
    @patch("memory.store.query_memory")
    def test_returns_formatted_results(self, mock_query, mock_load):
        from memory.store import MemoryEntry, MemoryStore

        mock_load.return_value = MemoryStore(
            entries=[
                MemoryEntry(
                    content="Refund within 7 days",
                    entry_type="rule",
                    confidence=0.95,
                    embedding=[0.1, 0.2],
                    freshness=0.92,
                    source_ids=["rule-1"],
                )
            ]
        )
        mock_query.return_value = mock_load.return_value.entries

        result = _mcp_server.query_company_knowledge("refund policy", max_results=5)
        assert "Refund within 7 days" in result
        assert "confidence: 0.95" in result
        assert "freshness: 0.92" in result
        assert "[1]" in result

    @patch("memory.store.load_memory_store")
    def test_empty_store_message(self, mock_load):
        from memory.store import MemoryStore

        mock_load.return_value = MemoryStore()
        result = _mcp_server.query_company_knowledge("anything")
        assert "No knowledge entries found" in result


class TestGetRuleByCategory:
    @patch("output.writer.load_rules")
    def test_filters_by_category(self, mock_load):
        rules = [
            _make_rule(rule_text="Engineering rule: all deploys need approval", category="engineering"),
            _make_rule(
                rule_id="bbbbbbbb-2222-3333-4444-555555555555",
                rule_text="Pricing rule: always show list price first",
                category="pricing",
            ),
        ]
        mock_load.return_value = rules

        result = _mcp_server.get_rule_by_category("engineering")
        assert "engineering" in result
        assert rules[0].rule_text in result
        assert rules[1].rule_text not in result

    @patch("output.writer.load_rules")
    def test_no_rules_message(self, mock_load):
        mock_load.return_value = []
        result = _mcp_server.get_rule_by_category("engineering")
        assert "No verified rules found" in result


class TestCheckActionAgainstRules:
    @patch("memory.store.load_memory_store")
    @patch("memory.store.query_memory")
    @patch("providers.llm.complete_json")
    def test_returns_structured_result(self, mock_complete, mock_query, mock_load):
        from memory.store import MemoryEntry, MemoryStore

        mock_load.return_value = MemoryStore(
            entries=[
                MemoryEntry(
                    content="All deploys need approval",
                    entry_type="rule",
                    confidence=0.9,
                )
            ]
        )
        mock_query.return_value = mock_load.return_value.entries
        mock_complete.return_value = (
            {
                "violations": [
                    {
                        "rule_text": "All deploys need approval",
                        "violation_reason": "No approval mentioned",
                    }
                ],
                "safe_to_proceed": False,
            },
            0,
        )

        result = _mcp_server.check_action_against_rules("deploy to production now")
        assert result["safe_to_proceed"] is False
        assert len(result["violations"]) == 1
        assert "all deploys need approval" in result["violations"][0]["rule_text"].lower()

    @patch("memory.store.load_memory_store")
    @patch("memory.store.query_memory")
    def test_no_relevant_rules_returns_safe(self, mock_query, mock_load):
        from memory.store import MemoryStore

        mock_load.return_value = MemoryStore(entries=[])
        mock_query.return_value = []

        result = _mcp_server.check_action_against_rules("deploy to production now")
        assert result["safe_to_proceed"] is True
        assert result["violations"] == []

    @patch("memory.store.load_memory_store")
    @patch("memory.store.query_memory")
    @patch("providers.llm.complete_json")
    def test_llm_failure_returns_caution(self, mock_complete, mock_query, mock_load):
        from memory.store import MemoryEntry, MemoryStore

        mock_load.return_value = MemoryStore(
            entries=[MemoryEntry(content="rule", entry_type="rule", confidence=0.9)]
        )
        mock_query.return_value = mock_load.return_value.entries
        mock_complete.side_effect = RuntimeError("LLM unavailable")

        result = _mcp_server.check_action_against_rules("deploy to production now")
        assert result["safe_to_proceed"] is False
        assert "failed" in result["note"].lower()


class TestGetCompanyContext:
    @patch("memory.store.load_memory_store")
    @patch("memory.store.build_context_for_agent")
    def test_returns_context_string(self, mock_build, mock_load):
        from memory.store import MemoryStore, MemoryEntry

        mock_load.return_value = MemoryStore(
            entries=[MemoryEntry(content="rule", entry_type="rule", confidence=0.9)]
        )
        mock_build.return_value = "COMPANY KNOWLEDGE CONTEXT\n==========\nTest context"

        result = _mcp_server.get_company_context("do something", max_tokens=500)
        assert "COMPANY KNOWLEDGE CONTEXT" in result
        assert "Test context" in result

    @patch("memory.store.load_memory_store")
    def test_empty_store_message(self, mock_load):
        from memory.store import MemoryStore

        mock_load.return_value = MemoryStore()
        result = _mcp_server.get_company_context("do something")
        assert "No company context available" in result


class TestListAllRules:
    @patch("output.writer.load_rules")
    def test_lists_all_rules(self, mock_load):
        rules = [
            _make_rule(rule_text="Engineering rule: all deploys need approval first", category="engineering"),
            _make_rule(rule_id="bbbb-2222", rule_text="Pricing rule: always show list price first", category="pricing"),
        ]
        mock_load.return_value = rules

        result = _mcp_server.list_all_rules()
        assert "Total rules: 2" in result
        assert rules[0].rule_text in result
        assert rules[1].rule_text in result

    @patch("output.writer.load_rules")
    def test_filters_by_category(self, mock_load):
        rules = [
            _make_rule(rule_text="Engineering rule: all deploys need approval first", category="engineering"),
            _make_rule(rule_id="bbbb-2222", rule_text="Pricing rule: always show list price first", category="pricing"),
        ]
        mock_load.return_value = rules

        result = _mcp_server.list_all_rules(category="pricing")
        assert rules[1].rule_text in result
        assert rules[0].rule_text not in result

    @patch("output.writer.load_rules")
    def test_no_rules_message(self, mock_load):
        mock_load.return_value = []
        result = _mcp_server.list_all_rules()
        assert "No rules found" in result


# ---------------------------------------------------------------------------
# Server control tests
# ---------------------------------------------------------------------------


class TestIsRunning:
    def test_port_closed_returns_false(self):
        # Use a port that's almost certainly not open
        assert _mcp_server.is_running("127.0.0.1", port=65432) is False

    def test_port_open_returns_true(self):
        # Start a temporary listener
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.listen(1)
        try:
            assert _mcp_server.is_running("127.0.0.1", port=port) is True
        finally:
            sock.close()
