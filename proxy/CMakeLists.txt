cmake_minimum_required(VERSION 3.16)
project(token-board-proxy VERSION 1.0.0 LANGUAGES C CXX)

include(CTest)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ── Find required system libraries ──────────────────────────────────────
find_package(OpenSSL REQUIRED)
find_package(Threads REQUIRED)

# ── Source files (sqlite3.c is compiled as C, others as C++) ────────────
# Each runtime layer is a real target.  This prevents a handler façade from
# accidentally gaining access to another layer's private implementation just
# because all files happened to be in one executable target.
set(SQLITE_SOURCE third_party/sqlite3.c)

set(FORMAT_SOURCES
    src/format/ir.cpp src/format/codec.cpp src/format/format_common.cpp
    src/format/format_openai.cpp src/format/format_openai_helpers.cpp
    src/format/format_openai_request.cpp src/format/format_openai_response.cpp
    src/format/format_openai_stream.cpp src/format/format_anthropic.cpp
    src/format/format_anthropic_helpers.cpp src/format/format_anthropic_request.cpp
    src/format/format_anthropic_response.cpp src/format/format_anthropic_stream.cpp
    src/format/format_responses.cpp src/format/format_responses_helpers.cpp
    src/format/format_responses_request.cpp src/format/format_responses_request_items.cpp
    src/format/format_responses_response.cpp
    src/format/format_responses_stream.cpp src/format/format_responses_emitter.cpp
    src/format/format_media.cpp
    src/format/usage_parser.cpp
    src/format/reasoning_bridge.cpp
    src/format/think_filter.cpp)

set(NET_SOURCES
    src/net/upstream_client.cpp src/net/upstream_forward.cpp
    src/net/upstream_metrics.cpp)

set(STORE_SOURCES
    src/store/db.cpp src/store/database_lifecycle.cpp
    src/store/database_statements.cpp src/store/routing_repository.cpp
    src/store/pricing_repository.cpp src/store/durable_spool.cpp
    src/store/usage_writer.cpp src/store/database_catalog.cpp
    src/store/usage_recorder.cpp)

set(RUNTIME_SOURCES
    src/core/config.cpp
    src/core/endpoint_policy.cpp
    src/core/attempt_executor.cpp
    src/core/request_context.cpp
    src/core/error_render.cpp
    src/core/router.cpp
    src/core/proxy_server.cpp
    src/core/proxy_accounting.cpp
    src/core/proxy_routing_runtime.cpp
    src/core/proxy_chat.cpp
    src/core/responses_state_store.cpp
    src/core/responses_state_helpers.cpp
    src/core/stream_pipeline.cpp
    src/core/proxy_models.cpp
    src/core/proxy_embeddings.cpp
    src/core/proxy_routes.cpp
)

add_library(token_proxy_format STATIC ${FORMAT_SOURCES})
add_library(token_proxy_net STATIC ${NET_SOURCES})
add_library(token_proxy_store STATIC ${STORE_SOURCES} ${SQLITE_SOURCE})
add_library(token_proxy_runtime STATIC ${RUNTIME_SOURCES})
add_executable(token_proxy src/main.cpp)

# Compile sqlite3.c as C (not C++), with optimisations
set_source_files_properties(${SQLITE_SOURCE} PROPERTIES
    LANGUAGE C
    COMPILE_FLAGS "-O2 -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_JSON1"
)

set(PROXY_INCLUDE_DIRS
    ${CMAKE_CURRENT_SOURCE_DIR}/src
    ${CMAKE_CURRENT_SOURCE_DIR}/src/net
    ${CMAKE_CURRENT_SOURCE_DIR}/src/core
    ${CMAKE_CURRENT_SOURCE_DIR}/src/store
    ${CMAKE_CURRENT_SOURCE_DIR}/src/format
    ${CMAKE_CURRENT_SOURCE_DIR}/third_party
    ${OPENSSL_INCLUDE_DIR}
)
foreach(target token_proxy_format token_proxy_net token_proxy_store token_proxy_runtime)
    target_include_directories(${target} PRIVATE ${PROXY_INCLUDE_DIRS})
endforeach()
target_include_directories(token_proxy PRIVATE ${PROXY_INCLUDE_DIRS})

target_link_libraries(token_proxy_format PRIVATE OpenSSL::Crypto Threads::Threads)
target_link_libraries(token_proxy_net PRIVATE
    OpenSSL::SSL
    OpenSSL::Crypto
    Threads::Threads
    ${CMAKE_DL_LIBS}
)
target_link_libraries(token_proxy_store PRIVATE
    OpenSSL::Crypto Threads::Threads ${CMAKE_DL_LIBS})
target_link_libraries(token_proxy_runtime PRIVATE
    token_proxy_format token_proxy_net token_proxy_store
    OpenSSL::SSL OpenSSL::Crypto Threads::Threads ${CMAKE_DL_LIBS})
target_link_libraries(token_proxy PRIVATE
    token_proxy_runtime token_proxy_format token_proxy_net token_proxy_store
    OpenSSL::SSL OpenSSL::Crypto Threads::Threads ${CMAKE_DL_LIBS})

# cpp-httplib defaults to a listen backlog of 5. A 64-way client burst then
# waits for TCP retransmission before it even reaches the measured task queue.
foreach(target token_proxy token_proxy_runtime token_proxy_net)
    target_compile_definitions(${target} PRIVATE CPPHTTPLIB_LISTEN_BACKLOG=512)
endforeach()

# ── Tests ────────────────────────────────────────────────────────────────
# Test targets are defined only in tests/CMakeLists.txt and built solely
# under BUILD_TESTING. They never link into token_proxy.
if(BUILD_TESTING)
    enable_testing()
    add_subdirectory(tests)
    add_subdirectory(
        ${CMAKE_CURRENT_SOURCE_DIR}/../app/tests
        ${CMAKE_CURRENT_BINARY_DIR}/app-tests
    )
endif()

# ── Compiler options ─────────────────────────────────────────────────────
if(CMAKE_BUILD_TYPE STREQUAL "Release")
    if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
        foreach(target token_proxy token_proxy_format token_proxy_net token_proxy_store token_proxy_runtime)
            target_compile_options(${target} PRIVATE -O3 -march=native -flto)
        endforeach()
        target_link_options(token_proxy PRIVATE -flto)
    endif()
endif()
