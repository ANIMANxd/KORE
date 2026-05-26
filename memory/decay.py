"""
Freshness scoring and decay sweep for the memory store.

Entries lose freshness over time. Accesses slow decay.
Sweep recalculates freshness for all entries and persists
updated scores back to the store file.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from memory.store import MemoryEntry, MemoryStore, load_memory_store, save_memory_store

_DEFAULT_HALF_LIFE_DAYS = 30.0
_DEFAULT_ACCESS_BOOST = 0.1


def calculate_freshness(
    entry: MemoryEntry,
    half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
    access_boost: float = _DEFAULT_ACCESS_BOOST,
) -> float:
    """Calculate a freshness score for a single memory entry.

    Formula::

        freshness = confidence × time_decay × access_bonus

    where::

        time_decay  = exp(-days_old / half_life_days)
        access_bonus = min(1.0, 0.5 + access_count × access_boost)

    Result is clamped to [0.0, 1.0].
    """
    now = datetime.now(timezone.utc)
    days_old = (now - entry.last_updated).total_seconds() / 86400.0

    time_decay = math.exp(-days_old / half_life_days)
    access_bonus = min(1.0, 0.5 + entry.access_count * access_boost)

    raw = entry.confidence * time_decay * access_bonus
    return max(0.0, min(1.0, raw))


def decay_sweep(
    store_path: str = "memory/store.json",
    half_life_days: float = _DEFAULT_HALF_LIFE_DAYS,
    access_boost: float = _DEFAULT_ACCESS_BOOST,
) -> tuple[int, MemoryStore]:
    """Run a decay sweep over the memory store.

    Loads the store, recalculates freshness for every entry,
    and atomically saves back.

    Returns
    -------
    tuple[int, MemoryStore]
        Number of entries whose freshness changed, and the updated store.
    """
    store = load_memory_store(store_path)
    updated = 0

    for entry in store.entries:
        old = entry.freshness
        entry.freshness = calculate_freshness(entry, half_life_days, access_boost)
        if abs(entry.freshness - old) > 1e-6:
            updated += 1

    if updated:
        save_memory_store(store, store_path)

    return updated, store


def get_freshness_summary(store: MemoryStore) -> dict[str, int]:
    """Return a count of entries by freshness bucket."""
    summary: dict[str, int] = {"high": 0, "mid": 0, "low": 0}
    for entry in store.entries:
        if entry.freshness > 0.7:
            summary["high"] += 1
        elif entry.freshness > 0.4:
            summary["mid"] += 1
        else:
            summary["low"] += 1
    return summary
