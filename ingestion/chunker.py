"""
Source-aware chunking strategies.

The chunker reads Document.metadata["source_type"] and routes to the correct
strategy. Never apply one strategy to all sources.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document, TextNode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_text_node(text: str, metadata: dict, id_prefix: str, index: int, total: int) -> TextNode:
    meta = {
        **metadata,
        "chunk_index": index,
        "total_chunks": total,
    }
    return TextNode(text=text, metadata=meta, id_=f"{id_prefix}_chunk{index}")


def _parse_iso_ts(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# SlackChunker
# ---------------------------------------------------------------------------

class SlackChunker:
    """Group Slack messages into 15-minute conversation windows."""

    WINDOW_MINUTES = 15

    def chunk(self, documents: list[Document]) -> list[TextNode]:
        if not documents:
            return []

        # Sort by timestamp
        sorted_docs = sorted(
            documents,
            key=lambda d: _parse_iso_ts(d.metadata.get("timestamp", "1970-01-01T00:00:00Z")),
        )

        windows: list[list[Document]] = []
        current_window: list[Document] = [sorted_docs[0]]

        for doc in sorted_docs[1:]:
            last_ts = _parse_iso_ts(current_window[-1].metadata.get("timestamp", "1970-01-01T00:00:00Z"))
            this_ts = _parse_iso_ts(doc.metadata.get("timestamp", "1970-01-01T00:00:00Z"))
            if (this_ts - last_ts) <= timedelta(minutes=self.WINDOW_MINUTES):
                current_window.append(doc)
            else:
                windows.append(current_window)
                current_window = [doc]
        windows.append(current_window)

        nodes: list[TextNode] = []
        for win_idx, window in enumerate(windows):
            texts: list[str] = []
            participant_ids: set[str] = set()
            channel = window[0].metadata.get("channel", "unknown")
            start_ts = window[0].metadata.get("timestamp", "")
            end_ts = window[-1].metadata.get("timestamp", "")

            for doc in window:
                user = doc.metadata.get("user_name", doc.metadata.get("user_id", "unknown"))
                participant_ids.add(doc.metadata.get("user_id", "unknown"))
                ts_short = doc.metadata.get("timestamp", "")[11:16]  # HH:MM
                texts.append(f"[{ts_short}] {user}: {doc.text}")

            combined = "\n".join(texts)
            meta = {
                "source_type": "slack",
                "channel": channel,
                "window_start": start_ts,
                "window_end": end_ts,
                "participant_ids": sorted(participant_ids),
                "message_count": len(window),
            }
            node = _make_text_node(combined, meta, f"slack_win{win_idx}", 0, 1)
            nodes.append(node)

        # Re-number with proper totals per channel window
        for i, node in enumerate(nodes):
            node.metadata["chunk_index"] = i
            node.metadata["total_chunks"] = len(nodes)
            node.id_ = f"slack_chunk{i}"

        return nodes


# ---------------------------------------------------------------------------
# GitHubChunker
# ---------------------------------------------------------------------------

class GitHubChunker:
    """Markdown + diff-aware chunking for GitHub PRs / issues / markdown docs."""

    def chunk(self, documents: list[Document]) -> list[TextNode]:
        nodes: list[TextNode] = []
        for doc in documents:
            chunks = self._chunk_markdown(doc.text)
            parent_heading = doc.metadata.get("title", "")
            for i, chunk_text in enumerate(chunks):
                meta = {
                    **doc.metadata,
                    "source_type": "github",
                    "parent_heading": parent_heading,
                }
                nodes.append(_make_text_node(chunk_text, meta, f"gh_{doc.id_}", i, len(chunks)))
        return nodes

    def _chunk_markdown(self, text: str) -> list[str]:
        lines = text.splitlines(keepends=True)
        chunks: list[list[str]] = []
        current: list[str] = []
        in_code_block = False
        code_fence = ""

        for line in lines:
            stripped = line.strip()

            # Track code blocks
            if stripped.startswith("```"):
                if not in_code_block:
                    in_code_block = True
                    code_fence = stripped
                    if current:
                        chunks.append(current)
                        current = []
                    current.append(line)
                    continue
                else:
                    in_code_block = False
                    current.append(line)
                    chunks.append(current)
                    current = []
                    continue

            if in_code_block:
                current.append(line)
                continue

            # Heading boundary
            if re.match(r"^#{2,3}\s+", stripped):
                if current:
                    chunks.append(current)
                current = [line]
                continue

            current.append(line)

        if current:
            chunks.append(current)

        # Join and strip
        result = []
        for chunk_lines in chunks:
            chunk_text = "".join(chunk_lines).strip()
            if chunk_text:
                result.append(chunk_text)
        return result if result else [text.strip()]


# ---------------------------------------------------------------------------
# NotionChunker
# ---------------------------------------------------------------------------

class NotionChunker:
    """Heading-aware chunking for Notion pages. Tables are one chunk each."""

    def chunk(self, documents: list[Document]) -> list[TextNode]:
        nodes: list[TextNode] = []
        for doc in documents:
            page_title = doc.metadata.get("title", "")
            h1 = doc.metadata.get("h1", page_title)
            chunks = self._chunk_notion(doc.text)
            for i, chunk_text in enumerate(chunks):
                meta = {
                    **doc.metadata,
                    "source_type": "notion",
                    "page_title": page_title,
                    "h1": h1,
                }
                nodes.append(_make_text_node(chunk_text, meta, f"notion_{doc.id_}", i, len(chunks)))
        return nodes

    def _chunk_notion(self, text: str) -> list[str]:
        lines = text.splitlines(keepends=True)
        chunks: list[list[str]] = []
        current: list[str] = []
        in_table = False

        for line in lines:
            stripped = line.strip()

            # Notion tables are markdown tables
            if "|" in stripped and not in_table:
                in_table = True
                if current:
                    chunks.append(current)
                    current = []
                current.append(line)
                continue

            if in_table:
                if "|" in stripped:
                    current.append(line)
                    continue
                else:
                    in_table = False
                    chunks.append(current)
                    current = []
                    # Fall through to process this line normally

            # Heading boundary
            if re.match(r"^#{1,3}\s+", stripped):
                if current:
                    chunks.append(current)
                current = [line]
                continue

            current.append(line)

        if current:
            chunks.append(current)

        result = []
        for chunk_lines in chunks:
            chunk_text = "".join(chunk_lines).strip()
            if chunk_text:
                result.append(chunk_text)
        return result if result else [text.strip()]


# ---------------------------------------------------------------------------
# JiraChunker
# ---------------------------------------------------------------------------

class JiraChunker:
    """Field-based chunking for Jira tickets."""

    def chunk(self, documents: list[Document]) -> list[TextNode]:
        nodes: list[TextNode] = []
        for doc in documents:
            ticket_id = doc.metadata.get("ticket_id", "")
            status = doc.metadata.get("status", "")
            assignee = doc.metadata.get("assignee", "")

            # Description chunk
            desc_text = doc.metadata.get("description", "")
            if desc_text:
                meta = {
                    **doc.metadata,
                    "source_type": "jira",
                    "ticket_id": ticket_id,
                    "status": status,
                    "assignee": assignee,
                    "chunk_type": "description",
                }
                nodes.append(_make_text_node(desc_text, meta, f"jira_{ticket_id}_desc", 0, 1))

            # Comment chunks
            comments = doc.metadata.get("comments", [])
            for i, comment in enumerate(comments):
                comment_text = comment if isinstance(comment, str) else comment.get("text", "")
                if not comment_text:
                    continue
                meta = {
                    **doc.metadata,
                    "source_type": "jira",
                    "ticket_id": ticket_id,
                    "status": status,
                    "assignee": assignee,
                    "chunk_type": "comment",
                    "comment_index": i,
                    "comment_author": comment.get("author", "") if isinstance(comment, dict) else "",
                }
                nodes.append(
                    _make_text_node(
                        comment_text, meta, f"jira_{ticket_id}_comment{i}", i, len(comments)
                    )
                )

            # If no description or comments, chunk the main text
            if not desc_text and not comments:
                meta = {
                    **doc.metadata,
                    "source_type": "jira",
                    "ticket_id": ticket_id,
                    "status": status,
                    "assignee": assignee,
                    "chunk_type": "body",
                }
                nodes.append(_make_text_node(doc.text, meta, f"jira_{ticket_id}_body", 0, 1))

        # Re-number
        for i, node in enumerate(nodes):
            node.metadata["chunk_index"] = i
            node.metadata["total_chunks"] = len(nodes)
            node.id_ = f"jira_chunk{i}"

        return nodes


# ---------------------------------------------------------------------------
# GenericChunker (fallback)
# ---------------------------------------------------------------------------

class GenericChunker:
    """Sentence-based chunking fallback for unknown source types."""

    def __init__(self):
        self.splitter = SentenceSplitter(
            chunk_size=512,
            chunk_overlap=50,
        )

    def chunk(self, documents: list[Document]) -> list[TextNode]:
        nodes: list[TextNode] = []
        for doc in documents:
            chunk_nodes = self.splitter.get_nodes_from_documents([doc])
            for i, node in enumerate(chunk_nodes):
                if isinstance(node, TextNode):
                    node.metadata["source_type"] = doc.metadata.get("source_type", "unknown")
                    node.metadata["chunk_index"] = i
                    node.metadata["total_chunks"] = len(chunk_nodes)
                    nodes.append(node)
                else:
                    text_node = TextNode(
                        text=node.text,
                        metadata={
                            **node.metadata,
                            "source_type": doc.metadata.get("source_type", "unknown"),
                            "chunk_index": i,
                            "total_chunks": len(chunk_nodes),
                        },
                        id_=f"generic_{doc.id_}_chunk{i}",
                    )
                    nodes.append(text_node)
        return nodes


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

_CHUNKERS: dict[str, Any] = {
    "slack": SlackChunker,
    "github": GitHubChunker,
    "notion": NotionChunker,
    "jira": JiraChunker,
}


def chunk_documents(documents: list[Document]) -> list[TextNode]:
    """Route documents to the correct chunker based on source_type.

    Parameters
    ----------
    documents : list[Document]
        Documents with metadata["source_type"] set.

    Returns
    -------
    list[TextNode]
        Flat list of all chunks with chunk_index, total_chunks, source_type.
    """
    if not documents:
        return []

    # Group by source_type
    by_source: dict[str, list[Document]] = {}
    for doc in documents:
        source = doc.metadata.get("source_type", "unknown")
        by_source.setdefault(source, []).append(doc)

    all_nodes: list[TextNode] = []
    for source, docs in by_source.items():
        chunker_cls = _CHUNKERS.get(source, GenericChunker)
        chunker = chunker_cls()
        nodes = chunker.chunk(docs)
        all_nodes.extend(nodes)

    return all_nodes


# ---------------------------------------------------------------------------
# Test block
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ingestion.loader import load

    # Load Slack sample data
    docs = load("slack", path="data/sample")
    print(f"Loaded {len(docs)} raw documents from Slack export\n")

    chunks = chunk_documents(docs)
    print(f"Produced {len(chunks)} chunks\n")

    # Count per source type
    from collections import Counter
    source_counts = Counter(c.metadata.get("source_type", "unknown") for c in chunks)
    print("Chunks per source type:")
    for source, count in sorted(source_counts.items()):
        print(f"  {source}: {count}")
    print()

    # Print one sample chunk from Slack
    slack_chunks = [c for c in chunks if c.metadata.get("source_type") == "slack"]
    if slack_chunks:
        sample = slack_chunks[5]  # Pick a middle one
        print("--- Sample Slack chunk ---")
        print(f"ID:   {sample.id_}")
        print(f"Text preview:\n{sample.text[:400]}...")
        print("Metadata:")
        for k, v in sample.metadata.items():
            print(f"  {k}: {v}")
        print()

    # Create synthetic docs for other strategies to demo them
    print("--- Synthetic GitHub chunk demo ---")
    gh_doc = Document(
        text="## Setup\nRun `pip install`.\n\n## Usage\n```python\nprint('hello')\n```\n\n### Subsection\nDetails here.",
        metadata={"source_type": "github", "title": "README"},
        id_="gh_readme",
    )
    gh_chunks = chunk_documents([gh_doc])
    for c in gh_chunks[:2]:
        print(f"  Chunk {c.metadata['chunk_index']}/{c.metadata['total_chunks']}:")
        print(f"    {c.text[:100]}...")
    print()

    print("--- Synthetic Notion chunk demo ---")
    notion_doc = Document(
        text="# Project Plan\n## Milestones\n| Date | Task |\n|------|------|\n| Jan  | Kick |\n\n## Notes\nSome details here.",
        metadata={"source_type": "notion", "title": "Project Plan"},
        id_="notion_plan",
    )
    notion_chunks = chunk_documents([notion_doc])
    for c in notion_chunks:
        print(f"  Chunk {c.metadata['chunk_index']}/{c.metadata['total_chunks']}:")
        print(f"    {c.text[:80]}...")
    print()

    print("--- Synthetic Jira chunk demo ---")
    jira_doc = Document(
        text="",
        metadata={
            "source_type": "jira",
            "ticket_id": "PROJ-42",
            "status": "In Progress",
            "assignee": "alex.morgan",
            "description": "The API is returning 500s under load.",
            "comments": [
                {"text": "I can reproduce this locally.", "author": "jordan.lee"},
                {"text": "Looking at the logs now.", "author": "taylor.kim"},
            ],
        },
        id_="jira_proj42",
    )
    jira_chunks = chunk_documents([jira_doc])
    for c in jira_chunks:
        print(f"  Chunk {c.metadata['chunk_index']}/{c.metadata['total_chunks']} ({c.metadata.get('chunk_type', 'body')}):")
        print(f"    {c.text[:80]}...")
    print()

    print("--- Synthetic Generic chunk demo ---")
    generic_doc = Document(
        text="This is a long paragraph about machine learning and artificial intelligence. " * 20,
        metadata={"source_type": "unknown"},
        id_="generic_doc",
    )
    generic_chunks = chunk_documents([generic_doc])
    print(f"  Generic produced {len(generic_chunks)} chunks")
    for c in generic_chunks[:1]:
        print(f"    Chunk {c.metadata['chunk_index']}/{c.metadata['total_chunks']}:")
        print(f"      {c.text[:80]}...")
