#!/usr/bin/env bash
# Build AKDirectPrint.dmg for macOS distribution.
#
# The .dmg contains AKDirectPrint.app + an Applications shortcut so the user
# can drag-to-install. The app then manages its own Login Item via launchctl.
#
# Usage:  cd agent && bash build/build_dmg.sh
# Output: agent/dist/AKDirectPrint.dmg
#
# Requirements:
#   - macOS (hdiutil and osascript are built-in)
#   - dist/AKDirectPrint.app must already exist (run build/build.sh first)

set -e
cd "$(dirname "$0")/.."   # run from agent/

VERSION="1.0.0"
APP_NAME="AKDirectPrint"
APP_SRC="dist/${APP_NAME}.app"
DMG_OUTPUT="dist/${APP_NAME}.dmg"
STAGING_DIR="/tmp/ak_direct_print_dmg_staging"
TEMP_DMG="/tmp/${APP_NAME}_temp.dmg"

echo "=== AK Direct Print — macOS DMG build ==="
echo "Version : $VERSION"
echo "Output  : $DMG_OUTPUT"
echo ""

# ── Check macOS ───────────────────────────────────────────────────────────────
if [ "$(uname)" != "Darwin" ]; then
    echo "ERROR: This script must run on macOS."
    exit 1
fi

# ── Check .app exists ─────────────────────────────────────────────────────────
if [ ! -d "$APP_SRC" ]; then
    echo "ERROR: $APP_SRC not found."
    echo "Build it first:  bash build/build.sh"
    exit 1
fi
echo "✔ App found: $APP_SRC"

# ── Staging directory ─────────────────────────────────────────────────────────
rm -rf "$STAGING_DIR"
mkdir -p "$STAGING_DIR"

# Copy .app into staging
cp -r "$APP_SRC" "$STAGING_DIR/"

# Symlink to /Applications so user can drag-install
ln -s /Applications "$STAGING_DIR/Applications"

echo "✔ Staging ready"

# ── Create writable DMG from staging ─────────────────────────────────────────
rm -f "$DMG_OUTPUT" "$TEMP_DMG"

hdiutil create \
    -volname "AK Direct Print" \
    -srcfolder "$STAGING_DIR" \
    -ov \
    -format UDRW \
    "$TEMP_DMG" >/dev/null

echo "✔ Writable DMG created"

# ── Mount, set window layout, unmount ────────────────────────────────────────
DEVICE=$(hdiutil attach -readwrite -noverify -noautoopen "$TEMP_DMG" \
    | grep '^/dev/' | head -1 | awk '{print $1}')

sleep 2

# AppleScript: set icon positions and window size for a clean drag-to-install UI
osascript << 'APPLESCRIPT'
tell application "Finder"
    tell disk "AK Direct Print"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, 760, 440}
        set theViewOptions to the icon view options of container window
        set arrangement of theViewOptions to not arranged
        set icon size of theViewOptions to 128
        set position of item "AKDirectPrint.app" of container window to {160, 170}
        set position of item "Applications"      of container window to {400, 170}
        close
        open
        update without registering applications
        delay 1
        close
    end tell
end tell
APPLESCRIPT

sync
hdiutil detach "$DEVICE" >/dev/null

echo "✔ Window layout applied"

# ── Convert to compressed, read-only DMG ─────────────────────────────────────
mkdir -p dist
hdiutil convert "$TEMP_DMG" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -o "$DMG_OUTPUT" >/dev/null

rm -f "$TEMP_DMG"
rm -rf "$STAGING_DIR"

echo ""
echo "=== Build complete ==="
echo "Package : $DMG_OUTPUT ($(du -sh "$DMG_OUTPUT" | cut -f1))"
echo ""
echo "Client install steps:"
echo "  1. Double-click AKDirectPrint.dmg"
echo "  2. Drag AKDirectPrint → Applications folder"
echo "  3. Eject the disk image"
echo "  4. Open Applications → double-click AKDirectPrint"
echo "  5. Tray icon appears — check 'Start on Login' from the menu"
echo ""
