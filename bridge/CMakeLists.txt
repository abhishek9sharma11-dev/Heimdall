cmake_minimum_required(VERSION 3.16)
project(zoom-bridge CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# --- Where the Zoom SDK lives ---
# SDK v7 layout: .so files live in zoomsdk/ root, headers in zoomsdk/h/
set(ZOOM_SDK_DIR ${CMAKE_SOURCE_DIR}/zoomsdk)
set(ZOOM_SDK_INCLUDE ${ZOOM_SDK_DIR}/h)
set(ZOOM_SDK_INCLUDE_COMPONENTS ${ZOOM_SDK_DIR}/h/meeting_service_components)
set(ZOOM_SDK_LIB ${ZOOM_SDK_DIR})

if(NOT EXISTS ${ZOOM_SDK_INCLUDE}/zoom_sdk.h)
    message(FATAL_ERROR
        "Zoom SDK headers not found at ${ZOOM_SDK_INCLUDE}.\n"
        "Download the Linux Meeting SDK from your Zoom Marketplace app and "
        "extract into bridge/zoomsdk/. See bridge/README.md.")
endif()

# --- Threads + OpenSSL (the SDK's JWT signing wants OpenSSL) ---
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)

# GLib main loop — required for Zoom SDK auth/meeting callbacks on Linux
find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB REQUIRED IMPORTED_TARGET glib-2.0)

# nlohmann/json — install on Ubuntu with: apt install nlohmann-json3-dev
find_package(nlohmann_json 3.2.0 REQUIRED)

# --- Bridge binary ---
add_executable(zoom-bridge
    src/main.cpp
    src/meeting.cpp
    src/chat.cpp
)

target_include_directories(zoom-bridge PRIVATE
    src
    ${ZOOM_SDK_INCLUDE}
    ${ZOOM_SDK_INCLUDE_COMPONENTS}
)

target_link_directories(zoom-bridge PRIVATE ${ZOOM_SDK_LIB})

# Names of SDK shared libs vary slightly between releases; adjust if needed.
target_link_libraries(zoom-bridge PRIVATE
    meetingsdk
    Threads::Threads
    OpenSSL::Crypto
    OpenSSL::SSL
    nlohmann_json::nlohmann_json
    PkgConfig::GLIB
    dl
)

# The SDK's Qt/X11 deps are resolved at runtime via LD_LIBRARY_PATH.
# Allow unresolved symbols in the SDK's transitive shared libs at link time.
target_link_options(zoom-bridge PRIVATE -Wl,--allow-shlib-undefined)

# Make the binary find libmeetingsdk.so at runtime
set_target_properties(zoom-bridge PROPERTIES
    INSTALL_RPATH "${ZOOM_SDK_LIB}"
    BUILD_RPATH "${ZOOM_SDK_LIB}"
)

# Copy the SDK .so files next to the binary so it Just Works
add_custom_command(TARGET zoom-bridge POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        ${ZOOM_SDK_DIR}/libmeetingsdk.so
        ${ZOOM_SDK_DIR}/libcml.so
        ${ZOOM_SDK_DIR}/libmpg123.so
        $<TARGET_FILE_DIR:zoom-bridge>
)
