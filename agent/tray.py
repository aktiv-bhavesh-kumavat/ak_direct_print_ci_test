#!/usr/bin/env python3
"""
AK Direct Print - System Tray Component (Linux / AppIndicator3)

Runs as a subprocess under the system python3 so that gi (GObject
Introspection) loads against the system libpython — avoids the
"partially initialised module 'gi'" crash inside a PyInstaller binary.

Menu structure (matches Sprint 1 artifact):
  AK Direct Print v2.0          ← disabled header
  ─────────────────────────────
  ● Station Name  [Online]       ← live status, disabled
  ─────────────────────────────
  ⚙  Configure Station…
  ↺  Sync Printers               ← sensitive only when Online
  ─────────────────────────────
  ✓  Start on Login
     View Logs
  ─────────────────────────────
  ✕  Quit

The tray polls GET /station_status on the agent Flask server (port 7654)
every 5 s to update the station label and sync-button sensitivity.
"""

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import gi
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import AppIndicator3, GdkPixbuf, GLib, Gtk

# ---------------------------------------------------------------------------
# Config / constants
# ---------------------------------------------------------------------------

APP_NAME    = "AK Direct Print"
APP_ID      = "com.aktivsoftware.ak-direct-print"
APP_VERSION = "2.0"           # display version for the header label
LOG_FILE    = os.path.expanduser("~/.ak_direct_print/agent.log")
PID_FILE    = os.path.expanduser("~/.ak_direct_print/tray.pid")

SYSTEM_AUTOSTART = "/etc/xdg/autostart/ak-direct-print.desktop"
USER_AUTOSTART_D = os.path.expanduser("~/.config/autostart")
USER_AUTOSTART   = os.path.join(USER_AUTOSTART_D, f"{APP_ID}.desktop")

# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

_parser = argparse.ArgumentParser(description="AK Direct Print system tray")
_parser.add_argument("--port",      type=int, default=7654)
_parser.add_argument("--agent-pid", type=int, default=None, dest="agent_pid")
_args = _parser.parse_args()

AGENT_URL = f"http://127.0.0.1:{_args.port}"

# ---------------------------------------------------------------------------
# PID file
# ---------------------------------------------------------------------------

os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
try:
    with open(PID_FILE, "w") as _f:
        _f.write(str(os.getpid()))
except Exception:
    pass

atexit.register(lambda: _silently(lambda: os.unlink(PID_FILE)))


def _silently(fn):
    try:
        fn()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Icons — pre-generate two separate PNGs (printer-shaped, Cairo-drawn).
# AppIndicator3 caches by file path; switching between two files forces a
# visual refresh when online ↔ offline status changes.
# ---------------------------------------------------------------------------

import math as _math

_ICON_DIR          = tempfile.mkdtemp(prefix="ak-dp-tray-")
_ICON_PATH_ONLINE  = os.path.join(_ICON_DIR, "icon_online.png")
_ICON_PATH_OFFLINE = os.path.join(_ICON_DIR, "icon_offline.png")


def _make_icon(path: str, online: bool) -> None:
    """Draw a 22×22 printer icon: brand-purple bg when online, grey when offline."""
    try:
        import cairo
        _make_icon_cairo(path, online, cairo)
    except Exception:
        # Fallback: solid coloured square via GdkPixbuf
        pb = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 22, 22)
        pb.fill(0x22c55eff if online else 0x94a3b8ff)
        pb.savev(path, "png", [], [])


