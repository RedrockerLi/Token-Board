# ── Proxy test suite ─────────────────────────────────────────────────────
# This subdirectory is only reached when BUILD_TESTING is ON (see top-level
# CMakeLists.txt). Every target here is a test executable; none of them link
# into the production token_proxy binary.

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

# Wire/IR -> database accounting projection.  Cache creation remains an
# explicit wire field while the existing DB prompt bucket retains its frozen
# Anthropic compatibility fold.
add_executable(usage_accounting_test usage_accounting_test.cpp)
target_include_directories(usage_accounting_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(usage_accounting_test PRIVATE cxx_std_17)

# Protocol JSON/SSE extraction is a format concern. The store test above only
# covers the database-compatible projection, while this fixture freezes the
# relocated parser behavior and the single Anthropic cache fold.
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
    ${CMAKE_SOURCE_DIR}/src/store/database_catalog.cpp
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
find_package(Python3 COMPONENTS Interpreter REQUIRED)
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
    ${CMAKE_SOURCE_DIR}/src/store/database_catalog.cpp
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


# ── Test registration ────────────────────────────────────────────────────
add_test(NAME format_conversion COMMAND format_conv_test)
add_test(NAME session_affinity COMMAND session_affinity_test)
add_test(NAME responses_state_store COMMAND responses_state_store_test)
add_test(NAME responses_semantics COMMAND responses_semantics_test)
add_test(NAME endpoint_policy COMMAND endpoint_policy_test)
add_test(NAME usage_accounting COMMAND usage_accounting_test)
add_test(NAME usage_parser COMMAND usage_parser_test)
add_test(NAME spool_codec COMMAND spool_codec_test)
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
add_test(NAME upstream_client COMMAND upstream_client_test)
# Money-equivalence gate (1/2): v17 trigger == v18 view-based trigger across a
# pricing edge-case matrix (pure SQL, exact api_cost equality).  Proves the
# 0018 view extraction changes no money before the column drop lands.
add_test(
    NAME pricing_equivalence
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/pricing_equivalence_test.py
            ${CMAKE_SOURCE_DIR}/../schema/token-board/v0
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
    NAME v1_routing
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/v1_routing_test.py
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
            $<TARGET_FILE:token_proxy>
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
# GoUsageLimitError cooldown discrimination (#4 root B): quota-exhaustion 429
# cools a plan key down; a plain transient 429 only backs off briefly.
add_test(
    NAME cooldown_discrimination
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/cooldown_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema
            ${CMAKE_SOURCE_DIR}/..
)
# Fallback-matrix experiments A3/A4/B1/B2/B3 (no-fallback 429, fallback
# backoff recovery, non-streaming 5xx, streaming pre-commit 429, and the
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

# Keep CI selection explicit without maintaining a second test manifest.
set_tests_properties(upstream_client_perf_reuse PROPERTIES LABELS "performance")
set_tests_properties(
    pricing_snapshot_equiv schema_version pricing_equivalence
    PROPERTIES LABELS "schema;unit"
)
set_tests_properties(format_conversion session_affinity endpoint_policy source_size
    refactor_boundaries
    accounting_abort usage_accounting usage_parser
    PROPERTIES LABELS "unit")
set_tests_properties(upstream_client PROPERTIES LABELS "integration")
set_tests_properties(
    proxy_forwarding v1_routing responses_forwarding v1_admin cooldown_discrimination
    fallback_matrix cooldown_probe error_shape
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
    schema_version source_size refactor_boundaries pricing_equivalence
    proxy_forwarding v1_routing responses_forwarding v1_admin v1_sync
    transition_recovery queue_saturation accounting_recovery stream_rss_100
    accounting_soak_60s perf_stress perf_gate_1000_64 cooldown_discrimination
    fallback_matrix cooldown_probe error_shape
    PROPERTIES ENVIRONMENT "PYTHONPATH=${CMAKE_SOURCE_DIR}/..")
