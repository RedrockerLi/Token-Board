"""Lifecycle-managed background work for the dashboard process."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from app.db.proxy.billing import materialize_period_charges

log = logging.getLogger(__name__)


def _set_health(health: dict, lock: threading.Lock, name: str,
                status: str, error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with lock:
        item = health.setdefault(name, {})
        item.update({"status": status, "last_run": now})
        if error:
            item["last_error"] = error
        else:
            item.pop("last_error", None)


def _periodic(stop: threading.Event, interval: int, name: str, action,
              health: dict | None = None, lock: threading.Lock | None = None) -> None:
    health = health if health is not None else {}
    lock = lock if lock is not None else threading.Lock()
    while not stop.is_set():
        try:
            action()
            _set_health(health, lock, name, "ok")
        except Exception:
            log.exception("background task failed: %s", name)
            _set_health(health, lock, name, "degraded", "action failed")
        stop.wait(interval)
    _set_health(health, lock, name, "stopped")


def start_runtime_tasks(flask_app, proxy_db, proxy_db_path: str) -> None:
    """Start FX, lifecycle and billing workers with shared logging.

    Agent usage import (codex) is no longer in-process: it runs as an
    independent systemd user timer (token-agent-import) every 30 minutes,
    fully decoupled from this server.
    """
    from app.services import fx

    def prewarm_fx() -> None:
        conn = proxy_db._connect()
        try:
            failures = []
            fx.ensure_rate(conn, on_error=failures.append)
            if failures:
                raise RuntimeError(
                    f"FX refresh failed: {type(failures[0]).__name__}: {failures[0]}")
        finally:
            conn.close()

    health = flask_app.config.setdefault("BACKGROUND_TASK_HEALTH", {})
    health_lock = flask_app.config.setdefault(
        "BACKGROUND_TASK_HEALTH_LOCK", threading.Lock())
    threads = flask_app.config.setdefault("BACKGROUND_TASK_THREADS", [])
    workers = [
        ("fx-prewarm", 86400, prewarm_fx),
        ("deletion-finalizer", 60, proxy_db.finalize_deferred_deletions),
        ("billing-materializer", 60,
         lambda: materialize_period_charges(proxy_db_path)),
    ]
    for name, interval, action in workers:
        stop = threading.Event()
        flask_app.config[f"{name.upper().replace('-', '_')}_STOP"] = stop
        thread = threading.Thread(
            target=_periodic,
            args=(stop, interval, name, action, health, health_lock),
            daemon=True, name=name)
        threads.append(thread)
        thread.start()


def stop_runtime_tasks(flask_app, join_timeout: float = 2.0) -> None:
    """Request a clean stop for all app-owned workers.

    The factory keeps worker handles in config so tests and controlled
    shutdowns can stop background work without leaving importer/materializer
    threads attached to a discarded Flask app.
    """
    for key, value in flask_app.config.items():
        if key.endswith("_STOP") and isinstance(value, threading.Event):
            value.set()
    for thread in flask_app.config.get("BACKGROUND_TASK_THREADS", []):
        thread.join(timeout=join_timeout)
