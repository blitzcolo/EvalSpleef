cmake_minimum_required(VERSION 3.20)
project(vulkan_shadow CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# 着色器：要求环境里有 glslc（Vulkan SDK 提供）
set(SHADER_DIR ${CMAKE_CURRENT_SOURCE_DIR}/shaders)
set(SHADER_OUT ${CMAKE_CURRENT_BINARY_DIR}/shaders)
file(MAKE_DIRECTORY ${SHADER_OUT})
foreach(SHADER scene.vert scene.frag shadow.vert shadow.frag)
  add_custom_command(
    OUTPUT ${SHADER_OUT}/${SHADER}.spv
    COMMAND glslc ${SHADER_DIR}/${SHADER} -o ${SHADER_OUT}/${SHADER}.spv
    DEPENDS ${SHADER_DIR}/${SHADER}
  )
  list(APPEND SPV_FILES ${SHADER_OUT}/${SHADER}.spv)
endforeach()

add_executable(vulkan_shadow src/main.cpp src/shadow_math.cpp ${SPV_FILES})
target_include_directories(vulkan_shadow PRIVATE src)
target_compile_definitions(vulkan_shadow PRIVATE SHADER_OUT_DIR="${SHADER_OUT}")
find_package(Vulkan REQUIRED)
target_link_libraries(vulkan_shadow PRIVATE Vulkan::Vulkan)
