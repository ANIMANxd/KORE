import pytest
from unittest.mock import patch

from engine.nodes.extractor import extract_rule
from engine.state import create_initial_state
from providers.llm import JSONRepairFailed


class TestExtractorNode:
    """Tests for the extractor LangGraph node.

    All LLM calls are mocked.
    """

    def _make_state(self, chunks=None):
        state = create_initial_state("test query")
        if chunks:
            state["retrieved_chunks"] = chunks
        return state

    def test_successful_extraction_updates_state(self):
        """Mock valid LLM response → state populated correctly."""
        mock_response = {
            "rule_text": "Refunds processed within 48 hours.",
            "confidence": 0.85,
            "rule_category": "operations",
            "source_chunk_ids": ["chunk_1"],
            "ambiguity_notes": None,
        }

        with patch("engine.nodes.extractor.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(chunks=[
                {"chunk_id": "chunk_1", "content": "We do refunds in 48h.", "metadata": {"source_type": "slack", "channel": "#general"}},
            ])
            result = extract_rule(state)

        assert result["candidate_rule"] == "Refunds processed within 48 hours."
        assert result["rule_confidence"] == 0.85
        assert result["rule_category"] == "operations"
        assert result["verification_status"] == "pending"
        assert len(result["source_refs"]) == 1
        assert result["json_repair_attempts"] == 0

    def test_parse_failed_sets_status(self):
        """Mock JSONRepairFailed → state.verification_status = 'parse_failed'."""
        with patch("engine.nodes.extractor.complete_json") as mock_complete:
            mock_complete.side_effect = JSONRepairFailed(
                message="failed", raw_output="broken", attempts=3
            )

            state = self._make_state(chunks=[
                {"chunk_id": "chunk_1", "content": "Some text.", "metadata": {"source_type": "slack"}},
            ])
            result = extract_rule(state)

        assert result["verification_status"] == "parse_failed"
        assert result["json_repair_attempts"] == 3
        assert "failed" in result.get("error", "")

    def test_low_confidence_rejected(self):
        """Confidence < 0.3 → status rejected."""
        mock_response = {
            "rule_text": "Maybe refunds?",
            "confidence": 0.2,
            "rule_category": "other",
            "source_chunk_ids": ["chunk_1"],
            "ambiguity_notes": "unclear",
        }

        with patch("engine.nodes.extractor.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(chunks=[
                {"chunk_id": "chunk_1", "content": "Text.", "metadata": {"source_type": "slack"}},
            ])
            result = extract_rule(state)

        assert result["verification_status"] == "rejected"
        assert result["rejection_reason"] == "confidence_too_low (0.20)"

    def test_mid_confidence_needs_review(self):
        """Confidence 0.3–0.6 → status needs_review."""
        mock_response = {
            "rule_text": "Probably refunds within 48h.",
            "confidence": 0.45,
            "rule_category": "other",
            "source_chunk_ids": ["chunk_1"],
            "ambiguity_notes": "somewhat vague",
        }

        with patch("engine.nodes.extractor.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(chunks=[
                {"chunk_id": "chunk_1", "content": "Text.", "metadata": {"source_type": "slack"}},
            ])
            result = extract_rule(state)

        assert result["verification_status"] == "needs_review"

    def test_extraction_meta_populated(self):
        """extraction_meta should contain provider info from config."""
        mock_response = {
            "rule_text": "Test rule.",
            "confidence": 0.9,
            "rule_category": "other",
            "source_chunk_ids": ["chunk_1"],
            "ambiguity_notes": None,
        }

        with patch("engine.nodes.extractor.complete_json") as mock_complete:
            mock_complete.return_value = (mock_response, 0)

            state = self._make_state(chunks=[
                {"chunk_id": "chunk_1", "content": "Text.", "metadata": {"source_type": "slack"}},
            ])
            result = extract_rule(state)

        meta = result["extraction_meta"]
        assert "provider" in meta
        assert "model" in meta
        assert "deployment_mode" in meta
        assert "json_repair_attempts" in meta
