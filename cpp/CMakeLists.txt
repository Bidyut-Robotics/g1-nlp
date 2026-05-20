cmake_minimum_required(VERSION 3.14)
project(robot_agent CXX)

set(CMAKE_CXX_STANDARD 17)

# Headers SDK — new clone with G1-specific headers (no prebuilt lib needed)
# Override: cmake -DUNITREE_SDK2_PATH=/path/to/sdk ..
if(NOT DEFINED UNITREE_SDK2_PATH)
    set(UNITREE_SDK2_PATH "$ENV{HOME}/unitree_sdk2")
endif()

# Lib SDK — older release that has prebuilt .a and thirdparty .so files
# Override: cmake -DUNITREE_SDK2_LIB_PATH=/path/to/old-sdk ..
if(NOT DEFINED UNITREE_SDK2_LIB_PATH)
    set(UNITREE_SDK2_LIB_PATH ${UNITREE_SDK2_PATH})
endif()

set(ARCH ${CMAKE_SYSTEM_PROCESSOR})  # aarch64 or x86_64

add_executable(robot_agent robot_agent.cpp)

target_include_directories(robot_agent PRIVATE
    ${UNITREE_SDK2_PATH}/include
    ${UNITREE_SDK2_LIB_PATH}/thirdparty/include
    ${UNITREE_SDK2_LIB_PATH}/thirdparty/include/ddscxx
)

target_link_libraries(robot_agent PRIVATE
    ${UNITREE_SDK2_LIB_PATH}/lib/${ARCH}/libunitree_sdk2.a
    ${UNITREE_SDK2_LIB_PATH}/thirdparty/lib/${ARCH}/libddscxx.so
    ${UNITREE_SDK2_LIB_PATH}/thirdparty/lib/${ARCH}/libddsc.so
    pthread
)
