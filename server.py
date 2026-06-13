#!/usr/bin/env python3
"""AI API Usage Visualization Dashboard Server.

Reads cost and amount CSV files from the data/ directory (organised
by platform) and serves a web dashboard with token usage statistics
and ECharts visualizations.

Usage: python3 server.py --port <PORT>
"""

import argparse

from app import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI API Usage Dashboard Server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    args = parser.parse_args()

    app = create_app()
    app.config["DATA_STORE"].load()

    print(f" * Starting on http://0.0.0.0:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)
