#!/usr/bin/env python3
"""Standalone runtime maintenance service for Token Board.

This process owns only local/runtime work. Configuration mutation remains in
the manually started dashboard, and schema upgrades remain in ``start.sh
--all``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import threading
from pathlib import Path

from app.core.time import format_utc, utc_now
from app.db.proxy_db import ProxyDatabase
from app.db.proxy.billing import materialize_all_period_charges
from app.services import fx
from app.services.agent_usage.importer import import_once
from app.services.runtime_tasks import (
    AGENT_USAGE_IMPORT_INTERVAL_SECONDS,
    AgentUsageImportWorker,
    _periodic,
)

log = logging.getLogger(__name__)


class MaintenanceService:
    """Own three periodic workers and a local importer wake socket."""

    def __init__(self, db_path: str, schema_dir: str, socket_path: str,
                 health_path: str):
        self.db_path = db_path
        self.schema_dir = schema_dir
        self.socket_path = Path(socket_path)
        self.health_path = Path(health_path)
        self.stop_event = threading.Event()
        self.health: dict = {
            "pid": os.getpid(),
            "started_at": format_utc(utc_now()),
            "heartbeat_at": format_utc(utc_now()),
            "tasks": {},
        }
        self.health_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.proxy_db: ProxyDatabase | None = None
        self.importer: AgentUsageImportWorker | None = None
        self.sock: socket.socket | None = None

    def _write_health(self) -> None:
        self.health_path.parent.mkdir(parents=True, exist_ok=True)
        with self.health_lock:
            payload = dict(self.health)
            payload["tasks"] = {
                name: dict(value) for name, value in self.health.items()
                if name not in {"pid", "started_at", "heartbeat_at", "tasks"}
            }
            # Keep task entries out of the top level; older in-process health
            # dictionaries used that shape, but the sidecar contract exposes
            # one explicit ``tasks`` object for the standalone service.
            for name in list(payload):
                if name not in {"pid", "started_at", "heartbeat_at", "tasks"}:
                    payload.pop(name, None)
            payload["heartbeat_at"] = format_utc(utc_now())
        tmp = self.health_path.with_name(self.health_path.name + f".tmp.{os.getpid()}")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.health_path)

    def _heartbeat(self) -> None:
        while not self.stop_event.wait(15):
            try:
                self._write_health()
            except Exception:
                log.exception("failed to write maintenance health")

    def _prepare_socket(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            if not self.socket_path.is_socket():
                raise RuntimeError(f"maintenance socket is not a socket: {self.socket_path}")
            self.socket_path.unlink()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.sock.settimeout(1.0)

    def _socket_loop(self) -> None:
        assert self.sock is not None
        while not self.stop_event.is_set():
            try:
                message = self.sock.recv(32)
            except socket.timeout:
                continue
            except OSError:
                break
            if message.strip() == b"IMPORT" and self.importer is not None:
                self.importer.trigger()

    def start(self) -> None:
        self.proxy_db = ProxyDatabase(self.db_path, schema_dir=self.schema_dir)
        self._prepare_socket()
        health = self.health
        lock = self.health_lock

        def prewarm_fx() -> None:
            assert self.proxy_db is not None
            conn = self.proxy_db._connect()
            try:
                failures = []
                fx.ensure_rate(conn, on_error=failures.append)
                if failures:
                    raise RuntimeError(
                        f"FX refresh failed: {type(failures[0]).__name__}: {failures[0]}")
            finally:
                conn.close()

        workers = [
            ("fx-prewarm", 86400, prewarm_fx),
            ("billing-materializer", 60,
             lambda: materialize_all_period_charges(self.db_path)),
        ]
        for name, interval, action in workers:
            thread = threading.Thread(
                target=_periodic,
                args=(self.stop_event, interval, name, action, health, lock),
                daemon=True,
                name=name,
            )
            self.threads.append(thread)
            thread.start()

        assert self.proxy_db is not None
        self.importer = AgentUsageImportWorker(
            lambda: import_once(self.proxy_db, stop_event=self.stop_event),
            interval=AGENT_USAGE_IMPORT_INTERVAL_SECONDS,
            health=health,
            health_lock=lock,
        )
        self.threads.append(self.importer.thread)
        self.importer.start()

        socket_thread = threading.Thread(
            target=self._socket_loop, daemon=True, name="maintenance-socket")
        self.threads.append(socket_thread)
        socket_thread.start()
        heartbeat = threading.Thread(
            target=self._heartbeat, daemon=True, name="maintenance-health")
        self.threads.append(heartbeat)
        heartbeat.start()
        self._write_health()

    def stop(self, join_timeout: float = 10.0) -> None:
        self.stop_event.set()
        if self.importer is not None:
            self.importer.stop()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        for thread in self.threads:
            thread.join(timeout=join_timeout)
        try:
            if self.socket_path.is_socket():
                self.socket_path.unlink()
        except OSError:
            log.exception("failed to remove maintenance socket")
        with self.health_lock:
            for task in self.health["tasks"].values():
                task["status"] = "stopped"
        try:
            self._write_health()
        except Exception:
            log.exception("failed to write final maintenance health")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Token Board runtime maintenance")
    parser.add_argument("--token-board-db", required=True)
    parser.add_argument("--schema-dir", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--health", required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    service = MaintenanceService(
        args.token_board_db, args.schema_dir, args.socket, args.health)
    stopping = threading.Event()

    def handle_signal(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    try:
        service.start()
    except Exception as exc:
        log.exception("maintenance startup failed")
        service.stop(join_timeout=2.0)
        print(f"maintenance startup failed: {exc}", flush=True)
        return 2
    stopping.wait()
    service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
