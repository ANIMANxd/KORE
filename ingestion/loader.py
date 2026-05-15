"""
Slack export loader.

The LlamaHub Slack reader (llama-index-readers-slack) is designed for live
API access via a Slack token. Local export directories (the standard Slack
JSON dump format) must be parsed directly. This module does that and returns
LlamaIndex Document objects.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from llama_index.core.schema import Document


def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_valid_message(msg: dict) -> bool:
    if msg.get("type") != "message":
        return False
    if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
        return False
    text = msg.get("text", "").strip()
    if not text:
        return False
    if msg.get("bot_id") or msg.get("user", "").startswith("B"):
        return False
    return True


def _parse_ts(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts))


def load_slack_export(
    export_path: str,
    channels: list[str] | None = None,
) -> list[Document]:
    """Load a local Slack export directory into LlamaIndex Documents.

    Each Document represents one message (parent or reply). Threaded replies
    are linked via metadata["thread_ts"] pointing to the parent message's ts.

    Parameters
    ----------
    export_path : str
        Path to the Slack export directory (contains channels.json, users.json,
        and subdirectories per channel).
    channels : list[str] | None
        If provided, only load these channel names. Otherwise load all.

    Returns
    -------
    list[Document]
        One Document per valid message, with metadata set per AGENTS.md spec.
    """
    root = Path(export_path)
    if not root.exists():
        raise FileNotFoundError(f"Slack export directory not found: {root}")

    users_data = _load_json(root / "users.json") if (root / "users.json").exists() else []
    user_map = {u["id"]: u.get("name", u.get("real_name", u["id"])) for u in users_data}

    channels_data = (
        _load_json(root / "channels.json") if (root / "channels.json").exists() else []
    )
    channel_map = {c["id"]: c["name"] for c in channels_data}

    docs: list[Document] = []

    for ch_dir in root.iterdir():
        if not ch_dir.is_dir():
            continue
        ch_name = ch_dir.name
        if channels and ch_name not in channels:
            continue

        channel_id = None
        for c in channels_data:
            if c["name"] == ch_name:
                channel_id = c["id"]
                break

        # Read all daily JSON files in the channel directory
        daily_files = sorted(ch_dir.glob("*.json"))
        for day_file in daily_files:
            messages = _load_json(day_file)
            if not isinstance(messages, list):
                continue

            for msg in messages:
                if not _is_valid_message(msg):
                    continue

                user_id = msg.get("user", "")
                ts = msg.get("ts", "")
                thread_ts = msg.get("thread_ts")
                text = msg["text"].strip()
                user_name = user_map.get(user_id, user_id)
                dt = _parse_ts(ts)

                metadata = {
                    "source_type": "slack",
                    "channel": f"#{ch_name}",
                    "channel_id": channel_id,
                    "timestamp": dt.isoformat() + "Z",
                    "user_id": user_id,
                    "user_name": user_name,
                    "message_id": ts,
                    "thread_ts": thread_ts,
                    "date_file": day_file.stem,
                }

                doc = Document(
                    text=text,
                    metadata=metadata,
                    id_=f"slack_{ch_name}_{ts.replace('.', '_')}",
                )
                docs.append(doc)

    return docs


if __name__ == "__main__":
    docs = load_slack_export("data/sample")
    print(f"Loaded {len(docs)} documents from data/sample\n")

    for i, doc in enumerate(docs[:3]):
        print(f"--- Document {i + 1} ---")
        print(f"ID:   {doc.id_}")
        print(f"Text: {doc.text[:120]}...")
        print("Metadata:")
        for k, v in doc.metadata.items():
            print(f"  {k}: {v}")
        print()
