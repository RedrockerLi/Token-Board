# Python App tests belong to the App component. The proxy project is currently
# the repository's CTest entry point, so it adds this directory explicitly.
find_package(Python3 COMPONENTS Interpreter REQUIRED)
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
