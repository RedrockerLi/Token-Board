#pragma once

#include <string>

/// Parsed command-line arguments for the proxy.
struct Config {
    std::string db_path = "data/proxy.db";
    std::string schema_dir;      // schema root or schema/proxy[/vN]
    int port = 8800;
    std::string host = "127.0.0.1";  // loopback only — the proxy is a local endpoint
    std::string log_level = "info";
};

/// Parse CLI arguments (--db, --schema-dir, --port, --host, --log-level).
/// Prints help and exits on --help or unknown flags.
Config parse_args(int argc, char *argv[]);
