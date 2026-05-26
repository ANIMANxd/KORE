"""Unit tests for memory/store.py"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from memory.store import (
    MemoryEntry,
    MemoryStore,
    build_memory_store,
    load_memory_store,
    query_memory,
    build_context_for_agent,
    _cosine_similarity,
    _chunk_text,
)
from output.schema import BusinessRule, ExtractionMeta, SourceRef
from graph.models import Entity, KnowledgeGraph, Relationship


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rule(
    rule_id: str = "aaaaaaaa-1111-2222-3333-444444444444",
    verification_status: str = "verified",
    approved_by: str | None = "human_review",
    confidence: float = 0.92,
    rule_text: str = "Deploys to production must be approved by the Engineering manager.",
) -> BusinessRule:
    return BusinessRule(
        rule_id=rule_id,
        rule_text=rule_text,
        rule_category="engineering",
        confidence=confidence,
        verification_status=verification_status,  # type: ignore[arg-type]
        source_refs=[
            SourceRef(
                chunk_id="slack_eng_123",
                source_type="slack",
                channel="#engineering",
                timestamp="2024-01-01T10:00:00Z",
                excerpt="All production deploys need manager approval.",
            )
        ],
        approved_by=approved_by,
        extraction_meta=ExtractionMeta(
            provider="lmstudio",
            model="test-model",
            deployment_mode="local",
            json_repair_attempts=0,
        ),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 1.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


class TestChunkText:
    def test_short_text_no_split(self):
        assert _chunk_text("hello world", max_chars=100) == ["hello world"]

    def test_long_text_splits(self):
        text = "A. " * 500
        chunks = _chunk_text(text, max_chars=100)
        assert len(chunks) > 1
        assert all(len(c) <= 100 for c in chunks)


# ---------------------------------------------------------------------------
# MemoryEntry / MemoryStore
# ---------------------------------------------------------------------------


class TestMemoryEntry:
    def test_default_values(self):
        e = MemoryEntry(content="test", entry_type="rule")
        assert e.entry_id
        assert e.confidence == 0.5
        assert e.access_count == 0
        assert e.embedding is None

    def test_entry_type_literal(self):
        with pytest.raises(Exception):
            MemoryEntry(content="test", entry_type="invalid")  # type: ignore[call-arg]


class TestMemoryStore:
    def test_empty_store(self):
        s = MemoryStore()
        assert s.entry_count == 0
        assert s.entries == []

    def test_to_json_roundtrip(self):
        s = MemoryStore(entries=[MemoryEntry(content="hello", entry_type="rule")])
        data = json.loads(s.to_json())
        assert data["entry_count"] == 1


# ---------------------------------------------------------------------------
# build_memory_store
# ---------------------------------------------------------------------------


class TestBuildMemoryStore:
    @patch("memory.store.embed_batch")
    @patch("output.writer.load_rules")
    @patch("graph.store.load_graph")
    def test_builds_from_verified_rules_and_entities(
        self, mock_load_graph, mock_load_rules, mock_embed_batch,
    ):
        mock_embed_batch.return_value = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

        rule = _make_rule()
        mock_load_rules.return_value = [rule]

        graph = KnowledgeGraph()
        ent = Entity(entity_type="team", name="Engineering", mention_count=5)
        graph.add_entity(ent)
        mock_load_graph.return_value = graph

        store = build_memory_store(
            rules_dir="rules/extracted/",
            graph_path="graph/kg.json",
            output_path="memory/store_test.json",
        )

        assert store.entry_count == 2  # 1 rule + 1 entity
        assert len(store.entries) == 2

        # Check rule entry
        rule_entries = [e for e in store.entries if e.entry_type == "rule"]
        assert len(rule_entries) == 1
        assert rule_entries[0].content == rule.rule_text
        assert rule_entries[0].confidence == rule.confidence

        # Check entity entry
        ent_entries = [e for e in store.entries if e.entry_type == "entity"]
        assert len(ent_entries) == 1
        assert "Engineering" in ent_entries[0].content

        # Check embeddings were generated
        assert all(e.embedding is not None for e in store.entries)

        # Check file was saved
        assert Path("memory/store_test.json").exists()

    @patch("memory.store.embed_batch")
    @patch("output.writer.load_rules")
    @patch("graph.store.load_graph")
    def test_skips_unverified_rules(
        self, mock_load_graph, mock_load_rules, mock_embed_batch,
    ):
        mock_embed_batch.return_value = [[0.1, 0.2, 0.3]]

        verified = _make_rule(approved_by="human_review")
        unverified = _make_rule(
            rule_id="bbbbbbbb-2222-3333-4444-555555555555",
            verification_status="needs_review",
            approved_by=None,
        )
        mock_load_rules.return_value = [verified, unverified]
        mock_load_graph.return_value = KnowledgeGraph()

        store = build_memory_store(
            output_path="memory/store_test2.json",
        )

        rule_entries = [e for e in store.entries if e.entry_type == "rule"]
        assert len(rule_entries) == 1
        assert rule_entries[0].source_ids == [verified.rule_id]

    @patch("memory.store.embed_batch")
    @patch("output.writer.load_rules")
    @patch("graph.store.load_graph")
    def test_skips_low_mention_entities(
        self, mock_load_graph, mock_load_rules, mock_embed_batch,
    ):
        mock_embed_batch.return_value = [[0.1, 0.2, 0.3]]
        mock_load_rules.return_value = []

        graph = KnowledgeGraph()
        graph.add_entity(Entity(entity_type="team", name="Low", mention_count=1))
        graph.add_entity(Entity(entity_type="team", name="High", mention_count=5))
        mock_load_graph.return_value = graph

        store = build_memory_store(
            output_path="memory/store_test3.json",
        )

        ent_entries = [e for e in store.entries if e.entry_type == "entity"]
        assert len(ent_entries) == 1
        assert "High" in ent_entries[0].content

    @patch("memory.store.embed_batch")
    @patch("output.writer.load_rules")
    @patch("graph.store.load_graph")
    def test_embedding_failure_graceful(
        self, mock_load_graph, mock_load_rules, mock_embed_batch,
    ):
        mock_embed_batch.side_effect = RuntimeError("Embedding model unavailable")
        mock_load_rules.return_value = [_make_rule()]
        mock_load_graph.return_value = KnowledgeGraph()

        store = build_memory_store(
            output_path="memory/store_test4.json",
        )

        assert store.entry_count == 1
        assert store.entries[0].embedding is None

    def teardown_method(self):
        for p in Path("memory").glob("store_test*.json"):
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# load_memory_store
# ---------------------------------------------------------------------------


class TestLoadMemoryStore:
    def test_load_nonexistent_returns_empty(self):
        store = load_memory_store("memory/nonexistent.json")
        assert store.entry_count == 0

    def test_load_valid(self, tmp_path):
        store = MemoryStore(
            entries=[MemoryEntry(content="hello", entry_type="rule")]
        )
        path = tmp_path / "store.json"
        path.write_text(store.to_json(), encoding="utf-8")
        loaded = load_memory_store(str(path))
        assert loaded.entry_count == 1
        assert loaded.entries[0].content == "hello"

    def test_load_corrupt_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json", encoding="utf-8")
        store = load_memory_store(str(path))
        assert store.entry_count == 0


# ---------------------------------------------------------------------------
# query_memory
# ---------------------------------------------------------------------------


class TestQueryMemory:
    def test_empty_store_returns_empty(self):
        store = MemoryStore()
        assert query_memory("test", store) == []

    @patch("memory.store.embed_text")
    def test_ranks_by_similarity(self, mock_embed_text):
        mock_embed_text.return_value = [1.0, 0.0, 0.0]

        store = MemoryStore(
            entries=[
                MemoryEntry(
                    content="hello",
                    entry_type="rule",
                    embedding=[1.0, 0.0, 0.0],
                    confidence=0.9,
                ),
                MemoryEntry(
                    content="world",
                    entry_type="rule",
                    embedding=[0.0, 1.0, 0.0],
                    confidence=0.5,
                ),
            ]
        )
        results = query_memory("hello", store, top_k=1)
        assert len(results) == 1
        assert results[0].content == "hello"
        assert results[0].access_count == 1  # incremented

    @patch("memory.store.embed_text")
    def test_filters_by_entry_type(self, mock_embed_text):
        mock_embed_text.return_value = [1.0, 0.0, 0.0]

        store = MemoryStore(
            entries=[
                MemoryEntry(
                    content="rule text",
                    entry_type="rule",
                    embedding=[1.0, 0.0, 0.0],
                ),
                MemoryEntry(
                    content="entity text",
                    entry_type="entity",
                    embedding=[1.0, 0.0, 0.0],
                ),
            ]
        )
        results = query_memory("test", store, entry_types=["rule"])
        assert len(results) == 1
        assert results[0].entry_type == "rule"

    @patch("memory.store.embed_text")
    def test_skips_entries_without_embeddings(self, mock_embed_text):
        mock_embed_text.return_value = [1.0, 0.0, 0.0]

        store = MemoryStore(
            entries=[
                MemoryEntry(content="no emb", entry_type="rule", embedding=None),
                MemoryEntry(content="has emb", entry_type="rule", embedding=[1.0, 0.0, 0.0]),
            ]
        )
        results = query_memory("test", store)
        assert len(results) == 1
        assert results[0].content == "has emb"


# ---------------------------------------------------------------------------
# build_context_for_agent
# ---------------------------------------------------------------------------


class TestBuildContextForAgent:
    @patch("memory.store.embed_text")
    def test_formats_context(self, mock_embed_text):
        mock_embed_text.return_value = [1.0, 0.0, 0.0]

        store = MemoryStore(
            entries=[
                MemoryEntry(
                    content="Refund within 7 days",
                    entry_type="rule",
                    embedding=[1.0, 0.0, 0.0],
                    confidence=0.95,
                    source_ids=["rule-1"],
                ),
            ]
        )
        ctx = build_context_for_agent("refund policy", store)
        assert "COMPANY KNOWLEDGE CONTEXT" in ctx
        assert "Refund within 7 days" in ctx
        assert "confidence: 0.95" in ctx
        assert "[1] RULE" in ctx

    @patch("memory.store.embed_text")
    def test_returns_empty_when_no_results(self, mock_embed_text):
        mock_embed_text.return_value = [1.0, 0.0, 0.0]
        store = MemoryStore()
        assert build_context_for_agent("test", store) == ""

    @patch("memory.store.embed_text")
    def test_truncates_to_max_tokens(self, mock_embed_text):
        mock_embed_text.return_value = [1.0, 0.0, 0.0]

        long_text = "A" * 500
        store = MemoryStore(
            entries=[
                MemoryEntry(
                    content=long_text,
                    entry_type="rule",
                    embedding=[1.0, 0.0, 0.0],
                    confidence=0.9,
                ),
            ]
        )
        ctx = build_context_for_agent("test", store, max_tokens=50)
        assert "[truncated]" in ctx
