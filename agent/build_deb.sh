#!/usr/bin/env bash
# Build the AK Direct Print Ubuntu .deb installer from the current agent source.
# Run from the agent/ directory:  bash build_deb.sh
# Output: ../static/agent/ak_direct_print_combined_amd64.deb

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODULE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATIC_AGENT="$MODULE_ROOT/static/agent"

VERSION="2.0.5"
PKG_NAME="ak-direct-print"
ARCH="all"
DEB_FILE="$STATIC_AGENT/ak_direct_print_combined_amd64.deb"

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

PKG_ROOT="$BUILD_DIR/${PKG_NAME}_${VERSION}_${ARCH}"

echo "=== AK Direct Print — .deb builder ==="
echo "    Version : $VERSION"
echo "    Source  : $SCRIPT_DIR"
echo "    Output  : $DEB_FILE"
echo ""

# ── 1. Directory structure ────────────────────────────────────────────────────

mkdir -p "$PKG_ROOT/DEBIAN"
mkdir -p "$PKG_ROOT/usr/local/lib/ak-direct-print"
mkdir -p "$PKG_ROOT/usr/local/bin"
mkdir -p "$PKG_ROOT/usr/share/applications"
mkdir -p "$PKG_ROOT/etc/xdg/autostart"

# ── 2. Agent source files ────────────────────────────────────────────────────

cp "$SCRIPT_DIR/ak_direct_print_agent.py" "$PKG_ROOT/usr/local/lib/ak-direct-print/"
cp "$SCRIPT_DIR/tray.py"                  "$PKG_ROOT/usr/local/lib/ak-direct-print/"

# ── 3. Launcher wrapper script ────────────────────────────────────────────────

cat > "$PKG_ROOT/usr/local/bin/ak-direct-print" <<'EOF'
#!/usr/bin/env bash
exec python3 /usr/local/lib/ak-direct-print/ak_direct_print_agent.py "$@"
EOF
chmod 755 "$PKG_ROOT/usr/local/bin/ak-direct-print"

# ── 4. Desktop entry (app launcher) ──────────────────────────────────────────

cat > "$PKG_ROOT/usr/share/applications/ak-direct-print.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AK Direct Print
Comment=Print Station Agent — connects your printer to Odoo
Exec=/usr/local/bin/ak-direct-print
Icon=printer
Terminal=false
Categories=Utility;Office;
StartupNotify=false
EOF

# ── 5. System-wide autostart entry ───────────────────────────────────────────
#    Placed in /etc/xdg/autostart/ so it starts for every user on login.
#    Individual users can disable it via "Start on Login" in the tray menu.

cat > "$PKG_ROOT/etc/xdg/autostart/ak-direct-print.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=AK Direct Print
Comment=AK Direct Print Station Agent
Exec=/usr/local/bin/ak-direct-print
Icon=printer
Terminal=false
Hidden=false
X-GNOME-Autostart-enabled=true
EOF

# ── 6. DEBIAN/control ────────────────────────────────────────────────────────

cat > "$PKG_ROOT/DEBIAN/control" <<EOF
Package: $PKG_NAME
Version: $VERSION
Architecture: $ARCH
Maintainer: Aktiv Software <odoo@aktivsoftware.com>
Installed-Size: 128
Depends: python3 (>= 3.8), python3-tk, python3-gi, python3-cairo, gir1.2-appindicator3-0.1, gir1.2-gtk-3.0, python3-cups, python3-flask, python3-requests, python3-pil
Recommends: cups
Description: AK Direct Print — Station Agent
 Connects a client machine to Odoo's AK Direct Print module.
 Authenticates to Odoo over HTTPS (no port-forwarding needed),
 syncs local printers automatically, and picks up print jobs
 queued by Odoo within 5 seconds.
 .
 The setup dialog opens on first run — enter the Odoo URL and
 login credentials to register this machine as a Print Station.
EOF

# ── 7. DEBIAN/postinst — runs as root after files are placed ─────────────────

cat > "$PKG_ROOT/DEBIAN/postinst" <<'POSTINST'
#!/usr/bin/env bash
set -e

chmod 755 /usr/local/lib/ak-direct-print/ak_direct_print_agent.py 2>/dev/null || true
chmod 755 /usr/local/lib/ak-direct-print/tray.py 2>/dev/null || true

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications 2>/dev/null || true
fi

# ── Auto-launch for the active graphical user ─────────────────────────────────
# Reads DISPLAY + XAUTHORITY directly from the compositor's /proc environ —
# the only approach that works reliably on both X11 and Wayland (Ubuntu 22/24).

_launched=0
_launch_user=""
_launch_uid=""
_session_type=""

# ── Step 1: find the active graphical user via loginctl ───────────────────────
if command -v loginctl >/dev/null 2>&1; then
    while read -r SID UID_NUM USERNAME SEAT REST; do
        [ -z "$USERNAME" ] || [ "$USERNAME" = "root" ] && continue
        [ -z "$SEAT" ]    || [ "$SEAT"     = "-"    ] && continue
        _launch_user="$USERNAME"
        _launch_uid="$UID_NUM"
        _session_type=$(loginctl show-session "$SID" -p Type --value 2>/dev/null || echo "unknown")
        break
    done < <(loginctl list-sessions --no-legend 2>/dev/null)
fi

