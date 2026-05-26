"""
Claude Code integration helpers.

Generates config snippets and manages Claude Code settings files
so KORE can act as a pre-flight compliance layer.
"""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any


def _get_claude_settings_path() -> Path:
    """Detect the Claude Code settings file path for the current OS."""
    system = platform.system().lower()
    if system == "darwin":
        # macOS
        return Path.home() / ".claude" / "settings.json"
    elif system == "windows":
        # Windows
        app_data = os.getenv("APPDATA")
        if app_data:
            return Path(app_data) / "Claude" / "settings.json"
        return Path.home() / "AppData" / "Roaming" / "Claude" / "settings.json"
    else:
        # Linux and others
        return Path.home() / ".claude" / "settings.json"


def _get_project_root() -> Path:
    """Return the absolute path to the KORE project root."""
    return Path(__file__).parent.parent.resolve()


def _get_python_executable() -> str:
    """Return the path to the Python interpreter running this code."""
    import sys
    return sys.executable


def generate_claude_code_config(mcp_port: int = 3333) -> dict[str, Any]:
    """Generate a Claude Code settings snippet with KORE integration.

    Returns a dict that should be merged into Claude Code's settings.json.
    """
    project_root = _get_project_root()
    hook_script = project_root / "integrations" / "hooks" / "pre_tool_use.py"

    config: dict[str, Any] = {
        "mcpServers": {
            "kore": {
                "url": f"http://127.0.0.1:{mcp_port}/mcp",
                "env": {},
            }
        },
        "hooks": {
            "pre_tool_use": [
                _get_python_executable(),
                str(hook_script),
            ],
        },
    }

    return config


def read_claude_settings() -> dict[str, Any]:
    """Read existing Claude Code settings, or return empty dict."""
    path = _get_claude_settings_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def merge_kore_config(
    existing: dict[str, Any],
    kore_config: dict[str, Any],
) -> dict[str, Any]:
    """Deep-merge KORE config into existing Claude Code settings.

    Rules:
    - mcpServers.kore is overwritten (idempotent)
    - hooks.pre_tool_use is overwritten (idempotent)
    - Everything else is preserved
    """
    merged = dict(existing)

    # Merge mcpServers
    if "mcpServers" not in merged:
        merged["mcpServers"] = {}
    merged["mcpServers"]["kore"] = kore_config["mcpServers"]["kore"]

    # Merge hooks
    if "hooks" not in merged:
        merged["hooks"] = {}
    merged["hooks"]["pre_tool_use"] = kore_config["hooks"]["pre_tool_use"]

    return merged


def write_claude_settings(settings: dict[str, Any]) -> Path:
    """Write settings back to Claude Code settings file.

    Creates parent directories if needed.
    """
    path = _get_claude_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def setup_claude_code(mcp_port: int = 3333) -> tuple[bool, str]:
    """Install KORE integration into Claude Code settings.

    Returns
    -------
    tuple[bool, str]
        (success, message)
    """
    kore_config = generate_claude_code_config(mcp_port=mcp_port)
    existing = read_claude_settings()
    merged = merge_kore_config(existing, kore_config)

    try:
        path = write_claude_settings(merged)
        return True, str(path)
    except Exception as exc:
        return False, str(exc)
