"""Lifecycle-managed background work for the dashboard process."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

from app.core.time import format_utc, utc_now

log = logging.getLogger(__name__)

AGENT_USAGE_IMPORT_INTERVAL_SECONDS = 30 * 60


def _set_health(health: dict, lock: threading.Lock, name: str,
                status: str, error: str | None = None) -> None:
    now = format_utc(utc_now())
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


class AgentUsageImportWorker:
    """Serialize startup, periodic and browser-triggered usage imports.

    There is deliberately only one worker thread.  Browser requests wake that
    thread instead of starting their own import, so a page refresh can never
    race the scheduled pass against the same SQLite import cursor.
    """

    def __init__(self, action: Callable[[], int], *, interval: float,
                 health: dict, health_lock: threading.Lock) -> None:
        self._action = action
        self._interval = interval
        self._health = health
        self._health_lock = health_lock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="agent-usage-importer")

    def start(self) -> None:
        self.thread.start()

    @property
    def stop_event(self) -> threading.Event:
        """Cancellation signal shared with the currently running import."""
        return self._stop

    def trigger(self) -> bool:
        """Request an extra pass; repeated requests are safely coalesced."""
        if self._stop.is_set() or not self.thread.is_alive():
            return False
        self._wake.set()
        return True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run_once(self) -> None:
        try:
            inserted = self._action()
            _set_health(
                self._health, self._health_lock,
                "agent-usage-importer", "ok")
            with self._health_lock:
                self._health["agent-usage-importer"]["last_inserted"] = inserted
        except Exception as exc:
            log.exception("background task failed: agent-usage-importer")
            _set_health(
                self._health, self._health_lock,
                "agent-usage-importer", "degraded",
                f"{type(exc).__name__}: {exc}")

    def _run(self) -> None:
        # Anchor the automatic cadence to worker startup.  Browser wake-ups add
        # passes but never postpone the next 30-minute deadline.
        next_deadline = time.monotonic() + self._interval
        try:
            if self._stop.is_set():
                return
            self._run_once()
            while not self._stop.is_set():
                timeout = max(0.0, next_deadline - time.monotonic())
                browser_triggered = self._wake.wait(timeout)
                if self._stop.is_set():
                    break
                now = time.monotonic()
                if browser_triggered:
                    # Clear before the action so a trigger received while it is
                    # running remains pending for one coalesced follow-up pass.
                    self._wake.clear()
                if browser_triggered and now < next_deadline:
                    self._run_once()
                    continue

                # A trigger arriving at the deadline is satisfied by this
                # scheduled pass instead of causing two back-to-back scans.
                # Clear immediately before the action so a trigger that raced
                # with a timed-out wait is included, while one arriving during
                # the action remains pending for a follow-up.
                self._wake.clear()
                self._run_once()
                now = time.monotonic()
                while next_deadline <= now:
                    next_deadline += self._interval
        finally:
            _set_health(
                self._health, self._health_lock,
                "agent-usage-importer", "stopped")


def start_dashboard_tasks(flask_app, proxy_db) -> None:
    """Start only the config-mutating dashboard finalizer.

    FX refresh, billing materialization, and agent import are owned by the
    standalone token-maintenance service.  This worker is enabled by the
    configuration session only after a cloud baseline is ready.
    """
    if flask_app.config.get("DASHBOARD_TASKS_STARTED"):
        return
    flask_app.config["DASHBOARD_TASKS_STARTED"] = True
    health = flask_app.config.setdefault("BACKGROUND_TASK_HEALTH", {})
    health_lock = flask_app.config.setdefault(
        "BACKGROUND_TASK_HEALTH_LOCK", threading.Lock())
    stop = threading.Event()
    flask_app.config["DELETION_FINALIZER_STOP"] = stop
    threads = flask_app.config.setdefault("DASHBOARD_TASK_THREADS", [])
    thread = threading.Thread(
        target=_periodic,
        args=(stop, 60, "deletion-finalizer",
              proxy_db.finalize_deferred_deletions, health, health_lock),
        daemon=True,
        name="deletion-finalizer",
    )
    threads.append(thread)
    thread.start()


def trigger_agent_usage_import(flask_app) -> bool:
    """Wake the standalone importer after a dashboard is opened."""
    socket_path = flask_app.config.get("MAINTENANCE_SOCKET")
    if not socket_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.settimeout(0.2)
            client.sendto(b"IMPORT", socket_path)
        return True
    except (OSError, socket.timeout):
        log.debug("maintenance importer socket is unavailable: %s", socket_path)
        return False


def stop_dashboard_tasks(flask_app, join_timeout: float = 2.0) -> None:
    """Stop dashboard-owned workers without touching proxy maintenance."""
    stop = flask_app.config.get("DELETION_FINALIZER_STOP")
    if isinstance(stop, threading.Event):
        stop.set()
    all_stopped = True
    for thread in flask_app.config.get("DASHBOARD_TASK_THREADS", []):
        thread.join(timeout=join_timeout)
        all_stopped = all_stopped and not thread.is_alive()
    flask_app.config["DASHBOARD_TASKS_STARTED"] = not all_stopped
