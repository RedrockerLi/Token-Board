"""Functional WebDAV synchronization module."""

import json
import hashlib
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import requests
from requests.auth import HTTPBasicAuth

from app.core import sqlite_runtime
from app.core.time import utc_now
from app.services.sync.settings import SyncConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteArtifact:
    name: str
    timestamp: datetime | None = None
    etag: str | None = None


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _filename_now() -> datetime:
    """Return the legacy local wall-clock used in artifact filenames.

    Runtime timestamps use UTC, but artifact names are an existing remote
    ordering contract.  Keep that clock independent so consolidating runtime
    time helpers cannot silently rename a user's next upload.
    """

    return datetime.now()


def _make_timestamped_name(base: str,
                           clock: Callable[[], datetime] | None = None) -> str:
    """dashboard_sync.db → dashboard_sync_20260716_143025.db"""
    name, ext = base.rsplit(".", 1)
    stamp = (clock or _filename_now)().strftime("%Y%m%d_%H%M%S")
    return f"{name}_{stamp}.{ext}"


def _schema_manifest(db_path: str, artifact_name: str) -> dict:
    """Describe the exact schema image published alongside a sync artifact."""
    conn = sqlite_runtime.connect(db_path, "shadow_copy")
    try:
        version = conn.execute(
            "SELECT major,minor,database_name FROM schema_version WHERE id=1"
        ).fetchone()
        pragma = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    if version is None or pragma != int(version[0]) * 10_000 + int(version[1]):
        raise WebDAVError("schema version metadata is inconsistent")
    return {
        "artifact": artifact_name,
        "database": version[2],
        "major": int(version[0]),
        "minor": int(version[1]),
        "user_version": pragma,
        "sha256": file_checksum(Path(db_path)),
        "published_at": utc_now().isoformat(timespec="seconds"),
    }


def publish_schema_manifest(config: SyncConfig, db_path: str,
                            artifact_name: str,
                            *,
                            filename_clock: Callable[[], datetime] | None = None) -> None:
    """Publish a separate V1 marker without replacing older remote files.

    Transition never mutates the remote V0 artifact.  Every normal V1 upload
    gets an immutable marker, so an operator can verify the major before
    allowing a node to download or publish configuration.
    """
    marker = _schema_manifest(db_path, artifact_name)
    marker_path = Path(db_path).with_name(f"{artifact_name}.schema.json")
    marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    try:
        remote_name = (f"schema_manifest_{artifact_name}_v{marker['major']}-"
                       f"{marker['minor']}_{(filename_clock or _filename_now)().strftime('%Y%m%d_%H%M%S')}.json")
        upload_artifact(config, str(marker_path), remote_filename=remote_name)
    finally:
        marker_path.unlink(missing_ok=True)


def list_artifacts(config, prefix: str) -> list[RemoteArtifact]:
    """List filenames in the sync folder matching a prefix via PROPFIND.

    Returns sorted list of matching filenames.
    """
    folder_url = f"{config.base_url.rstrip('/')}/{config.folder.strip('/')}/"
    resp = requests.request(
        "PROPFIND", folder_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=15,
        headers={"Depth": "1"},
    )
    if not resp.ok:
        return []

    # Keep this dependency-free. Each response block may expose href,
    # getetag and getlastmodified; missing properties remain None.
    blocks = re.findall(r"<[^:>]*:?response[^>]*>(.*?)</[^:>]*:?response>",
                        resp.text, flags=re.IGNORECASE | re.DOTALL)
    artifacts: list[RemoteArtifact] = []
    for block in blocks or [resp.text]:
        href = re.search(r"<[^:>]*:?href[^>]*>([^<]+)</", block,
                         flags=re.IGNORECASE)
        if not href:
            continue
        name = unquote(href.group(1).rstrip("/").rsplit("/", 1)[-1])
        if not name.startswith(prefix):
            continue
        etag = re.search(r"<[^:>]*:?getetag[^>]*>([^<]+)</", block,
                         flags=re.IGNORECASE)
        modified = re.search(r"<[^:>]*:?getlastmodified[^>]*>([^<]+)</",
                             block, flags=re.IGNORECASE)
        stamp = None
        if modified:
            try:
                stamp = datetime.strptime(modified.group(1).strip(),
                                           "%a, %d %b %Y %H:%M:%S %Z")
            except ValueError:
                stamp = None
        artifacts.append(RemoteArtifact(name, stamp,
                                         etag.group(1).strip() if etag else None))
    return sorted(artifacts,
                  key=lambda item: (item.timestamp or datetime.min, item.name))


_ARTIFACT_RE = re.compile(
    r"^(?P<base>token-board_config|dashboard_sync)_(?P<stamp>\d{8}_\d{6})(?:_\w+)?\.db$")