# ── Step 2: fallback — infer user from X11 socket ownership ───────────────────
if [ -z "$_launch_user" ]; then
    for X_SOCK in /tmp/.X11-unix/X*; do
        [ -S "$X_SOCK" ] || continue
        SOCK_OWNER=$(stat -c '%U' "$X_SOCK" 2>/dev/null || true)
        [ -z "$SOCK_OWNER" ] || [ "$SOCK_OWNER" = "root" ] && continue
        _launch_user="$SOCK_OWNER"
        _launch_uid=$(id -u "$SOCK_OWNER" 2>/dev/null || true)
        _session_type="x11"
        break
    done
fi

# ── Step 3: launch as the identified user ─────────────────────────────────────
if [ -n "$_launch_user" ] && [ -n "$_launch_uid" ]; then
    RUNTIME="/run/user/${_launch_uid}"
    DBUS_ADDR="unix:path=${RUNTIME}/bus"

    # Read DISPLAY + XAUTHORITY directly from the compositor's process environment.
    # This is the most reliable approach: works on X11 (DISPLAY=:1, gdm Xauth) and
    # Wayland (DISPLAY=:0 XWayland, mutter random Xauth) without any guessing.
    DISP=""
    XAUTH=""
    COMPOSITOR_PID=$(pgrep -u "$_launch_user" gnome-shell 2>/dev/null | head -1 || \
                     pgrep -u "$_launch_user" plasmashell 2>/dev/null | head -1 || \
                     pgrep -u "$_launch_user" kwin_wayland 2>/dev/null | head -1 || \
                     pgrep -u "$_launch_user" xfwm4 2>/dev/null | head -1 || true)

    if [ -n "$COMPOSITOR_PID" ] && [ -r "/proc/${COMPOSITOR_PID}/environ" ]; then
        DISP=$(tr '\0' '\n' < "/proc/${COMPOSITOR_PID}/environ" 2>/dev/null \
               | grep '^DISPLAY=' | cut -d= -f2- | head -1 || true)
        XAUTH=$(tr '\0' '\n' < "/proc/${COMPOSITOR_PID}/environ" 2>/dev/null \
                | grep '^XAUTHORITY=' | cut -d= -f2- | head -1 || true)
    fi

    # Fallbacks if compositor env read failed
    if [ -z "$DISP" ]; then
        for X_SOCK in /tmp/.X11-unix/X*; do
            [ -S "$X_SOCK" ] && DISP=":${X_SOCK##/tmp/.X11-unix/X}" && break
        done
        [ -z "$DISP" ] && DISP=":0"
    fi
    if [ -z "$XAUTH" ]; then
        [ -f "${RUNTIME}/gdm/Xauthority" ] && XAUTH="${RUNTIME}/gdm/Xauthority"
        [ -z "$XAUTH" ] && XAUTH=$(ls "${RUNTIME}/.mutter-Xwaylandauth."* 2>/dev/null | head -1 || true)
        [ -z "$XAUTH" ] && XAUTH=$(ls "${RUNTIME}/.xauth"* 2>/dev/null | head -1 || true)
        [ -z "$XAUTH" ] && XAUTH="/home/${_launch_user}/.Xauthority"
    fi

    echo "AK Direct Print: launching for '${_launch_user}' (${_session_type}) display=${DISP} ..."
    su - "$_launch_user" -c \
        "DISPLAY=${DISP} XAUTHORITY=${XAUTH} DBUS_SESSION_BUS_ADDRESS=${DBUS_ADDR} \
         nohup /usr/local/bin/ak-direct-print >/tmp/ak-direct-print-launch.log 2>&1 &" \
        2>/dev/null && _launched=1 || true
fi

if [ "$_launched" = "0" ]; then
    echo "AK Direct Print: could not auto-launch (no active graphical session found)."
    echo "  → Open 'AK Direct Print' from your application launcher."
    echo "  → Or log out and back in — the agent starts automatically on login."
fi

echo "AK Direct Print: installation complete."
exit 0
POSTINST
chmod 755 "$PKG_ROOT/DEBIAN/postinst"

# ── 8. DEBIAN/prerm — runs as root before files are removed ──────────────────

cat > "$PKG_ROOT/DEBIAN/prerm" <<'PRERM'
#!/usr/bin/env bash
set -e

# Kill tray subprocess first (separate process — not a child of the agent after daemonise)
pkill -TERM -f "tray.py" 2>/dev/null || true
# Kill main agent
pkill -TERM -f "ak_direct_print_agent.py" 2>/dev/null || true
sleep 1
# Force-kill anything still alive
pkill -9 -f "tray.py" 2>/dev/null || true
pkill -9 -f "ak_direct_print_agent.py" 2>/dev/null || true

exit 0
PRERM
chmod 755 "$PKG_ROOT/DEBIAN/prerm"

# ── 9. DEBIAN/postrm — cleanup after removal ────────────────────────────────

cat > "$PKG_ROOT/DEBIAN/postrm" <<'POSTRM'
#!/usr/bin/env bash
set -e
if [ "$1" = "purge" ]; then
    rm -rf /usr/local/lib/ak-direct-print
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications 2>/dev/null || true
fi
exit 0
POSTRM
chmod 755 "$PKG_ROOT/DEBIAN/postrm"

# ── 10. Build .deb ─────────────────────────────────────────────────────────────

echo "Building .deb package..."
dpkg-deb --build --root-owner-group "$PKG_ROOT" "$DEB_FILE"

echo ""
echo "✓ Built: $DEB_FILE"
echo "  Size : $(du -sh "$DEB_FILE" | cut -f1)"
echo ""
echo "Install on Ubuntu client:"
echo "  sudo dpkg -i ak_direct_print_combined_amd64.deb"
echo "  # or double-click in Files (Ubuntu Software / GDebi)"
