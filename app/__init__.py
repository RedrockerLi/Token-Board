"""AI API Usage Dashboard application package."""

from pathlib import Path

from flask import Flask

from app.data_loader import DataStore


def create_app(proxy_db_path: str | None = None):
    """Flask application factory.

    Explicitly sets template_folder and static_folder because Flask
    defaults to looking under the package directory (app/), but we
    keep them at the project root for clean separation.

    Args:
        proxy_db_path: If provided, enables proxy management features
                       by attaching a ProxyDatabase instance to the app
                       config and registering the proxy blueprint.
    """
    root = Path(__file__).resolve().parent.parent  # project root

    flask_app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    flask_app.config["DATA_STORE"] = DataStore(root / "data")

    from app.routes import bp  # noqa: E402
    flask_app.register_blueprint(bp)

    # ── Proxy management (optional) ──
    if proxy_db_path:
        from app.proxy_db import ProxyDatabase  # noqa: E402
        from app.proxy_routes import bp_proxy  # noqa: E402

        pdb = ProxyDatabase(proxy_db_path)
        flask_app.config["PROXY_DB"] = pdb
        flask_app.register_blueprint(bp_proxy)
        print(f" * Proxy management enabled (DB: {proxy_db_path})")

        # Pull latest config from cloud on startup
        from app.sync import sync_config_download  # noqa: E402
        try:
            if sync_config_download(proxy_db_path):
                print(" * Config synced from cloud")
        except Exception:
            pass  # network / not configured — ignore

        # Reconcile the local dashboard archive: mirror upstream_accounts
        # (id → name) into dashboard.accounts and backfill any legacy
        # name-keyed buckets to account_id. Runs before the DataStore loads
        # (server.py calls .load() after create_app), so names display right.
        try:
            from app.dashboard_db import reconcile_accounts  # noqa: E402
            dash_db_path = str(Path(proxy_db_path).parent / "dashboard.db")
            reconcile_accounts(dash_db_path, proxy_db_path)
        except Exception:
            pass  # no dashboard archive yet / not migrated — ignore

        # Background, best-effort threads (daemon, never affect the request
        # path): pre-warm today's USD→CNY rate, then start the Codex session
        # importer which scans ~/.codex/sessions every 60 s.
        try:
            from app import fx  # noqa: E402
            def _fx_prewarm():
                try:
                    conn = pdb._connect()
                    try:
                        fx.ensure_rate(conn)
                    finally:
                        conn.close()
                except Exception:
                    pass
            import threading
            threading.Thread(target=_fx_prewarm, daemon=True,
                             name="fx-prewarm").start()
        except Exception:
            pass

        try:
            from app.codex_import import run_import  # noqa: E402
            import threading
            stop_event = threading.Event()
            flask_app.config["CODEX_IMPORT_STOP"] = stop_event
            threading.Thread(target=run_import, args=(pdb, stop_event),
                             daemon=True, name="codex-importer").start()
        except Exception:
            pass

    return flask_app
