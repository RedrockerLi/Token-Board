"""Functional WebDAV synchronization module."""

from app.services.sync.common import *  # noqa: F401,F403

@dataclass
class SyncConfig:
    """WebDAV connection parameters."""
    base_url: str     # e.g. "https://dav.example.com/remote.php/dav/files/user"
    folder: str       # e.g. "token-board-sync" — subfolder to store files in
    username: str
    password: str
    filename: str = "token-board_config.db"  # default filename for proxy config DB

    @property
    def full_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/{self.folder.strip('/')}/{self.filename}"


def save_sync_config(db_path: str, config: SyncConfig):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('url', ?)", (config.base_url,))
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('folder', ?)", (config.folder,))
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('username', ?)", (config.username,))
        conn.execute("INSERT OR REPLACE INTO sync_config VALUES ('password', ?)", (config.password,))
        conn.commit()
    finally:
        conn.close()


def load_sync_config(db_path: str) -> SyncConfig | None:
    conn = sqlite3.connect(db_path)
    try:
        rows = dict(conn.execute("SELECT key, value FROM sync_config").fetchall())
        if rows.get("url") and rows.get("username"):
            old_url = rows["url"]

            # If the old url is just a scheme or malformed, use it as-is with a default folder
            if old_url.count("/") < 3:
                # Probably just a base URL like "https://dav.jianguoyun.com/dav"
                base = old_url.rstrip("/")
                folder = rows.get("folder", "token-board-sync")
            else:
                # Full path like "https://dav.jianguoyun.com/dav/folder/file.db"
                # Strip last two segments (folder/file.db) to get base
                parts = old_url.rstrip("/").rsplit("/", 2)
                if len(parts) >= 3:
                    base = parts[0]
                    folder = parts[1] if not "." in parts[1] else rows.get("folder", "token-board-sync")
                    # Don't lose the actual base if we stripped too much
                    if not base.startswith("https://") and not base.startswith("http://"):
                        base = old_url.rstrip("/")
                        folder = rows.get("folder", "token-board-sync")
                else:
                    base = parts[0]
                    folder = rows.get("folder", "token-board-sync")
            folder = rows.get("folder", folder)  # explicit folder takes priority
            return SyncConfig(
                base_url=base,
                folder=folder,
                username=rows["username"],
                password=rows.get("password", ""),
            )
        return None
    finally:
        conn.close()
