import pytest
from unittest.mock import patch

from providers.llm import complete_json, JSONRepairFailed


class TestCompleteJson:
    """Tests for the JSON repair loop in complete_json().

    All LLM calls are mocked — never touches real providers.
    """

    def test_valid_json_parses_directly(self):
        """A clean JSON response should parse without any repair."""
        with patch("providers.llm.complete") as mock_complete:
            mock_complete.return_value = '{"rule_text": "test", "confidence": 0.9}'

            result, attempts = complete_json(
                messages=[{"role": "user", "content": "test"}],
                system_prompt="You are a test.",
                expected_schema={"rule_text": "", "confidence": ""},
            )

        assert result == {"rule_text": "test", "confidence": 0.9}
        assert attempts == 0
        assert mock_complete.call_count == 1

    def test_strips_markdown_fences(self):
        """Response wrapped in ```json fences should be stripped and parsed."""
        with patch("providers.llm.complete") as mock_complete:
            mock_complete.return_value = (
                '```json\n{"rule_text": "test", "confidence": 0.9}\n```'
            )

            result, attempts = complete_json(
                messages=[{"role": "user", "content": "test"}],
                system_prompt="You are a test.",
                expected_schema={"rule_text": "", "confidence": ""},
            )

        assert result == {"rule_text": "test", "confidence": 0.9}
        assert attempts == 0

    def test_repair_loop_fixes_broken_json(self):
        """A missing comma triggers the repair loop; second call returns valid JSON."""
        responses = [
            # First attempt: broken JSON (missing comma)
            '{"rule_text": "test" "confidence": 0.9}',
            # Second attempt: repair prompt → valid JSON
            '{"rule_text": "test", "confidence": 0.9}',
        ]

        with patch("providers.llm.complete") as mock_complete:
            mock_complete.side_effect = responses

            result, attempts = complete_json(
                messages=[{"role": "user", "content": "test"}],
                system_prompt="You are a test.",
                expected_schema={"rule_text": "", "confidence": ""},
            )

        assert result == {"rule_text": "test", "confidence": 0.9}
        assert attempts == 1  # one repair attempt
        assert mock_complete.call_count == 2

    def test_repair_exhaustion_raises_json_repair_failed(self):
        """If JSON is broken on every attempt, JSONRepairFailed is raised."""
        with patch("providers.llm.complete") as mock_complete:
            # Always return broken JSON
            mock_complete.return_value = '{"rule_text": "test" "confidence": 0.9}'

            with pytest.raises(JSONRepairFailed) as exc_info:
                complete_json(
                    messages=[{"role": "user", "content": "test"}],
                    system_prompt="You are a test.",
                    expected_schema={"rule_text": "", "confidence": ""},
                    max_retries=2,
                )

        assert exc_info.value.attempts == 2
        assert "test" in exc_info.value.raw_output
        assert mock_complete.call_count == 3  # initial + 2 retries

    def test_repair_attempts_counter_increments(self):
        """The repair counter should reflect how many repair rounds ran."""
        responses = [
            '{"broken": true',      # attempt 0
            '{"still": broken',     # attempt 1
            '{"fixed": true}',      # attempt 2
        ]

        with patch("providers.llm.complete") as mock_complete:
            mock_complete.side_effect = responses

            result, attempts = complete_json(
                messages=[{"role": "user", "content": "test"}],
                system_prompt="You are a test.",
                expected_schema={"fixed": ""},
                max_retries=3,
            )

        assert attempts == 2  # 2 repairs needed
        assert mock_complete.call_count == 3
