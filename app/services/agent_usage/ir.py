"""Normalized IR used by every local agent usage adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


def _non_negative_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """One billable usage event in the database-compatible shape.

    ``prompt_tokens`` follows the proxy convention and includes cache-read
    tokens.  ``completion_tokens`` includes reasoning tokens when a source
    exposes reasoning separately; the database currently has no separate
    reasoning column.  ``cache_read_tokens`` remains separate for pricing.
    """

    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    total_tokens: int
    requested_at: str
    event_id: str
    project: str | None = None
    session_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False,
                                        compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", str(self.model or "unknown"))
        for name in ("prompt_tokens", "completion_tokens", "cache_read_tokens",
                     "total_tokens"):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name)))
        object.__setattr__(self, "event_id", str(self.event_id))

    @classmethod
    def from_buckets(
        cls,
        *,
        model: Any,
        input_tokens: Any = 0,
        output_tokens: Any = 0,
        cached_input_tokens: Any = 0,
        reasoning_output_tokens: Any = 0,
        requested_at: str,
        event_id: str,
        project: str | None = None,
        session_id: str | None = None,
        input_includes_cache: bool = False,
        total_tokens: Any | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "UsageEvent":
        """Convert the reference bucket vocabulary to the request-log IR.

        Most reference parsers emit non-overlapping input/cache buckets.  A
        few native formats (notably Codex) report input inclusive of cache;
        adapters select that with ``input_includes_cache``.
        """
        input_count = _non_negative_int(input_tokens)
        cache_count = _non_negative_int(cached_input_tokens)
        prompt = input_count if input_includes_cache else input_count + cache_count
        completion = _non_negative_int(output_tokens) + _non_negative_int(
            reasoning_output_tokens)
        return cls(
            model=str(model or "unknown"),
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_count,
            total_tokens=(prompt + completion if total_tokens is None
                          else _non_negative_int(total_tokens)),
            requested_at=str(requested_at),
            event_id=str(event_id),
            project=project,
            session_id=session_id,
            metadata=metadata or {},
        )


@dataclass(frozen=True, slots=True)
class UsageSource:
    """A physical source unit scanned by an adapter.

    ``key`` is persisted in the per-software cursor.  ``path`` is the file
    whose stat signature protects a scan from concurrent appends.  ``context``
    carries small source-specific values such as a profile or session id.
    """

    path: Path
    key: str | None = None
    context: Mapping[str, Any] = field(default_factory=dict, repr=False,
                                       compare=False)

    @property
    def state_key(self) -> str:
        return self.key or str(self.path)


@dataclass(frozen=True, slots=True)
class ParseBatch:
    """Adapter output for one ``UsageSource``."""

    events: tuple[UsageEvent, ...] = ()
    record_count: int = 0

    @classmethod
    def from_events(cls, events: list[UsageEvent], record_count: int) -> "ParseBatch":
        return cls(tuple(events), max(0, int(record_count or 0)))
