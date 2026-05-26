"""
KORE MCP Server — Local Model Context Protocol server.

Exposes company knowledge as MCP tools for AI agents.
Runs entirely on-premise. Zero cloud dependency.

Usage:
    python mcp/server.py --host 127.0.0.1 --port 3333
    python run.py mcp-server --port 3333
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path for imports
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Create the MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "KORE",
    instructions=(
        "KORE — On-Premise Context Engine. "
        "Query company knowledge, rules, and context before taking actions."
    ),
    stateless_http=True,
    json_response=True,
)


# ---------------------------------------------------------------------------
# Tool 1 — query_company_knowledge
# ---------------------------------------------------------------------------

@mcp.tool()
def query_company_knowledge(query: str, max_results: int = 5) -> str:
    """Query the company knowledge store for relevant memory entries.

    Use this before taking any action that might be constrained by
    company rules, policies, or procedures.
    """
    from memory.store import load_memory_store, query_memory

    store = load_memory_store("memory/store.json")
    if not store.entries:
        return "No knowledge entries found. The memory store may need to be built."

    results = query_memory(query, store, top_k=max_results)
    if not results:
        return "No relevant knowledge found for this query."

    lines = [f"Knowledge results for: {query}\n"]
    for i, entry in enumerate(results, 1):
        lines.append(
            f"[{i}] {entry.entry_type.upper()} (confidence: {entry.confidence:.2f}, "
            f"freshness: {entry.freshness:.2f})\n"
            f"{entry.content}\n"
            f"Sources: {', '.join(entry.source_ids[:3]) or 'unknown'}\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 2 — get_rule_by_category
# ---------------------------------------------------------------------------

@mcp.tool()
def get_rule_by_category(category: str) -> str:
    """Return all verified rules in a specific category.

    Categories: pricing, operations, escalation, compliance,
    customer_success, engineering, hr, other
    """
    from output.writer import load_rules

    rules = load_rules("rules/extracted/")
    filtered = [r for r in rules if r.rule_category.lower() == category.lower()]

    if not filtered:
        return f"No verified rules found for category '{category}'."

    lines = [f"Rules in category: {category}\n"]
    for rule in filtered:
        status = rule.verification_status
        approved = rule.approved_by or "unapproved"
        lines.append(
            f"- {rule.rule_text}\n"
            f"  confidence: {rule.confidence:.2f} | status: {status} | approved_by: {approved}\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool 3 — check_action_against_rules
# ---------------------------------------------------------------------------

@mcp.tool()
def check_action_against_rules(proposed_action: str) -> dict:
    """Check whether a proposed action violates any company rules.

    This queries the knowledge store for relevant rules and uses an LLM
    to determine if the action is safe to proceed.

    Returns a structured result with violation details.
    """
    from memory.store import load_memory_store, query_memory
    from providers.llm import complete_json

    store = load_memory_store("memory/store.json")

    # Retrieve relevant rules
    relevant = query_memory(proposed_action, store, top_k=8, entry_types=["rule"])
    if not relevant:
        return {
            "violations": [],
            "safe_to_proceed": True,
            "note": "No relevant rules found in knowledge store.",
        }

    # Build the LLM prompt
    rules_text = "\n\n".join(
        f"Rule {i}: {entry.content}"
        for i, entry in enumerate(relevant, 1)
    )

    prompt = (
        f"You are a compliance checker for a company.\n\n"
        f"Proposed action:\n{proposed_action}\n\n"
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
        violations = result.get("violations", [])
        safe = result.get("safe_to_proceed", True)
        return {
            "violations": violations,
            "safe_to_proceed": safe,
        }
    except Exception as exc:
        return {
            "violations": [],
            "safe_to_proceed": False,
            "note": f"Compliance check failed: {exc}. Proceed with caution.",
        }


# ---------------------------------------------------------------------------
# Tool 4 — get_company_context
# ---------------------------------------------------------------------------

@mcp.tool()
def get_company_context(task_description: str, max_tokens: int = 2000) -> str:
    """Build a company knowledge context block for an agent task.

    Returns a formatted context string that should be prepended to
    the agent's system prompt or task description.
    """
    from memory.store import load_memory_store, build_context_for_agent

    store = load_memory_store("memory/store.json")
    if not store.entries:
        return "No company context available. Run 'python run.py build-memory' first."

    return build_context_for_agent(task_description, store, max_tokens=max_tokens)


# ---------------------------------------------------------------------------
# Tool 5 — list_all_rules
# ---------------------------------------------------------------------------

@mcp.tool()
def list_all_rules(category: str | None = None) -> str:
    """List all extracted rules, optionally filtered by category.

    Returns a summary with rule text, confidence, and status.
    """
    from output.writer import load_rules

    rules = load_rules("rules/extracted/")
    if category:
        rules = [r for r in rules if r.rule_category.lower() == category.lower()]

    if not rules:
        return "No rules found."

    lines = [f"Total rules: {len(rules)}\n"]
    for rule in rules:
        lines.append(
            f"- [{rule.rule_category}] {rule.rule_text[:80]}...\n"
            f"  confidence={rule.confidence:.2f} | status={rule.verification_status} | "
            f"v={rule.version} | approved_by={rule.approved_by or '—'}\n"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Server control
# ---------------------------------------------------------------------------

def start_server(host: str = "127.0.0.1", port: int = 3333) -> None:
    """Start the KORE MCP server on the given host and port."""
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


def is_running(host: str = "127.0.0.1", port: int = 3333) -> bool:
    """Check if the MCP server is accepting connections."""
    import socket
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def get_port() -> int | None:
    """Return the configured port (or None if not running)."""
    return mcp.settings.port


# ---------------------------------------------------------------------------
# Direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KORE MCP Server")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=3333, help="Port to bind to")
    args = parser.parse_args()
    start_server(host=args.host, port=args.port)
