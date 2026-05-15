import pytest
from unittest.mock import patch

from engine.nodes.verifier import verify_rule
from engine.state import create_initial_state
from providers.llm import JSONRepairFailed


class TestVerifierNode:
    """Tests for the verifier LangGraph node.

    All LLM calls are mocked.
    """

    def _make_state(self, candidate="", confidence=0.8, status="pending", chunks=None, refs=None):
        state = create_initial_state("test query")
        state["candidate_rule"] = candidate
        state["rule_confidence"] = confidence
        state["verification_status"] = status
        state["retrieved_chunks"] = chunks or []
        state["source_refs"] = refs or []
        return state

    def test_verified_true_sets_status_verified(self):
        """verified=true → status verified."""
        mock_response = {
            "verified": True,
            "adjusted_confidence": 0.92,
            "adjusted_rule_text": None,
            "rejection_reason": None,
            "has_contradictions": False,
            "contradiction_notes": None,
        }

        with patch("engine.nodes.verifier.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(
                candidate="Refunds within 48h.",
                confidence=0.85,
                chunks=[{"chunk_id": "c1", "content": "48h refunds", "metadata": {"source_type": "slack"}}],
                refs=[{"chunk_id": "c1", "source_type": "slack", "channel": "#general"}],
            )
            result = verify_rule(state)

        assert result["verification_status"] == "verified"
        assert result["rule_confidence"] == 0.92
        assert result["rejection_reason"] is None

    def test_verified_false_sets_rejected(self):
        """verified=false → status rejected with reason."""
        mock_response = {
            "verified": False,
            "adjusted_confidence": 0.2,
            "adjusted_rule_text": None,
            "rejection_reason": "rule not supported by source",
            "has_contradictions": False,
            "contradiction_notes": None,
        }

        with patch("engine.nodes.verifier.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(
                candidate="Refunds within 1 hour.",
                confidence=0.85,
                chunks=[{"chunk_id": "c1", "content": "48h refunds", "metadata": {"source_type": "slack"}}],
                refs=[{"chunk_id": "c1", "source_type": "slack"}],
            )
            result = verify_rule(state)

        assert result["verification_status"] == "rejected"
        assert result["rejection_reason"] == "rule not supported by source"
        assert result["rule_confidence"] == 0.2

    def test_contradictions_sets_needs_review(self):
        """has_contradictions=true → status needs_review."""
        mock_response = {
            "verified": True,
            "adjusted_confidence": 0.75,
            "adjusted_rule_text": None,
            "rejection_reason": None,
            "has_contradictions": True,
            "contradiction_notes": "Engineering says 48h, Ops says 24h",
        }

        with patch("engine.nodes.verifier.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(
                candidate="Refund window is 48h.",
                confidence=0.8,
                chunks=[
                    {"chunk_id": "c1", "content": "48h", "metadata": {"source_type": "slack"}},
                    {"chunk_id": "c2", "content": "24h", "metadata": {"source_type": "slack"}},
                ],
                refs=[{"chunk_id": "c1", "source_type": "slack"}, {"chunk_id": "c2", "source_type": "slack"}],
            )
            result = verify_rule(state)

        assert result["verification_status"] == "needs_review"
        assert result["ambiguity_notes"] == "Engineering says 48h, Ops says 24h"

    def test_adjusted_rule_text_replaces_candidate(self):
        """adjusted_rule_text not null → replaces candidate_rule."""
        mock_response = {
            "verified": True,
            "adjusted_confidence": 0.88,
            "adjusted_rule_text": "Refunds for annual plans processed within 48 business hours.",
            "rejection_reason": None,
            "has_contradictions": False,
            "contradiction_notes": None,
        }

        with patch("engine.nodes.verifier.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(
                candidate="Refunds within 48h.",
                chunks=[{"chunk_id": "c1", "content": "48h refunds", "metadata": {"source_type": "slack"}}],
                refs=[{"chunk_id": "c1", "source_type": "slack"}],
            )
            result = verify_rule(state)

        assert result["candidate_rule"] == "Refunds for annual plans processed within 48 business hours."
        assert result["verification_status"] == "verified"

    def test_json_repair_failed_sets_parse_failed(self):
        """JSONRepairFailed during verification → parse_failed."""
        with patch("engine.nodes.verifier.complete_json") as mock_complete:
            mock_complete.side_effect = JSONRepairFailed(
                message="failed", raw_output="broken", attempts=2
            )

            state = self._make_state(
                candidate="Some rule.",
                chunks=[{"chunk_id": "c1", "content": "text", "metadata": {"source_type": "slack"}}],
                refs=[{"chunk_id": "c1", "source_type": "slack"}],
            )
            result = verify_rule(state)

        assert result["verification_status"] == "parse_failed"
        assert "verifier" in result.get("error", "")
        assert result["json_repair_attempts"] == 2

    def test_missing_context_rejected(self):
        """No candidate rule or chunks → rejected immediately."""
        state = self._make_state(candidate="", chunks=[])
        result = verify_rule(state)

        assert result["verification_status"] == "rejected"
        assert "missing" in result["rejection_reason"].lower()