def _make_icon_cairo(path: str, online: bool, cairo) -> None:
    S = 22          # canvas size
    R = 4           # corner radius

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, S, S)
    ctx = cairo.Context(surface)

    # ── Rounded-rect background ──────────────────────────────────────────
    if online:
        ctx.set_source_rgb(0.38, 0.18, 0.62)   # brand purple  #612E9E
    else:
        ctx.set_source_rgb(0.58, 0.64, 0.72)   # slate grey    #94A3B8

    ctx.arc(    R,     R, R, _math.pi,       3*_math.pi/2)
    ctx.arc(S - R,     R, R, 3*_math.pi/2,  0)
    ctx.arc(S - R, S - R, R, 0,             _math.pi/2)
    ctx.arc(    R, S - R, R, _math.pi/2,    _math.pi)
    ctx.close_path()
    ctx.fill()

    # ── Printer shape (white) ────────────────────────────────────────────
    ctx.set_source_rgb(1.0, 1.0, 1.0)

    # Paper-input tray (top tab)
    ctx.rectangle(6, 2, 10, 3)
    ctx.fill()

    # Printer body
    ctx.rectangle(3, 6, 16, 9)
    ctx.fill()

    # Paper-feed slot (punch-out in the body — matches background colour)
    if online:
        ctx.set_source_rgb(0.38, 0.18, 0.62)
    else:
        ctx.set_source_rgb(0.58, 0.64, 0.72)
    ctx.rectangle(5, 9, 12, 3)
    ctx.fill()

    # Paper output (bottom sheet)
    ctx.set_source_rgb(1.0, 1.0, 1.0)
    ctx.rectangle(6, 17, 10, 3)
    ctx.fill()

    # ── Status dot (bottom-right corner) ────────────────────────────────
    if online:
        ctx.set_source_rgb(0.13, 0.77, 0.37)   # bright green  #22C35E
        ctx.arc(S - 3.5, S - 3.5, 3, 0, 2 * _math.pi)
        ctx.fill()

    surface.write_to_png(path)


# Generate both icons once at startup
_make_icon(_ICON_PATH_ONLINE,  online=True)
_make_icon(_ICON_PATH_OFFLINE, online=False)

# ---------------------------------------------------------------------------
# AppIndicator3
# ---------------------------------------------------------------------------

indicator = AppIndicator3.Indicator.new(
    APP_ID,
    _ICON_PATH_OFFLINE,
    AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
)
indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

# ---------------------------------------------------------------------------
# Autostart helpers
# ---------------------------------------------------------------------------

def _is_autostart_enabled() -> bool:
    if os.path.exists(USER_AUTOSTART):
        try:
            with open(USER_AUTOSTART) as _f:
                return "Hidden=true" not in _f.read()
        except Exception:
            pass
    return os.path.exists(SYSTEM_AUTOSTART)


