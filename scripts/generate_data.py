#!/usr/bin/env python3
"""
Usage:
    python generate_data.py --target-size-gb 0.25 --api-base http://127.0.0.1:8080/v1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import textwrap
import time
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Connector & Category Configuration
# ---------------------------------------------------------------------------
CONNECTORS: list[str] = [
    "slack", "github", "notion", "jira", "teams",
    "confluence", "linear", "zendesk", "google_drive", "documents"
]

RULE_CATEGORIES: list[str] = [
    "pricing", "operations", "escalation", "compliance",
    "customer_success", "engineering", "hr", "other"
]

# ---------------------------------------------------------------------------
# ANSI Logging & Formatting Setup
# ---------------------------------------------------------------------------
class ColoredFormatter(logging.Formatter):
    """Zero-dependency terminal coloring formatter for clean logging."""
    COLORS = {
        logging.DEBUG: "\033[36m",    # Cyan
        logging.INFO: "\033[32m",     # Green
        logging.WARNING: "\033[33m",  # Yellow
        logging.ERROR: "\033[31m",    # Red
        logging.CRITICAL: "\033[1;31m" # Bold Red
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        if color:
            record.levelname = f"{color}{record.levelname}{self.RESET}"
            record.msg = f"{color}{record.msg}{self.RESET}"
        return super().format(record)

def setup_logger(log_file: Path) -> logging.Logger:
    """Configures structured dual file and console logging outputs."""
    logger = logging.getLogger("KoreDataGen")
    logger.setLevel(logging.DEBUG)
    
    # Avoid duplicate handlers if logger re-initialized
    if logger.handlers:
        return logger

    # Console Handler (clean structured format)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = ColoredFormatter(
        "%(asctime)s [%(levelname)s] %(message)s", 
        datefmt="%H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File Handler (persistent debugging trace)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger

logger = logging.getLogger("KoreDataGen")

# ---------------------------------------------------------------------------
# Standard Library HTTP Client
# ---------------------------------------------------------------------------
class LocalLLMClient:
    """Zero-dependency wrapper around an OpenAI-compatible API endpoint."""

    def __init__(self, base_url: str, api_key: str | None = None, model: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "no-key-required"
        self.model = model or "gemma-4-26b-it"

    def chat(self, messages: list[dict[str, str]], temperature: float = 0.7, max_tokens: int = 2048) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        
        url = f"{self.base_url}/chat/completions"
        data_bytes = json.dumps(payload).encode("utf-8")
        
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=120) as response:
            resp_body = response.read().decode("utf-8")
            data = json.loads(resp_body)
            choices = data.get("choices", [])
            if not choices:
                return ""
            return str(choices[0].get("message", {}).get("content", ""))

# ---------------------------------------------------------------------------
# Robust JSON Parsing & Cleanup
# ---------------------------------------------------------------------------
def extract_robust_json(raw_text: str) -> dict[str, Any]:
    """Gracefully extracts JSON objects from LLM markdown code enclosures."""
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
        
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end+1]
        
    return json.loads(text)

# ---------------------------------------------------------------------------
# Phase 1: Ground-truth Rule Generation
# ---------------------------------------------------------------------------
SINGLE_RULE_PROMPT = textwrap.dedent(
    """\
    You are a corporate policy analyst. Generate ONE distinct, realistic
    business rule / SLA / protocol for the category: {category}.

    Return **only** a valid JSON object with these keys:
    - rule_text (1-3 sentences, plain English, actionable)
    - rule_category (must be "{category}")
    - confidence (float 0.85-0.99)

    No markdown, no commentary, no surrounding text.
    """
)

def generate_single_rule(client: LocalLLMClient, category: str, rule_id: int, max_retries: int = 3) -> dict[str, Any]:
    """Generates an isolated rule with exponential backoff on client failure."""
    prompt = SINGLE_RULE_PROMPT.format(category=category)
    delay = 2.0
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.debug(f"Requesting rule_id={rule_id} for category={category} (Attempt {attempt}/{max_retries})")
            raw = client.chat([{"role": "user", "content": prompt}], temperature=0.7, max_tokens=1024)
            parsed = extract_robust_json(raw)
            parsed["rule_id"] = rule_id
            parsed["rule_category"] = category
            parsed["created_at"] = datetime.now(timezone.utc).isoformat()
            return parsed
        except Exception as exc:
            logger.warning(f"Failed to generate rule_id={rule_id}: {exc}")
            if attempt == max_retries:
                raise
            time.sleep(delay)
            delay *= 2.0

# ---------------------------------------------------------------------------
# Phase 2: Platform Noise Re-expression
# ---------------------------------------------------------------------------
PLATFORM_STYLE_PROMPTS: dict[str, str] = {
    "slack": "Re-express this corporate rule as a casual, messy Slack message chain with emojis and typos: {rule_text}",
    "github": "Re-express this corporate rule as a technical GitHub pull request code-review comment thread: {rule_text}",
    "notion": "Re-express this corporate rule as a section of a Notion workspace wiki directory: {rule_text}",
    "jira": "Re-express this corporate rule as an enterprise Jira issue description with technical criteria: {rule_text}",
    "teams": "Re-express this corporate rule as a business-casual corporate thread in Microsoft Teams: {rule_text}",
    "confluence": "Re-express this corporate rule as an official Confluence wiki page excerpt with headings: {rule_text}",
    "linear": "Re-express this corporate rule as a high-velocity Linear task ticket description: {rule_text}",
    "zendesk": "Re-express this corporate rule as a customer success Zendesk template support macro: {rule_text}",
    "google_drive": "Re-express this corporate rule as a formal policy document excerpt stored on Google Drive: {rule_text}",
    "documents": "Re-express this corporate rule as an official handbook guidelines clause for a PDF manual: {rule_text}",
}

def generate_platform_noise(client: LocalLLMClient, rule: dict[str, Any], connector: str, max_retries: int = 3) -> str:
    """Asks the LLM to rewrite a canonical rule in the voice of a connector."""
    prompt = PLATFORM_STYLE_PROMPTS[connector].format(rule_text=rule["rule_text"])
    delay = 2.0
    
    for attempt in range(1, max_retries + 1):
        try:
            logger.debug(f"Generating style noise for rule_id={rule['rule_id']} / connector={connector} (Attempt {attempt}/{max_retries})")
            return client.chat([{"role": "user", "content": prompt}], temperature=0.8, max_tokens=2048)
        except Exception as exc:
            logger.warning(f"Style generation error on {connector}: {exc}")
            if attempt == max_retries:
                raise
            time.sleep(delay)
            delay *= 2.0

# ---------------------------------------------------------------------------
# Phase 3: Metadata Ingestion Matching
# ---------------------------------------------------------------------------
def _random_timestamp() -> str:
    now = datetime.now(timezone.utc)
    delta = timedelta(seconds=random.randint(0, 31536000))
    return (now - delta).isoformat()

def _faker_word(length: int = 8) -> str:
    import string
    return "".join(random.choices(string.ascii_lowercase, k=length))

def inject_metadata(rule: dict[str, Any], connector: str, text: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_type": connector,
        "rule_id": rule["rule_id"],
        "rule_category": rule["rule_category"],
        "confidence": rule["confidence"],
        "ground_truth_text": rule["rule_text"],
    }

    if connector == "slack":
        channel = random.choice(["#general", "#engineering", "#ops-alerts", "#compliance"])
        ts = _random_timestamp()
        base.update({"channel": channel, "timestamp": ts, "user": random.choice(["alice", "bob"]), "thread_ts": ts})
    elif connector == "github":
        repo = random.choice(["acme/webapp", "acme/api", "acme/infra", "acme/docs"])
        pr_num = random.randint(100, 999)
        base.update({
            "repo": repo, 
            "pr_number": pr_num, 
            "user": "reviewer_bot",
            "url": f"[https://github.com/](https://github.com/){repo}/pull/{pr_num}"
        })
    elif connector == "notion":
        base.update({
            "page_id": f"{random.randint(0x10000000, 0xFFFFFFFF):08x}", 
            "title": f"Security Manual — {rule['rule_category'].title()}",
            "last_modified": _random_timestamp()
        })
    elif connector == "jira":
        base.update({
            "issue_key": f"CORE-{random.randint(100, 999)}", 
            "issue_type": random.choice(["Task", "Story", "Bug"]),
            "status": "In Progress", 
            "assignee": "ops_engineer",
            "created": _random_timestamp(),
            "updated": _random_timestamp()
        })
    elif connector == "teams":
        base.update({
            "team_name": "SRE Group", 
            "channel_name": "Alert-Logs", 
            "conversation_id": _faker_word(12),
            "timestamp": _random_timestamp(),
            "user": random.choice(["alice@acme.com", "bob@acme.com"])
        })
    elif connector == "confluence":
        base.update({
            "page_id": str(random.randint(100000, 999999)), 
            "space_key": "SEC", 
            "title": f"SLA Policies — {rule['rule_category'].title()}",
            "last_modified": _random_timestamp()
        })
    elif connector == "linear":
        base.update({
            "issue_id": f"INFRA-{random.randint(100, 999)}", 
            "team_key": "INFRA", 
            "state": "done",
            "priority": random.randint(0, 4),
            "created_at": _random_timestamp(),
            "updated_at": _random_timestamp()
        })
    elif connector == "zendesk":
        doc_type = random.choice(["macro", "ticket", "article"])
        base.update({
            "doc_type": doc_type,
            "title": f"{rule['rule_category'].title()} guidance",
            "status": "active",
            "last_modified": _random_timestamp(),
        })
        if doc_type == "macro":
            base["macro_id"] = str(random.randint(100000, 999999))
            base["usage_7d"] = random.randint(0, 500)
        elif doc_type == "ticket":
            base["ticket_id"] = str(random.randint(100000, 999999))
            base["assignee_id"] = str(random.randint(100000, 999999))
            base["comments"] = [
                {
                    "text": text.split("\n")[0] if text else "",
                    "author_id": str(random.randint(100000, 999999)),
                }
            ]
        elif doc_type == "article":
            base["article_id"] = str(random.randint(100000, 999999))
    elif connector == "google_drive":
        base.update({
            "file_id": _faker_word(20), 
            "file_name": f"{rule['rule_category']}_sla_guidelines.md", 
            "mime_type": "text/markdown",
            "last_modified": _random_timestamp()
        })
    elif connector == "documents":
        ext = random.choice([".md", ".txt", ".pdf"])
        base.update({
            "file_type": ext.lstrip("."), 
            "file_name": f"compliance_handbook_{rule['rule_category']}{ext}", 
            "file_path": f"/data/synthetic/documents/compliance_handbook_{rule['rule_category']}{ext}",
            "file_size_kb": round(random.uniform(1.0, 500.0), 2),
            "last_modified": _random_timestamp(),
            "page_count": random.randint(1, 10) if ext == ".pdf" else 1,
            "likely_explicit_policy": True
        })

    return base

# ---------------------------------------------------------------------------
# Crash Recovery & State Synchronization Engine
# ---------------------------------------------------------------------------
def synchronize_and_resume(out_root: Path, ground_truth_path: Path) -> tuple[int, list[dict[str, Any]], int]:
    """
    Scans files on startup to find incomplete or unbalanced database entries.
    Truncates trailing connector lines to ensure absolute transactional symmetry,
    re-syncs the ground_truth.json file, and returns the next rule index to resume.
    """
    logger.info("Initializing Transactional Consistency Check...")
    
    # 1. Count physical lines in each connector JSONL file
    counts: dict[str, int] = {}
    for conn in CONNECTORS:
        p = out_root / conn / f"{conn}_synthetic.jsonl"
        if not p.exists():
            counts[conn] = 0
            continue
            
        with p.open("r", encoding="utf-8") as f:
            lines = sum(1 for _ in f)
        counts[conn] = lines
        logger.debug(f"Detected {lines} raw entries in {p.name}")

    # Find the minimum synchronized transactions across all active paths
    sync_count = min(counts.values()) if counts else 0
    
    # 2. Check and parse ground truth rule definitions
    gt_rules: list[dict[str, Any]] = []
    if ground_truth_path.exists():
        try:
            raw_gt = json.loads(ground_truth_path.read_text(encoding="utf-8"))
            if isinstance(raw_gt, list):
                gt_rules = raw_gt[:sync_count]
        except Exception as exc:
            logger.error(f"Ground truth configuration is corrupted: {exc}. Rebuilding from zero.")

    # Override target sync if ground truth entries are fewer than log lines
    sync_count = min(sync_count, len(gt_rules))
    gt_rules = gt_rules[:sync_count]

    # 3. Trim dangling logs to restore perfect line symmetry across connectors
    for conn in CONNECTORS:
        p = out_root / conn / f"{conn}_synthetic.jsonl"
        if not p.exists():
            if sync_count > 0:
                # Recreate file to write empty lines up to sync_count
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
            continue

        if counts[conn] > sync_count:
            logger.warning(f"Unbalanced write detected on {conn} ({counts[conn]} vs {sync_count}). Restoring...")
            
            # Read first sync_count lines
            with p.open("r", encoding="utf-8") as f:
                aligned_lines = [f.readline() for _ in range(sync_count)]
                
            # Perform atomic file swap override
            tmp_p = p.with_suffix(".tmp")
            with tmp_p.open("w", encoding="utf-8") as f:
                f.writelines(aligned_lines)
            os.replace(tmp_p, p)
            logger.info(f"Successfully truncated {p.name} to {sync_count} records.")

    # 4. Save synced ground truth manifest atomically
    if ground_truth_path.exists() or sync_count > 0:
        tmp_gt = ground_truth_path.with_suffix(".tmp")
        tmp_gt.write_text(json.dumps(gt_rules, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_gt, ground_truth_path)

    # 5. Measure total physical byte footprint of synchronized files
    bytes_verified = 0
    if ground_truth_path.exists():
        bytes_verified += ground_truth_path.stat().st_size
    for conn in CONNECTORS:
        p = out_root / conn / f"{conn}_synthetic.jsonl"
        if p.exists():
            bytes_verified += p.stat().st_size

    next_idx = sync_count + 1
    logger.info(f"System Check complete. Synchronized ruleset: {sync_count}. Verified Byte Count: {bytes_verified / (1024**2):.2f} MB.")
    logger.info(f"Generation will resume starting from rule_id={next_idx}.")
    
    return next_idx, gt_rules, bytes_verified

# ---------------------------------------------------------------------------
# Main Execution Workflow
# ---------------------------------------------------------------------------
def run(target_size_gb: float, api_base: str, api_key: str | None, model: str | None, throttle_ms: int, seed: int | None) -> None:
    if seed is not None:
        random.seed(seed)

    client = LocalLLMClient(base_url=api_base, api_key=api_key, model=model)
    out_root = Path("data/synthetic")
    out_root.mkdir(parents=True, exist_ok=True)
    ground_truth_path = out_root / "ground_truth.json"

    # Core system restore check
    rule_idx, all_ground_truth, total_bytes_written = synchronize_and_resume(out_root, ground_truth_path)

    total_bytes_target = int(target_size_gb * 1024 * 1024 * 1024)
    start_time = time.time()

    if total_bytes_written >= total_bytes_target:
        logger.info("Target data file size already satisfied. Exiting.")
        return

    # Open persistent stream writers
    writers: dict[str, Any] = {}
    for conn in CONNECTORS:
        p = out_root / conn / f"{conn}_synthetic.jsonl"
        # Open in append mode 'a' so we can resume gracefully
        writers[conn] = p.open("a", encoding="utf-8")

    logger.info("Connecting stream handlers... Running local LLM engine.")

    try:
        while total_bytes_written < total_bytes_target:
            category = RULE_CATEGORIES[(rule_idx - 1) % len(RULE_CATEGORIES)]
            
            # --- TRANSACTION BOUNDARY START ---
            logger.info(f"Starting Transaction: rule_id={rule_idx} (Category: {category})")
            
            # Phase 1: Atomic Ground Truth Generation
            try:
                rule = generate_single_rule(client, category, rule_idx)
            except Exception as e:
                logger.error(f"Rule generation drop on rule_id={rule_idx}: {e}")
                # Inject structural fallback to prevent stopping execution mid-run
                rule = {
                    "rule_id": rule_idx,
                    "rule_text": f"Strict internal protocol regarding operational parameters, safety constraints, and {category} monitoring.",
                    "rule_category": category,
                    "confidence": 0.85,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            
            # Phase 2: Platform Variation Generation (Stage in Buffer)
            connector_payloads: dict[str, str] = {}
            for conn in CONNECTORS:
                try:
                    noise_text = generate_platform_noise(client, rule, conn)
                    fallback = False
                except Exception as e:
                    logger.warning(f"Platform variation generation dropped on {conn}: {e}. Applying fallback payload.")
                    noise_text = f"[{conn.upper()} COMPLIANCE DIRECTIVE] System Notice:\n{rule['rule_text']}"
                    fallback = True
                
                metadata = inject_metadata(rule, conn, noise_text)
                if fallback:
                    metadata["synthetic_fallback_applied"] = True

                record = {
                    "text": noise_text,
                    "metadata": metadata,
                    "id": f"{conn}_rule{rule_idx}_{_faker_word(6)}",
                }
                connector_payloads[conn] = json.dumps(record, ensure_ascii=False) + "\n"

            # Phase 3: Synchronized Transaction Committal (Write & Flush safely)
            # This ensures we append the lines only when all generations are completely done
            current_tx_bytes = 0
            for conn, payload in connector_payloads.items():
                writers[conn].write(payload)
                writers[conn].flush() # Instant flush to keep lines atomic
                current_tx_bytes += len(payload.encode("utf-8"))

            # Append the rule definition to the running manifest list
            all_ground_truth.append(rule)
            
            # Perform atomic save swap on ground_truth config file
            tmp_gt = ground_truth_path.with_suffix(".tmp")
            tmp_gt.write_text(json.dumps(all_ground_truth, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_gt, ground_truth_path)
            
            # Track overall bytes
            total_bytes_written += current_tx_bytes
            elapsed = time.time() - start_time
            written_mb = total_bytes_written / (1024**2)
            speed_mb = (written_mb - (total_bytes_written - current_tx_bytes) / (1024**2)) / (time.time() - (time.time() - throttle_ms/1000.0 - 1)) if rule_idx > 1 else 0
            
            logger.info(
                f"Transaction rule_id={rule_idx} Committed. "
                f"Total size: {written_mb:.2f} MB / {target_size_gb*1024:.1f} MB ({(written_mb/(target_size_gb*1024))*100:.1f}%)"
            )

            if throttle_ms > 0:
                time.sleep(throttle_ms / 1000.0)

            rule_idx += 1
            # --- TRANSACTION BOUNDARY END ---

    except KeyboardInterrupt:
        logger.warning("Interruption signal captured (Ctrl+C). Saving database state safely and exiting.")
    except Exception as exc:
        logger.critical(f"Critical execution error: {exc}")
        logger.critical(traceback.format_exc())
    finally:
        # Gracefully shut down file handles
        logger.info("Closing file descriptors and clearing thread context.")
        for conn, handler in writers.items():
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
        
        # Sync the absolute ground truth list to the byte size of ground_truth.json
        if ground_truth_path.exists():
            total_bytes_written += ground_truth_path.stat().st_size
        
        logger.info(f"Stream generation stopped. Final disk footprint: {total_bytes_written / (1024**2):.2f} MB.")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-Dependency Continuous Data Generator.")
    parser.add_argument("--target-size-gb", type=float, default=0.01, help="Target total output size in GB.")
    parser.add_argument("--api-base", type=str, default=os.getenv("KORE_SYNTH_API_BASE", "[http://127.0.0.1:8080/v1](http://127.0.0.1:8080/v1)"))
    parser.add_argument("--api-key", type=str, default=os.getenv("KORE_SYNTH_API_KEY", None))
    parser.add_argument("--model", type=str, default=os.getenv("KORE_SYNTH_MODEL", "gemma-4-26b-it"))
    parser.add_argument("--throttle-ms", type=int, default=0, help="Optional write throttling in ms.")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args(argv)

    # Core directory and file logging initialization
    log_file = Path("generation.log")
    setup_logger(log_file)

    try:
        run(
            target_size_gb=args.target_size_gb,
            api_base=args.api_base,
            api_key=args.api_key,
            model=args.model,
            throttle_ms=args.throttle_ms,
            seed=args.seed,
        )
    except Exception as exc:
        logger.critical(f"Fatal script execution crash: {exc}")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())