"""
YAML file writer and loader for extracted business rules.

Handles persistence, validation, and updates of BusinessRule objects.
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from output.schema import BusinessRule


_DEFAULT_DIR = "rules/extracted"


def _make_filename(rule: BusinessRule) -> str:
    """Generate a filename from rule metadata."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    category = rule.rule_category.replace(" ", "_").lower()
    short_id = rule.rule_id[:8]
    return f"{ts}_{category}_{short_id}.yaml"


def save_rule(rule: BusinessRule, output_dir: str = _DEFAULT_DIR) -> str:
    """Save a BusinessRule to a YAML file.

    Parameters
    ----------
    rule : BusinessRule
        The validated rule to persist.
    output_dir : str
        Directory to write the file into.

    Returns
    -------
    str
        Full path to the written file.
    """
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    filename = _make_filename(rule)
    file_path = out_path / filename

    data = rule.model_dump(mode="json")
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            data,
            stream=f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )

    return str(file_path)


def load_rules(directory: str = _DEFAULT_DIR) -> list[BusinessRule]:
    """Load and validate all YAML rule files from a directory.

    Invalid files are logged and skipped.

    Returns
    -------
    list[BusinessRule]
        Valid rules in filename-sorted order.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        return []

    valid_rules: list[BusinessRule] = []
    yaml_files = sorted(dir_path.glob("*.yaml"))

    for file_path in yaml_files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if raw is None:
                print(f"[KORE] WARN: Empty YAML file skipped: {file_path.name}")
                continue
            rule = BusinessRule(**raw)
            valid_rules.append(rule)
        except ValidationError as exc:
            print(f"[KORE] WARN: Invalid rule skipped ({file_path.name}): {exc.errors()[0]['msg']}")
        except Exception as exc:
            print(f"[KORE] WARN: Failed to load {file_path.name}: {exc}")

    return valid_rules


def update_rule(
    rule_id: str,
    updates: dict[str, Any],
    directory: str = _DEFAULT_DIR,
) -> BusinessRule:
    """Find a rule by ID, apply updates, increment version, and overwrite file.

    Parameters
    ----------
    rule_id : str
        Full UUID of the rule to update.
    updates : dict
        Fields to update (e.g. {"approved_by": "sarah"}).
    directory : str
        Directory containing the YAML files.

    Returns
    -------
    BusinessRule
        The updated rule.

    Raises
    ------
    FileNotFoundError
        If no rule with the given ID exists.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        raise FileNotFoundError(f"Rules directory not found: {directory}")

    # Find the file containing the rule
    target_file: Path | None = None
    target_rule: BusinessRule | None = None

    for file_path in dir_path.glob("*.yaml"):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if raw and raw.get("rule_id") == rule_id:
                target_file = file_path
                target_rule = BusinessRule(**raw)
                break
        except Exception:
            continue

    if target_rule is None or target_file is None:
        raise FileNotFoundError(f"Rule not found with ID: {rule_id}")

    # Build updated data
    current_data = target_rule.model_dump(mode="json")
    current_data.update(updates)
    current_data["version"] = current_data.get("version", 1) + 1

    updated_rule = BusinessRule(**current_data)

    # Overwrite file
    with open(target_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            updated_rule.model_dump(mode="json"),
            stream=f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
        )

    return updated_rule
