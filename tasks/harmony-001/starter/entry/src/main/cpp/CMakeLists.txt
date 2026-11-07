cmake_minimum_required(VERSION 3.5.0)
project(imagefilters)

set(NATIVERENDER_ROOT_PATH ${CMAKE_CURRENT_SOURCE_DIR})

include_directories(${NATIVERENDER_ROOT_PATH})

add_library(imageops SHARED
    napi_init.cpp
    image_ops.cpp
)

target_link_libraries(imageops PUBLIC
    libace_napi.z.so
    libhilog_ndk.z.so
)
