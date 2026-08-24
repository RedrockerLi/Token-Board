"""Functional WebDAV synchronization module."""

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote

from app.services.sync.common import *  # noqa: F401,F403
from app.services.sync.settings import SyncConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteArtifact:
    name: str
    timestamp: datetime | None = None
    etag: str | None = None


def _file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _make_timestamped_name(base: str) -> str:
    """dashboard_sync.db → dashboard_sync_20260716_143025.db"""
    name, ext = base.rsplit(".", 1)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{name}_{stamp}.{ext}"


def _schema_manifest(db_path: str, artifact_name: str) -> dict:
    """Describe the exact schema image published alongside a sync artifact."""
    conn = sqlite3.connect(db_path)
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
        "sha256": _file_checksum(Path(db_path)),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _publish_schema_manifest(config: SyncConfig, db_path: str,
                             artifact_name: str) -> None:
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
                       f"{marker['minor']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        _webdav_upload(config, str(marker_path), remote_filename=remote_name)
    finally:
        marker_path.unlink(missing_ok=True)


def _list_folder_files(config, prefix: str) -> list[RemoteArtifact]:
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


def _latest_artifact_name(config, base: str) -> str | None:
    artifact = _latest_artifact(config, base)
    return artifact.name if artifact else None


def _latest_artifact(config, base: str) -> RemoteArtifact | None:
    files = [item for item in _list_folder_files(config, base + "_")
             if _ARTIFACT_RE.match(item.name)]
    if not files:
        return None
    return max(files, key=lambda item: (_artifact_sort_key(item.name),
                                        item.timestamp or datetime.min))


def _download_latest_artifact(config, dest_path: str,
                              base: str) -> RemoteArtifact | None:
    artifact = _latest_artifact(config, base)
    remote_name = artifact.name if artifact else base + ".db"
    if not _webdav_download(config, dest_path, remote_filename=remote_name):
        return None
    return artifact or RemoteArtifact(remote_name)


def _download_latest(config, dest_path: str, base: str) -> bool:
    """Find the latest timestamped file matching *base* and download it.

    base = "dashboard_sync" → matches "dashboard_sync_20260716_143025.db"
    Returns True if a file was found and downloaded, False if no files exist.
    """
    prefix = base + "_"
    latest = _latest_artifact_name(config, base)
    if latest is None:
        # Also try the bare name for backward compatibility
        return _webdav_download(config, dest_path, remote_filename=base + ".db")
    return _webdav_download(config, dest_path, remote_filename=latest)


class WebDAVError(Exception):
    pass


class WebDAVConflict(WebDAVError):
    """The remote version changed while a versioned artifact was prepared."""


def _build_url(config: SyncConfig, remote_filename: str | None = None) -> str:
    """Build the full WebDAV URL. Override filename if provided."""
    fn = remote_filename or config.filename
    return f"{config.base_url.rstrip('/')}/{config.folder.strip('/')}/{fn}"


def _webdav_download(config: SyncConfig, dest_path: str, remote_filename: str | None = None) -> bool:
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


def _webdav_ensure_folder(config: SyncConfig, remote_filename: str | None = None):
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


def _webdav_upload(config: SyncConfig, src_path: str,
                   remote_filename: str | None = None,
                   *, if_match: str | None = None,
                   if_none_match: bool = False):
    """Upload a file to WebDAV from src_path."""
    _webdav_ensure_folder(config, remote_filename)
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


def _upload_versioned_artifact(config: SyncConfig, src_path: str, base: str,
                               expected: RemoteArtifact | None = None) -> RemoteArtifact:
    """Publish a new immutable artifact only if the observed latest version is
    still current.  A second listing closes the download/build/upload race;
    timestamp collisions are rejected with If-None-Match.
    """
    current = _latest_artifact(config, base)
    if expected and current:
        if expected.etag and current.etag and expected.etag != current.etag:
            raise WebDAVConflict("远端版本在上传前已变化")
        if expected.name != current.name and not expected.etag:
            raise WebDAVConflict("远端版本在上传前已变化")
    remote_name = _make_timestamped_name(base + ".db")
    _webdav_upload(config, src_path, remote_filename=remote_name,
                   if_none_match=True)
    published = _latest_artifact(config, base)
    return published or RemoteArtifact(remote_name)


def _webdav_test(config: SyncConfig) -> str | None:
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
