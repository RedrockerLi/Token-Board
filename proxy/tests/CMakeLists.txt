# ── Proxy test suite ─────────────────────────────────────────────────────
# This subdirectory is only reached when BUILD_TESTING is ON (see top-level
# CMakeLists.txt). Every target here is a test executable; none of them link
# into the production token_proxy binary.

# ── Python test interpreter ──────────────────────────────────────────────
# The Python-backed tests import flask, requests and jinja2. Discovery can land
# on a bare python3, which then fails inside a test with an opaque
# ModuleNotFoundError far from the configure step that picked it. Resolve the
# default once (PY), find the interpreter once, and validate it immediately so
# a wrong interpreter fails configure with the command that fixes it.
#
# These helpers are reused by app/tests/CMakeLists.txt, which the proxy project
# adds after this directory.
#
# An explicit -DPython3_EXECUTABLE=... always wins: it is already a cache entry,
# so the PY default below cannot override it.
function(tb_prepare_python3_for_tests)
    if(DEFINED ENV{PY} AND NOT "$ENV{PY}" STREQUAL "")
        set(Python3_EXECUTABLE "$ENV{PY}" CACHE FILEPATH
            "Python interpreter for tests")
    endif()
endfunction()

function(tb_require_python_test_deps)
    execute_process(
        COMMAND "${Python3_EXECUTABLE}" -c "import flask, requests, jinja2"
        RESULT_VARIABLE tb_python_deps_status
        ERROR_VARIABLE tb_python_deps_error
        OUTPUT_QUIET
    )
    if("${tb_python_deps_status}" STREQUAL "0")
        return()
    endif()
    string(REGEX MATCH "No module named '([^']+)'" tb_python_deps_match
        "${tb_python_deps_error}")
    if(tb_python_deps_match)
        set(tb_python_deps_missing "${CMAKE_MATCH_1}")
    else()
        set(tb_python_deps_missing "<unknown, see interpreter error>")
    endif()
    string(STRIP "${tb_python_deps_error}" tb_python_deps_error)
    message(FATAL_ERROR
        "The Python interpreter used by the test suite is missing dependencies.\n"
        "  interpreter:      ${Python3_EXECUTABLE}\n"
        "  missing module:   ${tb_python_deps_missing}\n"
        "  required modules: flask, requests, jinja2\n"
        "  interpreter says: ${tb_python_deps_error}\n"
        "  fix: re-run cmake with an interpreter that provides them, for example:\n"
        "    cmake -S proxy -B ${CMAKE_BINARY_DIR} -DPython3_EXECUTABLE=/home/redrocker/miniconda3/bin/python"
    )
endfunction()

# Tests must actually assert: the parent Release build adds -DNDEBUG, which
# compiles assert() out and silently disables every check in this suite.
# -UNDEBUG here re-enables assertions for the test executables regardless of
# the outer build type.
add_compile_options(-UNDEBUG)

set(TEST_INCLUDE_DIRS
    ${CMAKE_SOURCE_DIR}/src
    ${CMAKE_SOURCE_DIR}/src/net
    ${CMAKE_SOURCE_DIR}/src/core
    ${CMAKE_SOURCE_DIR}/src/store
    ${CMAKE_SOURCE_DIR}/src/format
    ${CMAKE_SOURCE_DIR}/third_party
)

