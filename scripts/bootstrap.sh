#!/usr/bin/env bash
# bootstrap.sh — Install build dependencies on the remote VPS.
# Uploaded and run by kcb/build.py::bootstrap().
# Idempotent: safe to run multiple times.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "[bootstrap] Updating apt package lists..."
apt-get update -qq

echo "[bootstrap] Installing build dependencies..."
apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    flex \
    bison \
    bc \
    libssl-dev \
    libelf-dev \
    libncurses-dev \
    wget \
    curl \
    unzip \
    cpio \
    rsync \
    git \
    xz-utils \
    bzip2 \
    file \
    patch \
    perl \
    python3 \
    python3-dev \
    gawk \
    fakeroot \
    texinfo \
    m4 \
    gettext \
    e2fsprogs

HOST_ARCH="$(uname -m)"
echo "[bootstrap] Detected host arch: ${HOST_ARCH}"

if [ "${HOST_ARCH}" = "x86_64" ]; then
    echo "[bootstrap] Installing cross-toolchain for arm64 target..."
    apt-get install -y --no-install-recommends \
        gcc-aarch64-linux-gnu \
        g++-aarch64-linux-gnu \
        binutils-aarch64-linux-gnu
elif [ "${HOST_ARCH}" = "aarch64" ]; then
    echo "[bootstrap] Installing cross-toolchain for x86_64 target..."
    apt-get install -y --no-install-recommends \
        gcc-x86-64-linux-gnu \
        g++-x86-64-linux-gnu \
        binutils-x86-64-linux-gnu
else
    echo "[bootstrap] Warning: unknown host arch ${HOST_ARCH}, skipping cross-toolchain install"
fi

GO_VERSION="1.26.1"
if [ "${HOST_ARCH}" = "aarch64" ]; then
    GO_ARCH="arm64"
else
    GO_ARCH="amd64"
fi
GO_TARBALL="go${GO_VERSION}.linux-${GO_ARCH}.tar.gz"
GO_URL="https://go.dev/dl/${GO_TARBALL}"

echo "[bootstrap] Installing Go ${GO_VERSION} from ${GO_URL}..."
wget -q "${GO_URL}" -O "/tmp/${GO_TARBALL}"
rm -rf /usr/local/go
tar -C /usr/local -xzf "/tmp/${GO_TARBALL}"
rm "/tmp/${GO_TARBALL}"
ln -sf /usr/local/go/bin/go /usr/local/bin/go
ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
echo "[bootstrap] Found: $(go version)"

echo "[bootstrap] Bootstrap complete. All build dependencies installed."
