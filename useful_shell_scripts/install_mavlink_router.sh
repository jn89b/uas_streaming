#!/usr/bin/env bash

set -euo pipefail

REPO_DIR="${REPO_DIR:-mavlink-router}"

# Install dependencies (Debian / Raspberry Pi OS / Ubuntu)
sudo apt update
sudo apt install -y \
    git \
    meson \
    ninja-build \
    build-essential \
    pkg-config \
    cmake \
    libsystemd-dev \
    systemd-dev

# Clone repository if it is not already present
git clone https://github.com/mavlink-router/mavlink-router.git "$REPO_DIR"

cd "$REPO_DIR"

# Initialize/update MAVLink submodules
git submodule update --init --recursive

# Verify the exact pkg-config dependency requested by mavlink-router
pkg-config --modversion systemd

# Remove a failed, stale, or Meson-version-mismatched build directory
rm -rf build

# Configure build
meson setup build .

# Compile
meson compile -C build

# Install
sudo meson install -C build

echo "MAVLink Router installed successfully."