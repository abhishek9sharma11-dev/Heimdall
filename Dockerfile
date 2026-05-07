FROM --platform=linux/amd64 ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# System deps
RUN apt-get update && apt-get install -y \
    cmake g++ make \
    libssl-dev \
    nlohmann-json3-dev \
    python3.10 python3.10-venv python3-pip \
    curl \
    libglib2.0-0 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libxkbcommon-x11-0 \
    libxcb-keysyms1 \
    libxcb-shm0 \
    libxcb-xkb1 \
    libxcb-shape0 \
    libxcb-randr0 \
    libxcb-image0 \
    libxcb-render-util0 \
    libxcb-render0 \
    libxcb-icccm4 \
    libxcb-sync1 \
    libxcb-xtest0 \
    libxcb-xfixes0 \
    libxcb-util1 \
    libxcb-cursor0 \
    libx11-xcb1 \
    libx11-6 \
    libgl1 \
    libgles2 \
    libgbm1 \
    libegl1 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (better layer caching)
COPY requirements.txt .
RUN python3.10 -m pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Fix RPATH so the bridge binary finds SDK libs at $ORIGIN at runtime
RUN sed -i 's|INSTALL_RPATH "${ZOOM_SDK_LIB}"|INSTALL_RPATH "$ORIGIN"|' bridge/CMakeLists.txt && \
    sed -i 's|BUILD_RPATH "${ZOOM_SDK_LIB}"|BUILD_RPATH "$ORIGIN"|' bridge/CMakeLists.txt

# Build C++ bridge
RUN cd bridge && mkdir -p build && cd build && cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc)

# Copy all SDK runtime files next to the bridge binary
RUN cp bridge/zoomsdk/libmeetingsdk.so bridge/zoomsdk/libcml.so bridge/zoomsdk/libmpg123.so bridge/build/ && \
    cp -r bridge/zoomsdk/qt_libs bridge/build/ && \
    cp bridge/zoomsdk/cpthost bridge/build/ && \
    cp -r bridge/zoomsdk/imjs bridge/build/ && \
    cp -r bridge/zoomsdk/images bridge/build/ && \
    cp -r bridge/zoomsdk/json bridge/build/ && \
    ln -sf libmeetingsdk.so bridge/build/libmeetingsdk.so.1 && \
    ln -sf libcml.so bridge/build/libcml.so.1 && \
    ln -sf libmpg123.so bridge/build/libmpg123.so.1

EXPOSE 8765

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
