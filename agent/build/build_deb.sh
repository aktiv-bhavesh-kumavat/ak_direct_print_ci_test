#!/usr/bin/env bash
# Build ak-direct-print.deb
# Usage: cd agent && bash build/build_deb.sh
# Output: agent/dist/ak-direct-print_1.0.0_amd64.deb
#
# The client installs it by double-clicking in Files → Ubuntu Software Center,
# or via:  sudo dpkg -i ak-direct-print_1.0.0_amd64.deb

set -e
cd "$(dirname "$0")/.."   # run from agent/

VERSION="${DEB_VERSION:-1.1.0}"
PKG_NAME="ak-direct-print"
ARCH="amd64"
DEB_NAME="${PKG_NAME}_${VERSION}_${ARCH}.deb"
BINARY_SRC="dist/AKDirectPrint"
BUILD_DIR="build/deb"
STAGING_DIR="/tmp/ak_direct_print_deb_staging"

echo "=== AK Direct Print — .deb build ==="
echo "Version : $VERSION"
echo "Output  : dist/$DEB_NAME"
echo ""

# ── 1. Check the PyInstaller binary exists ───────────────────────────────────
if [ ! -f "$BINARY_SRC" ]; then
    echo "ERROR: $BINARY_SRC not found."
    echo "Build it first:  bash build/build.sh"
    exit 1
fi
echo "✔ Binary found: $BINARY_SRC ($(du -sh "$BINARY_SRC" | cut -f1))"

# ── 2. Prepare staging directory ─────────────────────────────────────────────
rm -rf "$STAGING_DIR"
cp -r "$BUILD_DIR" "$STAGING_DIR"

# ── 3. Copy binary into staging ──────────────────────────────────────────────
cp "$BINARY_SRC" "$STAGING_DIR/usr/local/bin/$PKG_NAME"
chmod 755 "$STAGING_DIR/usr/local/bin/$PKG_NAME"

# ── 3b. Copy tray.py + icon into staging ────────────────────────────────────
mkdir -p "$STAGING_DIR/usr/local/lib/ak-direct-print"
cp tray.py "$STAGING_DIR/usr/local/lib/ak-direct-print/tray.py"
chmod 644 "$STAGING_DIR/usr/local/lib/ak-direct-print/tray.py"
# Copy module icon for the desktop launcher
ICON_SRC="../static/description/icon.png"
if [ -f "$ICON_SRC" ]; then
    cp "$ICON_SRC" "$STAGING_DIR/usr/local/lib/ak-direct-print/icon.png"
    chmod 644 "$STAGING_DIR/usr/local/lib/ak-direct-print/icon.png"
    echo "✔ Icon copied from $ICON_SRC"
else
    echo "⚠  Icon not found at $ICON_SRC — system 'printer' icon will be used"
fi
# Remove .gitkeep placeholder if present
rm -f "$STAGING_DIR/usr/local/lib/ak-direct-print/.gitkeep"

# ── 4. Stamp version into control + set permissions ──────────────────────────
sed -i "s/^Version:.*/Version: $VERSION/" "$STAGING_DIR/DEBIAN/control"
chmod 755 "$STAGING_DIR/DEBIAN/postinst"
chmod 755 "$STAGING_DIR/DEBIAN/prerm"
chmod 644 "$STAGING_DIR/DEBIAN/control"

# ── 5. Generate md5sums ──────────────────────────────────────────────────────
find "$STAGING_DIR" -not -path "$STAGING_DIR/DEBIAN/*" -type f \
    | sort \
    | xargs md5sum 2>/dev/null \
    | sed "s|$STAGING_DIR/||" \
    > "$STAGING_DIR/DEBIAN/md5sums"
chmod 644 "$STAGING_DIR/DEBIAN/md5sums"

# ── 6. Build the .deb ────────────────────────────────────────────────────────
mkdir -p dist
dpkg-deb --build --root-owner-group "$STAGING_DIR" "dist/$DEB_NAME"

# ── 7. Cleanup staging ───────────────────────────────────────────────────────
rm -rf "$STAGING_DIR"

echo ""
echo "=== Build complete ==="
echo "Package : dist/$DEB_NAME ($(du -sh "dist/$DEB_NAME" | cut -f1))"
echo ""
echo "Client install options:"
echo "  A) Double-click $DEB_NAME in Files manager → Ubuntu Software Center"
echo "  B) sudo dpkg -i dist/$DEB_NAME"
echo ""
echo "To verify package contents:"
echo "  dpkg-deb -c dist/$DEB_NAME"
