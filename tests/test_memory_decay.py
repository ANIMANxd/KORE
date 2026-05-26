"""Unit tests for memory/decay.py"""

from datetime import datetime, timedelta, timezone

import pytest

from memory.store import MemoryEntry, MemoryStore
from memory.decay import (
    calculate_freshness,
    decay_sweep,
    get_freshness_summary,
    _DEFAULT_HALF_LIFE_DAYS,
    _DEFAULT_ACCESS_BOOST,
)


class TestCalculateFreshness:
    def test_perfectly_fresh_entry(self):
        """Entry updated right now with high confidence and no accesses."""
        entry = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=datetime.now(timezone.utc),
            access_count=0,
            freshness=1.0,
        )
        score = calculate_freshness(entry)
        assert score == pytest.approx(0.5, abs=0.01)  # confidence(1) * decay(1) * access_bonus(0.5)

    def test_confidence_scales_result(self):
        entry = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=0.5,
            last_updated=datetime.now(timezone.utc),
            access_count=0,
            freshness=1.0,
        )
        score = calculate_freshness(entry)
        assert score == pytest.approx(0.25, abs=0.01)

    def test_accesses_boost_freshness(self):
        now = datetime.now(timezone.utc)
        entry_low = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=now,
            access_count=0,
            freshness=1.0,
        )
        entry_high = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=now,
            access_count=10,
            freshness=1.0,
        )
        low_score = calculate_freshness(entry_low)
        high_score = calculate_freshness(entry_high)
        assert high_score > low_score
        assert high_score == pytest.approx(1.0, abs=0.01)  # capped at 1.0

    def test_time_decay_lowers_score(self):
        now = datetime.now(timezone.utc)
        entry = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=now - timedelta(days=60),
            access_count=0,
            freshness=1.0,
        )
        score = calculate_freshness(entry)
        # 60 days with 30-day half-life → decay ≈ exp(-2) ≈ 0.135
        assert score == pytest.approx(0.5 * 0.135, abs=0.02)
        assert 0.0 < score < 0.5

    def test_very_old_entry_nears_zero(self):
        now = datetime.now(timezone.utc)
        entry = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=now - timedelta(days=365),
            access_count=0,
            freshness=1.0,
        )
        score = calculate_freshness(entry)
        assert 0.0 <= score < 0.1

    def test_clamped_to_zero_one(self):
        now = datetime.now(timezone.utc)
        entry = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=now - timedelta(days=1000),
            access_count=0,
            freshness=1.0,
        )
        score = calculate_freshness(entry)
        assert score == pytest.approx(0.0, abs=1e-10)

    def test_custom_half_life(self):
        now = datetime.now(timezone.utc)
        entry = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=now - timedelta(days=30),
            access_count=0,
            freshness=1.0,
        )
        # Default half-life 30 days → decay = exp(-1) ≈ 0.368
        default_score = calculate_freshness(entry)
        # Half-life 60 days → decay = exp(-0.5) ≈ 0.607
        longer_score = calculate_freshness(entry, half_life_days=60.0)
        assert longer_score > default_score


class TestDecaySweep:
    def test_updates_freshness_on_all_entries(self, tmp_path):
        store_path = tmp_path / "store.json"
        store = MemoryStore(
            entries=[
                MemoryEntry(
                    content="old",
                    entry_type="rule",
                    confidence=1.0,
                    last_updated=datetime.now(timezone.utc) - timedelta(days=60),
                    access_count=0,
                    freshness=1.0,
                ),
                MemoryEntry(
                    content="new",
                    entry_type="rule",
                    confidence=1.0,
                    last_updated=datetime.now(timezone.utc),
                    access_count=5,
                    freshness=0.5,
                ),
            ]
        )
        store_path.write_text(store.to_json(), encoding="utf-8")

        updated, result = decay_sweep(str(store_path))
        assert updated == 2
        assert result.entries[0].freshness < 1.0  # old entry decayed
        assert result.entries[1].freshness > 0.5  # new entry boosted

        # Check persistence
        from memory.store import load_memory_store
        loaded = load_memory_store(str(store_path))
        assert loaded.entries[0].freshness == pytest.approx(result.entries[0].freshness)

    def test_no_changes_when_freshness_already_accurate(self, tmp_path):
        now = datetime.now(timezone.utc)
        store_path = tmp_path / "store.json"
        entry = MemoryEntry(
            content="test",
            entry_type="rule",
            confidence=1.0,
            last_updated=now,
            access_count=0,
            freshness=0.5,  # already accurate for zero-access brand-new entry
        )
        store = MemoryStore(entries=[entry])
        store_path.write_text(store.to_json(), encoding="utf-8")

        updated, result = decay_sweep(str(store_path))
        assert updated == 0

    def test_empty_store(self, tmp_path):
        store_path = tmp_path / "store.json"
        store = MemoryStore()
        store_path.write_text(store.to_json(), encoding="utf-8")
        updated, result = decay_sweep(str(store_path))
        assert updated == 0
        assert result.entry_count == 0


class TestGetFreshnessSummary:
    def test_buckets_entries_correctly(self):
        store = MemoryStore(
            entries=[
                MemoryEntry(content="a", entry_type="rule", freshness=0.95),
                MemoryEntry(content="b", entry_type="rule", freshness=0.71),
                MemoryEntry(content="c", entry_type="rule", freshness=0.50),
                MemoryEntry(content="d", entry_type="rule", freshness=0.41),
                MemoryEntry(content="e", entry_type="rule", freshness=0.30),
                MemoryEntry(content="f", entry_type="rule", freshness=0.0),
            ]
        )
        summary = get_freshness_summary(store)
        assert summary["high"] == 2  # > 0.7
        assert summary["mid"] == 2  # 0.4 - 0.7
        assert summary["low"] == 2  # < 0.4

    def test_empty_store(self):
        summary = get_freshness_summary(MemoryStore())
        assert summary == {"high": 0, "mid": 0, "low": 0}
