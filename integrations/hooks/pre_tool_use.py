#!/usr/bin/env python3
"""
Claude Code pre_tool_use hook.

Receives tool use context via stdin, checks the proposed action
against company rules, and prints warnings if violations are found.

Usage:
    This script is called by Claude Code before every tool use.
    Configure it in Claude Code settings under hooks.pre_tool_use.

Behavior:
    - Always exits 0 (warn, never block)
    - Prints warnings to stderr so Claude Code shows them to the user
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure KORE project root is on sys.path for imports
_PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_action_description(tool_context: dict) -> str:
    """Build a human-readable description of the proposed action."""
    tool_name = tool_context.get("tool", "unknown")
    tool_input = tool_context.get("input", {})

    # Handle common tool types
    if tool_name == "bash" and "command" in tool_input:
        return f"Run bash command: {tool_input['command']}"
    if tool_name == "write" and "file" in tool_input:
        return f"Write to file {tool_input['file']}"
    if tool_name == "read" and "file" in tool_input:
        return f"Read file {tool_input['file']}"
    if tool_name == "edit" and "file" in tool_input:
        return f"Edit file {tool_input['file']}"

    # Generic fallback
    input_str = json.dumps(tool_input) if tool_input else "no arguments"
    return f"Use tool '{tool_name}' with input: {input_str}"


def _check_action(action_description: str) -> dict:
    """Check the proposed action against company rules.

    Directly uses KORE memory store and LLM (no MCP server round-trip).
    """
    from memory.store import load_memory_store, query_memory
    from providers.llm import complete_json

    store = load_memory_store("memory/store.json")

    # Retrieve relevant rules
    relevant = query_memory(action_description, store, top_k=6, entry_types=["rule"])
    if not relevant:
        return {"violations": [], "safe_to_proceed": True, "note": "No relevant rules found."}

    # Build the LLM prompt
    rules_text = "\n\n".join(
        f"Rule {i}: {entry.content}"
        for i, entry in enumerate(relevant, 1)
    )

    prompt = (
        f"You are a compliance checker for a company.\n\n"
        f"Proposed action:\n{action_description}\n\n"
        f"Relevant company rules:\n{rules_text}\n\n"
        f"Determine if the proposed action violates any of the rules above. "
        f"Respond in JSON only with this exact schema:\n"
        f'{{"violations": [{{"rule_text": str, "violation_reason": str}}], '
        f'"safe_to_proceed": bool}}'
    )

    expected_schema = {
        "violations": "list of violations",
        "safe_to_proceed": "boolean",
    }

    try:
        result, _ = complete_json(
            messages=[{"role": "user", "content": prompt}],
            system_prompt="You are a compliance checker. Be conservative — flag any potential violation. Respond in JSON only.",
            expected_schema=expected_schema,
        )
        return {
            "violations": result.get("violations", []),
            "safe_to_proceed": result.get("safe_to_proceed", True),
        }
    except Exception as exc:
        return {
            "violations": [],
            "safe_to_proceed": False,
            "note": f"Compliance check failed: {exc}. Proceed with caution.",
        }


def main() -> int:
    """Main entry point for the pre_tool_use hook."""
    # Read context from stdin
    raw = sys.stdin.read()
    if not raw.strip():
        # No input — nothing to check
        return 0

    try:
        tool_context = json.loads(raw)
    except json.JSONDecodeError:
        # Malformed input — warn and proceed
        print("[KORE] Warning: Could not parse tool context from Claude Code.", file=sys.stderr)
        return 0

    # Build action description
    action_description = _build_action_description(tool_context)

    # Check against rules
    result = _check_action(action_description)

    # Print warnings if violations found
    if not result.get("safe_to_proceed", True):
        violations = result.get("violations", [])
        print(f"\n{'='*60}", file=sys.stderr)
        print("[KORE] ⚠ COMPANY RULE VIOLATION DETECTED", file=sys.stderr)
        print(f"{'='*60}\n", file=sys.stderr)
        print(f"Action: {action_description}\n", file=sys.stderr)

        for i, v in enumerate(violations, 1):
            rule = v.get("rule_text", "unknown rule")
            reason = v.get("violation_reason", "no reason given")
            print(f"  {i}. Rule: {rule}", file=sys.stderr)
            print(f"     Reason: {reason}\n", file=sys.stderr)

        print("Review the rules above before proceeding.\n", file=sys.stderr)

    if result.get("note"):
        print(f"[KORE] {result['note']}", file=sys.stderr)

    # Always exit 0 — warn, never block
    return 0


if __name__ == "__main__":
    sys.exit(main())
