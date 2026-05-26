"""Unit tests for integrations/hooks/pre_tool_use.py"""

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import importlib.util


def _load_hook_module():
    """Load the hook module via importlib."""
    spec = importlib.util.spec_from_file_location("pre_tool_use_hook", "integrations/hooks/pre_tool_use.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pre_tool_use_hook"] = mod
    spec.loader.exec_module(mod)
    return mod


_hook = _load_hook_module()


class TestBuildActionDescription:
    def test_bash_command(self):
        ctx = {"tool": "bash", "input": {"command": "rm -rf /"}}
        desc = _hook._build_action_description(ctx)
        assert "bash" in desc.lower()
        assert "rm -rf /" in desc

    def test_write_file(self):
        ctx = {"tool": "write", "input": {"file": "/etc/passwd"}}
        desc = _hook._build_action_description(ctx)
        assert "write" in desc.lower()
        assert "/etc/passwd" in desc

    def test_read_file(self):
        ctx = {"tool": "read", "input": {"file": "config.yaml"}}
        desc = _hook._build_action_description(ctx)
        assert "read" in desc.lower()
        assert "config.yaml" in desc

    def test_edit_file(self):
        ctx = {"tool": "edit", "input": {"file": "main.py"}}
        desc = _hook._build_action_description(ctx)
        assert "edit" in desc.lower()
        assert "main.py" in desc

    def test_generic_tool(self):
        ctx = {"tool": "custom_tool", "input": {"foo": "bar"}}
        desc = _hook._build_action_description(ctx)
        assert "custom_tool" in desc
        assert "bar" in desc

    def test_unknown_tool_no_input(self):
        ctx = {"tool": "mystery"}
        desc = _hook._build_action_description(ctx)
        assert "mystery" in desc
        assert "no arguments" in desc


class TestCheckAction:
    @patch("memory.store.load_memory_store")
    @patch("memory.store.query_memory")
    @patch("providers.llm.complete_json")
    def test_violation_detected(self, mock_complete, mock_query, mock_load):
        from memory.store import MemoryEntry, MemoryStore

        mock_load.return_value = MemoryStore(
            entries=[MemoryEntry(content="Never delete production data", entry_type="rule", confidence=0.9)]
        )
        mock_query.return_value = mock_load.return_value.entries
        mock_complete.return_value = (
            {
                "violations": [
                    {"rule_text": "Never delete production data", "violation_reason": "rm command detected"}
                ],
                "safe_to_proceed": False,
            },
            0,
        )

        result = _hook._check_action("Run bash command: rm -rf /data")
        assert result["safe_to_proceed"] is False
        assert len(result["violations"]) == 1
        assert "delete production data" in result["violations"][0]["rule_text"].lower()

    @patch("memory.store.load_memory_store")
    @patch("memory.store.query_memory")
    def test_no_relevant_rules(self, mock_query, mock_load):
        from memory.store import MemoryStore

        mock_load.return_value = MemoryStore()
        mock_query.return_value = []

        result = _hook._check_action("Run bash command: ls")
        assert result["safe_to_proceed"] is True
        assert result["violations"] == []

    @patch("memory.store.load_memory_store")
    @patch("memory.store.query_memory")
    @patch("providers.llm.complete_json")
    def test_llm_failure_graceful(self, mock_complete, mock_query, mock_load):
        from memory.store import MemoryEntry, MemoryStore

        mock_load.return_value = MemoryStore(
            entries=[MemoryEntry(content="rule", entry_type="rule", confidence=0.9)]
        )
        mock_query.return_value = mock_load.return_value.entries
        mock_complete.side_effect = RuntimeError("LLM unavailable")

        result = _hook._check_action("Run bash command: rm -rf /")
        assert result["safe_to_proceed"] is False
        assert "failed" in result["note"].lower()


class TestMain:
    @patch("sys.stdin", StringIO(json.dumps({"tool": "bash", "input": {"command": "ls"}})))
    @patch("pre_tool_use_hook._check_action")
    def test_valid_input_no_violation(self, mock_check):
        mock_check.return_value = {"safe_to_proceed": True, "violations": []}
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            code = _hook.main()
        assert code == 0
        assert "VIOLATION" not in stderr_capture.getvalue()

    @patch("sys.stdin", StringIO(json.dumps({"tool": "bash", "input": {"command": "rm -rf /"}})))
    @patch("pre_tool_use_hook._check_action")
    def test_violation_prints_warning(self, mock_check):
        mock_check.return_value = {
            "safe_to_proceed": False,
            "violations": [
                {"rule_text": "Never delete system files", "violation_reason": "rm -rf / detected"}
            ],
        }
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            code = _hook.main()
        assert code == 0
        output = stderr_capture.getvalue()
        assert "VIOLATION DETECTED" in output
        assert "Never delete system files" in output
        assert "rm -rf / detected" in output

    @patch("sys.stdin", StringIO("not json"))
    def test_malformed_json_warns(self):
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            code = _hook.main()
        assert code == 0
        assert "Could not parse" in stderr_capture.getvalue()

    @patch("sys.stdin", StringIO(""))
    def test_empty_input_exits_cleanly(self):
        stderr_capture = StringIO()
        with patch("sys.stderr", stderr_capture):
            code = _hook.main()
        assert code == 0
        assert stderr_capture.getvalue() == ""
