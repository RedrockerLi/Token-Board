#include "db.h"
#include "json.hpp"
#include "core/account_types.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <exception>
#include <fcntl.h>
#include <filesystem>
#include <fstream>
#include <limits>
#include <iomanip>
#include <regex>
#include <sstream>
#include <sys/file.h>
#include <sys/stat.h>
#include <tuple>
#include <unistd.h>
#include <utility>
#include <vector>
#include <openssl/sha.h>
#include <sqlite3.h>

// ── Internal helpers ────────────────────────────────────────────────────

namespace {

using json = nlohmann::json;

} // anonymous namespace

// ── Constructor / Destructor ─────────────────────────────────────────────

Database::~Database() { close(); }

// ── open ─────────────────────────────────────────────────────────────────
