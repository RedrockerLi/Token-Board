"""Generic source/cursor importer for all registered agent adapters."""

from __future__ import annotations

import json
import logging
from typing import Callable, Mapping

from app.core.time import format_utc, utc_now
from . import common, parse_cache
from .ir import ParseBatch, UsageEvent, UsageSource
from .registry import get_adapter, get_adapter_spec

log = logging.getLogger(__name__)


SPEED_ESTIMATE_MIN_OUTPUT_TOKENS = 200
SPEED_ESTIMATE_MIN_DURATION_MS = 1000


def _estimated_output_tps(completion_tokens: int, duration_ms: int) -> float:
    """估算输出速度:耗时含首字等待,门槛(200/1000ms)与 ref/cc-switch 一致。"""
    if (duration_ms < SPEED_ESTIMATE_MIN_DURATION_MS
            or completion_tokens < SPEED_ESTIMATE_MIN_OUTPUT_TOKENS):
        return 0.0
    return completion_tokens * 1000.0 / duration_ms


def _same_stat(previous: dict, stat) -> bool:
    """True when the stored cursor still describes the same physical file.

    A cursor written before inodes were recorded counts as changed: the source
    is parsed once more, which the permanent receipts make idempotent.
    """
    if not isinstance(previous, dict):
        return False
    identity = common.stat_identity(stat)
    if "mtime_ns" in previous:
        keys = common.IDENTITY_FIELDS
    else:
        # Second-resolution legacy cursors only carried size and mtime.
        if previous.get("mtime") != int(stat.st_mtime):
            return False
        keys = ("size",)
    return all(previous.get(name) == identity[name] for name in keys)


def _unchanged(before, after) -> bool:
    """True when both stats describe one file that did not change during parse."""
    return common.stat_identity(before) == common.stat_identity(after)


def _coerce_batch(raw, *, kind: str, source_item: UsageSource) -> ParseBatch | None:
    if raw is None:
        return None
    if isinstance(raw, ParseBatch):
        return raw
    # Compatibility for the pre-IR adapter container shape: (line_count, rows).
    # The adapter still owns identity. The importer must never manufacture an
    # ordinal event id because skipped or reordered lines would change the
    # deduplication key.
    if isinstance(raw, tuple) and len(raw) == 2:
        record_count, rows = raw
        events = []
        for row in rows or []:
            if isinstance(row, UsageEvent):
                events.append(row)
                continue
            if not isinstance(row, dict):
                continue
            if not row.get("event_id"):
                # Stable identity is an adapter output contract.  A source
                # row without it is invalid rather than something the
                # importer may repair from its current ordinal.
                continue
            try:
                events.append(UsageEvent(
                    model=row.get("model", "unknown"),
                    prompt_tokens=row.get("prompt_tokens", 0),
                    completion_tokens=row.get("completion_tokens", 0),
                    cache_read_tokens=row.get("cache_read_tokens", 0),
                    total_tokens=row.get("total_tokens", 0),
                    requested_at=row.get("requested_at") or "",
                    event_id=row.get("event_id"),
                    project=row.get("project"), session_id=row.get("session_id"),
                ))
            except (TypeError, ValueError, KeyError):
                continue
        return ParseBatch.from_events(events, int(record_count or 0))
    raise TypeError(f"agent adapter {kind} returned an invalid ParseBatch")


def _parse(adapter, source_item: UsageSource, stop_event, *, skip_token_count: int,
           parser_overrides: Mapping[str, Callable] | None):
    overrides = parser_overrides or {}
    parser = overrides.get(adapter.KIND)
    if parser is not None:
        # Compatibility parsers predate UsageSource and intentionally receive
        # a Path.  Keep this branch explicit instead of relying on function
        # identity (which is fragile for bound callables and test doubles).
        if skip_token_count:
            return parser(source_item.path, stop_event,
                          skip_token_count=skip_token_count)
        return parser(source_item.path, stop_event)
    return adapter.parse(source_item, stop_event,
                         skip_token_count=skip_token_count)


