# ── Proxy test suite ─────────────────────────────────────────────────────
# This subdirectory is only reached when BUILD_TESTING is ON (see top-level
# CMakeLists.txt). Every target here is a test executable; none of them link
# into the production token_proxy binary.

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
