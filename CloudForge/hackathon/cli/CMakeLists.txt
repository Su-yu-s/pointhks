cmake_minimum_required(VERSION 3.15)
project(cloudforge_cli LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(MSVC)
  add_compile_options(/utf-8 /DNOMINMAX /W3)
endif()

# 复用仓库内核心库（RANSAC 检测 + 精拟合），与 src-tauri/cpp 共用同一份源码
add_subdirectory(
    ${CMAKE_CURRENT_SOURCE_DIR}/../../src-tauri/cpp
    ${CMAKE_CURRENT_BINARY_DIR}/cloudforge_core_build
)

add_executable(cloudforge-cli main.cpp)
target_link_libraries(cloudforge-cli PRIVATE cloudforge_core)