def _import_software(pdb, software: dict, stop_event=None,
                     parser_overrides: Mapping[str, Callable] | None = None) -> int:
    software_id = int(software["id"])
    kind = str(software.get("agent_kind") or "").strip().lower()
    adapter = get_adapter(kind)
    spec = get_adapter_spec(kind)
    if adapter is None or spec is None:
        return 0
    sources = list(spec.discover(software, stop_event))
    if not sources:
        return 0
    replay_skips = {}
    prepare = spec.replay_skips
    if prepare is not None:
        replay_skips = prepare(sources, stop_event)

    conn = pdb._connect()
    inserted = 0
    cache_hits = 0
    cache_misses = 0
    read_failures_before = common.sqlite_read_failure_count()
    try:
        cursor = conn.execute(
            "SELECT cursor_json FROM agent_software_runtime WHERE software_id=?",
            (software_id,),
        ).fetchone()
        try:
            states = json.loads(cursor[0]) if cursor and cursor[0] else {}
        except (TypeError, ValueError):
            states = {}
        if not isinstance(states, dict):
            states = {}

        for source_item in sources:
            if stop_event is not None and stop_event.is_set():
                return inserted
            try:
                before = source_item.path.stat()
            except OSError:
                discovery_warnings = tuple(
                    str(value) for value in
                    (source_item.context.get("discovery_warnings") or ())
                    if value
                )
                if discovery_warnings:
                    conn.execute(
                        "UPDATE agent_software_runtime SET last_error=? WHERE software_id=?",
                        ("; ".join(discovery_warnings)[:500], software_id),
                    )
                    conn.commit()
                continue
            state_key = source_item.state_key
            previous = states.get(state_key)
            always_scan = spec.always_scan
            if (not always_scan and isinstance(previous, dict)
                    and previous.get("size") == before.st_size
                    and _same_stat(previous, before)):
                continue
            skip = replay_skips.get(str(source_item.path), 0)
            source_read_failures_before = common.sqlite_read_failure_count()
            parser = (parser_overrides or {}).get(adapter.KIND) or adapter.parse
            fingerprint = (parse_cache.source_fingerprint(source_item.path)
                           if spec.parse_cache else None)
            cache_prefix = None
            from_cache = False
            parsed = None
            if fingerprint is not None:
                cache_prefix = parse_cache.cache_key(kind, state_key, skip, parser)
                parsed = parse_cache.lookup(cache_prefix, fingerprint)
                if parsed is None:
                    cache_misses += 1
                else:
                    from_cache = True
                    cache_hits += 1
            if parsed is None:
                raw = _parse(adapter, source_item, stop_event,
                             skip_token_count=skip, parser_overrides=parser_overrides)
                parsed = _coerce_batch(raw, kind=kind, source_item=source_item)
                if parsed is None:
                    return inserted
            try:
                after = source_item.path.stat()
            except OSError:
                continue
            read_failed = common.sqlite_read_failure_count() > source_read_failures_before
            stable = _unchanged(before, after)
            # A stop request can land *inside* parse: an adapter that forgets to
            # return skipped=True then hands back a half batch that looks
            # complete.  Such a batch must never be cached or recorded as the
            # source's state — either would freeze a partially read database
            # into a permanent undercount.
            interrupted = stop_event is not None and stop_event.is_set()
            if (cache_prefix is not None and not from_cache and stable
                    and not read_failed and not interrupted):
                # Only a batch produced from a source that stayed unchanged, was
                # not reported as skipped, needed no failed read and survived to
                # the end of the pass is reusable: caching a partial or failed
                # result would turn one transient failure into a permanent
                # undercount.
                parse_cache.store(cache_prefix, fingerprint, parsed,
                                  cacheable=not parsed.skipped)
            file_inserted = 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                for event in parsed.events:
                    if pdb._insert_agent_usage_row(
                            conn, software_id, event.model, event.prompt_tokens,
                            event.completion_tokens, event.cache_read_tokens,
                            event.total_tokens, event.requested_at, event.event_id,
                            duration_ms=event.duration_ms,
                            output_tps=_estimated_output_tps(event.completion_tokens, event.duration_ms)):
                        file_inserted += 1
                if stable and not parsed.skipped and not read_failed and not interrupted:
                    states[state_key] = {
                        **common.stat_identity(after),
                        "mtime": int(after.st_mtime),
                        "record_count": parsed.record_count,
                        "parsed_at": format_utc(utc_now()),
                    }
                    conn.execute(
                        "UPDATE agent_software_runtime SET cursor_json=?,last_scan_at=?,last_error=NULL WHERE software_id=?",
                        (json.dumps(states, ensure_ascii=False, separators=(",", ":")), format_utc(utc_now()), software_id),
                    )
                elif parsed.skipped and parsed.warnings:
                    conn.execute(
                        "UPDATE agent_software_runtime SET last_error=? WHERE software_id=?",
                        ("; ".join(parsed.warnings)[:500], software_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            inserted += file_inserted
        if spec.parse_cache and (cache_hits or cache_misses):
            log.info("agent usage parse cache: kind=%s hits=%d misses=%d",
                     kind, cache_hits, cache_misses)
        read_failures = common.sqlite_read_failure_count() - read_failures_before
        if read_failures:
            # A failed external read returns no rows, which is indistinguishable
            # from "this agent has no usage".  Record it on the runtime row so a
            # user can see that this pass was incomplete instead of only seeing
            # an empty result; the cursor of the affected source was not
            # advanced, so the next pass retries it.
            conn.execute(
                "UPDATE agent_software_runtime SET last_error=? WHERE software_id=?",
                (f"外部 SQLite 读取失败 {read_failures} 次：本轮这些来源的用量未计入，"
                 f"游标未推进，下一轮重试。"[:500], software_id),
            )
            conn.commit()
    except Exception as exc:
        try:
            conn.execute("UPDATE agent_software_runtime SET last_error=? WHERE software_id=?",
                         (str(exc)[:500], software_id))
            conn.commit()
        except Exception:
            conn.rollback()
        raise
    finally:
        conn.close()
    return inserted


# Sources whose record identity used to be the record's *position* in the file
# and is now the record's own content (``adapters/record_identity.py``).  Only
# those can hold legacy positional ids in the database.
LEGACY_EVENT_ID_KINDS = frozenset({
    "amp", "antigravity", "cline", "cursor", "dsh", "gemini-cli", "openclaw",
    "roo-code",
})

# Per-kind completion marker for the re-key below.  ``sync_state`` is the
# existing machine-local key/value table: it is stripped from configuration
# artifacts (``schema_upgrade/artifact.py``) and is never cloud-synced, so the
# markers describe this database only.  The migration is idempotent without
# them — a re-run finds no legacy ids left — but a marker also keeps the extra
# source parse off every later pass.
#
# The marker is per kind on purpose: one source that can never be read (a
# compressed DSH session without a decoder, an agent whose store is
# unavailable) must not keep the other kinds from completing their rewrite.
EVENT_ID_REKEY_MARKER_PREFIX = "agent_usage_event_id_rekey_v2"


def event_id_rekey_marker(kind: str) -> str:
    """The ``sync_state`` key recording that *kind* finished its rewrite."""
    return f"{EVENT_ID_REKEY_MARKER_PREFIX}:{str(kind).strip().lower()}"

_IDENTITY_SQL = (
    "requested_at,model,prompt_tokens,completion_tokens,cache_read_tokens")


def _event_identity(event: UsageEvent) -> tuple[str, str, int, int, int]:
    """The five fields the content-stable id is derived from."""
    return (str(event.requested_at), str(event.model), int(event.prompt_tokens),
            int(event.completion_tokens), int(event.cache_read_tokens))


def _row_identity(row) -> tuple[str, str, int, int, int]:
    return (str(row["requested_at"]), str(row["model"]), int(row["prompt_tokens"]),
            int(row["completion_tokens"]), int(row["cache_read_tokens"]))


def _next_import_events(software: dict, kind: str, stop_event=None):
    """Events the next import pass would write for ``software``.

    The adapters own the new ids, so the migration asks them instead of
    re-deriving an id (which would be a second implementation of the encoding).
    ``None`` means some source could not be read completely: pairing the wrong
    records is worse than waiting, and the import that follows this call will
    insert nothing for an unreadable source either.
    """
    adapter = get_adapter(kind)
    spec = get_adapter_spec(kind)
    if adapter is None or spec is None:
        return None
    sources = list(spec.discover(software, stop_event))
    replay_skips = {}
    if spec.replay_skips is not None:
        replay_skips = spec.replay_skips(sources, stop_event)
    events = []
    for source_item in sources:
        if stop_event is not None and stop_event.is_set():
            return None
        skip = replay_skips.get(str(source_item.path), 0)
        raw = _parse(adapter, source_item, stop_event, skip_token_count=skip,
                     parser_overrides=None)
        parsed = _coerce_batch(raw, kind=kind, source_item=source_item)
        if parsed is None or parsed.skipped:
            return None
        events.extend(parsed.events)
    return events


def _existing_event_ids(conn, event_ids: set[str]) -> set[str]:
    """Which of ``event_ids`` already occupy a row in either table."""
    found: set[str] = set()
    ordered = sorted(event_ids)
    for start in range(0, len(ordered), 400):
        chunk = ordered[start:start + 400]
        marks = ",".join("?" * len(chunk))
        for table in ("request_log", "agent_usage_receipts"):
            found.update(
                str(row[0]) for row in conn.execute(
                    f"SELECT event_id FROM {table} WHERE event_id IN ({marks})",
                    chunk))
    return found


def _mark_event_id_rekey_done(conn, kind: str) -> None:
    conn.execute(
        "INSERT INTO sync_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (event_id_rekey_marker(kind), format_utc(utc_now())))


def _content_identity_adapters_installed() -> bool:
    """Whether the adapters that produce content identities are present.

    A tree with only the positional adapters cannot be holding ids that no
    adapter will produce again, so the migration must not claim to be done
    there: setting the marker would suppress the rewrite after the adapters are
    upgraded.
    """
    try:
        from .adapters import record_identity  # noqa: F401
    except ImportError:
        return False
    return True


def _log_rekey_outcome(kind: str, changed: int, unpaired: int,
                       occupied: int) -> None:
    """Report the re-key result, including the rows it deliberately kept.

    A kept row is either a record that no longer exists in its source (harmless:
    it can never collide) or a target id that another row already owns (it will
    be retried only if that other row disappears).  Both are worth a line, but
    neither is a reason to abort the rewrite.
    """
    if not (changed or unpaired or occupied):
        return
    log.info("agent usage event_id re-key (%s): %d row(s) re-keyed, "
             "%d legacy row(s) left unpaired, %d target id(s) already present",
             kind, changed, unpaired, occupied)


def _rekey_precondition(kind: str) -> tuple[bool, str]:
    """Cheap up-front check before paying for a kind's sources.

    ``dsh`` stores almost every session compressed, and its adapter reports an
    undecodable session as ``skipped``.  Without a decoder library that would
    leave the kind permanently deferred while still re-scanning (and
    re-decoding) every session on every pass, so the rewrite waits for the
    decoder instead of paying for a scan that cannot succeed.  The check is a
    library import, not a file walk.
    """
    if kind != "dsh":
        return True, ""
    try:
        from .adapters import zstd_reader
    except ImportError:
        # Adapters in this tree do not use the library at all.
        return True, ""
    if zstd_reader.available():
        return True, ""
    return False, "no Zstandard decoder library is importable"


def _rekey_plan_for_kind(conn, kind: str, softwares: list[dict],
                         stop_event) -> tuple[list[tuple[str, str]], int, int, bool]:
    """Work out the rewrites for one kind: ``(pairs, unpaired, occupied, ok)``.

    ``ok`` is False when some source of this kind could not be read completely;
    the caller then leaves this kind for a later pass instead of guessing pairs.
    """
    pairs: list[tuple[str, str]] = []
    unpaired = 0
    occupied_targets = 0
    for software in softwares:
        if stop_event is not None and stop_event.is_set():
            return pairs, unpaired, occupied_targets, False
        software_id = int(software["id"])
        rows = conn.execute(
            f"SELECT id,event_id,{_IDENTITY_SQL} FROM request_log "
            "WHERE agent_software_id=? ORDER BY id",
            (software_id,)).fetchall()
        if not rows:
            continue
        events = _next_import_events(software, kind, stop_event)
        if events is None:
            return pairs, unpaired, occupied_targets, False
        current_ids = {event.event_id for event in events}
        legacy = [row for row in rows if str(row["event_id"]) not in current_ids]
        if not legacy:
            continue
        stale: dict[tuple, list] = {}
        for row in legacy:
            stale.setdefault(_row_identity(row), []).append(row)
        # A target id may already exist (this pass normally runs before the
        # import, but a deferred pass can leave rows the adapters produced in
        # the meantime).  Re-keying onto an occupied id would violate the UNIQUE
        # constraint and overwrite an existing record, so those targets are
        # dropped and the legacy row stays.
        candidates = [event for event in events if _event_identity(event) in stale]
        taken = _existing_event_ids(
            conn, {event.event_id for event in candidates})
        fresh: dict[tuple, list[UsageEvent]] = {}
        for event in candidates:
            if event.event_id in taken:
                occupied_targets += 1
                continue
            fresh.setdefault(_event_identity(event), []).append(event)
        for identity, group in stale.items():
            # Old rows in request_log.id order (the query's ORDER BY) pair with
            # new events in event_id order: both orders are stable, and the
            # records in one group are indistinguishable in the IR, so which one
            # takes which occurrence cannot change an amount.
            targets = sorted(fresh.get(identity, ()), key=lambda item: item.event_id)
            for row, event in zip(group, targets):
                pairs.append((str(row["event_id"]), event.event_id))
            unpaired += max(0, len(group) - len(targets))
    return pairs, unpaired, occupied_targets, True


def rekey_legacy_event_ids(pdb, stop_event=None) -> int:
    """Re-key positional ``event_id``s to the adapters' content identities.

    ``adapters/record_identity.py`` replaced positional record identities with
    content ones, so a row imported before that change keeps an id no adapter
    will ever produce again: its receipt no longer matches and the source's
    history is imported a second time.  This pass rewrites those ids once.

    The rewrite is per kind, and so is its completion marker: a kind whose
    store cannot be read (a compressed DSH session without a decoder, an agent
    that is not running) is deferred on its own and never blocks the others.

    Only ``event_id`` changes, in ``agent_usage_receipts`` and
    ``request_log``, in one transaction per kind; tokens, timestamps, costs,
    ``pricing_status`` and the FX lock are untouched, and no row is deleted.
    A legacy row whose record is gone from its source is left as it is: it can
    never collide with a new id, and rewriting it would be a guess.

    Returns the number of rows re-keyed (0 when nothing was left to do).
    """
    if not _content_identity_adapters_installed():
        return 0
    conn = pdb._connect()
    try:
        done = {str(row[0]) for row in conn.execute(
            "SELECT key FROM sync_state WHERE key LIKE ?",
            (f"{EVENT_ID_REKEY_MARKER_PREFIX}:%",))}
        by_kind: dict[str, list[dict]] = {}
        for software in pdb.get_agent_software():
            kind = str(software.get("agent_kind") or "").strip().lower()
            if (kind in LEGACY_EVENT_ID_KINDS
                    and event_id_rekey_marker(kind) not in done):
                by_kind.setdefault(kind, []).append(software)
        if not by_kind:
            return 0
        changed = 0
        deferred: set[str] = set()
        not_ready: dict[str, str] = {}
        for kind in sorted(by_kind):
            if stop_event is not None and stop_event.is_set():
                return changed
            ready, reason = _rekey_precondition(kind)
            if not ready:
                not_ready[kind] = reason
                continue
            try:
                pairs, unpaired, occupied, ok = _rekey_plan_for_kind(
                    conn, kind, by_kind[kind], stop_event)
            except Exception:  # noqa: BLE001 - one kind must not stop the rest
                # An adapter that raises while the migration merely reads it is
                # a reason to defer that kind, not to abandon the rewrite of the
                # others or, worse, to let the import run without it.
                log.warning(
                    "agent usage event_id re-key skipped %s: reading its "
                    "sources failed", kind, exc_info=True)
                deferred.add(kind)
                continue
            if not ok:
                deferred.add(kind)
                continue
            duplicates = len(pairs) - len({new for _, new in pairs})
            occupied_now = _existing_event_ids(conn, {new for _, new in pairs})
            if duplicates or occupied_now:
                log.warning(
                    "agent usage event_id re-key aborted for %s: %d duplicate "
                    "target(s), %d target(s) already present; both tables are "
                    "unchanged for that kind",
                    kind, duplicates, len(occupied_now))
                continue
            conn.execute("BEGIN IMMEDIATE")
            try:
                for old, new in pairs:
                    conn.execute(
                        "UPDATE agent_usage_receipts SET event_id=? "
                        "WHERE event_id=?", (new, old))
                    conn.execute(
                        "UPDATE request_log SET event_id=? WHERE event_id=?",
                        (new, old))
                _mark_event_id_rekey_done(conn, kind)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            _log_rekey_outcome(kind, len(pairs), unpaired, occupied)
            changed += len(pairs)
        if deferred:
            log.warning(
                "agent usage event_id re-key deferred for %s: a source is "
                "unreadable; those ids will be rewritten on a later pass",
                ", ".join(sorted(deferred)))
        if not_ready:
            # No marker is written: once the missing runtime is installed the
            # rewrite completes on the next pass without any manual step.
            log.warning(
                "agent usage event_id re-key waiting for %s; %d software row(s) "
                "keep their current ids until the runtime is available",
                ", ".join(f"{kind} ({reason})"
                          for kind, reason in sorted(not_ready.items())),
                sum(len(by_kind[kind]) for kind in not_ready))
        return changed
    finally:
        conn.close()


def import_once(pdb, stop_event=None, *, parser_overrides: Mapping[str, Callable] | None = None) -> int:
    if stop_event is not None and stop_event.is_set():
        return 0
    if parser_overrides is None:
        try:
            rekey_legacy_event_ids(pdb, stop_event)
        except Exception:  # noqa: BLE001 - a failed migration must not stop imports
            # The migration is idempotent and finishes its own transaction, so a
            # failure leaves both tables untouched; the next pass retries it.
            log.warning(
                "agent usage event_id re-key did not complete; "
                "it will be retried on the next pass", exc_info=True)
    inserted = 0
    for software in pdb.get_agent_software():
        kind = str(software.get("agent_kind") or "").strip().lower()
        if kind in get_registered_kinds():
            inserted += _import_software(pdb, software, stop_event, parser_overrides)
    return inserted


def get_registered_kinds() -> tuple[str, ...]:
    from .registry import ADAPTERS
    return tuple(ADAPTERS)
