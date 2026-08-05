cmake_minimum_required(VERSION 3.16)
project(token-board-proxy VERSION 1.0.0 LANGUAGES C CXX)

include(CTest)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── Find required system libraries ──────────────────────────────────────
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

# ── Source files (sqlite3.c is compiled as C, others as C++) ────────────
# Business sources are organized by function under src/<subdir>. Test code
# lives exclusively in tests/ and is never part of PROXY_SOURCES.
set(SQLITE_SOURCE third_party/sqlite3.c)

set(PROXY_SOURCES
    src/main.cpp
    src/core/config.cpp
    src/core/router.cpp
    src/core/proxy_server.cpp
    src/store/db.cpp
    src/store/usage_tracker.cpp
    src/net/upstream_client.cpp
    src/format/ir.cpp
    src/format/codec.cpp
    src/format/format_common.cpp
    src/format/format_openai.cpp
    src/format/format_anthropic.cpp
    src/format/format_responses.cpp
    src/format/think_filter.cpp
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
    ${CMAKE_CURRENT_SOURCE_DIR}/src/net
    ${CMAKE_CURRENT_SOURCE_DIR}/src/core
    ${CMAKE_CURRENT_SOURCE_DIR}/src/store
    ${CMAKE_CURRENT_SOURCE_DIR}/src/format
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party
    ${OPENSSL_INCLUDE_DIR}
)

target_link_libraries(token_proxy PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
    ${CMAKE_DL_LIBS}
)

# ── Tests ────────────────────────────────────────────────────────────────
# Test targets are defined only in tests/CMakeLists.txt and built solely
# under BUILD_TESTING. They never link into token_proxy.
if(BUILD_TESTING)
    enable_testing()
    add_subdirectory(tests)
endif()

# ── Compiler options ─────────────────────────────────────────────────────
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
        target_compile_options(token_proxy PRIVATE -O3 -march=native -flto)
        target_link_options(token_proxy PRIVATE -flto)
    endif()
endif()