# Standalone format-conversion test. Codec sources are listed explicitly with
# absolute paths (relative paths would resolve against this tests/ directory).
add_executable(format_conv_test format_conv_test.cpp
    ${CMAKE_SOURCE_DIR}/src/format/ir.cpp
    ${CMAKE_SOURCE_DIR}/src/format/codec.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_common.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_openai.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_openai_helpers.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_openai_request.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_openai_response.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_openai_stream.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_anthropic.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_anthropic_helpers.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_anthropic_request.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_anthropic_response.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_anthropic_stream.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses_helpers.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses_request.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses_request_items.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses_response.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses_stream.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses_emitter.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_google.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_google_helpers.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_google_parts.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_google_request.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_google_response.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_google_stream.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_media.cpp
    ${CMAKE_SOURCE_DIR}/src/format/reasoning_bridge.cpp
)
target_include_directories(format_conv_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(format_conv_test PRIVATE cxx_std_17)
target_link_libraries(format_conv_test PRIVATE OpenSSL::Crypto Threads::Threads)

# Standalone regression test for the process-local session affinity store.
add_executable(session_affinity_test session_affinity_test.cpp)
target_include_directories(session_affinity_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(session_affinity_test PRIVATE cxx_std_17)

add_executable(responses_state_store_test responses_state_store_test.cpp
    ${CMAKE_SOURCE_DIR}/src/core/responses_state_store.cpp)
target_include_directories(responses_state_store_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(responses_state_store_test PRIVATE cxx_std_17)

add_executable(responses_semantics_test responses_semantics_test.cpp)
target_include_directories(responses_semantics_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(responses_semantics_test PRIVATE cxx_std_17)
target_link_libraries(responses_semantics_test PRIVATE token_proxy_format OpenSSL::Crypto Threads::Threads)

# Endpoint policy metadata is the single source of truth for management and
# usage endpoints. Keep a small standalone guard so embeddings/models cannot
# regress to the chat path when runtime forwarding is refactored.
add_executable(endpoint_policy_test
    endpoint_policy_test.cpp
    ${CMAKE_SOURCE_DIR}/src/core/endpoint_policy.cpp
    ${CMAKE_SOURCE_DIR}/src/format/ir.cpp
)
target_include_directories(endpoint_policy_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(endpoint_policy_test PRIVATE cxx_std_17)

# Upstream sub-type dispatch table (header-only; nothing to link).
add_executable(upstream_profile_test upstream_profile_test.cpp)
target_include_directories(upstream_profile_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(upstream_profile_test PRIVATE cxx_std_17)

# Frame-aware single-cycle guard for passthrough streams.
add_executable(sse_cycle_guard_test sse_cycle_guard_test.cpp)
target_include_directories(sse_cycle_guard_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(sse_cycle_guard_test PRIVATE cxx_std_17)
target_link_libraries(sse_cycle_guard_test PRIVATE token_proxy_format)

# Wire/IR -> database accounting projection.  Cache creation remains an
# explicit wire field while the existing DB prompt bucket retains its frozen
# Anthropic compatibility fold.
add_executable(usage_accounting_test usage_accounting_test.cpp)
target_include_directories(usage_accounting_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(usage_accounting_test PRIVATE cxx_std_17)

# Protocol JSON extraction is a format concern. The store test above only
# covers the database-compatible projection, while this fixture freezes the
# non-streaming parser behavior and the single Anthropic cache fold.
add_executable(usage_parser_test usage_parser_test.cpp)
target_include_directories(usage_parser_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(usage_parser_test PRIVATE cxx_std_17)
target_link_libraries(usage_parser_test PRIVATE token_proxy_format)

# Frozen durable-spool framing: every truncation boundary, checksum/length
# guard and the exact-I/O EINTR retry path are covered without opening a
# network socket or depending on a running proxy.
add_executable(spool_codec_test spool_codec_test.cpp)
target_include_directories(spool_codec_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(spool_codec_test PRIVATE cxx_std_17)

# SemaphorePool lifecycle: concurrent enqueue/resize/shutdown must not lose
# shutdown permits; a watchdog turns any deadlocked join into a failure.
add_executable(semaphore_pool_test semaphore_pool_test.cpp)
target_include_directories(semaphore_pool_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(semaphore_pool_test PRIVATE cxx_std_17)
target_link_libraries(semaphore_pool_test PRIVATE Threads::Threads)

# 04-C-04 / 04-C-05: a double shutdown (shutdown() racing the destructor) must
# join instead of tearing the workers down, and a parked request must not pin
# the drain past its grace budget.  Separate target: the C-04 case aborts the
# process when the guard is missing, which must not take the pool suite with it.
# 04-C-01: a warm affinity bind/resolve must not allocate.  Overriding the
# global allocator is scoped to this target so no other case is affected.
add_executable(session_affinity_alloc_test session_affinity_alloc_test.cpp)
target_include_directories(session_affinity_alloc_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(session_affinity_alloc_test PRIVATE cxx_std_17)

# 04-C-08: one candidate_order() must read each candidate's ordering cost once
# before sorting.  The comparator used to call cost_of() twice per comparison,
# which measured 128 lock acquisitions in the all-cost fixture below and
# 192-256 in the w9 fixture for 20 candidates.  The snapshot makes it exactly
# 2N (gate mutex + ledger rwlock per candidate).  --wrap counts acquisitions
# while the real pthread functions still run.
add_executable(candidate_order_lock_test candidate_order_lock_test.cpp)
target_include_directories(candidate_order_lock_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(candidate_order_lock_test PRIVATE cxx_std_17)
target_link_libraries(candidate_order_lock_test PRIVATE Threads::Threads)
target_link_options(candidate_order_lock_test PRIVATE
    -Wl,--wrap=pthread_mutex_lock
    -Wl,--wrap=pthread_rwlock_rdlock
)

# 04-C-11: the watchdog's per-round scan follows the number of active watches,
# not the number of committed downstream streams, and a zero-watch round never
# calls expire_if_due().  The counters are production-observable metrics; a
# deadline far in the future keeps time out of the assertion.
add_executable(watchdog_scan_test watchdog_scan_test.cpp)
target_include_directories(watchdog_scan_test PRIVATE
    ${TEST_INCLUDE_DIRS}
    ${OPENSSL_INCLUDE_DIR}
)
target_compile_features(watchdog_scan_test PRIVATE cxx_std_17)
target_link_libraries(watchdog_scan_test PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
)

# 04-C-12: the thread_local accounting reservation is a hand-off carrier, so a
# second adopt on the same thread must trip the proxy_server.h assert.  The
# abort runs in a child process; the CTest process itself stays healthy and
# inspects exit 134 plus the assertion text on stderr.
add_executable(accounting_adopt_test accounting_adopt_test.cpp)
target_include_directories(accounting_adopt_test PRIVATE
    ${TEST_INCLUDE_DIRS}
    ${OPENSSL_INCLUDE_DIR}
)
target_compile_features(accounting_adopt_test PRIVATE cxx_std_17)
target_link_libraries(accounting_adopt_test PRIVATE
    token_proxy_runtime
    token_proxy_format
    token_proxy_net
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
    ${CMAKE_DL_LIBS}
)

# 04-C-17: PooledLease reports an empty lease as empty and keeps its client
# across a self-move-assignment.  The header is a fragment included inside
# namespace tb_transport; this target supplies only the declarations it needs.
add_executable(pooled_lease_test pooled_lease_test.cpp)
target_include_directories(pooled_lease_test PRIVATE
    ${TEST_INCLUDE_DIRS}
    ${OPENSSL_INCLUDE_DIR}
)
target_compile_features(pooled_lease_test PRIVATE cxx_std_17)
target_link_libraries(pooled_lease_test PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
)

# 04-C-10: releasing one limiter slot must wake one waiter, not the herd.
# The counter is voluntary context switches (getrusage), a structural metric
# rather than a timing one.
add_executable(origin_limiter_herd_test origin_limiter_herd_test.cpp)
target_include_directories(origin_limiter_herd_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(origin_limiter_herd_test PRIVATE cxx_std_17)
target_link_libraries(origin_limiter_herd_test PRIVATE Threads::Threads)

add_executable(semaphore_shutdown_test semaphore_shutdown_test.cpp)
target_include_directories(semaphore_shutdown_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(semaphore_shutdown_test PRIVATE cxx_std_17)
target_link_libraries(semaphore_shutdown_test PRIVATE Threads::Threads)

# The same source under AddressSanitizer: the pre-fix double shutdown was a
# use-after-free that only ASAN reports reliably.  ASAN stays on this dedicated
# target so the Release binaries are unaffected.
add_executable(semaphore_shutdown_asan_test semaphore_shutdown_test.cpp)
target_include_directories(semaphore_shutdown_asan_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(semaphore_shutdown_asan_test PRIVATE cxx_std_17)
target_compile_options(semaphore_shutdown_asan_test PRIVATE
    -fsanitize=address -fno-omit-frame-pointer -g)
target_link_options(semaphore_shutdown_asan_test PRIVATE -fsanitize=address)
target_link_libraries(semaphore_shutdown_asan_test PRIVATE Threads::Threads)

# Upstream forwarding regression: fast responses must not wait for a watchdog
# polling interval, and invalid targets must normalize to a retryable 502.
add_executable(upstream_client_test
    upstream_client_test.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_client.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_forward.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_metrics.cpp
)
target_include_directories(upstream_client_test PRIVATE
    ${TEST_INCLUDE_DIRS}
    ${OPENSSL_INCLUDE_DIR}
)
target_link_libraries(upstream_client_test PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
)
target_compile_features(upstream_client_test PRIVATE cxx_std_17)

# Debug HTTP exchange formatting: bodies are visible only at debug level and
# authentication headers are redacted before they reach the terminal.
add_executable(http_debug_log_test http_debug_log_test.cpp)
target_include_directories(http_debug_log_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(http_debug_log_test PRIVATE cxx_std_17)
target_link_libraries(http_debug_log_test PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
)

# Runtime forwarding regression: the actual UpstreamClient emits non-streaming
# bodies and every streaming response chunk at debug level.
add_executable(upstream_debug_exchange_test
    upstream_debug_exchange_test.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_client.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_forward.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_metrics.cpp
)
target_include_directories(upstream_debug_exchange_test PRIVATE
    ${TEST_INCLUDE_DIRS}
    ${OPENSSL_INCLUDE_DIR}
)
target_link_libraries(upstream_debug_exchange_test PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
)
target_compile_features(upstream_debug_exchange_test PRIVATE cxx_std_17)

# Connection-reuse gate: sequential forward() calls against one origin must
# reuse a pooled keep-alive connection (accepted connections << requests).
# "fresh" mode remains available on the binary for Before/After comparison;
# the committed gate asserts reuse (fails on a pre-wiring build).
add_executable(upstream_client_perf
    upstream_client_perf.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_client.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_forward.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_metrics.cpp
)
target_include_directories(upstream_client_perf PRIVATE
    ${TEST_INCLUDE_DIRS}
    ${OPENSSL_INCLUDE_DIR}
)
target_link_libraries(upstream_client_perf PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
)
target_compile_features(upstream_client_perf PRIVATE cxx_std_17)
add_test(
    NAME upstream_client_perf_reuse
    COMMAND upstream_client_perf reuse
)

# V1 pricing-authority gate: the runtime delegates pricing to the SQLite V1
# trigger; the test verifies provenance and historical-rate selection.
add_executable(pricing_snapshot_equiv_test
    pricing_snapshot_equiv_test.cpp
    ${CMAKE_SOURCE_DIR}/src/store/db.cpp
    ${CMAKE_SOURCE_DIR}/src/store/database_lifecycle.cpp
    ${CMAKE_SOURCE_DIR}/src/store/database_statements.cpp
    ${CMAKE_SOURCE_DIR}/src/store/routing_repository.cpp
    ${CMAKE_SOURCE_DIR}/src/store/pricing_repository.cpp
    ${CMAKE_SOURCE_DIR}/src/store/durable_spool.cpp
    ${CMAKE_SOURCE_DIR}/src/store/usage_writer.cpp
    ${CMAKE_SOURCE_DIR}/third_party/sqlite3.c
)
target_include_directories(pricing_snapshot_equiv_test PRIVATE
    ${TEST_INCLUDE_DIRS}
)
target_compile_features(pricing_snapshot_equiv_test PRIVATE cxx_std_17)
set_source_files_properties(${CMAKE_SOURCE_DIR}/third_party/sqlite3.c PROPERTIES
    LANGUAGE C
    COMPILE_FLAGS "-O2 -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1"
)
target_link_libraries(pricing_snapshot_equiv_test PRIVATE
    OpenSSL::Crypto
    Threads::Threads
    ${CMAKE_DL_LIBS}
)
tb_prepare_python3_for_tests()
find_package(Python3 COMPONENTS Interpreter REQUIRED)
tb_require_python_test_deps()
# The interpreter is a test dependency of its own: a CTest entry keeps the
# requirement visible when someone runs the suite with a different shell.
add_test(NAME python_env COMMAND ${Python3_EXECUTABLE} -c "import flask, requests, jinja2")
# Production start scripts must configure without the test suite, so a machine
# whose default python3 lacks the test dependencies still builds the proxy.
add_test(NAME start_scripts_configure COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/start_scripts_configure_test.py
)
# The deploy entry point must refuse a binary that carries the test crash
# marker (05-TB05-F12); the check is driven without starting a proxy.
add_test(NAME start_proxy_binary_guard COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/start_proxy_binary_guard_test.py
    ${CMAKE_SOURCE_DIR}/..
    $<TARGET_FILE:token_proxy>
)
# Source-shape assertions for the dead-code / hygiene audit rows a compile
# cannot see (03 proxy items plus 01-P-06/P-14/P-15), and the codec warnings.
add_test(NAME static_source COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/static_source_test.py
    ${CMAKE_SOURCE_DIR}/..
    $<TARGET_FILE:format_conv_test>
)
# Concurrency/robustness rows from audit 04: terminal provider quota (C-18),
# upstream error visibility (C-19), listen failure (C-07) and clean-FIN
# detection over TCP plus the transport semantics it depends on (C-16).
add_test(NAME concurrency_regression COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/concurrency_regression_test.py
    $<TARGET_FILE:token_proxy>
    ${CMAKE_SOURCE_DIR}/../schema
    ${CMAKE_SOURCE_DIR}/..
)
add_test(NAME pricing_snapshot_equiv COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/run_prepared_cpp_db_test.py
    $<TARGET_FILE:pricing_snapshot_equiv_test>
    ${CMAKE_SOURCE_DIR}/../schema
    ${CMAKE_SOURCE_DIR}/..
)

# UsageReservation internal_abort contract: a reserved slot marked as having
# begun upstream work but dropped without a completed event must write a 599
# zero-token record instead of silently releasing the slot.
add_executable(accounting_abort_test
    accounting_abort_test.cpp
    ${CMAKE_SOURCE_DIR}/src/store/db.cpp
    ${CMAKE_SOURCE_DIR}/src/store/database_lifecycle.cpp
    ${CMAKE_SOURCE_DIR}/src/store/database_statements.cpp
    ${CMAKE_SOURCE_DIR}/src/store/routing_repository.cpp
    ${CMAKE_SOURCE_DIR}/src/store/pricing_repository.cpp
    ${CMAKE_SOURCE_DIR}/src/store/durable_spool.cpp
    ${CMAKE_SOURCE_DIR}/src/store/usage_writer.cpp
    ${CMAKE_SOURCE_DIR}/third_party/sqlite3.c
)
target_include_directories(accounting_abort_test PRIVATE
    ${TEST_INCLUDE_DIRS}
)
target_compile_features(accounting_abort_test PRIVATE cxx_std_17)
set_source_files_properties(${CMAKE_SOURCE_DIR}/third_party/sqlite3.c PROPERTIES
    LANGUAGE C
    COMPILE_FLAGS "-O2 -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1"
)
target_link_libraries(accounting_abort_test PRIVATE
    OpenSSL::Crypto
    Threads::Threads
    ${CMAKE_DL_LIBS}
)
add_test(NAME accounting_abort COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/run_prepared_cpp_db_test.py
    $<TARGET_FILE:accounting_abort_test>
    ${CMAKE_SOURCE_DIR}/../schema
    ${CMAKE_SOURCE_DIR}/..
)

# Crash-injection build of the production proxy: the store sources are
# compiled here with TB_ENABLE_TEST_CRASH so accounting_recovery can stop the
# process at an exact durable-spool boundary.  Test targets compile the
# sources they exercise; production libraries never define test-only macros.
add_executable(token_proxy_test
    ${CMAKE_SOURCE_DIR}/src/main.cpp
    ${CMAKE_SOURCE_DIR}/src/store/db.cpp
    ${CMAKE_SOURCE_DIR}/src/store/database_lifecycle.cpp
    ${CMAKE_SOURCE_DIR}/src/store/database_statements.cpp
    ${CMAKE_SOURCE_DIR}/src/store/routing_repository.cpp
    ${CMAKE_SOURCE_DIR}/src/store/pricing_repository.cpp
    ${CMAKE_SOURCE_DIR}/src/store/durable_spool.cpp
    ${CMAKE_SOURCE_DIR}/src/store/usage_writer.cpp
    ${CMAKE_SOURCE_DIR}/src/store/usage_recorder.cpp
    ${CMAKE_SOURCE_DIR}/third_party/sqlite3.c
)
set_source_files_properties(${CMAKE_SOURCE_DIR}/third_party/sqlite3.c PROPERTIES
    LANGUAGE C
    COMPILE_FLAGS "-O2 -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1"
)
target_include_directories(token_proxy_test PRIVATE
    ${TEST_INCLUDE_DIRS}
    ${OPENSSL_INCLUDE_DIR}
)
# NDEBUG cancels this directory's -UNDEBUG so the binary matches the Release
# production build; the crash point is the only intended difference.
target_compile_definitions(token_proxy_test PRIVATE
    TB_ENABLE_TEST_CRASH=1
    CPPHTTPLIB_LISTEN_BACKLOG=512
    NDEBUG
)
target_compile_features(token_proxy_test PRIVATE cxx_std_17)
target_link_libraries(token_proxy_test PRIVATE
    token_proxy_runtime token_proxy_format token_proxy_net
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
    ${CMAKE_DL_LIBS}
)


# ── Test registration ────────────────────────────────────────────────────
add_test(NAME format_conversion COMMAND format_conv_test)
add_test(NAME session_affinity COMMAND session_affinity_test)
add_test(NAME responses_state_store COMMAND responses_state_store_test)
add_test(NAME responses_semantics COMMAND responses_semantics_test)
add_test(NAME endpoint_policy COMMAND endpoint_policy_test)
add_test(NAME upstream_profile COMMAND upstream_profile_test)
add_test(NAME sse_cycle_guard COMMAND sse_cycle_guard_test)
add_test(NAME usage_accounting COMMAND usage_accounting_test)
add_test(NAME usage_parser COMMAND usage_parser_test)
add_test(NAME spool_codec COMMAND spool_codec_test)
add_test(NAME semaphore_pool COMMAND semaphore_pool_test)
# Build-time contract: the crash-injection marker must exist in the test
# binary and must never appear in the shipped proxy.
add_test(
    NAME crash_hook_isolation
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/crash_hook_isolation_test.py
            $<TARGET_FILE:token_proxy>
            $<TARGET_FILE:token_proxy_test>
)
add_test(
    NAME schema_version
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/schema_version_test.py
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME source_size
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_source_size.py
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME refactor_boundaries
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_refactor_boundaries.py
            ${CMAKE_SOURCE_DIR}/..
)
# Registration freshness gate: add_test() lines only reach a build tree at
# configure time (CTestTestfile.cmake), so an edited tests/CMakeLists.txt in an
# already-configured tree would keep running the old list. The script compares
# the source-side expansion with `ctest -N` for this very build directory.
add_test(
    NAME ctest_registration_fresh
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_ctest_registration.py
            --build-dir ${CMAKE_BINARY_DIR}
            --source-root ${CMAKE_SOURCE_DIR}/..
            --ctest ${CMAKE_CTEST_COMMAND}
)
# Repository environment gates. Each one pins a contract that neither configure
# nor a test suite can express on its own; see doc/development.md
# 「测试环境与门禁契约」.
add_test(
    NAME python_env_contract
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_python_env.py
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME readonly_pytest_tree
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_readonly_pytest_tree.py
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME ctest_readonly_recovery
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_ctest_readonly_recovery.py
)
add_test(
    NAME sqlite_leak_audit
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_sqlite_leaks.py
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME maintenance_socket_path
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_SOURCE_DIR}/../scripts/check_maintenance_socket_path.py
            ${CMAKE_SOURCE_DIR}/..
)
set_tests_properties(
    python_env_contract readonly_pytest_tree ctest_readonly_recovery
    sqlite_leak_audit maintenance_socket_path
    PROPERTIES LABELS "unit" WORKING_DIRECTORY ${CMAKE_BINARY_DIR}
)
add_test(NAME session_affinity_alloc COMMAND session_affinity_alloc_test)
add_test(NAME origin_limiter_herd COMMAND origin_limiter_herd_test)
add_test(NAME semaphore_shutdown COMMAND semaphore_shutdown_test)
add_test(NAME semaphore_shutdown_asan COMMAND semaphore_shutdown_asan_test)
add_test(NAME candidate_order_locks COMMAND candidate_order_lock_test)
add_test(NAME watchdog_scan_cost COMMAND watchdog_scan_test)
add_test(NAME accounting_double_adopt COMMAND accounting_adopt_test)
add_test(NAME pooled_lease_guard COMMAND pooled_lease_test)
add_test(NAME upstream_client COMMAND upstream_client_test)
add_test(NAME http_debug_log COMMAND http_debug_log_test)
add_test(NAME upstream_debug_exchange COMMAND upstream_debug_exchange_test)
# Current pricing gate: V1 is the published pre-transition schema and V2 is
# what the proxy runs.  Keep both the equivalence and restart/recovery paths
# tied to those current migration directories.
add_test(
    NAME pricing_v1_v2_equivalence
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/pricing_v1_v2_equivalence_test.py
            equivalence
            ${CMAKE_SOURCE_DIR}/../schema
)
add_test(
    NAME pricing_v1_v2_recovery
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/pricing_v1_v2_equivalence_test.py
            recovery
            ${CMAKE_SOURCE_DIR}/../schema
)
add_test(
    NAME stream_cycle_guard
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/stream_cycle_guard_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# The two production gray switches (PROXY_STRICT_TERMINAL, PROXY_STREAM_CYCLE_GUARD)
# default to ON; this pins their OFF branches against the ON controls.
add_test(
    NAME gray_switch
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/gray_switch_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME proxy_forwarding
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/proxy_forwarding_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME client_disconnect
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/client_disconnect_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME payload_limit
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/payload_limit_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME v1_routing
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/v1_routing_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME model_accounting
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/model_accounting_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME responses_forwarding
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/responses_forwarding_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME google_forwarding
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/google_forwarding_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME v1_admin
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/v1_admin_test.py
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME v1_sync
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/v1_sync_test.py
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME transition_recovery
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/transition_recovery_test.py
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME queue_saturation
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/queue_saturation_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME accounting_recovery
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/accounting_recovery_test.py
            $<TARGET_FILE:token_proxy_test>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME stream_rss_100
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/stream_rss_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
add_test(
    NAME accounting_soak_60s
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/accounting_soak_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# End-to-end load smoke: drives the real proxy against mock_upstream and
# asserts every request is a 200.  Run perf_stress_test.py directly with
# --requests/--concurrency for the full Before/After measurement.
add_test(
    NAME perf_stress
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/perf_stress_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
            --requests 40 --concurrency 8
)
add_test(
    NAME perf_gate_1000_64
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/perf_stress_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
            --requests 1000 --concurrency 64
            --min-rps 1000 --max-p95 200 --max-queue-p95 50
)
# The gate drives exactly 64 concurrent clients. Keep the proxy's dynamic
# worker ceiling aligned with that workload so small CI/desktop hosts do not
# spend the short benchmark growing an unnecessarily large native thread pool.
set_tests_properties(perf_gate_1000_64 PROPERTIES ENVIRONMENT "TB_MAX_WORKERS=64")
# GoUsageLimitError cooldown discrimination: quota-exhaustion 429 cools a plan
# key down; ordinary provider failures never create cross-request key state.
add_test(
    NAME cooldown_discrimination
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/cooldown_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# An ending plan contract (a future `billing_contracts.ends_at`) is still the
# account's live contract: its requests bill as a subscription and its quota
# 429 keeps the subscription cooldown window.
add_test(
    NAME ending_contract
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/ending_contract_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# Fallback-matrix experiments A3/A4/B1/B2/B3 (provider cooldown, ordinary
# fallback retry, non-streaming 5xx, streaming pre-commit 429, and the
# commit-then-stall #5 baseline).
add_test(
    NAME fallback_matrix
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/fallback_matrix_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# Cooldown-probe: a cooled plan key is re-probed every interval (env override,
# TB_COOLDOWN_PROBE_SECS=2) and cleared early when the upstream returns 2xx;
# a GoUsageLimitError keeps the cooldown.  Which key served is asserted via
# request_attempts.upstream_key_id.
add_test(
    NAME cooldown_probe
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/cooldown_probe_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# Unified terminal-error rendering: chat/embeddings/models must produce the
# same structured error JSON (parse_error_body + harness_codec envelope), with
# Anthropic clients getting the {"type":"error","error":{...}} wrapper.
add_test(
    NAME error_shape
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/error_shape_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# OpenCode Go sub-type: session-header injection (fingerprint / downstream
# pass-through / random fallback) and usage_json-aware key scheduling
# (lead key first, exhausted key last, sibling fallback).
add_test(
    NAME opencode_session
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/opencode_session_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)

# Keep CI selection explicit without maintaining a second test manifest.
set_tests_properties(upstream_client_perf_reuse PROPERTIES LABELS "performance")
set_tests_properties(
    pricing_snapshot_equiv schema_version pricing_v1_v2_equivalence pricing_v1_v2_recovery
    PROPERTIES LABELS "schema;unit"
)
set_tests_properties(format_conversion session_affinity endpoint_policy source_size
    refactor_boundaries ctest_registration_fresh python_env start_scripts_configure
    accounting_abort usage_accounting usage_parser semaphore_pool
    crash_hook_isolation semaphore_shutdown semaphore_shutdown_asan
    origin_limiter_herd session_affinity_alloc
    candidate_order_locks watchdog_scan_cost accounting_double_adopt
    pooled_lease_guard
    PROPERTIES LABELS "unit")
set_tests_properties(upstream_client PROPERTIES LABELS "integration")
set_tests_properties(
    proxy_forwarding v1_routing model_accounting responses_forwarding google_forwarding v1_admin cooldown_discrimination
    fallback_matrix cooldown_probe ending_contract error_shape opencode_session
    PROPERTIES LABELS "integration"
)
set_tests_properties(v1_sync transition_recovery
    PROPERTIES LABELS "integration;schema")
set_tests_properties(accounting_recovery
    PROPERTIES LABELS "integration;soak")
set_tests_properties(queue_saturation perf_stress perf_gate_1000_64
    PROPERTIES LABELS "performance")
set_tests_properties(stream_rss_100 accounting_soak_60s
    PROPERTIES LABELS "performance;soak")

# Python tests load the application as a package.  CTest's working directory
# is the build tree, so make the repository root explicit instead of relying
# on whichever interpreter happens to be launched from the source tree.
set_tests_properties(
    schema_version source_size refactor_boundaries pricing_v1_v2_equivalence pricing_v1_v2_recovery
    proxy_forwarding v1_routing model_accounting responses_forwarding v1_admin v1_sync
    transition_recovery queue_saturation accounting_recovery stream_rss_100
    accounting_soak_60s perf_stress perf_gate_1000_64 cooldown_discrimination
    fallback_matrix cooldown_probe ending_contract error_shape opencode_session
    PROPERTIES ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/..")

# Every registered test gets a timeout so a hung test cannot occupy CI forever.
# add_test() has no TIMEOUT argument; set it once for the directory instead of
# repeating it per test, then lengthen it for the measured performance/soak
# runs. Both lists come from the directory itself, so tests added later inherit
# the default without touching this block.
get_property(TB_DIRECTORY_TESTS DIRECTORY PROPERTY TESTS)
set_tests_properties(${TB_DIRECTORY_TESTS} PROPERTIES TIMEOUT 300)
foreach(TB_TEST IN LISTS TB_DIRECTORY_TESTS)
    get_property(TB_TEST_LABELS TEST ${TB_TEST} PROPERTY LABELS)
    if("performance" IN_LIST TB_TEST_LABELS OR "soak" IN_LIST TB_TEST_LABELS)
        set_tests_properties(${TB_TEST} PROPERTIES TIMEOUT 1800)
    endif()
endforeach()

# The leak audit re-runs the app suite under instrumentation, so it needs more
# than the default 300s on a slow runner.
set_tests_properties(sqlite_leak_audit PROPERTIES TIMEOUT 900)
