"""AI API Usage Dashboard application package."""

from pathlib import Path

from app.services.data_loader import DataStore


def create_app(proxy_db_path: str | None = None, host: str = "127.0.0.1",
               *, testing: bool = False,
               start_background_tasks: bool = True):
    """Flask application factory.

    Explicitly sets template_folder and static_folder because Flask
    defaults to looking under the package directory (app/), but we
    keep them at the project root for clean separation.

    Args:
        proxy_db_path: If provided, enables proxy management features
                       by attaching a ProxyDatabase instance to the app
                       config and registering the proxy blueprint.
        host: The bind address.  A non-loopback address (or a configured
              TB_DASHBOARD_TOKEN) activates the access-token auth guard.
    """
    # Keep database/schema tooling importable on maintenance hosts where the
    # optional web dependency is not installed.  Flask is required only when
    # constructing the HTTP application itself.
    from flask import Flask

    root = Path(__file__).resolve().parent.parent  # project root

    flask_app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    flask_app.config["TESTING"] = testing
    # Keep the dashboard archive next to an explicitly supplied proxy DB.
    # Embedded deployments and the App testbench must read the same
    # normalized V1 dataset, not the repository's default data/ directory.
    data_dir = (Path(proxy_db_path).resolve().parent
                if proxy_db_path else root / "data")
    flask_app.config["DATA_STORE"] = DataStore(data_dir)

    from app.routes.routes import bp  # noqa: E402
    flask_app.register_blueprint(bp)

    # ── Proxy management (optional) ──
    if proxy_db_path:
        from app.db.proxy_db import ProxyDatabase  # noqa: E402
        from app.routes.proxy_routes import bp_proxy  # noqa: E402

        # Direct server.py launches use the same unattended local upgrade
        # boundary as start.sh.  The C++ proxy still opens only V1; this call
        # completes the upgrade before ProxyDatabase creates its connection.
        from app.db.schema_upgrade import ensure_local_databases  # noqa: E402
        dash_db_path = str(Path(proxy_db_path).resolve().parent / "dashboard.db")
        ensure_local_databases(
            proxy_db_path, dash_db_path,
            str(root / "schema"),
            source_timezone="Asia/Shanghai",
        )

        pdb = ProxyDatabase(proxy_db_path)
        flask_app.config["PROXY_DB"] = pdb
        flask_app.register_blueprint(bp_proxy)
        print(f" * Proxy management enabled (DB: {proxy_db_path})")

        # Pull latest config from cloud on startup. Tests use isolated V1
        # fixtures and deliberately do not touch network state.
        if not testing:
            from app.services.sync import sync_config_download  # noqa: E402
            try:
                if sync_config_download(proxy_db_path):
                    print(" * Config synced from cloud")
            except Exception:
                flask_app.logger.exception("startup config sync failed")

        # Reconcile the local dashboard archive: mirror normalized accounts
        # (id → name) into dashboard.accounts and backfill any legacy
        # name-keyed buckets to account_id. Runs before the DataStore loads
        # (server.py calls .load() after create_app), so names display right.
        try:
            from app.db.dashboard_db import reconcile_accounts  # noqa: E402
            if Path(dash_db_path).exists():
                reconcile_accounts(dash_db_path, proxy_db_path)
        except Exception:
            flask_app.logger.exception("dashboard account reconcile failed")

        if start_background_tasks:
            from app.services.runtime_tasks import start_runtime_tasks  # noqa: E402
            start_runtime_tasks(flask_app, pdb, proxy_db_path)

    # ── Access-token auth (off-loopback or TB_DASHBOARD_TOKEN) ──
    from app import dashboard_auth  # noqa: E402
    token = dashboard_auth.resolve_token(host, root / "data")
    if token:
        dashboard_auth.install_auth(flask_app, token)
        print(" * Dashboard access token required: /login (loopback bypassed "
              "only when not exposed)")

    return flask_app
