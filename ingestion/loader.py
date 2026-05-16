"""
Data loader supporting both local exports and live API ingestion.

Local exports are parsed directly from JSON.
Live ingestion uses the Slack SDK (installed with llama-index-readers-slack).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from llama_index.core.schema import Document

load_dotenv()


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


def _load_slack_export(
    export_path: str,
    channels: list[str] | None = None,
) -> list[Document]:
    """Load a local Slack export directory into LlamaIndex Documents."""
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


def _load_slack_live() -> list[Document]:
    """Load messages from live Slack API using the Slack SDK.

    Requires environment variables:
      SLACK_BOT_TOKEN - Bot User OAuth Token (xoxb-...)
      SLACK_CHANNEL_IDS - Comma-separated list of channel IDs (e.g. C01234567,C09876543)
      SLACK_EARLIEST_DATE - ISO date string, optional (default: 90 days ago)

    The bot must be invited to each channel and have these scopes:
      channels:history, channels:read, users:read
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "SLACK_BOT_TOKEN not set. Add it to your .env file.\n"
            "Create a Slack app at https://api.slack.com/apps, install it to your workspace,\n"
            "and copy the Bot User OAuth Token (starts with xoxb-).\n"
            "Required bot token scopes: channels:history, channels:read, users:read"
        )

    channel_ids_raw = os.getenv("SLACK_CHANNEL_IDS")
    if not channel_ids_raw:
        raise RuntimeError(
            "SLACK_CHANNEL_IDS not set. Add it to your .env file as a comma-separated list,\n"
            "e.g. SLACK_CHANNEL_IDS=C01234567,C09876543\n"
            "Find channel IDs in Slack: right-click channel name → Copy link → ID is the last segment."
        )
    channel_ids = [c.strip() for c in channel_ids_raw.split(",") if c.strip()]

    earliest_date_str = os.getenv("SLACK_EARLIEST_DATE")
    if earliest_date_str:
        earliest_dt = datetime.fromisoformat(earliest_date_str)
        oldest_ts = str(earliest_dt.timestamp())
    else:
        from datetime import timedelta
        oldest_dt = datetime.now(timezone.utc) - timedelta(days=90)
        oldest_ts = str(oldest_dt.timestamp())

    client = WebClient(token=token)

    # Fetch users for name mapping
    try:
        users_result = client.users_list()
        user_map = {
            u["id"]: u.get("name", u.get("real_name", u["id"]))
            for u in users_result.get("members", [])
        }
    except SlackApiError:
        user_map = {}

    docs: list[Document] = []

    for channel_id in channel_ids:
        try:
            # Get channel info for name
            channel_info = client.conversations_info(channel=channel_id)
            channel_name = channel_info.get("channel", {}).get("name", channel_id)
        except SlackApiError:
            channel_name = channel_id

        # Fetch messages
        next_cursor = None
        while True:
            kwargs = {
                "channel": channel_id,
                "cursor": next_cursor,
                "oldest": oldest_ts,
                "limit": 200,
            }
            result = client.conversations_history(**kwargs)
            messages = result.get("messages", [])

            for msg in messages:
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if msg.get("subtype") in ("channel_join", "channel_leave", "bot_message"):
                    continue
                if msg.get("bot_id") or msg.get("user", "").startswith("B"):
                    continue

                user_id = msg.get("user", "")
                ts = msg.get("ts", "")
                thread_ts = msg.get("thread_ts")
                user_name = user_map.get(user_id, user_id)
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)

                metadata = {
                    "source_type": "slack",
                    "channel": f"#{channel_name}",
                    "channel_id": channel_id,
                    "timestamp": dt.isoformat(),
                    "user_id": user_id,
                    "user_name": user_name,
                    "message_id": ts,
                    "thread_ts": thread_ts,
                }

                doc = Document(
                    text=text,
                    metadata=metadata,
                    id_=f"slack_{channel_name}_{ts.replace('.', '_')}",
                )
                docs.append(doc)

            if not result.get("has_more"):
                break
            next_cursor = result.get("response_metadata", {}).get("next_cursor")

    return docs


def load(
    source: Literal["slack", "github", "notion", "jira"],
    path: str | None = None,
    live: bool = False,
    **kwargs: Any,
) -> list[Document]:
    """Load data from a source.

    Parameters
    ----------
    source : {"slack", "github", "notion", "jira"}
        The data source type.
    path : str | None
        Path to local export directory. Required for local mode.
    live : bool
        If True, fetch data via live API instead of local export.
    **kwargs
        Additional arguments passed to the loader.

    Returns
    -------
    list[Document]
        LlamaIndex Document objects with metadata["source_type"] set.

    Raises
    ------
    NotImplementedError
        For sources not yet supported (github, notion, jira).
    FileNotFoundError
        If path is provided but does not exist.
    RuntimeError
        If live mode is selected but required env vars are missing.
    """
    if source == "slack":
        if live:
            return _load_slack_live()
        if path is None:
            raise ValueError("path is required for local Slack export loading")
        return _load_slack_export(path, **kwargs)

    raise NotImplementedError(f"Source '{source}' is coming soon. Only 'slack' is supported in Phase 1.")


if __name__ == "__main__":
    # Test local export
    docs = load("slack", path="data/sample")
    print(f"Loaded {len(docs)} documents from local export\n")

    for i, doc in enumerate(docs[:3]):
        print(f"--- Document {i + 1} ---")
        print(f"ID:   {doc.id_}")
        print(f"Text: {doc.text[:120]}...")
        print("Metadata:")
        for k, v in doc.metadata.items():
            print(f"  {k}: {v}")
        print()
