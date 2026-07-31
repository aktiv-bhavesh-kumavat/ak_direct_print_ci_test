#!/usr/bin/env bash
# Cross-compile the Windows agent binary from Linux using Wine + Docker.
#
# Requirements:
#   - Docker installed and running
#   - Internet access (pulls tobix/pywine image on first run ~2 GB)
#
# Usage:
#   cd agent && bash build/build_docker_windows.sh
#
# Output:
#   dist/AKDirectPrint.exe          — standalone Windows binary
#   static/agent/AKDirectPrint_Setup.exe   — NOT built here (needs Inno Setup on Windows)
#
# Note: Inno Setup cannot run under Wine reliably for packaging.
# After building the .exe here, copy it to a Windows machine and run:
#   build\build_installer_win.bat
# to produce the final AKDirectPrint_Setup.exe installer.

set -e
cd "$(dirname "$0")/.."   # run from agent/

WINDOWS_VERSION="${WIN_VERSION:-1.0.0}"
IMAGE="tobix/pywine:3.12"
HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

echo "=== AK Direct Print — Windows Cross-Build (Wine/Docker) ==="
echo "Version  : $WINDOWS_VERSION"
echo "Builder  : $IMAGE"
echo "Output   : dist/AKDirectPrint.exe"
echo ""

# ── Check Docker ──────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: Docker not installed."
    echo "  sudo apt install docker.io"
    echo "  sudo usermod -aG docker \$USER   # then log out and back in"
    exit 1
fi

echo "--- Pulling Wine Docker image (only once) ---"
docker pull "$IMAGE"

echo ""
echo "--- Running PyInstaller inside Wine ---"
docker run --rm \
    --name ak_direct_print_win_build \
    -v "$(pwd)":/agent \
    -w /agent \
    -e WINEDEBUG=-all \
    -e WINEARCH=win64 \
    "$IMAGE" \
    bash -c "
        set -e

        echo '--- Installing Python packages (via Wine pip) ---'
        wine python -m pip install --quiet --upgrade pip
        wine python -m pip install --quiet \
            flask Pillow pystray pywin32 pyinstaller

        echo '--- Building with PyInstaller ---'
        wine python -m PyInstaller build/ak_direct_print.spec \
            --distpath dist_win \
            --workpath /tmp/ak_pyinstaller_win \
            --clean --noconfirm

        echo '--- Fixing ownership ---'
        chown -R ${HOST_UID}:${HOST_GID} dist_win/ 2>/dev/null || true
    "

# Normalize output path
if [ -f "dist_win/AKDirectPrint.exe" ]; then
    mkdir -p dist
    cp "dist_win/AKDirectPrint.exe" "dist/AKDirectPrint.exe"
    echo ""
    echo "✔ Binary: dist/AKDirectPrint.exe ($(du -sh dist/AKDirectPrint.exe | cut -f1))"
else
    echo "ERROR: dist_win/AKDirectPrint.exe not found — check Wine/PyInstaller output above."
    exit 1
fi

echo ""
echo "=== Windows binary build complete ==="
echo ""
echo "Next steps to create the installer:"
echo "  1. Copy dist/AKDirectPrint.exe to a Windows machine (or VM)"
echo "  2. cd agent"
echo "  3. build\\build_installer_win.bat"
echo "  4. Copy dist\\AKDirectPrint_Setup.exe to:"
echo "     static/agent/AKDirectPrint_Setup.exe"
echo ""
echo "For a quick test without the installer, you can run AKDirectPrint.exe directly."
