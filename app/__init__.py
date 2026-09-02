"""AI API Usage Dashboard application package."""

from pathlib import Path

from app.services.data_loader import DataStore


def create_app(token_board_db_path: str | None = None, host: str = "127.0.0.1",
               *, testing: bool = False,
               start_background_tasks: bool = True,
               schema_dir: str | None = None):
    """Flask application factory.

    Explicitly sets template_folder and static_folder because Flask
    defaults to looking under the package directory (app/), but we
    keep them at the project root for clean separation.

    Args:
        token_board_db_path: If provided, enables proxy management features
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
    schema_root = Path(schema_dir).resolve() if schema_dir else root / "schema"

    flask_app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
    )
    flask_app.config["TESTING"] = testing
    flask_app.config["SCHEMA_DIR"] = str(schema_root)
    # Keep the dashboard archive next to an explicitly supplied token-board DB.
    # Embedded deployments and the App testbench must read the same
    # normalized V1 dataset, not the repository's default data/ directory.
    data_dir = (Path(token_board_db_path).resolve().parent
                if token_board_db_path else root / "data")
    flask_app.config["DATA_STORE"] = DataStore(
        data_dir, schema_dir=str(schema_root))

    from app.routes.routes import bp  # noqa: E402
    flask_app.register_blueprint(bp)

    # ── Proxy management (optional) ──
    if token_board_db_path:
        from app.db.proxy_db import ProxyDatabase  # noqa: E402
        from app.routes.proxy_routes import bp_proxy  # noqa: E402

        pdb = ProxyDatabase(token_board_db_path, schema_dir=str(schema_root))
        flask_app.config["TOKEN_BOARD_DB"] = pdb
        flask_app.register_blueprint(bp_proxy)
        print(f" * Proxy management enabled (DB: {token_board_db_path})")

        # Schema upgrades are owned by ``start.sh --all``. Runtime facades
        # verify the current schema only. The dashboard starts a lightweight,
        # asynchronous cloud-config session after its DataStore is loaded.
        from app.services.runtime_tasks import start_dashboard_tasks  # noqa: E402
        from app.services.sync.config_session import ConfigSession  # noqa: E402

        def start_finalizer() -> None:
            if not testing:
                start_dashboard_tasks(flask_app, pdb)

        session = ConfigSession(
            token_board_db_path,
            schema_dir=str(schema_root),
            on_writable=start_finalizer,
        )
        if testing:
            session._set_state("local_only")
        flask_app.config["CONFIG_SESSION"] = session
        flask_app.config["MAINTENANCE_SOCKET"] = str(
            Path(token_board_db_path).resolve().parent / "token-maintenance.sock")

        dash_db_path = str(Path(token_board_db_path).resolve().parent / "dashboard.db")

        # Reconcile the local dashboard archive: mirror normalized accounts
        # (id → name) into dashboard.accounts and backfill any legacy
        # name-keyed buckets to account_id. Runs before the DataStore loads
        # (server.py calls .load() after create_app), so names display right.
        try:
            from app.db.dashboard_db import reconcile_accounts  # noqa: E402
            if Path(dash_db_path).exists():
                reconcile_accounts(dash_db_path, token_board_db_path)
        except Exception:
            flask_app.logger.exception("dashboard account reconcile failed")

    # ── Access-token auth (off-loopback or TB_DASHBOARD_TOKEN) ──
    from app import dashboard_auth  # noqa: E402
    token = dashboard_auth.resolve_token(host, data_dir)
    if token:
        dashboard_auth.install_auth(flask_app, token)
        print(" * Dashboard access token required: /login (loopback bypassed "
              "only when not exposed)")

    # Background maintenance is owned by token-maintenance.service. The
    # dashboard only starts its configuration-gated deletion finalizer after
    # the async cloud baseline has completed.
    if token_board_db_path and start_background_tasks:
        if testing:
            # Test apps may still exercise the legacy in-process worker. The
            # production server never takes this branch; maintenance.py owns
            # those workers there.
            from app.services.runtime_tasks import start_runtime_tasks
            start_runtime_tasks(flask_app, pdb, token_board_db_path)
        else:
            start_config_session(flask_app)

    return flask_app


def start_config_session(flask_app) -> None:
    """Begin the production dashboard's asynchronous config pull."""
    session = flask_app.config.get("CONFIG_SESSION")
    if session is not None and not flask_app.config.get("CONFIG_SESSION_STARTED"):
        flask_app.config["CONFIG_SESSION_STARTED"] = True
        session.start()