def _artifact_sort_key(name: str) -> tuple[datetime, str]:
    match = _ARTIFACT_RE.match(name)
    if not match:
        return (datetime.min, name)
    try:
        stamp = datetime.strptime(match.group("stamp"), "%Y%m%d_%H%M%S")
    except ValueError:
        stamp = datetime.min
    return stamp, name


def latest_artifact(config, base: str) -> RemoteArtifact | None:
    files = [item for item in list_artifacts(config, base + "_")
             if _ARTIFACT_RE.match(item.name)]
    if not files:
        return None
    return max(files, key=lambda item: (_artifact_sort_key(item.name),
                                        item.timestamp or datetime.min))


class WebDAVError(Exception):
    pass


class WebDAVConflict(WebDAVError):
    """The remote version changed while a versioned artifact was prepared."""


class WebDAVClient:
    """Explicit transport boundary for one configured WebDAV endpoint.

    Domain workflows use these methods instead of knowing URL, auth, PROPFIND
    XML, or HTTP status details.
    """

    def __init__(self, config: SyncConfig):
        self.config = config

    def list_artifacts(self, prefix: str) -> list[RemoteArtifact]:
        return list_artifacts(self.config, prefix)

    def find_artifact(self, name: str) -> RemoteArtifact | None:
        return find_artifact(self.config, name)

    def download_artifact(self, name: str, destination: str) -> bool:
        return download_artifact(self.config, destination, remote_filename=name)

    def upload_artifact(self, source: str, name: str, *,
                        if_match: str | None = None,
                        if_none_match: bool = False) -> None:
        upload_artifact(self.config, source, remote_filename=name,
                        if_match=if_match, if_none_match=if_none_match)

    def test_connection(self) -> str | None:
        return test_connection(self.config)


class ArtifactTransaction:
    """Shared immutable-artifact publication protocol.

    A successful publication is PUT followed by a confirmation PROPFIND that
    contains the exact new filename.  The clock is called inside each publish
    attempt; callers retrying the complete workflow therefore obtain a fresh
    filename instead of reusing a colliding timestamp.
    """

    def __init__(self, client: WebDAVClient, *,
                 filename_clock: Callable[[], datetime] | None = None,
                 retry_count: int = 3, retry_interval: float = 0.0,
                 sleeper: Callable[[float], None] = time.sleep):
        if retry_count < 1:
            raise ValueError("retry_count must be positive")
        if retry_interval < 0:
            raise ValueError("retry_interval must not be negative")
        self.client = client
        self.filename_clock = filename_clock or _filename_now
        self.retry_count = retry_count
        self.retry_interval = retry_interval
        self.sleeper = sleeper

    @staticmethod
    def _changed(expected: RemoteArtifact | None,
                 current: RemoteArtifact | None) -> bool:
        if expected is None or current is None:
            return False
        if expected.etag and current.etag:
            return expected.etag != current.etag
        return expected.name != current.name

    def publish_versioned_artifact(
            self, source: str, base: str,
            expected: RemoteArtifact | None = None,
            *, list_latest: Callable[[], RemoteArtifact | None] | None = None,
            upload: Callable[[str, str], None] | None = None,
            _attempt: int = 0) -> RemoteArtifact:
        latest = list_latest or (lambda: latest_artifact_from_client(self.client, base))
        put = upload or (
            lambda path, name: self.client.upload_artifact(
                path, name, if_none_match=True))
        current = latest()
        if self._changed(expected, current):
            raise WebDAVConflict("远端版本在上传前已变化")

        # This call is intentionally per publication, not in __init__; a
        # complete retry invokes this method again and gets a new clock value.
        remote_name = _make_timestamped_name(base + ".db", self.filename_clock)
        if _attempt:
            stem, extension = remote_name.rsplit(".", 1)
            remote_name = f"{stem}_{_attempt}.{extension}"
        put(source, remote_name)
        # Confirmation is an exact-name PROPFIND observation.  Looking only
        # at the latest artifact would report a false failure if another
        # writer published a newer file between our PUT and this check.
        confirmed = next(
            (item for item in self.client.list_artifacts(base + "_")
             if item.name == remote_name),
            None,
        )
        if confirmed is not None:
            return confirmed
        raise WebDAVError("上传后未能通过 PROPFIND 确认新 artifact")

    def publish_with_retry(self, source: str, base: str,
                           expected: RemoteArtifact | None = None) -> RemoteArtifact:
        """Retry only the transport-level publication with fresh filenames."""

        last_error: Exception | None = None
        for attempt in range(self.retry_count):
            try:
                return self.publish_versioned_artifact(
                    source, base, expected, _attempt=attempt)
            except WebDAVError as exc:
                last_error = exc
                if attempt + 1 == self.retry_count:
                    break
                if self.retry_interval:
                    self.sleeper(self.retry_interval)
                expected = None
        assert last_error is not None
        raise last_error


