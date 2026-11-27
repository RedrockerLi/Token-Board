"""Content-stable record identities for sources that can be rewritten.

Several sources have no record id of their own: a Cursor CSV export, a task's
``ui_messages.json``, a JSONL session log.  Using the record's *position* as
its ``event_id`` makes the identity depend on the file layout, so a rewrite or
a reordered export yields fresh identities for records that were already
imported — the permanent receipt no longer matches and the same usage is
counted twice.

The identity here is what the imported row itself says: normalized timestamp,
model and the three token buckets that survive into ``request_log``.  It is
independent of position, so reordering or rewriting a source no longer changes
it.  Records that are byte-identical in those fields are told apart by an
occurrence counter, the same way ``mcode`` already treats duplicated ledger
rows; assigning that counter over the sorted identities rather than file order
keeps the resulting id a function of the record *multiset* alone.

Every field of the identity is stored on the imported row, which is what makes
the one-time re-key described in "compatibility" below computable from the
database instead of from the sources.

Compatibility
-------------
The previous identities were positional, so every record still present in a
source gets a new ``event_id`` once.  Existing receipts keep the old ids and
therefore no longer match; unless the receipts and ``request_log`` rows are
re-keyed once (see the fragment for the exact rule), a source re-imports the
history it still contains exactly once.  That is a visible, bounded cost, and
nothing here rewrites, deletes or re-prices an existing row.
"""

from __future__ import annotations

import hashlib

from ..common import safe_int


def event_identity(payload: dict) -> tuple[str, str, int, int, int]:
    """Identity tuple of one usage record, in ``UsageEvent`` bucket terms."""
    cached = safe_int(payload.get("cached_input_tokens"))
    prompt = safe_int(payload.get("input_tokens")) + (
        0 if payload.get("input_includes_cache") else cached)
    completion = (safe_int(payload.get("output_tokens"))
                  + safe_int(payload.get("reasoning_output_tokens")))
    return (str(payload.get("requested_at") or ""),
            str(payload.get("model") or "unknown"),
            prompt, completion, cached)


def _has_usage(identity: tuple[str, str, int, int, int]) -> bool:
    return identity[2] + identity[3] + identity[4] > 0


def _digest(identity: tuple[str, str, int, int, int]) -> str:
    payload = repr(tuple(identity)).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()[:16]


def assign_ordinals(payloads: list[dict]) -> list[str | None]:
    """Return the content ordinal of every payload, ``None`` without usage.

    ``payloads`` are the keyword fields of the events an adapter is about to
    build.  Records that carry no token at all produce no event and stay out of
    the occurrence numbering, so a zero-record can never shift another id.
    """
    identities = [event_identity(payload) for payload in payloads]
    order = sorted((index for index, identity in enumerate(identities)
                    if _has_usage(identity)),
                   key=lambda index: (identities[index], index))
    seen: dict[tuple, int] = {}
    ordinals: list[str | None] = [None] * len(payloads)
    for index in order:
        identity = identities[index]
        occurrence = seen.get(identity, 0)
        seen[identity] = occurrence + 1
        ordinals[index] = f"{_digest(identity)}:{occurrence}"
    return ordinals


__all__ = ["assign_ordinals", "event_identity"]
