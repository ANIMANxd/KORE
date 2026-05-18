"""
Skills export writer.

Exports verified, human-approved rules to a machine-readable `skills.json`
format that AI agents can consume directly.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from output.schema import BusinessRule
from output.writer import load_rules


def _build_source_string(ref: dict) -> str:
    """Build a human-readable source citation from a SourceRef dict."""
    channel = ref.get("channel", "")
    timestamp = ref.get("timestamp", "")
    parts = []
    if channel:
        prefix = "#" if not channel.startswith("#") else ""
        parts.append(f"{prefix}{channel}")
    if timestamp:
        parts.append(f"@ {timestamp}")
    return " ".join(parts) if parts else ref.get("chunk_id", "unknown")


def _rule_to_skill(rule: BusinessRule) -> dict:
    """Convert a single BusinessRule to the skills.json skill dict."""
    constraints = []
    if rule.ambiguity_notes:
        constraints.append(f"Only applies when: {rule.ambiguity_notes}")

    sources = [_build_source_string(ref.model_dump(mode="json")) for ref in rule.source_refs]

    return {
        "id": rule.rule_id,
        "name": f"{rule.rule_category}_{rule.rule_id[:6]}",
        "description": rule.rule_text,
        "category": rule.rule_category,
        "confidence": rule.confidence,
        "constraints": constraints,
        "sources": sources,
        "approved": True,
        "version": rule.version,
    }


def export_skills_json(
    rules_dir: str = "rules/extracted/",
    output_path: str = "rules/skills.json",
) -> str:
    """Export verified, approved rules to skills.json.

    Only rules with ``verification_status == "verified"`` and
    ``approved_by is not None`` are included.

    Parameters
    ----------
    rules_dir : str
        Directory containing YAML rule files.
    output_path : str
        Destination JSON file path.

    Returns
    -------
    str
        Path to the written file.
    """
    rules = load_rules(directory=rules_dir)

    approved_rules = [
        r for r in rules
        if r.verification_status == "verified" and r.approved_by is not None
    ]

    skills = [_rule_to_skill(r) for r in approved_rules]

    payload = {
        "version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "KORE",
        "total_skills": len(skills),
        "skills": skills,
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    return str(out_path)
