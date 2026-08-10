#include "config.h"
#include "logging.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

static void print_help(const char *argv0) {
    printf(
        "Token Board Proxy — OpenAI-compatible API proxy for CSTCloud\n"
        "Usage: %s [OPTIONS]\n"
        "Options:\n"
        "  --db PATH       SQLite database path (default: data/proxy.db)\n"
        "  --schema-dir PATH\n"
        "                  Migration file directory (default: derived from --db\n"
        "                  as <db_dir>/../schema)\n"
        "  --port PORT     Listen port (default: 8800)\n"
        "  --host HOST     Bind address (default: 127.0.0.1 — loopback only)\n"
        "  --log-level LVL Log level: debug|info|warn|error (default: info)\n"
        "  --help          Show this message\n",
        argv0);
}

Config parse_args(int argc, char *argv[]) {
    Config cfg;

    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--help") == 0) {
            print_help(argv[0]);
            exit(0);
        } else if (strcmp(argv[i], "--db") == 0 && i + 1 < argc) {
            cfg.db_path = argv[++i];
        } else if (strcmp(argv[i], "--schema-dir") == 0 && i + 1 < argc) {
            cfg.schema_dir = argv[++i];
        } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            cfg.port = atoi(argv[++i]);
            if (cfg.port <= 0 || cfg.port > 65535) {
                TB_LOG_ERROR( "Invalid port: %s\n", argv[i]);
                exit(1);
            }
        } else if (strcmp(argv[i], "--host") == 0 && i + 1 < argc) {
            cfg.host = argv[++i];
        } else if (strcmp(argv[i], "--log-level") == 0 && i + 1 < argc) {
            cfg.log_level = argv[++i];
        } else {
            TB_LOG_ERROR( "Unknown option: %s\nTry --help\n", argv[i]);
            exit(1);
        }
    }

    return cfg;
}