def latest_artifact_from_client(client: WebDAVClient,
                                base: str) -> RemoteArtifact | None:
    files = [item for item in client.list_artifacts(base + "_")
             if _ARTIFACT_RE.match(item.name)]
    return max(files, key=lambda item: (_artifact_sort_key(item.name),
                                        item.timestamp or datetime.min),
               default=None)


def find_artifact(config: SyncConfig, name: str) -> RemoteArtifact | None:
    """Return the exact remote filename observed by a fresh PROPFIND."""
    for item in list_artifacts(config, name):
        if item.name == name:
            return item
    return None


def _build_url(config: SyncConfig, remote_filename: str | None = None) -> str:
    """Build the full WebDAV URL. Override filename if provided."""
    fn = remote_filename or config.filename
    return f"{config.base_url.rstrip('/')}/{config.folder.strip('/')}/{fn}"


def download_artifact(config: SyncConfig, dest_path: str,
                      remote_filename: str | None = None) -> bool:
    """Download remote DB. Returns True if downloaded, False if not found (first sync)."""
    url = _build_url(config, remote_filename)
    resp = requests.get(
        url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=30,
    )
    if resp.status_code in (404, 409, 410):
        return False  # no remote DB yet — first sync
    if not resp.ok:
        raise WebDAVError(f"Download failed: HTTP {resp.status_code}")
    destination = Path(dest_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".download", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(resp.content)
        os.replace(temporary, destination)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            log.debug("download temporary descriptor already closed")
        Path(temporary).unlink(missing_ok=True)
        raise
    return True


def _ensure_folder(config: SyncConfig, remote_filename: str | None = None):
    """Ensure the target folder exists on the WebDAV server (create if needed)."""
    url = _build_url(config, remote_filename)
    folder_url = url.rsplit("/", 1)[0] + "/"
    # Try MKCOL first — succeeds if folder is created, returns 405 if already exists
    resp = requests.request(
        "MKCOL", folder_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=10,
    )
    if resp.ok or resp.status_code in (405, 409):  # 405=exists, 409=坚果云
        return
    # MKCOL failed — try PROPFIND to check
    resp2 = requests.request(
        "PROPFIND", folder_url,
        auth=HTTPBasicAuth(config.username, config.password),
        timeout=10,
    )
    if resp2.ok:
        return
    raise WebDAVError(f"无法创建/访问文件夹 {folder_url}: HTTP {resp.status_code}/{resp2.status_code}")


def upload_artifact(config: SyncConfig, src_path: str,
                    remote_filename: str | None = None,
                    *, if_match: str | None = None,
                    if_none_match: bool = False):
    """Upload a file to WebDAV from src_path."""
    _ensure_folder(config, remote_filename)
    url = _build_url(config, remote_filename)
    # Attempt PUT directly; if it fails with 404/409, folder issue already caught above
    with open(src_path, "rb") as f:
        headers = {}
        if if_match:
            headers["If-Match"] = if_match
        if if_none_match:
            headers["If-None-Match"] = "*"
        resp = requests.put(
            url,
            data=f,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=60,
            headers=headers,
        )
    if resp.status_code in (409, 412):
        raise WebDAVConflict(
            f"远端版本发生变化 (HTTP {resp.status_code})")
    if not resp.ok:
        raise WebDAVError(f"Upload failed: HTTP {resp.status_code} — {resp.text[:200]}")


def publish_versioned_artifact(config: SyncConfig, src_path: str, base: str,
                               expected: RemoteArtifact | None = None) -> RemoteArtifact:
    """Publish one attempt with mandatory exact-name PROPFIND confirmation.

    Domain workflows retry their complete transaction around this boundary.
    ``ArtifactTransaction.publish_with_retry`` remains available for callers
    that explicitly want transport-only retry semantics in a contract test.
    """
    return ArtifactTransaction(
        WebDAVClient(config), retry_count=1).publish_versioned_artifact(
            src_path, base, expected)


def test_connection(config: SyncConfig) -> str | None:
    """Test WebDAV connectivity. Returns None on success, error string on failure."""
    try:
        folder_url = config.full_url.rsplit("/", 1)[0] + "/"

        # 1. Try MKCOL — works on most servers, returns 405 if folder exists
        resp = requests.request(
            "MKCOL", folder_url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.ok or resp.status_code in (405, 409):
            return None

        # 2. Check for auth errors
        if resp.status_code in (401, 403):
            return f"认证失败 (HTTP {resp.status_code}) — 坚果云请使用「应用密码」"

        # 3. Fallback: try GET on the file
        resp = requests.get(
            config.full_url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=10,
        )
        if resp.status_code in (200, 404, 409, 410):
            return None
        return f"连接失败: HTTP {resp.status_code} — {resp.text[:200]}"
    except requests.RequestException as e:
        return f"网络错误: {e}"
