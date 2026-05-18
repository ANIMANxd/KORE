"""
Expiry detection engine.

Identifies rules whose source references have gone stale.
A rule is considered expired when its most recent source timestamp
is older than a configurable threshold (default 90 days).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from output.schema import BusinessRule
from output.writer import load_rules


@dataclass
class ExpiryReport:
    rule_id: str
    rule_text: str
    rule_category: str
    last_source_date: datetime
    days_since_referenced: int
    recommendation: str


def _parse_source_timestamp(ts: str) -> datetime | None:
    """Parse a source-ref timestamp string into a timezone-aware datetime.

    Supports ISO-8601 and a handful of common Slack/Notion/GitHub/Jira formats.
    Returns ``None`` if the string cannot be parsed.
    """
    if not ts or not ts.strip():
        return None

    ts = ts.strip()

    # ISO-8601 with timezone (e.g. 2024-01-01T10:00:00Z or +00:00)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(ts, fmt)
            # If no timezone info, assume UTC
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            continue

    # Python's fromisoformat handles some edge cases strptime does not
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass

    return None


def check_rule_expiry(
    rule: BusinessRule,
    days_threshold: int = 90,
    reference_date: datetime | None = None,
) -> ExpiryReport | None:
    """Check whether a single rule has gone stale.

    Parameters
    ----------
    rule : BusinessRule
        The rule to evaluate.
    days_threshold : int
        Number of days after which a rule is considered expired.
    reference_date : datetime | None
        The date to compare against. Defaults to UTC now.

    Returns
    -------
    ExpiryReport | None
        ``ExpiryReport`` if the rule's newest source is older than the
        threshold, otherwise ``None``.
    """
    if not rule.source_refs:
        return None

    latest: datetime | None = None
    for ref in rule.source_refs:
        parsed = _parse_source_timestamp(ref.timestamp)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed

    if latest is None:
        # No parseable timestamps — we cannot determine freshness.
        return None

    now = reference_date or datetime.now(timezone.utc)
    delta = now - latest
    days_old = delta.days

    if days_old <= days_threshold:
        return None

    excerpt = rule.rule_text[:80] + "..." if len(rule.rule_text) > 80 else rule.rule_text
    recommendation = (
        f"Rule last referenced {days_old} days ago. "
        "Consider re-extracting from current data or marking as deprecated."
    )

    return ExpiryReport(
        rule_id=rule.rule_id,
        rule_text=excerpt,
        rule_category=rule.rule_category,
        last_source_date=latest,
        days_since_referenced=days_old,
        recommendation=recommendation,
    )


def scan_all_rules(
    rules_dir: str = "rules/extracted/",
    days_threshold: int = 90,
    reference_date: datetime | None = None,
) -> list[ExpiryReport]:
    """Scan every rule in a directory for staleness.

    Parameters
    ----------
    rules_dir : str
        Directory containing YAML rule files.
    days_threshold : int
        Staleness threshold in days.
    reference_date : datetime | None
        The date to compare against. Defaults to UTC now.

    Returns
    -------
    list[ExpiryReport]
        Expired rules sorted by ``days_since_referenced`` descending.
    """
    rules = load_rules(directory=rules_dir)
    reports: list[ExpiryReport] = []

    for rule in rules:
        report = check_rule_expiry(rule, days_threshold, reference_date)
        if report is not None:
            reports.append(report)

    reports.sort(key=lambda r: r.days_since_referenced, reverse=True)
    return reports
