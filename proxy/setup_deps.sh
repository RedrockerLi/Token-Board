#!/usr/bin/env bash
# Download third-party dependencies for the C++ proxy.
# Run once after cloning the repository.
set -e

THIRD="$(cd "$(dirname "$0")" && pwd)/third_party"
mkdir -p "$THIRD"

echo "[1/3] cpp-httplib..."
if [ ! -f "$THIRD/httplib.h" ]; then
    curl -sL -o "$THIRD/httplib.h" \
        https://raw.githubusercontent.com/yhirose/cpp-httplib/v0.18.1/httplib.h
    echo "  OK ($(wc -l < "$THIRD/httplib.h") lines)"
else
    echo "  Already exists"
fi

echo "[2/3] nlohmann/json..."
if [ ! -f "$THIRD/json.hpp" ]; then
    curl -sL -o "$THIRD/json.hpp" \
        https://raw.githubusercontent.com/nlohmann/json/develop/single_include/nlohmann/json.hpp
    echo "  OK ($(wc -l < "$THIRD/json.hpp") lines)"
else
    echo "  Already exists"
fi

echo "[3/3] sqlite3 amalgamation..."
if [ ! -f "$THIRD/sqlite3.h" ] || [ ! -f "$THIRD/sqlite3.c" ]; then
    TMP=$(mktemp -d)
    curl -sL -o "$TMP/sqlite.zip" \
        https://www.sqlite.org/2025/sqlite-amalgamation-3490100.zip
    unzip -o "$TMP/sqlite.zip" -d "$TMP" > /dev/null
    cp "$TMP"/sqlite-amalgamation-*/sqlite3.h "$THIRD/"
    cp "$TMP"/sqlite-amalgamation-*/sqlite3.c "$THIRD/"
    rm -rf "$TMP"
    echo "  OK (sqlite3.h: $(wc -l < "$THIRD/sqlite3.h") lines, sqlite3.c: $(wc -l < "$THIRD/sqlite3.c") lines)"
else
    echo "  Already exists"
fi

echo ""
echo "All dependencies ready. Run: cd proxy && cmake -B build && cmake --build build"
