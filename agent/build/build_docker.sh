#!/usr/bin/env bash
# Build AKDirectPrint inside Ubuntu 22.04 Docker container.
# The resulting binary runs on Ubuntu 22.04, 23.x, 24.04 and later.
#
# Usage:  cd agent && bash build/build_docker.sh
# Output: agent/dist/AKDirectPrint   +   agent/dist/ak-direct-print_1.0.0_amd64.deb
#
# Requirements: docker installed and running on the host.

set -e
cd "$(dirname "$0")/.."   # run from agent/

IMAGE="ubuntu:22.04"
CONTAINER="ak_direct_print_build"

echo "=== AK Direct Print — Docker build (Ubuntu 22.04 → GLIBC 2.35) ==="
echo "Host   : $(lsb_release -ds 2>/dev/null || uname -r)"
echo "Builder: $IMAGE"
echo ""

# ── Check Docker is available ─────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker not installed."
    echo "Install: sudo apt install docker.io && sudo usermod -aG docker \$USER"
    echo "Then log out and back in, and re-run this script."
    exit 1
fi

# ── Run build inside Ubuntu 22.04 container ───────────────────────────────────
docker run --rm \
    --name "$CONTAINER" \
    -v "$(pwd)":/agent \
    -w /agent \
    "$IMAGE" \
    bash -c '
        set -e
        export DEBIAN_FRONTEND=noninteractive

        echo "--- Installing system packages inside container ---"
        apt-get update -qq
        apt-get install -y -q \
            python3 python3-pip python3-venv \
            cups-client avahi-utils \
            python3-gi gir1.2-appindicator3-0.1 \
            binutils upx-ucl 2>/dev/null || true

        echo "--- Setting up Python venv ---"
        python3 -m venv /build_venv --system-site-packages
        echo "/usr/lib/python3/dist-packages" > \
            /build_venv/lib/python3.10/site-packages/system-gi.pth

        echo "--- Installing pip packages ---"
        /build_venv/bin/pip install --quiet \
            flask Pillow pystray pyinstaller

        echo "--- Running PyInstaller ---"
        /build_venv/bin/pyinstaller build/ak_direct_print.spec \
            --clean --noconfirm

        echo "--- Fixing ownership (container runs as root) ---"
        chown -R '"$(id -u):$(id -g)"' dist/ build/ 2>/dev/null || true
    '

echo ""
echo "=== Binary built ==="
echo "Binary : dist/AKDirectPrint ($(du -sh dist/AKDirectPrint | cut -f1))"

# ── Now build the .deb ────────────────────────────────────────────────────────
echo ""
echo "--- Building .deb package ---"
bash build/build_deb.sh

echo ""
echo "=== All done ==="
echo "Binary : dist/AKDirectPrint"
echo "Package: dist/ak-direct-print_1.0.0_amd64.deb"
echo ""
echo "Compatible with: Ubuntu 22.04, 23.x, 24.04+"
