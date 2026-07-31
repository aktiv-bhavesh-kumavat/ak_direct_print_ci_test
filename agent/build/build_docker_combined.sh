#!/usr/bin/env bash
# Build a SINGLE .deb that works on both Ubuntu 22 and Ubuntu 24.
#
# Strategy: build the PyInstaller binary inside an Ubuntu 22.04 Docker
# container so the resulting binary links against GLIBC 2.35.
# Ubuntu 22 has GLIBC 2.35; Ubuntu 24 has GLIBC 2.39 (backward-compatible).
# One binary, one .deb, runs on both.
#
# Usage:  cd agent && bash build/build_docker_combined.sh
# Output: dist/ak-direct-print_1.2.0_amd64.deb
#         static/agent/ak_direct_print_combined_amd64.deb  (copy for Odoo)
#
# Requirements: docker installed and running on the host.

set -e
cd "$(dirname "$0")/.."   # run from agent/

COMBINED_VERSION="1.2.0"
COMBINED_DEB="ak-direct-print_${COMBINED_VERSION}_amd64.deb"
IMAGE="ubuntu:22.04"
CONTAINER="ak_direct_print_combined_build"

echo "=== AK Direct Print — Combined Build (Ubuntu 22 + 24) ==="
echo "Version  : $COMBINED_VERSION"
echo "Builder  : $IMAGE  (GLIBC 2.35 — runs on Ubuntu 22.04 and 24.04+)"
echo "Output   : dist/$COMBINED_DEB"
echo ""

# ── Check Docker ──────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker not installed."
    echo "  sudo apt install docker.io"
    echo "  sudo usermod -aG docker \$USER   # then log out and back in"
    exit 1
fi

# ── Build PyInstaller binary inside Ubuntu 22.04 container ───────────────────
echo "--- Starting Docker build (this takes a few minutes on first run) ---"
docker run --rm \
    --name "$CONTAINER" \
    -v "$(pwd)":/agent \
    -w /agent \
    "$IMAGE" \
    bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive

        echo "--- Installing system packages ---"
        apt-get update -qq
        apt-get install -y -q \
            python3 python3-pip python3-venv \
            binutils upx-ucl \
            cups-client avahi-utils 2>/dev/null || true

        echo "--- Setting up venv ---"
        python3 -m venv /build_venv
        /build_venv/bin/pip install --quiet --upgrade pip

        echo "--- Installing Python packages ---"
        /build_venv/bin/pip install --quiet \
            flask Pillow pystray pyinstaller

        echo "--- Running PyInstaller ---"
        /build_venv/bin/pyinstaller build/ak_direct_print.spec \
            --distpath dist --workpath /tmp/ak_pyinstaller_build \
            --clean --noconfirm

        echo "--- Fixing file ownership ---"
        chown -R '"$(id -u):$(id -g)"' dist/ 2>/dev/null || true
    '

echo ""
echo "✔ Binary built: dist/AKDirectPrint ($(du -sh dist/AKDirectPrint | cut -f1))"

# ── Package the .deb ─────────────────────────────────────────────────────────
echo ""
echo "--- Packaging .deb ($COMBINED_VERSION) ---"
DEB_VERSION="$COMBINED_VERSION" bash build/build_deb.sh

# ── Copy to static/agent for Odoo download ───────────────────────────────────
STATIC_DIR="$(dirname "$0")/../../static/agent"
STATIC_DIR="$(cd "$STATIC_DIR" && pwd)"
cp "dist/$COMBINED_DEB" "$STATIC_DIR/ak_direct_print_combined_amd64.deb"

echo ""
echo "=== Combined build complete ==="
echo ""
echo "  Package  : dist/$COMBINED_DEB"
echo "  Static   : static/agent/ak_direct_print_combined_amd64.deb"
echo "  GLIBC    : 2.35  (Ubuntu 22.04+, Ubuntu 24.04+)"
echo ""
echo "Install on the client:"
echo "  sudo dpkg -i ak_direct_print_combined_amd64.deb"
