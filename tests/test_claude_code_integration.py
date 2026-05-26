"""Unit tests for integrations/claude_code.py"""

import json
import platform
from pathlib import Path
from unittest.mock import patch

import pytest

from integrations.claude_code import (
    _get_claude_settings_path,
    _get_project_root,
    generate_claude_code_config,
    read_claude_settings,
    merge_kore_config,
    write_claude_settings,
    setup_claude_code,
)


class TestGetClaudeSettingsPath:
    def test_returns_path_object(self):
        path = _get_claude_settings_path()
        assert isinstance(path, Path)
        assert path.name == "settings.json"
        assert "claude" in str(path).lower()


class TestGenerateClaudeCodeConfig:
    def test_returns_dict_with_mcp_and_hooks(self):
        config = generate_claude_code_config(mcp_port=3333)
        assert "mcpServers" in config
        assert "kore" in config["mcpServers"]
        assert config["mcpServers"]["kore"]["url"] == "http://127.0.0.1:3333/mcp"

        assert "hooks" in config
        assert "pre_tool_use" in config["hooks"]
        assert len(config["hooks"]["pre_tool_use"]) == 2
        assert "pre_tool_use.py" in config["hooks"]["pre_tool_use"][1]

    def test_custom_port(self):
        config = generate_claude_code_config(mcp_port=8080)
        assert "8080" in config["mcpServers"]["kore"]["url"]


class TestReadClaudeSettings:
    def test_returns_empty_dict_when_missing(self):
        with patch("integrations.claude_code._get_claude_settings_path", return_value=Path("/nonexistent/settings.json")):
            result = read_claude_settings()
            assert result == {}

    def test_reads_existing_file(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        data = {"theme": "dark", "mcpServers": {"other": {}}}
        settings_file.write_text(json.dumps(data), encoding="utf-8")

        with patch("integrations.claude_code._get_claude_settings_path", return_value=settings_file):
            result = read_claude_settings()
            assert result["theme"] == "dark"
            assert "other" in result["mcpServers"]

    def test_returns_empty_on_corrupt_json(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text("not json", encoding="utf-8")

        with patch("integrations.claude_code._get_claude_settings_path", return_value=settings_file):
            result = read_claude_settings()
            assert result == {}


class TestMergeKoreConfig:
    def test_adds_mcp_server_and_hook_to_empty(self):
        kore = generate_claude_code_config()
        merged = merge_kore_config({}, kore)
        assert "kore" in merged["mcpServers"]
        assert "pre_tool_use" in merged["hooks"]

    def test_preserves_existing_settings(self):
        existing = {"theme": "dark", "editor": "vim"}
        kore = generate_claude_code_config()
        merged = merge_kore_config(existing, kore)
        assert merged["theme"] == "dark"
        assert merged["editor"] == "vim"
        assert "kore" in merged["mcpServers"]

    def test_overwrites_kore_mcp_server(self):
        existing = {
            "mcpServers": {
                "kore": {"url": "http://old:1234/mcp"},
                "other": {"url": "http://other:5678/mcp"},
            }
        }
        kore = generate_claude_code_config(mcp_port=3333)
        merged = merge_kore_config(existing, kore)
        assert merged["mcpServers"]["kore"]["url"] == "http://127.0.0.1:3333/mcp"
        assert merged["mcpServers"]["other"]["url"] == "http://other:5678/mcp"

    def test_overwrites_pre_tool_use_hook(self):
        existing = {
            "hooks": {
                "pre_tool_use": ["old_hook"],
                "post_tool_use": ["other_hook"],
            }
        }
        kore = generate_claude_code_config()
        merged = merge_kore_config(existing, kore)
        assert "pre_tool_use.py" in merged["hooks"]["pre_tool_use"][1]
        assert merged["hooks"]["post_tool_use"] == ["other_hook"]


class TestWriteClaudeSettings:
    def test_creates_file_and_parent_dirs(self, tmp_path):
        target = tmp_path / "deep" / "nested" / "settings.json"
        with patch("integrations.claude_code._get_claude_settings_path", return_value=target):
            path = write_claude_settings({"test": True})
            assert path.exists()
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["test"] is True


class TestSetupClaudeCode:
    def test_successfully_writes_config(self, tmp_path):
        target = tmp_path / "settings.json"

        with patch("integrations.claude_code._get_claude_settings_path", return_value=target):
            success, message = setup_claude_code(mcp_port=3333)
            assert success is True
            assert str(target) == message
            assert target.exists()
            data = json.loads(target.read_text(encoding="utf-8"))
            assert "kore" in data["mcpServers"]

    def test_merges_with_existing(self, tmp_path):
        target = tmp_path / "settings.json"
        target.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

        with patch("integrations.claude_code._get_claude_settings_path", return_value=target):
            success, message = setup_claude_code(mcp_port=3333)
            assert success is True
            data = json.loads(target.read_text(encoding="utf-8"))
            assert data["theme"] == "dark"
            assert "kore" in data["mcpServers"]
