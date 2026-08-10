# Python App tests belong to the App component. The proxy project is currently
# the repository's CTest entry point, so it adds this directory explicitly.
find_package(Python3 COMPONENTS Interpreter REQUIRED)

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
