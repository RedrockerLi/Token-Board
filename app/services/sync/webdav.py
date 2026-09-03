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
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import requests
from requests.auth import HTTPBasicAuth

from app.core import sqlite_runtime
from app.core.time import utc_now
from app.services.sync.artifact_codec import (
    ArtifactCodecError,
    decode_sqlite_artifact,
    encode_sqlite_artifact,
)
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
    """Return a timestamped gzip artifact name for a database base name."""
    name = base
    if name.endswith(".gz"):
        name = name[:-len(".gz")]
    if name.endswith(".db"):
        name = name[:-len(".db")]
    stamp = (clock or _filename_now)().strftime("%Y%m%d_%H%M%S")
    return f"{name}_{stamp}.db.gz"


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
        if resp.status_code in (404, 409, 410):
            return []
        raise WebDAVError(
            f"PROPFIND failed: HTTP {resp.status_code} — {resp.text[:200]}")

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
    r"^(?P<base>token-board_config|dashboard_sync)_"
    r"(?P<stamp>\d{8}_\d{6})(?:_(?P<attempt>\w+))?\.db"
    r"(?P<gzip>\.gz)?$")


def _artifact_sort_key(name: str) -> tuple[datetime, int, int, str]:
    match = _ARTIFACT_RE.match(name)
    if not match:
        return (datetime.min, -1, 0, name)
    try:
        stamp = datetime.strptime(match.group("stamp"), "%Y%m%d_%H%M%S")
    except ValueError:
        stamp = datetime.min
    attempt_text = match.group("attempt") or ""
    attempt = int(attempt_text) if attempt_text.isdigit() else 0
    compression = 1 if match.group("gzip") else 0
    return stamp, attempt, compression, name


def _log_upload_sizes(remote_name: str, source: Path, encoded: Path) -> None:
    raw_bytes = source.stat().st_size
    wire_bytes = encoded.stat().st_size
    log.info(
        "uploaded artifact name=%s encoding=gzip raw_bytes=%d wire_bytes=%d ratio=%.3f",
        remote_name, raw_bytes, wire_bytes,
        wire_bytes / raw_bytes if raw_bytes else 0.0)


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
        remote_name = _make_timestamped_name(base, self.filename_clock)
        if _attempt:
            stem = remote_name[:-len(".db.gz")]
            remote_name = f"{stem}_{_attempt}.db.gz"
        try:
            with encode_sqlite_artifact(source) as encoded:
                put(str(encoded), remote_name)
                _log_upload_sizes(remote_name, Path(source), encoded)
        except ArtifactCodecError as exc:
            raise WebDAVError(str(exc)) from exc
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
    """Download a `.db` or `.db.gz` database artifact atomically.

    Returns True when an artifact was materialized and False when the remote
    endpoint reports that no artifact exists yet.
    """
    url = _build_url(config, remote_filename)
    remote_name = remote_filename or Path(config.filename).name
    response = None
    fd: int | None = None
    payload_path: Path | None = None
    wire_bytes = 0
    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(config.username, config.password),
            timeout=30,
            stream=True,
        )
        if response.status_code in (404, 409, 410):
            return False  # no remote DB yet — first sync
        if not response.ok:
            raise WebDAVError(f"Download failed: HTTP {response.status_code}")

        destination = Path(dest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".payload",
            dir=destination.parent)
        payload_path = Path(temporary)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = None
            for block in response.iter_content(chunk_size=1024 * 1024):
                if not block:
                    continue
                wire_bytes += len(block)
                handle.write(block)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            decode_sqlite_artifact(payload_path, remote_name, destination)
        except ArtifactCodecError as exc:
            raise WebDAVError(str(exc)) from exc
        raw_bytes = destination.stat().st_size
        log.info(
            "downloaded artifact name=%s encoding=%s wire_bytes=%d raw_bytes=%d ratio=%.3f",
            remote_name, "gzip" if remote_name.endswith(".db.gz") else "identity",
            wire_bytes, raw_bytes, wire_bytes / raw_bytes if raw_bytes else 0.0)
        return True
    except requests.RequestException as exc:
        raise WebDAVError(f"Download failed: {exc}") from exc
    finally:
        if response is not None:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        if fd is not None:
            os.close(fd)
        if payload_path is not None:
            payload_path.unlink(missing_ok=True)


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


def publish_config_artifact(config: SyncConfig, src_path: str,
                            base: str = "token-board_config") -> RemoteArtifact:
    """Publish a configuration artifact using PUT acknowledgement only.

    Configuration sync intentionally has a simpler single-editor contract than
    dashboard export.  Some WebDAV providers make a freshly uploaded file
    invisible to directory PROPFIND for a short period; requiring an immediate
    listing confirmation turns a successful PUT into a sticky local failure.
    Dashboard publication continues to use ``publish_versioned_artifact`` and
    its stronger confirmation protocol.
    """
    remote_name = _make_timestamped_name(base)
    try:
        with encode_sqlite_artifact(src_path) as encoded:
            upload_artifact(config, str(encoded), remote_filename=remote_name)
            _log_upload_sizes(remote_name, Path(src_path), encoded)
    except ArtifactCodecError as exc:
        raise WebDAVError(str(exc)) from exc
    return RemoteArtifact(remote_name)


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
