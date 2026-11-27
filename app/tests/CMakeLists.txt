# Python App tests belong to the App component. The proxy project is currently
# the repository's CTest entry point, so it adds this directory explicitly.
# The interpreter default and dependency validation helpers are defined by
# proxy/tests/CMakeLists.txt, which the proxy project processes first.
tb_prepare_python3_for_tests()
find_package(Python3 COMPONENTS Interpreter REQUIRED)
tb_require_python_test_deps()
find_program(NODE_EXECUTABLE node REQUIRED)

get_filename_component(REPOSITORY_ROOT "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)

add_test(
    NAME app_testbench
    COMMAND ${Python3_EXECUTABLE} -m unittest discover
            -s ${CMAKE_CURRENT_LIST_DIR} -t ${REPOSITORY_ROOT}
            -p test_*.py -v
)
set_tests_properties(app_testbench PROPERTIES
    ENVIRONMENT "PYTHONPATH=${REPOSITORY_ROOT}"
    LABELS "app;integration"
)

add_test(
    NAME frontend_proxy_billing_export
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_proxy_billing_export.js
)

add_test(
    NAME frontend_proxy_billing_stats_card
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_proxy_billing_stats_card.js
)

add_test(
    NAME frontend_proxy_billing_daily_chart
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_proxy_billing_daily_chart.js
)

add_test(
    NAME frontend_dashboard_calendar_tooltip
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_dashboard_calendar_tooltip.js
)

add_test(
    NAME frontend_proxy_manager_key_order
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_proxy_manager_key_order.js
)

add_test(
    NAME frontend_proxy_manager_usage_card
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_proxy_manager_usage_card.js
)

add_test(
    NAME frontend_proxy_manager_usage_refresh
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_proxy_manager_usage_refresh.js
)

add_test(
    NAME frontend_proxy_manager_accounts_actions
    COMMAND ${NODE_EXECUTABLE}
            ${CMAKE_CURRENT_LIST_DIR}/test_proxy_manager_accounts_actions.js
)

foreach(FRONTEND_TZ Asia/Shanghai America/New_York UTC Pacific/Kiritimati)
    string(REPLACE "/" "_" FRONTEND_TZ_NAME "${FRONTEND_TZ}")
    add_test(
        NAME frontend_time_utils_${FRONTEND_TZ_NAME}
        COMMAND ${NODE_EXECUTABLE}
                ${CMAKE_CURRENT_LIST_DIR}/test_time_utils.js
                ${FRONTEND_TZ}
    )
    set_tests_properties(frontend_time_utils_${FRONTEND_TZ_NAME}
        PROPERTIES ENVIRONMENT "TZ=${FRONTEND_TZ}")
endforeach()

# Same CI guard as the proxy test suite: a hung test must not occupy the runner
# forever. add_test() has no TIMEOUT argument, so set it for the directory once;
# the list comes from the directory, so tests added later inherit it.
get_property(TB_DIRECTORY_TESTS DIRECTORY PROPERTY TESTS)
set_tests_properties(${TB_DIRECTORY_TESTS} PROPERTIES TIMEOUT 300)
