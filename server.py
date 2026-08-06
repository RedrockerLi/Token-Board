#!/usr/bin/env python3
"""AI API Usage Visualization Dashboard Server.

Reads cost and amount CSV files from the data/ directory (organised
by platform) and serves a web dashboard with token usage statistics
and ECharts visualizations.

Usage: python3 server.py --port <PORT> [--proxy-db <PATH>]
"""

import argparse

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
        "--proxy-db",
        type=str,
        default=None,
        help="Path to proxy SQLite database (enables proxy management UI)",
    )
    args = parser.parse_args()

    app = create_app(proxy_db_path=args.proxy_db, host=args.host)
    app.config["DATA_STORE"].load()

    print(f" * Starting on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)
