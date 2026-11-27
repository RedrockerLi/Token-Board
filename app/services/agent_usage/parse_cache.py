"""Parse-result cache for adapters whose parse is a pure function of its source.

The importer re-runs ``parse`` for every ``ALWAYS_SCAN`` source on every pass,
because those sources can change without their own file's size/mtime moving.
Re-parsing an unchanged source reproduces exactly the batch the previous pass
already inserted, so this module keeps that batch under a stat fingerprint and
replays it instead.

The cache is opt-in per adapter (``PARSE_CACHE = True`` in the adapter module,
exposed as ``AdapterSpec.parse_cache``) because the purity claim is per
adapter, not generic: the Cursor adapter fetches its CSV from the cloud inside
``parse``, Cline reads SDK session artifacts beside its state file, Codex reads
the other segments of one session, and Kiro aggregates several live stores.
Caching those on their own file's stat would skip newly produced usage, so they
must not opt in.

A fingerprint covers the source file *and* its ``-wal``/``-shm``/``-journal``
sidecars, so a SQLite database whose new rows still live in the WAL is still
recognised as changed.  A batch is only stored when the source stayed unchanged
across the parse, the parse did not report a skip, no external SQLite read
failed while producing it, and it contains events: caching a skipped, failed or
empty batch would turn one transient failure into a permanent undercount.

A hit is observable: ``stats()`` counts hits/misses/stores/evictions and the
importer logs one line per pass for an opted-in kind.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from .common import IDENTITY_FIELDS, stat_identity
from .ir import ParseBatch

# Enough for every source of a large installation without holding an unbounded
# number of parsed event tuples in memory.
MAX_ENTRIES = 4096
MAX_EVENTS = 250_000

_lock = threading.Lock()
_entries: OrderedDict[tuple, ParseBatch] = OrderedDict()
_stats = {"hits": 0, "misses": 0, "stores": 0, "evictions": 0}
_cached_events = 0


def _stat_signature(stat) -> tuple:
    identity = stat_identity(stat)
    return tuple(identity[name] for name in IDENTITY_FIELDS)


def source_fingerprint(path: Path) -> tuple | None:
    """Identity of one source file including its SQLite/journal sidecars.

    Returns ``None`` when the source itself cannot be stat'ed (it disappeared
    between discovery and the cache lookup); the caller then parses normally.
    """
    path = Path(path)
    try:
        primary = _stat_signature(path.stat())
    except OSError:
        return None
    sidecars = []
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            sidecars.append(_stat_signature(Path(str(path) + suffix).stat()))
        except OSError:
            sidecars.append(None)
    return (primary, tuple(sidecars))


def cache_key(kind: str, state_key: str, skip_token_count: int,
              parser: Callable) -> tuple:
    """Key prefix shared by lookups and stores.

    ``parser`` is the adapter's parse callable or an API/test override.  It is
    part of the key so that an override never reads a batch produced by a
    different parser for the same file, and ``skip_token_count`` is part of it
    because a replay-skip changes what the parser returns.
    """
    return (str(kind), str(state_key), int(skip_token_count or 0), parser)


def lookup(prefix: tuple, fingerprint: tuple) -> ParseBatch | None:
    """Return the cached batch for *fingerprint*, recording a hit or a miss."""
    key = prefix + (fingerprint,)
    with _lock:
        batch = _entries.get(key)
        if batch is None:
            _stats["misses"] += 1
            return None
        _entries.move_to_end(key)
        _stats["hits"] += 1
        return batch


def store(prefix: tuple, fingerprint: tuple, batch: ParseBatch, *,
          cacheable: bool) -> bool:
    """Cache *batch* when it is reusable; returns whether it was stored.

    ``cacheable`` carries the caller's decision (unchanged source, no skip, no
    failed read).  This function additionally refuses empty batches, because
    replaying "nothing" saves nothing and would also hide a source that is
    currently unreadable.
    """
    if not cacheable or not batch.events:
        return False
    global _cached_events
    events = len(batch.events)
    if events > MAX_EVENTS:
        return False
    key = prefix + (fingerprint,)
    with _lock:
        previous = _entries.pop(key, None)
        if previous is not None:
            _cached_events -= len(previous.events)
        _entries[key] = batch
        _cached_events += events
        _stats["stores"] += 1
        while _entries and (len(_entries) > MAX_ENTRIES
                            or _cached_events > MAX_EVENTS):
            _, evicted = _entries.popitem(last=False)
            _cached_events -= len(evicted.events)
            _stats["evictions"] += 1
    return True


def stats() -> dict:
    """Cumulative cache counters, so a hit is observable rather than implicit."""
    with _lock:
        snapshot = dict(_stats)
        snapshot["entries"] = len(_entries)
        snapshot["cached_events"] = _cached_events
        return snapshot


def reset() -> None:
    """Drop every cached batch and zero the counters (tests, manual rescan)."""
    global _cached_events
    with _lock:
        _entries.clear()
        _cached_events = 0
        for name in _stats:
            _stats[name] = 0


__all__ = [
    "MAX_ENTRIES", "MAX_EVENTS", "cache_key", "lookup", "reset",
    "source_fingerprint", "stats", "store",
]
