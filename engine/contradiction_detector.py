"""
Contradiction detection engine.

Detects when two extracted rules conflict with each other.
Called automatically after every new rule is saved.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml

from output.schema import BusinessRule
from providers.llm import complete_json, JSONRepairFailed


@dataclass
class ContradictionReport:
    rule_id_a: str
    rule_id_b: str
    rule_text_a: str
    rule_text_b: str
    contradiction_description: str
    severity: Literal["high", "medium", "low"]
    detected_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def to_dict(self) -> dict:
        return {
            "rule_id_a": self.rule_id_a,
            "rule_id_b": self.rule_id_b,
            "rule_text_a": self.rule_text_a,
            "rule_text_b": self.rule_text_b,
            "contradiction_description": self.contradiction_description,
            "severity": self.severity,
            "detected_at": self.detected_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContradictionReport":
        return cls(
            rule_id_a=data["rule_id_a"],
            rule_id_b=data["rule_id_b"],
            rule_text_a=data["rule_text_a"],
            rule_text_b=data["rule_text_b"],
            contradiction_description=data["contradiction_description"],
            severity=data["severity"],
            detected_at=datetime.fromisoformat(data["detected_at"]),
        )


_CONTRADICTION_SCHEMA = {
    "contradicts": bool,
    "description": str,
    "severity": str,
}

_SYSTEM_PROMPT = (
    "You are a compliance checker. Given two business rules, determine if following one "
    "would violate the other. Consider explicit conflicts, opposite requirements, or "
    "conditions that cannot both be true at the same time. Respond in JSON only."
)


def _build_contradiction_prompt(rule_a: BusinessRule, rule_b: BusinessRule) -> list[dict[str, str]]:
    return [
        {
            "role": "user",
            "content": (
                f"Rule A:\n{rule_a.rule_text}\n\n"
                f"Rule B:\n{rule_b.rule_text}\n\n"
                "Analyze whether these two rules contradict each other. "
                'Return JSON with fields: {"contradicts": bool, "description": str|null, "severity": "high|medium|low"}. '
                "Set contradicts=true only if following one rule would force violating the other."
            ),
        }
    ]


def find_contradictions(
    new_rule: BusinessRule,
    existing_rules: list[BusinessRule],
) -> list[ContradictionReport]:
    """Compare a new rule against existing rules in the same category.

    For each existing rule with the same ``rule_category``, an LLM is asked
    whether the two rules contradict. If the LLM responds that they do, a
    ``ContradictionReport`` is generated.

    Parameters
    ----------
    new_rule : BusinessRule
        The freshly extracted rule to evaluate.
    existing_rules : list[BusinessRule]
        All previously extracted rules.

    Returns
    -------
    list[ContradictionReport]
        Non-empty when one or more contradictions were detected.
    """
    reports: list[ContradictionReport] = []

    candidates = [r for r in existing_rules if r.rule_category == new_rule.rule_category]

    for existing in candidates:
        if existing.rule_id == new_rule.rule_id:
            continue

        messages = _build_contradiction_prompt(new_rule, existing)

        try:
            parsed, _ = complete_json(
                messages=messages,
                system_prompt=_SYSTEM_PROMPT,
                expected_schema=_CONTRADICTION_SCHEMA,
            )
        except JSONRepairFailed as exc:
            # If the LLM cannot produce valid JSON, assume no contradiction
            # and continue gracefully — we never block the pipeline.
            print(
                f"[KORE] WARN: Contradiction check JSON repair failed for "
                f"{new_rule.rule_id[:8]} vs {existing.rule_id[:8]}. "
                f"Assuming no contradiction. Raw: {exc.raw_output[:200]}..."
            )
            continue

        contradicts = bool(parsed.get("contradicts", False))
        if not contradicts:
            continue

        description = str(parsed.get("description") or "Contradiction detected but no description provided.")
        severity = str(parsed.get("severity", "medium")).lower()
        if severity not in ("high", "medium", "low"):
            severity = "medium"

        report = ContradictionReport(
            rule_id_a=new_rule.rule_id,
            rule_id_b=existing.rule_id,
            rule_text_a=new_rule.rule_text,
            rule_text_b=existing.rule_text,
            contradiction_description=description,
            severity=severity,  # type: ignore[arg-type]
        )
        reports.append(report)

    return reports


def save_contradiction_report(
    report: ContradictionReport,
    output_dir: str = "rules/contradictions",
) -> str:
    """Persist a single contradiction report to YAML.

    Returns
    -------
    str
        Path to the written file.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    ts = report.detected_at.strftime("%Y%m%d_%H%M%S")
    short_a = report.rule_id_a[:8]
    short_b = report.rule_id_b[:8]
    filename = f"{ts}_contradiction_{short_a}_vs_{short_b}.yaml"
    file_path = out_path / filename

    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            report.to_dict(),
            stream=f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )

    return str(file_path)


def load_contradiction_reports(
    directory: str = "rules/contradictions",
) -> list[ContradictionReport]:
    """Load all persisted contradiction reports.

    Invalid files are logged and skipped.

    Returns
    -------
    list[ContradictionReport]
        Reports in filename-sorted order.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return []

    reports: list[ContradictionReport] = []
    yaml_files = sorted(dir_path.glob("*.yaml"))

    for file_path in yaml_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if raw is None:
                print(f"[KORE] WARN: Empty contradiction report skipped: {file_path.name}")
                continue
            reports.append(ContradictionReport.from_dict(raw))
        except KeyError as exc:
            print(f"[KORE] WARN: Malformed contradiction report ({file_path.name}): missing {exc}")
        except Exception as exc:
            print(f"[KORE] WARN: Failed to load contradiction report {file_path.name}: {exc}")

    return reports
