#pragma once

#include <string>

/// Parsed command-line arguments for the proxy.
struct Config {
    std::string db_path = "data/proxy.db";
    int port = 8800;
    std::string host = "0.0.0.0";
    std::string log_level = "info";
};

/// Parse CLI arguments (--db, --port, --host, --log-level).
/// Prints help and exits on --help or unknown flags.
Config parse_args(int argc, char *argv[]);
