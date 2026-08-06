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
    ${CMAKE_SOURCE_DIR}/src/format/format_anthropic.cpp
    ${CMAKE_SOURCE_DIR}/src/format/format_responses.cpp
)
target_include_directories(format_conv_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(format_conv_test PRIVATE cxx_std_17)

# Standalone regression test for the process-local session affinity store.
add_executable(session_affinity_test session_affinity_test.cpp)
target_include_directories(session_affinity_test PRIVATE ${TEST_INCLUDE_DIRS})
target_compile_features(session_affinity_test PRIVATE cxx_std_17)

# Upstream forwarding regression: fast responses must not wait for a watchdog
# polling interval, and invalid targets must normalize to a retryable 502.
add_executable(upstream_client_test
    upstream_client_test.cpp
    ${CMAKE_SOURCE_DIR}/src/net/upstream_client.cpp
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


# ── Test registration ────────────────────────────────────────────────────
find_package(Python3 COMPONENTS Interpreter REQUIRED)
add_test(NAME format_conversion COMMAND format_conv_test)
add_test(NAME session_affinity COMMAND session_affinity_test)
add_test(NAME upstream_client COMMAND upstream_client_test)
add_test(
    NAME proxy_forwarding
    COMMAND ${Python3_EXECUTABLE}
            ${CMAKE_CURRENT_SOURCE_DIR}/proxy_forwarding_test.py
            $<TARGET_FILE:token_proxy>
            ${CMAKE_SOURCE_DIR}/../schema/proxy
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
            ${CMAKE_SOURCE_DIR}/../schema/proxy
            ${CMAKE_SOURCE_DIR}/..
            --requests 40 --concurrency 8
)
