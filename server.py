#!/usr/bin/env python3
"""AI API Usage Visualization Dashboard Server.

Reads cost and amount CSV files from the data/ directory (organised
by platform) and serves a web dashboard with token usage statistics
and ECharts visualizations.

Usage: python3 server.py --port <PORT> [--token-board-db <PATH>] [--schema-dir <PATH>]
"""

import argparse
import signal

from app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI API Usage Dashboard Server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Bind address (default 127.0.0.1 — loopback only; the dashboard "
             "serves API keys, keep it off the network)",
    )
    parser.add_argument(
        "--token-board-db",
        type=str,
        default=None,
        help="Path to token-board SQLite database (enables proxy management UI)",
    )
    parser.add_argument(
        "--schema-dir",
        type=str,
        default=None,
        help="Path to the versioned schema root",
    )
    args = parser.parse_args()

    # systemd stops services with SIGTERM.  Turn it into a normal Python exit
    # so the integrated scheduler is woken and joined in ``finally``.
    def _handle_sigterm(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    app = None
    try:
        app = create_app(
            token_board_db_path=args.token_board_db, host=args.host,
            schema_dir=args.schema_dir,
            start_background_tasks=False,
        )
        app.config["DATA_STORE"].load()
        if args.token_board_db:
            from app.services.runtime_tasks import start_runtime_tasks
            start_runtime_tasks(app, app.config["TOKEN_BOARD_DB"], args.token_board_db)

        print(f" * Starting on http://{args.host}:{args.port}")
        app.run(host=args.host, port=args.port, debug=False)
    finally:
        if app is not None:
            from app.services.runtime_tasks import stop_runtime_tasks
            # Leave enough time for SQLite's bounded busy timeout and an
            # in-progress session parse to observe the stop event cleanly.
            stop_runtime_tasks(app, join_timeout=10.0)
