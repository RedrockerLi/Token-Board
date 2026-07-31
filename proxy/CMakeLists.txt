cmake_minimum_required(VERSION 3.16)
project(token-board-proxy VERSION 1.0.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── Find required system libraries ──────────────────────────────────────
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

# ── Source files (sqlite3.c is compiled as C, others as C++) ────────────
set(SQLITE_SOURCE third_party/sqlite3.c)

set(PROXY_SOURCES
    src/main.cpp
    src/config.cpp
    src/db.cpp
    src/router.cpp
    src/think_filter.cpp
    src/upstream_client.cpp
    src/usage_tracker.cpp
    src/proxy_server.cpp
    src/ir.cpp
    src/codec.cpp
    src/format_common.cpp
    src/format_openai.cpp
    src/format_anthropic.cpp
    src/format_responses.cpp
)

# Format codec sources (no sqlite / http deps) — shared by the proxy and the
# standalone conversion test.
set(CODEC_SOURCES
    src/ir.cpp
    src/codec.cpp
    src/format_common.cpp
    src/format_openai.cpp
    src/format_anthropic.cpp
    src/format_responses.cpp
)

# ── Executable ───────────────────────────────────────────────────────────
add_executable(token_proxy ${PROXY_SOURCES} ${SQLITE_SOURCE})

# Compile sqlite3.c as C (not C++), with optimisations
set_source_files_properties(${SQLITE_SOURCE} PROPERTIES
    LANGUAGE C
    COMPILE_FLAGS "-O2 -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1"
)

target_include_directories(token_proxy PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party
    ${OPENSSL_INCLUDE_DIR}
)

target_link_libraries(token_proxy PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
    ${CMAKE_DL_LIBS}
)

# ── Standalone format-conversion test ────────────────────────────────────
add_executable(format_conv_test src/format_conv_test.cpp ${CODEC_SOURCES})

target_include_directories(format_conv_test PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party
)

target_compile_features(format_conv_test PRIVATE cxx_std_17)

# ── Compiler options ─────────────────────────────────────────────────────
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
        target_compile_options(token_proxy PRIVATE -O3 -march=native -flto)
        target_link_options(token_proxy PRIVATE -flto)
    endif()
endif()
