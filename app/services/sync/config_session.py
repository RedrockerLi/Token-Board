"""In-process configuration-session state for the manual dashboard.

The session coordinates one startup/retry pull, one endpoint change, or one
upload at a time. It intentionally has no durable queue or distributed lock:
the cloud is the authority and this process is the single editor.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from app.services.sync.config_sync import sync_config_pull, sync_config_upload
from app.services.sync.settings import SyncConfig, load_sync_config, save_sync_config
from app.services.sync.snapshot import snapshot_config
from app.services.sync.state import get_sync_state
from app.services.sync.webdav import WebDAVClient

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConfigSessionStatus:
    state: str
    writable: bool
    message: str | None = None
    remote_artifact: str | None = None


class ConfigSession:
    """Own the dashboard process's ephemeral cloud-sync state."""

    VALID_STATES = frozenset({"syncing", "writable", "read_only", "local_only"})

    def __init__(self, db_path: str, schema_dir: str | None = None,
                 *, on_writable=None):
        self.db_path = db_path
        self.schema_dir = schema_dir
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._pull_thread: threading.Thread | None = None
        self._state = "syncing"
        self._message: str | None = None
        self._on_writable = on_writable

    def start(self) -> None:
        """Start the first pull without blocking Flask from serving usage."""
        self.trigger_pull()

    def status(self) -> ConfigSessionStatus:
        with self._lock:
            remote = get_sync_state(self.db_path, "remote_artifact")
            return ConfigSessionStatus(
                self._state,
                self._state in {"writable", "local_only"},
                self._message,
                remote,
            )

    def is_writable(self) -> bool:
        with self._lock:
            return self._state in {"writable", "local_only"}

    def _set_state(self, state: str, message: str | None = None) -> None:
        if state not in self.VALID_STATES:
            raise ValueError(f"invalid config session state: {state}")
        callback = None
        with self._lock:
            self._state = state
            self._message = message
            if state in {"writable", "local_only"}:
                callback = self._on_writable
        if callback is not None:
            try:
                callback()
            except Exception:
                log.exception("dashboard writable callback failed")

    def trigger_pull(self) -> bool:
        """Start one pull/retry, coalescing repeated requests."""
        with self._lock:
            if self._pull_thread is not None and self._pull_thread.is_alive():
                return False
            self._state = "syncing"
            self._message = None
            thread = threading.Thread(
                target=self._pull_worker,
                name="config-sync",
                daemon=True,
            )
            self._pull_thread = thread
            thread.start()
            return True

    def _pull_worker(self) -> None:
        with self._operation:
            try:
                config = load_sync_config(self.db_path)
                result = sync_config_pull(
                    self.db_path, schema_dir=self.schema_dir, config=config)
                status = result.get("status")
                if status == "unconfigured":
                    self._set_state("local_only")
                    return
                if status == "empty":
                    # A reachable empty directory gets a single local seed.
                    seeded = sync_config_upload(
                        self.db_path, schema_dir=self.schema_dir, config=config)
                    if seeded.get("status") == "ok":
                        self._set_state("writable")
                    else:
                        self._set_state(
                            "read_only",
                            seeded.get("message", "云端为空且无法建立配置基线"),
                        )
                    return
                if status == "pulled":
                    self._set_state("writable")
                    return
                self._set_state(
                    "read_only", result.get("message", "云端配置拉取失败"))
            except Exception as exc:
                log.exception("configuration startup pull failed")
                self._set_state("read_only", f"云端配置拉取失败: {exc}")

    def upload(self) -> dict:
        """Upload the current config; failed PUTs roll back to the baseline."""
        with self._lock:
            state = self._state
        if state == "local_only":
            return {"status": "unconfigured", "message": "未配置同步服务器"}
        if state != "writable":
            return {
                "status": "read_only",
                "message": self.status().message or "云端配置尚未就绪",
            }
        with self._operation:
            result = sync_config_upload(
                self.db_path, schema_dir=self.schema_dir)
            if result.get("status") == "error":
                self._set_state("read_only", result.get("message"))
            return result

    def switch_endpoint(self, candidate: SyncConfig) -> dict:
        """Test a candidate WebDAV target and establish its first baseline."""
        with self._operation:
            error = WebDAVClient(candidate).test_connection()
            if error:
                return {"status": "error", "message": f"连接失败: {error}"}

            result = sync_config_pull(
                self.db_path, schema_dir=self.schema_dir, config=candidate)
            if result.get("status") == "pulled":
                save_sync_config(self.db_path, candidate)
                snapshot_config(self.db_path)
                self._set_state("writable")
                return {"status": "pulled", "message": "已拉取新云端配置"}
            if result.get("status") == "empty":
                seeded = sync_config_upload(
                    self.db_path, schema_dir=self.schema_dir, config=candidate)
                if seeded.get("status") == "ok":
                    save_sync_config(self.db_path, candidate)
                    snapshot_config(self.db_path)
                    self._set_state("writable")
                    return {"status": "seeded", "message": "已建立新云端配置"}
                self._set_state("read_only", seeded.get("message"))
                return seeded

            self._set_state("read_only", result.get("message"))
            return result


__all__ = ["ConfigSession", "ConfigSessionStatus"]
