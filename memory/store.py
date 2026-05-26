"""
Memory store for agent-accessible company knowledge.

Builds a searchable index from verified rules and high-confidence graph
entities. Agents query this store to receive company-specific context.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from providers.embedder import embed_batch, embed_text


EntryType = Literal["rule", "entity", "relationship", "context", "procedure"]


class MemoryEntry(BaseModel):
    """A single searchable memory entry for agent context."""

    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    content: str = Field(min_length=1)
    entry_type: EntryType
    source_ids: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    last_updated: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    access_count: int = Field(default=0, ge=0)
    freshness: float = Field(ge=0.0, le=1.0, default=1.0)


class MemoryStore(BaseModel):
    """Container for all memory entries."""

    entries: list[MemoryEntry] = Field(default_factory=list)
    last_built: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    entry_count: int = 0

    def model_post_init(self, __context: object) -> None:
        self.entry_count = len(self.entries)

    def to_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            indent=2,
            ensure_ascii=False,
        )


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _chunk_text(text: str, max_chars: int = 800) -> list[str]:
    """Split text into chunks roughly bounded by sentences."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    sentences = text.replace("!", ".").replace("?", ".").split(".")
    current = ""
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        candidate = f"{current} {sent}." if current else f"{sent}."
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = f"{sent}."
    if current:
        chunks.append(current.strip())
    return chunks if chunks else [text[:max_chars]]


def build_memory_store(
    rules_dir: str = "rules/extracted/",
    graph_path: str = "graph/knowledge_graph.json",
    output_path: str = "memory/store.json",
) -> MemoryStore:
    """Build a MemoryStore from verified rules and high-confidence graph entities.

    Parameters
    ----------
    rules_dir : str
        Directory containing extracted rule YAML files.
    graph_path : str
        Path to the persisted knowledge graph JSON.
    output_path : str
        Where to save the resulting memory store JSON.

    Returns
    -------
    MemoryStore
        The built and persisted memory store.
    """
    from output.writer import load_rules
    from graph.store import load_graph

    store = MemoryStore()
    entries: list[MemoryEntry] = []

    # --- Rules ---
    rules = load_rules(directory=rules_dir)
    for rule in rules:
        if rule.verification_status != "verified" or not rule.approved_by:
            continue

        content = rule.rule_text
        keywords = [rule.rule_category]
        if rule.ambiguity_notes:
            keywords.extend(rule.ambiguity_notes.lower().split()[:5])

        entry = MemoryEntry(
            content=content,
            entry_type="rule",
            source_ids=[rule.rule_id],
            keywords=keywords,
            confidence=rule.confidence,
        )
        entries.append(entry)

    # --- Graph entities with mention_count > 2 ---
    graph = load_graph(graph_path)
    for entity in graph.entities.values():
        if entity.mention_count <= 2:
            continue
        desc = entity.description or ""
        content = f"{entity.name} ({entity.entity_type}): {desc}".strip()
        if not content:
            continue

        entry = MemoryEntry(
            content=content,
            entry_type="entity",
            source_ids=entity.source_rule_ids,
            keywords=[entity.entity_type, entity.name.lower()],
            confidence=min(0.5 + entity.mention_count * 0.05, 1.0),
        )
        entries.append(entry)

    # --- Generate embeddings ---
    if entries:
        print(f"[KORE] Generating embeddings for {len(entries)} memory entries...")
        texts = [e.content for e in entries]
        try:
            embeddings = embed_batch(texts)
            for entry, emb in zip(entries, embeddings):
                entry.embedding = emb
        except Exception as exc:
            print(f"[KORE] WARNING: Embedding generation failed: {exc}. Store built without embeddings.")

    store.entries = entries
    store.entry_count = len(entries)
    store.last_built = datetime.now(timezone.utc)

    # --- Save ---
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(store.to_json(), encoding="utf-8")
    os.replace(str(tmp_path), str(out_path))

    return store


def load_memory_store(path: str = "memory/store.json") -> MemoryStore:
    """Load a MemoryStore from a JSON file."""
    file_path = Path(path)
    if not file_path.exists():
        return MemoryStore()
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        return MemoryStore.model_validate(data)
    except Exception:
        return MemoryStore()


def save_memory_store(store: MemoryStore, path: str = "memory/store.json") -> None:
    """Persist a MemoryStore to a JSON file atomically."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = file_path.with_suffix(".tmp")
    tmp_path.write_text(store.to_json(), encoding="utf-8")
    os.replace(str(tmp_path), str(file_path))


def query_memory(
    query: str,
    store: MemoryStore,
    top_k: int = 10,
    entry_types: list[str] | None = None,
) -> list[MemoryEntry]:
    """Search memory entries by embedding cosine similarity.

    Parameters
    ----------
    query : str
        Natural language query.
    store : MemoryStore
        The memory store to search.
    top_k : int
        Number of results to return.
    entry_types : list[str] | None
        If provided, filter to these entry types.

    Returns
    -------
    list[MemoryEntry]
        Top-k matching entries, sorted by similarity desc.
        access_count is incremented for returned entries.
    """
    if not store.entries:
        return []

    allowed_types = set(entry_types or [])
    candidates = [e for e in store.entries if e.embedding is not None]
    if allowed_types:
        candidates = [e for e in candidates if e.entry_type in allowed_types]

    if not candidates:
        return []

    query_emb = embed_text(query)

    scored: list[tuple[float, MemoryEntry]] = []
    for entry in candidates:
        sim = _cosine_similarity(query_emb, entry.embedding)  # type: ignore[arg-type]
        scored.append((sim, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [e for _, e in scored[:top_k]]

    # Increment access_count
    for entry in results:
        entry.access_count += 1
        entry.last_updated = datetime.now(timezone.utc)

    return results


def build_context_for_agent(
    query: str,
    store: MemoryStore,
    max_tokens: int = 2000,
) -> str:
    """Build a formatted context block from memory for agent consumption.

    Approximates token count as ~4 characters per token.
    """
    entries = query_memory(query, store, top_k=10)
    if not entries:
        return ""

    lines = [
        "COMPANY KNOWLEDGE CONTEXT",
        "=" * 40,
        "",
    ]

    char_budget = max_tokens * 4
    current_chars = sum(len(line) for line in lines)

    for i, entry in enumerate(entries, 1):
        # Format entry
        source_str = " | ".join(entry.source_ids[:3]) if entry.source_ids else "unknown"
        entry_lines = [
            f"[{i}] {entry.entry_type.upper()} (confidence: {entry.confidence:.2f})",
            f"{entry.content}",
            f"Source: {source_str}",
            "",
        ]
        entry_chars = sum(len(line) for line in entry_lines)
        if current_chars + entry_chars > char_budget:
            lines.append("[truncated]")
            break
        lines.extend(entry_lines)
        current_chars += entry_chars

    return "\n".join(lines)