def _set_autostart(enabled: bool) -> None:
    if os.path.exists(SYSTEM_AUTOSTART):
        if not enabled:
            os.makedirs(USER_AUTOSTART_D, exist_ok=True)
            with open(USER_AUTOSTART, "w") as _f:
                _f.write("[Desktop Entry]\nHidden=true\n")
        else:
            _silently(lambda: os.unlink(USER_AUTOSTART))
    else:
        if enabled:
            os.makedirs(USER_AUTOSTART_D, exist_ok=True)
            with open(USER_AUTOSTART, "w") as _f:
                _f.write(
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    f"Name={APP_NAME}\n"
                    "Exec=/usr/local/bin/ak-direct-print\n"
                    "Hidden=false\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
        else:
            _silently(lambda: os.unlink(USER_AUTOSTART))

# ---------------------------------------------------------------------------
# Agent HTTP helpers
# ---------------------------------------------------------------------------

def _agent_get(path: str):
    """GET from the agent Flask server; returns parsed JSON or None."""
    try:
        with urllib.request.urlopen(f"{AGENT_URL}{path}", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _agent_post(path: str):
    """POST to the agent Flask server (no body); returns parsed JSON or None."""
    try:
        req = urllib.request.Request(
            f"{AGENT_URL}{path}", data=b"", method="POST"
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Menu callbacks
# ---------------------------------------------------------------------------

def _on_configure(_item: Gtk.MenuItem) -> None:
    _agent_post("/tray/configure")


def _on_sync(_item: Gtk.MenuItem) -> None:
    _agent_post("/tray/sync")


def _on_toggle_autostart(item: Gtk.CheckMenuItem) -> None:
    _set_autostart(item.get_active())


def _on_view_logs(_item: Gtk.MenuItem) -> None:
    _silently(lambda: subprocess.Popen(["xdg-open", LOG_FILE]))


def _on_quit(_item: Gtk.MenuItem) -> None:
    if _args.agent_pid:
        _silently(lambda: os.kill(_args.agent_pid, signal.SIGTERM))
    Gtk.main_quit()

# ---------------------------------------------------------------------------
# Build menu
# ---------------------------------------------------------------------------

menu = Gtk.Menu()

# ── Header ──────────────────────────────────────────────────────────────────
_header_item = Gtk.MenuItem(label=f"{APP_NAME} v{APP_VERSION}")
_header_item.set_sensitive(False)
menu.append(_header_item)

menu.append(Gtk.SeparatorMenuItem())

# ── Station status label ─────────────────────────────────────────────────────
_status_item = Gtk.MenuItem(label="○  Connecting…")
_status_item.set_sensitive(False)
menu.append(_status_item)

menu.append(Gtk.SeparatorMenuItem())

# ── Configure Station ────────────────────────────────────────────────────────
_configure_item = Gtk.MenuItem(label="Configure Station…")
_configure_item.connect("activate", _on_configure)
menu.append(_configure_item)

# ── Sync Printers (disabled until online) ───────────────────────────────────
_sync_item = Gtk.MenuItem(label="Sync Printers")
_sync_item.connect("activate", _on_sync)
_sync_item.set_sensitive(False)
menu.append(_sync_item)

menu.append(Gtk.SeparatorMenuItem())

# ── Start on Login ───────────────────────────────────────────────────────────
_autostart_item = Gtk.CheckMenuItem(label="Start on Login")
_autostart_item.set_active(_is_autostart_enabled())
_autostart_item.connect("toggled", _on_toggle_autostart)
menu.append(_autostart_item)

# ── View Logs ────────────────────────────────────────────────────────────────
_logs_item = Gtk.MenuItem(label="View Logs")
_logs_item.connect("activate", _on_view_logs)
menu.append(_logs_item)

menu.append(Gtk.SeparatorMenuItem())

# ── Quit ─────────────────────────────────────────────────────────────────────
_quit_item = Gtk.MenuItem(label="Quit")
_quit_item.connect("activate", _on_quit)
menu.append(_quit_item)

menu.show_all()
indicator.set_menu(menu)

# ---------------------------------------------------------------------------
# Status monitor — polls /station_status every 5 s
# ---------------------------------------------------------------------------

def _apply_status(data):
    """GTK-thread callback: update the station label and icon."""
    if data and data.get("online"):
        name      = data.get("station_name", APP_NAME)
        label     = f"●  {name}  [Online]"
        sensitive = True
        icon_path = _ICON_PATH_ONLINE
    elif data and data.get("station_name"):
        name      = data["station_name"]
        label     = f"○  {name}  [Offline]"
        sensitive = False
        icon_path = _ICON_PATH_OFFLINE
    else:
        label     = "○  Not configured  [Offline]"
        sensitive = False
        icon_path = _ICON_PATH_OFFLINE

    _status_item.set_label(label)
    _sync_item.set_sensitive(sensitive)
    # Switch to the pre-generated file with the correct colour.
    # Using a different path forces AppIndicator3 to reload the image.
    indicator.set_icon_full(icon_path, label)
    return False   # GLib.idle_add: run once


def _status_monitor() -> None:
    time.sleep(3)   # let the agent come up
    while True:
        data = _agent_get("/station_status")
        GLib.idle_add(_apply_status, data)
        time.sleep(5)


threading.Thread(target=_status_monitor, daemon=True).start()


# ---------------------------------------------------------------------------
# Agent PID watcher — exit tray if the agent process dies
# (handles uninstall, crash, manual kill — without relying solely on prerm)
# ---------------------------------------------------------------------------

def _watch_agent_pid() -> None:
    pid = _args.agent_pid
    if not pid:
        return
    while True:
        time.sleep(5)
        if not os.path.exists("/proc/%d" % pid):
            GLib.idle_add(Gtk.main_quit)
            return


if _args.agent_pid:
    threading.Thread(target=_watch_agent_pid, daemon=True).start()

# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(Gtk.main_quit))
signal.signal(signal.SIGINT,  lambda *_: GLib.idle_add(Gtk.main_quit))

# ---------------------------------------------------------------------------
# Run GTK loop
# ---------------------------------------------------------------------------

Gtk.main()
