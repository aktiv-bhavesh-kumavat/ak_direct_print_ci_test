#!/usr/bin/env python3
"""
ak_direct_print — AK Direct Print Station Agent
=================================================
Connects outbound to your Odoo instance (HTTPS) and polls for print jobs.

Station mode (config file present):
  - Authenticates with Odoo → receives an auth token
  - Heartbeat thread keeps the station marked Online every 30 s
  - Job poll thread fetches pending jobs every 5 s and prints them
  - Printer sync runs on startup so Odoo shows current OS printers

Legacy localhost mode (no config file):
  - Old Flask localhost server on port 7654
  - Browser JS posts jobs directly to http://127.0.0.1:7654/print
  - Fully backward-compatible; works without any configuration

Usage (script):
    pip install -r requirements.txt
    python ak_direct_print_agent.py [--port 7654] [--headless] [--setup]

Usage (packaged .exe / .deb):
    Double-click the installer or run the binary.
"""

import argparse
import base64
import ctypes
import http.client
import json
import logging
import logging.handlers
import os
import platform
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid as _uuid_mod

# ── Linux: inject system Python packages so gi loads from the system ──────────
if sys.platform.startswith("linux"):
    _sys_pkg_dirs = [
        "/usr/lib/python3/dist-packages",
        "/usr/lib/python3.12/dist-packages",
        "/usr/lib/python3.11/dist-packages",
        "/usr/lib/python3.10/dist-packages",
        "/usr/local/lib/python3/dist-packages",
    ]
    for _d in _sys_pkg_dirs:
        if os.path.isdir(_d) and _d not in sys.path:
            sys.path.insert(0, _d)

    _typelib_dirs = [
        "/usr/lib/girepository-1.0",
        "/usr/lib/x86_64-linux-gnu/girepository-1.0",
        "/usr/lib/aarch64-linux-gnu/girepository-1.0",
    ]
    _existing = [d for d in _typelib_dirs if os.path.isdir(d)]
    if _existing:
        _current = os.environ.get("GI_TYPELIB_PATH", "")
        _all = _existing + ([_current] if _current else [])
        os.environ["GI_TYPELIB_PATH"] = ":".join(_all)
    os.environ.setdefault("PYSTRAY_BACKEND", "appindicator")

from flask import Flask, jsonify, request as flask_request  # noqa: E402

try:
    import requests as _requests_lib
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ---------------------------------------------------------------------------
# App identity
# ---------------------------------------------------------------------------

APP_NAME    = "AK Direct Print"
APP_ID      = "com.aktivsoftware.ak-direct-print"
APP_VERSION = "2.0.5"
LOG_DIR     = os.path.join(os.path.expanduser("~"), ".ak_direct_print")
LOG_FILE    = os.path.join(LOG_DIR, "agent.log")

# Captured once at process start. Used by SetupDialog, when running as the
# --_setup_subprocess child, to detect its launching parent dying (crash,
# force-quit) and self-close rather than linger as an orphaned window.
_LAUNCH_PARENT_PID = os.getppid()


# ---------------------------------------------------------------------------
# Logging — console + rotating file (2 MB × 3 backups)
# ---------------------------------------------------------------------------

def _setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    handlers = []
    try:
        has_console = sys.stdout is not None and sys.stdout.fileno() >= 0
    except Exception:
        has_console = False
    if has_console:
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(errors="backslashreplace")
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(fmt)
            handlers.append(console)
        except Exception:
            pass

    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        handlers.append(fh)
    except Exception:
        pass

    logging.basicConfig(level=logging.INFO, handlers=handlers)


_setup_logging()
_logger = logging.getLogger("ak_direct_print_agent")


# ---------------------------------------------------------------------------
# Config file management
# ---------------------------------------------------------------------------

def _get_config_path():
    """Return the path to the agent config file for the current OS."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "AKDirectPrint", "config.json")
    return os.path.join(os.path.expanduser("~"), ".config", "ak_direct_print", "config.json")


def load_config():
    """Load config from disk; return empty dict if not found or malformed."""
    path = _get_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            _logger.warning("Could not load config from %s: %s", path, exc)
    return {}


def save_config(config):
    """Persist config dict to disk (creates directories as needed)."""
    path = _get_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as exc:
        _logger.warning("Could not save config to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Flask app (always running — /health for tray compat, legacy endpoints)
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Reference to the active StationClient — set by main() before Flask starts.
_station_client_ref = [None]


@app.after_request
def _cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    return jsonify({"status": "ok", "platform": platform.system(), "version": APP_VERSION})


@app.route("/station_status", methods=["GET", "OPTIONS"])
def station_status():
    """Used by the Linux tray subprocess to display station name and online state."""
    sc = _station_client_ref[0]
    cfg = load_config()
    name = cfg.get("station_name", APP_NAME)
    online = bool(sc and sc.connected)
    return jsonify({
        "station_name": name,
        "online": online,
        "version": APP_VERSION,
    })


@app.route("/tray/sync", methods=["POST", "OPTIONS"])
def tray_sync():
    """Tray subprocess requests a printer re-sync."""
    sc = _station_client_ref[0]
    if sc and sc.token:
        threading.Thread(target=sc.sync_printers, daemon=True, name="tray_sync").start()
        return jsonify({"status": "ok"})
    return jsonify({"status": "offline"}), 503


@app.route("/tray/configure", methods=["POST", "OPTIONS"])
def tray_configure():
    """Tray subprocess requests the Setup dialog (only works when display is available)."""
    def _open():
        cfg = load_config()
        dlg = SetupDialog(cfg)
        dlg.run()
    threading.Thread(target=_open, daemon=True, name="tray_configure").start()
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Local printer enumeration
# ---------------------------------------------------------------------------

_WIN32_VIRTUAL_DRIVER_MARKERS = (
    "xps document writer",
    "print to pdf",
    "shared fax driver",
    "software printer driver",
)


def _is_virtual_win32_printer(printer_name):
    try:
        import win32print
        handle = win32print.OpenPrinter(printer_name)
        try:
            info = win32print.GetPrinter(handle, 2)
        finally:
            win32print.ClosePrinter(handle)
        driver = (info.get("pDriverName") or "").lower()
        return any(marker in driver for marker in _WIN32_VIRTUAL_DRIVER_MARKERS)
    except Exception:
        return False


def _get_local_printers():
    """Return list of {name, raw_name, is_default} for OS printers."""
    printers = []
    system = platform.system()

    if system == "Windows":
        try:
            import win32print
            default_name = ""
            try:
                default_name = win32print.GetDefaultPrinter()
            except Exception:
                pass
            flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            for p in win32print.EnumPrinters(flags, None, 4):
                name = p["pPrinterName"]
                if _is_virtual_win32_printer(name):
                    continue
                printers.append({
                    "name": name.replace("_", " "),
                    "raw_name": name,
                    "is_default": name == default_name,
                })
        except ImportError:
            _logger.warning("win32print not available")
        except Exception as exc:
            _logger.warning("_get_local_printers (Windows): %s", exc)
    else:
        default_name = ""
        try:
            dp = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=5)
            # "system default destination: HP_LaserJet_Pro"
            parts = dp.stdout.strip().split(":")
            if len(parts) >= 2:
                default_name = parts[-1].strip()
        except Exception:
            pass
        try:
            proc = subprocess.run(["lpstat", "-a"], capture_output=True, text=True, timeout=10)
            for line in proc.stdout.splitlines():
                parts = line.split()
                if parts:
                    raw_name = parts[0]
                    printers.append({
                        "name": raw_name.replace("_", " "),
                        "raw_name": raw_name,
                        "is_default": raw_name == default_name,
                    })
        except FileNotFoundError:
            _logger.warning("lpstat not found — no CUPS printers discovered")
        except Exception as exc:
            _logger.warning("_get_local_printers (Linux/macOS): %s", exc)

    return printers


@app.route("/printers", methods=["GET", "OPTIONS"])
def list_printers():
    """Legacy endpoint: return local printers for the old agent discovery flow."""
    cups_printers = [p["raw_name"] for p in _get_local_printers()]
    return jsonify({"printers": cups_printers, "network": []})


# ---------------------------------------------------------------------------
# IPP protocol constants & helpers  (RFC 8011)
# ---------------------------------------------------------------------------

_IPP_PRINT_JOB            = 0x0002
_IPP_GET_PRINTER_ATTRS    = 0x000B
_IPP_TAG_OPERATION        = 0x01
_IPP_TAG_JOB              = 0x02
_IPP_TAG_END              = 0x03
_IPP_TAG_INTEGER          = 0x21
_IPP_TAG_KEYWORD          = 0x44
_IPP_TAG_URI              = 0x45
_IPP_TAG_CHARSET          = 0x47
_IPP_TAG_NATURAL_LANGUAGE = 0x48
_IPP_TAG_NAME             = 0x42
_IPP_TAG_MIMETYPE         = 0x49
_IPP_STATUS_CLIENT_ERR    = 0x0400
_IPP_RETRYABLE            = frozenset({0x0400, 0x040A, 0x040B, 0x040C})
_IPP_RESOURCE_PROBES      = ["/ipp/print", "/ipp/port1", "/ipp", "/print", "/"]


def _ipp_encode_attr(tag, name, value):
    name_b = name.encode("utf-8") if name else b""
    value_b = struct.pack(">I", int(value)) if tag == _IPP_TAG_INTEGER else (
        value if isinstance(value, bytes) else str(value).encode("utf-8")
    )
    return (struct.pack(">B", tag)
            + struct.pack(">H", len(name_b)) + name_b
            + struct.pack(">H", len(value_b)) + value_b)


def _ipp_build_request(op_id, req_id, op_attrs, data=b""):
    buf = struct.pack(">BBH", 1, 1, op_id) + struct.pack(">I", req_id)
    buf += bytes([_IPP_TAG_OPERATION])
    for attr in op_attrs:
        buf += _ipp_encode_attr(*attr)
    buf += bytes([_IPP_TAG_END])
    return buf + data


def _ipp_parse_status(response_data):
    return struct.unpack(">H", response_data[2:4])[0] if len(response_data) >= 4 else None


def _ipp_http_post(host, port, resource, packet):
    sock = socket.create_connection((host, port), timeout=5)
    conn = http.client.HTTPConnection(host, port)
    conn.sock = sock
    sock.settimeout(30)
    conn.request("POST", resource, body=packet, headers={
        "Content-Type": "application/ipp",
        "Content-Length": str(len(packet)),
        "Connection": "close",
    })
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def _ipp_resolve_resource(host, port, resource):
    def _probe(res):
        uri = "ipp://%s:%d%s" % (host, port, res)
        attrs = [
            (_IPP_TAG_CHARSET,          "attributes-charset",          "utf-8"),
            (_IPP_TAG_NATURAL_LANGUAGE, "attributes-natural-language", "en"),
            (_IPP_TAG_URI,              "printer-uri",                 uri),
            (_IPP_TAG_KEYWORD,          "requested-attributes",        "printer-state"),
        ]
        try:
            http_status, _ = _ipp_http_post(host, port, res, _ipp_build_request(_IPP_GET_PRINTER_ATTRS, 1, attrs))
            return http_status
        except OSError:
            return None

    status = _probe(resource)
    if status is None or status != 404:
        return resource

    _logger.warning("IPP 404 on '%s' — probing standard paths for %s:%d", resource, host, port)
    for candidate in _IPP_RESOURCE_PROBES:
        if candidate == resource:
            continue
        status = _probe(candidate)
        if status is None:
            break
        if status != 404:
            _logger.info("IPP: found working resource '%s' for %s:%d", candidate, host, port)
            return candidate
    return resource


# ---------------------------------------------------------------------------
# Windows virtual printer detection
# ---------------------------------------------------------------------------

_WIN32_BLOCKING_STATUS = {
    0x00000001: "Paused",
    0x00000002: "Error",
    0x00000004: "Pending deletion",
    0x00000008: "Paper jam",
    0x00000010: "Out of paper",
    0x00000040: "Paper problem",
    0x00000080: "Offline",
    0x00000800: "Output bin full",
    0x00001000: "Not available",
    0x00040000: "Out of toner",
    0x00100000: "Needs user intervention",
    0x00200000: "Out of memory",
    0x00400000: "Door open",
}
_WIN32_BLOCKING_STATUS_MASK = 0
for _bit in _WIN32_BLOCKING_STATUS:
    _WIN32_BLOCKING_STATUS_MASK |= _bit


# ---------------------------------------------------------------------------
# Printer connectivity check (legacy endpoint)
# ---------------------------------------------------------------------------

@app.route("/check_printer", methods=["POST", "OPTIONS"])
def check_printer():
    if flask_request.method == "OPTIONS":
        return jsonify({}), 200

    data = flask_request.get_json(silent=True) or {}
    connection_type = data.get("connection_type", "ipp")

    if connection_type == "ipp":
        host = (data.get("printer_host") or "").strip()
        port = int(data.get("printer_port") or 631)
        resource = (data.get("ipp_resource") or "/ipp/print").strip()
        if not resource.startswith("/"):
            resource = "/" + resource
        if not host:
            return jsonify({"success": False, "state": "error",
                            "message": "No Printer Host / IP configured."})
        printer_uri = "ipp://%s:%d%s" % (host, port, resource)
        op_attrs = [
            (_IPP_TAG_CHARSET,          "attributes-charset",          "utf-8"),
            (_IPP_TAG_NATURAL_LANGUAGE, "attributes-natural-language", "en"),
            (_IPP_TAG_URI,              "printer-uri",                 printer_uri),
            (_IPP_TAG_KEYWORD,          "requested-attributes",        "printer-state"),
        ]
        packet = _ipp_build_request(_IPP_GET_PRINTER_ATTRS, 1, op_attrs)
        try:
            http_status, _ = _ipp_http_post(host, port, resource, packet)
            if http_status == 200:
                return jsonify({"success": True, "state": "online",
                                "message": "Printer is online at %s." % printer_uri})
            return jsonify({"success": False, "state": "error",
                            "message": "Printer returned HTTP %d." % http_status})
        except OSError as exc:
            return jsonify({"success": False, "state": "offline",
                            "message": "Cannot reach %s:%d — %s." % (host, port, exc)})
        except Exception as exc:
            return jsonify({"success": False, "state": "error", "message": str(exc)})

    elif connection_type == "cups":
        cups_name = (data.get("cups_name") or "").strip()
        if not cups_name:
            return jsonify({"success": False, "state": "error",
                            "message": "No CUPS Printer Name configured."})
        try:
            result = subprocess.run(["lpstat", "-p", cups_name],
                                    capture_output=True, text=True, timeout=10)
            if result.returncode != 0:
                return jsonify({"success": False, "state": "offline",
                                "message": result.stderr.strip() or "CUPS printer not found."})
            output = result.stdout.lower()
            if "disabled" in output or "not available" in output:
                return jsonify({"success": False, "state": "offline",
                                "message": "CUPS printer '%s' is disabled." % cups_name})
            return jsonify({"success": True, "state": "online",
                            "message": "CUPS printer '%s' is available." % cups_name})
        except FileNotFoundError:
            return jsonify({"success": False, "state": "error",
                            "message": "CUPS not installed (lpstat not found)."})
        except Exception as exc:
            return jsonify({"success": False, "state": "error", "message": str(exc)})

    elif connection_type == "win32":
        printer_name = (data.get("win32_printer_name") or "").strip()
        try:
            import win32print
            if not printer_name:
                printer_name = win32print.GetDefaultPrinter()
            handle = win32print.OpenPrinter(printer_name)
            try:
                info = win32print.GetPrinter(handle, 2)
            finally:
                win32print.ClosePrinter(handle)
            status = info.get("Status", 0)
            blocking = status & _WIN32_BLOCKING_STATUS_MASK
            if not blocking:
                return jsonify({"success": True, "state": "online",
                                "message": "Windows printer '%s' is ready." % printer_name})
            reasons = [label for bit, label in _WIN32_BLOCKING_STATUS.items() if status & bit]
            return jsonify({"success": False, "state": "offline",
                            "message": "%s: %s." % (printer_name, ", ".join(reasons))})
        except ImportError:
            return jsonify({"success": False, "state": "error",
                            "message": "pywin32 not installed."})
        except Exception as exc:
            return jsonify({"success": False, "state": "error", "message": str(exc)})

    return jsonify({"success": False, "state": "error",
                    "message": "Unknown connection type: %s" % connection_type})


# ---------------------------------------------------------------------------
# Print backends
# ---------------------------------------------------------------------------

def _find_cups_queue_for_host(host):
    import re
    from urllib.parse import unquote
    try:
        lp = subprocess.run(["lpstat", "-v"], capture_output=True, text=True, timeout=10)
        lines = lp.stdout.splitlines()
        for line in lines:
            if host in line:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == "device" and parts[1] == "for":
                    return parts[2].rstrip(":")
        try:
            r = subprocess.run(["avahi-resolve", "--address", host],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                local_host = r.stdout.strip().split()[-1].rstrip(".")
                short_host = local_host.split(".")[0]
                for line in lines:
                    if local_host in line or short_host in line:
                        parts = line.split()
                        if len(parts) >= 4 and parts[0] == "device" and parts[1] == "for":
                            return parts[2].rstrip(":")
        except (FileNotFoundError, Exception):
            pass
    except Exception as exc:
        _logger.debug("_find_cups_queue_for_host(%s): %s", host, exc)
    return None


# IPP job-state values (RFC 8011 §5.3.7)
_IPP_JOB_PENDING    = 3
_IPP_JOB_PROCESSING = 4
_IPP_JOB_STOPPED    = 5
_IPP_JOB_CANCELLED  = 7
_IPP_JOB_ABORTED    = 8
_IPP_JOB_COMPLETED  = 9

_CUPS_UNREACHABLE_MSG = (
    "Cannot reach printer '%s' (CUPS: Connecting To Device). "
    "Check the printer is powered on and network-connected. "
    "If duplicate queues exist (e.g. DCP-7065DN / DCP-7065DN-2), "
    "use the one showing 'Ready' in System Settings → Printers."
)


def _send_cups(pdf_bytes, cups_name, job_name, copies):
    """Print via CUPS.  Tries IPP (python-cups) first for reliable job-state
    detection; falls back to the lp command if python-cups is unavailable.

    The key problem with the lp-only approach: when a CUPS job is aborted
    (e.g. 'Connecting To Device') it disappears from lpstat — identical to a
    successful completion.  python-cups gives us the exact IPP job-state
    (completed=9, aborted=8, cancelled=7) so we never report false success.
    """
    if platform.system() == "Windows":
        return {"success": False, "message": "CUPS not available on Windows."}
    if not cups_name:
        return {"success": False, "message": "CUPS printer name is required."}

    try:
        import cups as _cups_mod
        return _cups_ipp_print(_cups_mod, pdf_bytes, cups_name, job_name, copies)
    except ImportError:
        pass

    return _cups_lp_print(pdf_bytes, cups_name, job_name, copies)


def _cups_ipp_print(cups_mod, pdf_bytes, cups_name, job_name, copies):
    """Submit job via python-cups and poll getJobAttributes for exact IPP state."""
    tmp_path = None
    conn = None
    job_id = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        conn = cups_mod.Connection()
        options = {}
        if copies > 1:
            options["copies"] = str(copies)
        job_id = conn.printFile(cups_name, tmp_path, job_name[:255], options)
        if not job_id:
            return {"success": False,
                    "message": "CUPS rejected the job (no job ID returned)."}

        _logger.info("CUPS job %d submitted to '%s' — polling IPP state", job_id, cups_name)

        deadline = time.time() + 90
        connecting_since = None

        while time.time() < deadline:
            time.sleep(3)
            try:
                attrs = conn.getJobAttributes(
                    job_id,
                    requested_attributes=["job-state", "job-state-reasons"],
                )
            except Exception:
                # Job removed from history — completed successfully
                return {"success": True,
                        "message": "Printed via CUPS (%s)." % cups_name}

            state = attrs.get("job-state", 0)
            reasons = attrs.get("job-state-reasons", "")
            if isinstance(reasons, list):
                reasons = " ".join(str(r) for r in reasons)
            reasons_lower = reasons.lower()

            if state == _IPP_JOB_COMPLETED:
                return {"success": True,
                        "message": "Printed via CUPS (%s)." % cups_name}

            if state == _IPP_JOB_ABORTED:
                return {"success": False,
                        "message": "CUPS job aborted: %s" % (reasons or "(no reason)")}

            if state == _IPP_JOB_CANCELLED:
                return {"success": False,
                        "message": "CUPS job cancelled: %s" % (reasons or "(no reason)")}

            if "connecting-to-device" in reasons_lower:
                if connecting_since is None:
                    connecting_since = time.time()
                elif time.time() - connecting_since > 20:
                    try:
                        conn.cancelJob(job_id)
                    except Exception:
                        pass
                    _logger.warning("CUPS: '%s' unreachable (Connecting To Device)", cups_name)
                    return {"success": False,
                            "message": _CUPS_UNREACHABLE_MSG % cups_name}
            else:
                connecting_since = None

        try:
            conn.cancelJob(job_id)
        except Exception:
            pass
        return {"success": False,
                "message": "CUPS job timed out after 90 s (cancelled). Job ID: %d" % job_id}

    except Exception as exc:
        return {"success": False, "message": "CUPS (IPP) error: %s" % exc}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _cups_lp_print(pdf_bytes, cups_name, job_name, copies):
    """Fallback: submit via lp command when python-cups is unavailable.

    Limitation: lpstat shows an empty result for both completed AND aborted
    jobs, so we cannot reliably distinguish success from silent failure.
    We rely on the 90 s timeout and keyword scanning as best-effort.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        cmd = ["lp", "-d", cups_name, "-t", job_name[:255], "-n", str(copies), tmp_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {"success": False,
                    "message": result.stderr.strip() or "CUPS lp command failed."}

        import re as _re
        m = _re.search(r"request id is (\S+)", result.stdout)
        if not m:
            return {"success": True,
                    "message": result.stdout.strip() or "Job submitted to CUPS."}

        cups_job_id = m.group(1)
        _logger.info("CUPS job submitted via lp: %s — polling lpstat", cups_job_id)

        # Poll with `lpstat -l -o <job_id>`.  Note: -j is NOT a valid lpstat flag
        # on CUPS 2.x — it prints usage text containing "held" which would falsely
        # trigger our error keywords.  -o accepts both queue names and job IDs.
        deadline = time.time() + 90
        connecting_since = None
        while time.time() < deadline:
            time.sleep(3)
            st = subprocess.run(
                ["lpstat", "-l", "-o", cups_job_id],
                capture_output=True, text=True, timeout=10,
            )
            output = (st.stdout + st.stderr).lower()

            # Guard: if lpstat printed usage text, the job_id syntax is still not
            # accepted — break and report timeout rather than looping on help text.
            if "usage: lpstat" in output:
                _logger.warning("lpstat returned usage text for job '%s'", cups_job_id)
                break

            if not output.strip():
                # Job left the active queue.  Distinguish success from abort:
                # completed jobs appear in `lpstat -W completed`; aborted ones don't.
                chk = subprocess.run(
                    ["lpstat", "-W", "completed", "-o", cups_job_id],
                    capture_output=True, text=True, timeout=10,
                )
                if chk.stdout.strip():
                    return {"success": True,
                            "message": "Printed via CUPS (%s)." % cups_name}
                return {"success": False,
                        "message": "CUPS job was aborted without printing on '%s'." % cups_name}

            if any(w in output for w in ("error", "aborted", "stopped", "held")):
                detail = (st.stdout + st.stderr).strip()[:300]
                return {"success": False, "message": "CUPS job failed: %s" % detail}
            if "connecting-to-device" in output or "connecting to device" in output:
                if connecting_since is None:
                    connecting_since = time.time()
                elif time.time() - connecting_since > 20:
                    return {"success": False,
                            "message": _CUPS_UNREACHABLE_MSG % cups_name}
            else:
                connecting_since = None

        return {"success": False,
                "message": "CUPS job still processing after 90 s. Job ID: %s" % cups_job_id}

    except FileNotFoundError:
        return {"success": False, "message": "CUPS not installed (lp not found)."}
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "CUPS command timed out."}
    except Exception as exc:
        return {"success": False, "message": "CUPS error: %s" % exc}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _send_ipp(pdf_bytes, host, port, resource, job_name, copies):
    if not host:
        return {"success": False, "message": "Printer host/IP is required for IPP mode."}
    if not resource.startswith("/"):
        resource = "/" + resource

    resource = _ipp_resolve_resource(host, port, resource)
    printer_uri = "ipp://%s:%d%s" % (host, port, resource)

    def _build_op_attrs(doc_format, include_user=True, include_job_name=True):
        attrs = [
            (_IPP_TAG_CHARSET,          "attributes-charset",          "utf-8"),
            (_IPP_TAG_NATURAL_LANGUAGE, "attributes-natural-language", "en"),
            (_IPP_TAG_URI,              "printer-uri",                 printer_uri),
        ]
        if include_user:
            attrs.append((_IPP_TAG_NAME, "requesting-user-name", "odoo-agent"))
        if include_job_name:
            attrs.append((_IPP_TAG_NAME, "job-name", job_name[:255]))
        attrs.append((_IPP_TAG_MIMETYPE, "document-format", doc_format))
        return attrs

    probes = [
        ("application/pdf",          True,  True),
        ("application/pdf",          True,  False),
        ("application/pdf",          False, False),
        ("application/octet-stream", True,  True),
        ("application/octet-stream", True,  False),
        ("application/octet-stream", False, False),
    ]

    working = None
    last_error = None

    for doc_format, inc_user, inc_job in probes:
        label = "%s+%s+%s" % (doc_format.split("/")[1], "user" if inc_user else "", "job" if inc_job else "")
        try:
            packet = _ipp_build_request(_IPP_PRINT_JOB, 1, _build_op_attrs(doc_format, inc_user, inc_job), data=pdf_bytes)
            http_status, resp_data = _ipp_http_post(host, port, resource, packet)
            if http_status != 200:
                last_error = "HTTP %d from printer." % http_status
                break
            status_code = _ipp_parse_status(resp_data)
            if status_code is None:
                last_error = "Invalid IPP response."
                break
            if status_code in _IPP_RETRYABLE:
                last_error = "IPP 0x%04X (probe: %s)." % (status_code, label)
                continue
            if status_code >= _IPP_STATUS_CLIENT_ERR:
                last_error = "IPP rejected (0x%04X, probe: %s)." % (status_code, label)
                break
            working = (doc_format, inc_user, inc_job)
            break
        except OSError as exc:
            return {"success": False, "message": "Cannot connect to %s:%d — %s" % (host, port, exc)}
        except Exception as exc:
            return {"success": False, "message": "IPP error: %s" % exc}

    if not working:
        if platform.system() != "Windows" and shutil.which("lp"):
            cups_name = _find_cups_queue_for_host(host)
            if cups_name:
                return _send_cups(pdf_bytes, cups_name, job_name, copies)
        return {"success": False,
                "message": "Printer at %s does not accept PDF via IPP." % host}

    if copies > 1:
        doc_format, inc_user, inc_job = working
        for copy_num in range(2, copies + 1):
            cname = "%s (%d/%d)" % (job_name[:240], copy_num, copies) if inc_job else job_name
            try:
                pkt = _ipp_build_request(_IPP_PRINT_JOB, copy_num, _build_op_attrs(doc_format, inc_user, inc_job), data=pdf_bytes)
                http_status, resp_data = _ipp_http_post(host, port, resource, pkt)
                sc = _ipp_parse_status(resp_data)
                if http_status != 200 or sc is None or sc >= _IPP_STATUS_CLIENT_ERR:
                    return {"success": False, "message": "IPP copy %d/%d failed." % (copy_num, copies)}
            except Exception as exc:
                return {"success": False, "message": "IPP copy %d/%d error: %s" % (copy_num, copies, exc)}

    return {"success": True, "message": "IPP: %d %s sent to %s" % (copies, "copy" if copies == 1 else "copies", printer_uri)}


_WIN32_RENDER_DPI = 300


class _DOCINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize",       ctypes.c_int),
        ("lpszDocName",  ctypes.c_wchar_p),
        ("lpszOutput",   ctypes.c_wchar_p),
        ("lpszDatatype", ctypes.c_wchar_p),
        ("fwType",       ctypes.c_ulong),
    ]


_GDI32 = None


def _gdi32():
    global _GDI32
    if _GDI32 is not None:
        return _GDI32
    dll = ctypes.WinDLL("gdi32", use_last_error=True)
    dll.StartDocW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DOCINFOW)]
    dll.StartDocW.restype  = ctypes.c_int
    dll.StartPage.argtypes = [ctypes.c_void_p]
    dll.StartPage.restype  = ctypes.c_int
    dll.EndPage.argtypes   = [ctypes.c_void_p]
    dll.EndPage.restype    = ctypes.c_int
    dll.EndDoc.argtypes    = [ctypes.c_void_p]
    dll.EndDoc.restype     = ctypes.c_int
    dll.GetDeviceCaps.argtypes = [ctypes.c_void_p, ctypes.c_int]
    dll.GetDeviceCaps.restype  = ctypes.c_int
    _GDI32 = dll
    return dll


_GDI_HORZRES = 8
_GDI_VERTRES = 10


def _send_win32(pdf_bytes, printer_name, job_name, copies=1):
    try:
        import win32print
        import win32gui
        import fitz
        from PIL import Image, ImageWin
    except ImportError:
        return {"success": False, "message": "Windows print dependencies not installed (pywin32, pymupdf, pillow)."}

    if not printer_name:
        printer_name = win32print.GetDefaultPrinter()
    if not printer_name:
        return {"success": False, "message": "No Windows printer name specified and no default found."}

    copies = max(1, int(copies))
    gdi32 = _gdi32()

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        return {"success": False, "message": "Could not read PDF: %s" % exc}

    if doc.page_count == 0:
        doc.close()
        return {"success": False, "message": "PDF has no pages."}

    try:
        for copy_num in range(1, copies + 1):
            copy_job_name = job_name if copies == 1 else "%s (%d/%d)" % (job_name, copy_num, copies)
            hdc = win32gui.CreateDC("WINSPOOL", printer_name, None)
            hres = gdi32.GetDeviceCaps(hdc, _GDI_HORZRES)
            vres = gdi32.GetDeviceCaps(hdc, _GDI_VERTRES)
            docinfo = _DOCINFOW(ctypes.sizeof(_DOCINFOW), copy_job_name, None, None, 0)
            doc_started = False
            try:
                if gdi32.StartDocW(hdc, ctypes.byref(docinfo)) <= 0:
                    raise OSError("StartDoc failed (error 0x%x)" % ctypes.get_last_error())
                doc_started = True
                matrix = fitz.Matrix(_WIN32_RENDER_DPI / 72.0, _WIN32_RENDER_DPI / 72.0)
                for page in doc:
                    pix = page.get_pixmap(matrix=matrix, alpha=False)
                    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                    scale = min(hres / pix.width, vres / pix.height)
                    draw_w = int(pix.width * scale)
                    draw_h = int(pix.height * scale)
                    off_x = (hres - draw_w) // 2
                    off_y = (vres - draw_h) // 2
                    gdi32.StartPage(hdc)
                    try:
                        ImageWin.Dib(img).draw(hdc, (off_x, off_y, off_x + draw_w, off_y + draw_h))
                    finally:
                        gdi32.EndPage(hdc)
            finally:
                if doc_started:
                    gdi32.EndDoc(hdc)
                win32gui.DeleteDC(hdc)
        return {"success": True,
                "message": "Sent %d %s to '%s'" % (copies, "copy" if copies == 1 else "copies", printer_name)}
    except Exception as exc:
        _logger.exception("/print: Windows GDI print error")
        return {"success": False, "message": "Windows print error: %s" % exc}
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Legacy /print endpoint (old agent mode — browser posts PDF to localhost)
# ---------------------------------------------------------------------------

@app.route("/print", methods=["POST", "OPTIONS"])
def print_job():
    if flask_request.method == "OPTIONS":
        return jsonify({}), 200

    data = flask_request.get_json(silent=True) or {}
    pdf_b64 = data.get("pdf_b64", "")
    if not pdf_b64:
        return jsonify({"success": False, "message": "No PDF data received."}), 400
    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except Exception as exc:
        return jsonify({"success": False, "message": "Invalid base64 PDF: %s" % exc}), 400

    connection_type = data.get("connection_type", "ipp")
    job_name = data.get("job_name", "Odoo Print Job")
    copies = max(1, int(data.get("copies", 1)))

    if connection_type == "cups":
        result = _send_cups(pdf_bytes, data.get("cups_name", ""), job_name, copies)
    elif connection_type == "ipp":
        result = _send_ipp(
            pdf_bytes,
            data.get("printer_host", ""),
            int(data.get("printer_port") or 631),
            data.get("ipp_resource") or "/ipp/print",
            job_name, copies,
        )
    elif connection_type == "win32":
        result = _send_win32(pdf_bytes, data.get("win32_printer_name", ""), job_name, copies)
    else:
        result = {"success": False, "message": "Unknown connection type: %s" % connection_type}

    return jsonify(result)


# ---------------------------------------------------------------------------
# Station client — outbound HTTPS polling mode
# ---------------------------------------------------------------------------

class StationClient:
    """Polls Odoo for pending print jobs and executes them locally."""

    def __init__(self, config):
        self.config = config
        self.odoo_url = config.get("odoo_url", "").rstrip("/")
        self.token = config.get("auth_token", "")
        self.station_id = config.get("station_id")
        self.connected = False   # True only when heartbeats are succeeding
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active_jobs = set()      # job IDs currently being processed
        self._active_jobs_lock = threading.Lock()

        if not HAS_REQUESTS:
            raise RuntimeError(
                "The 'requests' package is required for station mode. "
                "Run: pip install requests"
            )

        self._session = _requests_lib.Session()
        self._session.verify = config.get("verify_ssl", True)
        if self.token:
            self._session.headers["X-AK-Token"] = self.token

    def _url(self, path):
        return self.odoo_url + path

    def authenticate(self):
        """POST /ak_print/auth and persist the new token."""
        station_uuid = self.config.get("station_uuid") or _uuid_mod.uuid4().hex
        self.config["station_uuid"] = station_uuid

        resp = self._session.post(
            self._url("/ak_print/auth"),
            json={
                "email": self.config.get("email", ""),
                "password": self.config.get("password", ""),
                "station_uuid": station_uuid,
                "station_name": self.config.get("station_name", platform.node()),
                "os": platform.system(),
                "app_version": APP_VERSION,
            },
            timeout=30,
        )
        if resp.status_code == 401:
            raise PermissionError("Invalid Odoo credentials — check email/password in config.")
        resp.raise_for_status()

        data = resp.json()
        with self._lock:
            self.token = data["token"]
            self.station_id = data["station_id"]
            self._session.headers["X-AK-Token"] = self.token
            self.config["auth_token"] = self.token
            self.config["station_id"] = self.station_id
            save_config(self.config)

        self.connected = True
        _logger.info(
            "Authenticated: station '%s' (id=%d)", data["station_name"], self.station_id
        )

    def _reauth_if_needed(self, resp):
        """Return True if we got 401 and should retry."""
        if resp.status_code == 401:
            _logger.info("Token expired — re-authenticating...")
            try:
                self.authenticate()
            except Exception as exc:
                _logger.error("Re-authentication failed: %s", exc)
            return True
        return False

    def heartbeat_loop(self):
        """POST /ak_print/heartbeat every 30 s."""
        while not self._stop.is_set():
            try:
                resp = self._session.post(
                    self._url("/ak_print/heartbeat"),
                    json={"station_id": self.station_id},
                    timeout=10,
                )
                if self._reauth_if_needed(resp):
                    pass  # next iteration will use new token
                elif resp.status_code == 200:
                    data = resp.json()
                    self.connected = True
                    _logger.debug("Heartbeat OK")
                    if data.get("sync_printers"):
                        _logger.info("Heartbeat: Odoo requested printer re-sync")
                        threading.Thread(
                            target=self.sync_printers,
                            daemon=True,
                            name="sync_printers_req",
                        ).start()
                else:
                    self.connected = False
                    _logger.warning("Heartbeat: HTTP %d", resp.status_code)
            except Exception as exc:
                self.connected = False
                _logger.warning("Heartbeat error: %s", exc)
            self._stop.wait(30)

    def job_poll_loop(self):
        """GET /ak_print/jobs/pending every 5 s, print each job, report result."""
        while not self._stop.is_set():
            try:
                resp = self._session.get(
                    self._url("/ak_print/jobs/pending"),
                    params={"station_id": self.station_id},
                    timeout=15,
                )
                if self._reauth_if_needed(resp):
                    pass
                elif resp.status_code == 200:
                    jobs = resp.json().get("jobs", [])
                    if jobs:
                        _logger.info("Received %d pending job(s)", len(jobs))
                    for job in jobs:
                        job_id = job.get("id")
                        with self._active_jobs_lock:
                            if job_id in self._active_jobs:
                                continue  # already being processed in another thread
                            self._active_jobs.add(job_id)
                        # Run in a thread so a slow CUPS job doesn't block polling
                        threading.Thread(
                            target=self._process_job_safe,
                            args=(job,),
                            daemon=True,
                            name="job_%s" % job_id,
                        ).start()
                else:
                    _logger.warning("Job poll: HTTP %d", resp.status_code)
            except Exception as exc:
                _logger.warning("Job poll error: %s", exc)
            self._stop.wait(5)

    def _process_job_safe(self, job):
        """Wrapper that removes job from active set after processing."""
        job_id = job.get("id")
        try:
            self._process_job(job)
        finally:
            with self._active_jobs_lock:
                self._active_jobs.discard(job_id)

    def sync_printers(self):
        """POST /ak_print/sync_printers with the local OS printer list."""
        printers = _get_local_printers()
        if not printers:
            _logger.info("No local printers found — skipping sync.")
            return
        try:
            resp = self._session.post(
                self._url("/ak_print/sync_printers"),
                json={"station_id": self.station_id, "printers": printers},
                timeout=30,
            )
            if resp.status_code == 200:
                count = resp.json().get("synced", 0)
                _logger.info("Synced %d printer(s) to Odoo", count)
            else:
                _logger.warning("sync_printers: HTTP %d", resp.status_code)
        except Exception as exc:
            _logger.warning("sync_printers error: %s", exc)

    def _process_job(self, job):
        job_id = job["id"]
        printer_name = (job.get("printer_name") or "").strip()
        copies = max(1, int(job.get("copies") or 1))
        pdf_data_raw = job.get("pdf_data") or ""
        pdf_filename = (job.get("pdf_filename") or "print.pdf")

        _logger.info(
            "Processing job %d: printer=%s, copies=%d", job_id, printer_name, copies
        )

        if not pdf_data_raw:
            self._report_done(job_id, False, "No PDF data in job")
            return

        try:
            # pdf_data_raw is base64 (string or bytes)
            if isinstance(pdf_data_raw, bytes):
                pdf_data_raw = pdf_data_raw.decode("ascii")
            pdf_bytes = base64.b64decode(pdf_data_raw)
        except Exception as exc:
            self._report_done(job_id, False, "Invalid PDF data: %s" % exc)
            return

        printer_type = (job.get("printer_type") or "pdf").strip()
        if printer_type == "escpos":
            result = self._print_escpos(pdf_bytes, job)
        else:
            result = self._print_to_os_printer(pdf_bytes, printer_name, pdf_filename, copies)
        self._report_done(job_id, result["success"], result["message"])

    def _print_to_os_printer(self, pdf_bytes, printer_name, job_name, copies):
        """Print using the best backend for the current OS."""
        system = platform.system()
        if system == "Windows":
            return _send_win32(pdf_bytes, printer_name, job_name, copies)
        elif shutil.which("lp"):
            return _send_cups(pdf_bytes, printer_name, job_name, copies)
        else:
            return {"success": False, "message": "No print backend available (lp/CUPS not found)."}

    def _print_escpos(self, pdf_bytes, job):
        """Rasterize PDF and send to a network ESC/POS thermal printer.

        Pipeline:
          1. pdf2image (poppler) converts the PDF page(s) to PIL images at 203 DPI.
          2. python-escpos sends each image to the printer over TCP and cuts the paper.

        System requirement: poppler-utils must be installed on the agent machine.
          Linux : sudo apt-get install poppler-utils
          macOS : brew install poppler
          Windows: install poppler binaries and add to PATH
        """
        job_id = job.get("id")
        paper_width_mm = int(job.get("paper_width_mm") or 80)
        printer_ip = (job.get("printer_ip") or "").strip()
        printer_port = int(job.get("printer_tcp_port") or 9100)
        copies = max(1, int(job.get("copies") or 1))

        # ── Dependency checks ─────────────────────────────────────────────────
        try:
            from pdf2image import convert_from_bytes
        except ImportError:
            return {
                "success": False,
                "message": (
                    "pdf2image is not installed. "
                    "Run: pip install pdf2image  and  apt install poppler-utils"
                ),
            }

        try:
            from escpos.printer import Network as EscNetwork
        except ImportError:
            return {
                "success": False,
                "message": "python-escpos is not installed. Run: pip install python-escpos",
            }

        if not printer_ip:
            return {
                "success": False,
                "message": (
                    "ESC/POS printer has no IP address configured. "
                    "Open the printer record in Odoo and set the Printer IP Address."
                ),
            }

        # ── Step 1: Rasterize PDF → PIL images ───────────────────────────────
        # 203 DPI is the standard resolution for thermal receipt printers.
        # width_px is derived from the physical paper width so the image fills
        # the printable area exactly without scaling on the printer side.
        try:
            width_px = int(paper_width_mm / 25.4 * 203)
            images = convert_from_bytes(pdf_bytes, dpi=203, size=(width_px, None))
        except Exception as exc:
            return {"success": False, "message": "PDF rasterize failed: %s" % exc}

        # ── Step 2: Send via ESC/POS ──────────────────────────────────────────
        try:
            p = EscNetwork(printer_ip, printer_port)
            for _ in range(copies):
                for img in images:
                    p.image(img)
                p.cut()
            p.close()
            _logger.info(
                "ESC/POS job %s: %d page(s) × %d copies → %s:%d",
                job_id, len(images), copies, printer_ip, printer_port,
            )
            return {
                "success": True,
                "message": "ESC/POS print OK — %d page(s)" % len(images),
            }
        except Exception as exc:
            return {"success": False, "message": "ESC/POS send failed: %s" % exc}

    def _report_done(self, job_id, success, message):
        try:
            resp = self._session.post(
                self._url("/ak_print/jobs/%d/done" % job_id),
                json={"station_id": self.station_id, "success": success, "message": message},
                timeout=10,
            )
            if resp.status_code != 200:
                _logger.warning(
                    "report_done: HTTP %d for job %d", resp.status_code, job_id
                )
        except Exception as exc:
            _logger.warning("report_done error for job %d: %s", job_id, exc)

    def stop(self):
        self._stop.set()

    def run(self):
        """Authenticate, sync printers, then start poll threads. Blocks caller."""
        _logger.info("Station mode: connecting to %s ...", self.odoo_url)
        try:
            if not self.token:
                self.authenticate()
            else:
                # Validate existing token with a heartbeat
                try:
                    resp = self._session.post(
                        self._url("/ak_print/heartbeat"),
                        json={"station_id": self.station_id},
                        timeout=10,
                    )
                    if resp.status_code == 401:
                        _logger.info("Saved token expired — re-authenticating...")
                        self.authenticate()
                    elif resp.status_code == 200:
                        _logger.info("Token valid — station online.")
                except Exception:
                    self.authenticate()
        except Exception as exc:
            _logger.error("Station authentication failed: %s", exc)
            return False

        self.sync_printers()

        for target, name in [
            (self.heartbeat_loop, "heartbeat"),
            (self.job_poll_loop,  "job_poll"),
        ]:
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()

        _logger.info("Station client running (heartbeat=30s, poll=5s).")
        return True


def _apply_new_station_config(new_config):
    """Swap in a freshly-saved config without requiring an app restart.

    Stops the currently-running StationClient (if any) and replaces it with
    one built from the new config, updating _station_client_ref so every
    consumer (Flask /station_status, /tray/sync, the tray menu) picks up the
    new client on its next check — no manual restart needed after
    reconfiguring via "Configure Station…".
    """
    old = _station_client_ref[0]
    if old:
        old.stop()

    if not new_config.get("odoo_url"):
        _station_client_ref[0] = None
        return False

    new_client = StationClient(new_config)
    try:
        ok = new_client.run()
    except Exception as exc:
        _logger.error("Failed to apply new station config: %s", exc)
        _station_client_ref[0] = None
        return False

    _station_client_ref[0] = new_client
    # Use run()'s own return value, not .connected — .connected is only set
    # by the background heartbeat thread on its first round-trip, which
    # hasn't happened yet the instant run() returns (that raced to a false
    # "failed" result in testing even on a genuinely successful reconnect).
    return bool(ok)


# ---------------------------------------------------------------------------
# Auto-start helpers
# ---------------------------------------------------------------------------

def _get_exe_path():
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def _get_launch_argv(*extra_args):
    """Argv to re-launch this same program (frozen binary or dev script)."""
    if getattr(sys, "frozen", False):
        return [sys.executable] + list(extra_args)
    return [sys.executable, os.path.abspath(sys.argv[0])] + list(extra_args)


def _is_autostart_enabled():
    system = platform.system()
    if system == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
            winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return True
        except Exception:
            return False
    elif system == "Darwin":
        plist = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % APP_ID)
        return os.path.exists(plist)
    else:
        desktop = os.path.expanduser("~/.config/autostart/%s.desktop" % APP_ID)
        return os.path.exists(desktop)


def _set_autostart(enabled):
    system = platform.system()
    exe = _get_exe_path()
    if system == "Windows":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
            if enabled:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except Exception as exc:
            _logger.warning("Auto-start (Windows) error: %s", exc)
    elif system == "Darwin":
        plist_path = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % APP_ID)
        if enabled:
            os.makedirs(os.path.dirname(plist_path), exist_ok=True)
            plist = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"'
                ' "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0"><dict>\n'
                '  <key>Label</key><string>%s</string>\n'
                '  <key>ProgramArguments</key>\n'
                '  <array><string>%s</string></array>\n'
                '  <key>RunAtLoad</key><true/>\n'
                '  <key>KeepAlive</key><false/>\n'
                '  <key>StandardOutPath</key><string>%s</string>\n'
                '  <key>StandardErrorPath</key><string>%s</string>\n'
                '</dict></plist>\n'
            ) % (APP_ID, exe, LOG_FILE, LOG_FILE)
            with open(plist_path, "w") as f:
                f.write(plist)
            subprocess.run(["launchctl", "load", plist_path], check=False)
        else:
            if os.path.exists(plist_path):
                subprocess.run(["launchctl", "unload", plist_path], check=False)
                os.unlink(plist_path)
    else:
        desktop_dir = os.path.expanduser("~/.config/autostart")
        desktop_path = os.path.join(desktop_dir, "%s.desktop" % APP_ID)
        if enabled:
            os.makedirs(desktop_dir, exist_ok=True)
            content = (
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=%s\n"
                "Exec=%s\n"
                "Hidden=false\n"
                "NoDisplay=false\n"
                "X-GNOME-Autostart-enabled=true\n"
                "Comment=AK Direct Print station agent for Odoo printing\n"
            ) % (APP_NAME, exe)
            with open(desktop_path, "w") as f:
                f.write(content)
        else:
            if os.path.exists(desktop_path):
                os.unlink(desktop_path)


# ---------------------------------------------------------------------------
# Tray icon
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Setup dialog (tkinter — stdlib only, no extra deps)
# ---------------------------------------------------------------------------

class SetupDialog:
    """First-time configuration dialog.  Opens before the tray icon starts,
    or on demand via 'Configure Station...' in the tray menu.

    Returns the saved config dict on success, or None if cancelled.
    """

    # ── Palette (matches artifact design) ──────────────────────────────────
    _HDR_BG  = "#4A1F5E"   # dark purple header strip
    _BRAND   = "#6C3483"   # brand purple — primary button, links
    _LIGHT   = "#F5F0FA"   # light purple form background
    _WHITE   = "#FFFFFF"
    _GREEN   = "#1e8449"
    _RED     = "#c0392b"
    _AMBER   = "#b45309"
    _MUTED   = "#6B7280"
    _BORDER  = "#D1D5DB"
    _TEXT    = "#1F2937"

    def __init__(self, config=None):
        self.config = config or {}

    # ── Internal helpers ───────────────────────────────────────────────────

    def _styled_button(self, parent, text, command, style="primary", width=14):
        """Return a Label styled and behaving as a button.

        tk.Button ignores custom bg/activebackground on macOS Aqua — it
        always renders as the native grey system button regardless of
        what's set, which made "Save & Connect" show up as a barely-visible
        grey box. A Label (not a native Aqua control) fully respects custom
        colors, so it's used here with click/hover bindings instead.
        """
        import tkinter as tk
        if style == "primary":
            bg, fg, hover_bg = self._BRAND, self._WHITE, "#5a2b70"
            font = ("Helvetica", 11, "bold")
            relief, bd, hl_bg, hl_thick = "flat", 0, self._LIGHT, 0
        elif style == "outline":
            bg, fg, hover_bg = self._WHITE, self._BRAND, self._LIGHT
            font = ("Helvetica", 11, "normal")
            relief, bd, hl_bg, hl_thick = "solid", 1, self._BRAND, 1
        else:  # ghost
            bg, fg, hover_bg = "#F9FAFB", self._MUTED, "#E5E7EB"
            font = ("Helvetica", 11, "normal")
            relief, bd, hl_bg, hl_thick = "solid", 1, self._BORDER, 1

        btn = tk.Label(
            parent, text=text, bg=bg, fg=fg, font=font,
            relief=relief, bd=bd,
            highlightbackground=hl_bg, highlightthickness=hl_thick,
            padx=18, pady=9 if style == "primary" else 8,
            width=width, cursor="hand2",
        )
        btn.bind("<Enter>", lambda _e: btn.configure(bg=hover_bg))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bg))
        btn.bind("<Button-1>", lambda _e: command())
        return btn

    def _field(self, parent, grid_row, label_text, key,
               placeholder="", is_pw=False,
               col=0, colspan=2, padx_l=28, padx_r=28):
        """Add a labelled entry row and return the StringVar.

        grid_row:  absolute grid row for the label (entry goes at grid_row+1)
        col/colspan: column placement for multi-column layouts
        padx_l/r: left and right padding
        """
        import tkinter as tk
        tk.Label(
            parent, text=label_text,
            font=("Helvetica", 10, "bold"),
            fg=self._MUTED, bg=self._LIGHT,
            anchor="w",
        ).grid(row=grid_row, column=col, columnspan=colspan, sticky="w",
               padx=(padx_l, padx_r), pady=(10, 2))

        val = self.config.get(key, placeholder)
        var = tk.StringVar(value=val)
        entry_width = 44 if colspan == 2 else 21
        ent = tk.Entry(
            parent, textvariable=var, width=entry_width,
            show="•" if is_pw else "",
            font=("Helvetica", 12),
            relief="solid", bd=1,
            highlightbackground=self._BORDER, highlightthickness=1,
            bg=self._WHITE, fg=self._TEXT,
            insertbackground=self._TEXT, insertwidth=2,
        )
        ent.grid(row=grid_row + 1, column=col, columnspan=colspan, sticky="ew",
                 padx=(padx_l, padx_r), pady=(0, 4))
        self._bind_editing_keys(ent)
        return var

    @staticmethod
    def _bind_editing_keys(entry):
        """Bind standard editing shortcuts on an Entry for all platforms.

        Linux/Windows use Control+Key; macOS uses Command+Key.
        Ctrl+A (select all) is not bound by default on Linux tkinter.
        Both key families are bound so the same binary works everywhere.
        """
        def _select_all(event):
            event.widget.select_range(0, "end")
            event.widget.icursor("end")
            return "break"

        def _copy(event):
            event.widget.event_generate("<<Copy>>")
            return "break"

        def _paste(event):
            event.widget.event_generate("<<Paste>>")
            return "break"

        def _cut(event):
            event.widget.event_generate("<<Cut>>")
            return "break"

        # Linux / Windows
        entry.bind("<Control-a>", _select_all)
        entry.bind("<Control-c>", _copy)
        entry.bind("<Control-v>", _paste)
        entry.bind("<Control-x>", _cut)
        # macOS (Command key)
        entry.bind("<Command-a>", _select_all)
        entry.bind("<Command-c>", _copy)
        entry.bind("<Command-v>", _paste)
        entry.bind("<Command-x>", _cut)

    def run(self):
        """Block until the dialog closes; return saved config or None."""
        try:
            import tkinter as tk
        except ImportError:
            _logger.warning("tkinter not available — cannot show setup dialog.")
            return None

        result_holder = [None]
        root = tk.Tk()
        root.title("AK Direct Print — Station Setup")
        root.configure(bg=self._LIGHT)
        root.resizable(False, False)
        # Fix minimum width so the 2-column email/password row doesn't squish
        root.minsize(520, 0)

        # Self-close if our launching parent process dies (crash, force-quit)
        # while this dialog is open, so it doesn't linger as an orphaned
        # window. Driven by Tk's own event loop via root.after — a plain
        # background thread doesn't reliably get scheduled while mainloop()
        # is running Tk's native macOS (Cocoa) event loop.
        def _check_parent_alive():
            if os.getppid() != _LAUNCH_PARENT_PID:
                root.destroy()
                return
            root.after(2000, _check_parent_alive)
        root.after(2000, _check_parent_alive)

        # ── Header strip (dark purple — filled) ───────────────────────────
        hdr = tk.Frame(root, bg=self._HDR_BG, padx=28, pady=18)
        hdr.pack(fill="x", side="top")

        # Printer icon in a brand-purple square (44×44)
        icon_outer = tk.Frame(hdr, bg=self._BRAND, width=44, height=44)
        icon_outer.pack_propagate(False)
        icon_outer.pack(side="left", padx=(0, 14))
        tk.Label(
            icon_outer, text="🖨",
            font=("Helvetica", 20),
            bg=self._BRAND, fg=self._WHITE,
        ).place(relx=0.5, rely=0.5, anchor="center")

        hdr_text = tk.Frame(hdr, bg=self._HDR_BG)
        hdr_text.pack(side="left")
        tk.Label(
            hdr_text, text=APP_NAME,
            font=("Helvetica", 17, "bold"),
            bg=self._HDR_BG, fg=self._WHITE,
        ).pack(anchor="w")
        tk.Label(
            hdr_text, text="Connect this machine to your Odoo instance",
            font=("Helvetica", 10),
            bg=self._HDR_BG, fg="#C9A8E8",
        ).pack(anchor="w")

        # Thin separator between header and form
        tk.Frame(root, bg="#E8D8F5", height=1).pack(fill="x")

        # ── Footer — packed before body so it pins to the very bottom ──────
        tk.Frame(root, bg=self._BRAND, height=3).pack(fill="x", side="bottom")
        _foot = tk.Frame(root, bg=self._LIGHT, padx=20, pady=10)
        _foot.pack(fill="x", side="bottom")
        tk.Label(
            _foot,
            text="© Copyright 2010-2026, Aktiv Software"
                 " (Registered in India/USA). All rights reserved.",
            font=("Helvetica", 8), bg=self._LIGHT, fg=self._MUTED,
        ).pack()
        tk.Label(
            _foot, text="version: %s" % APP_VERSION,
            font=("Helvetica", 8), bg=self._LIGHT, fg=self._MUTED,
        ).pack()
        _link = tk.Label(
            _foot, text="www.aktivsoftware.com",
            font=("Helvetica", 8, "underline"),
            bg=self._LIGHT, fg=self._BRAND, cursor="hand2",
        )
        _link.pack()
        _link.bind(
            "<Button-1>",
            lambda _e: __import__("webbrowser").open("https://www.aktivsoftware.com"),
        )

        # ── Form body ──────────────────────────────────────────────────────
        body = tk.Frame(root, bg=self._LIGHT)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

        entry_vars = {}

        # Row 0–1: ODOO URL (full width)
        entry_vars["odoo_url"] = self._field(
            body, 0, "ODOO URL", "odoo_url",
            "https://yourcompany.odoo.com", False,
            col=0, colspan=2, padx_l=28, padx_r=28,
        )

        # Row 2–3: EMAIL (col 0) + PASSWORD (col 1)
        entry_vars["email"] = self._field(
            body, 2, "EMAIL", "email", "", False,
            col=0, colspan=1, padx_l=28, padx_r=6,
        )
        entry_vars["password"] = self._field(
            body, 2, "PASSWORD", "password", "", True,
            col=1, colspan=1, padx_l=6, padx_r=28,
        )

        # Row 4–5: STATION NAME (full width)
        entry_vars["station_name"] = self._field(
            body, 4, "STATION NAME", "station_name",
            platform.node(), False,
            col=0, colspan=2, padx_l=28, padx_r=28,
        )

        # Row 6: SSL checkbox
        ssl_var = tk.BooleanVar(value=self.config.get("verify_ssl", True))
        ssl_row = tk.Frame(body, bg=self._LIGHT)
        ssl_row.grid(row=6, column=0, columnspan=2,
                     sticky="w", padx=28, pady=(8, 4))
        tk.Checkbutton(
            ssl_row, variable=ssl_var,
            text="Verify SSL certificate  (uncheck for self-signed certs)",
            font=("Helvetica", 10), bg=self._LIGHT, fg=self._MUTED,
            activebackground=self._LIGHT, cursor="hand2",
        ).pack(side="left")

        # Row 7: Status banner (colored background like artifact)
        _BG_NONE   = self._LIGHT
        _BG_LOAD   = "#FEF3C7"   # amber-light
        _BG_OK     = "#E8F5E1"   # green-light
        _BG_ERR    = "#FDE8E6"   # red-light

        status_frame = tk.Frame(body, bg=_BG_NONE, height=40)
        status_frame.grid(row=7, column=0, columnspan=2,
                          sticky="ew", padx=28, pady=(6, 0))
        status_frame.pack_propagate(False)

        status_var = tk.StringVar(value="")
        status_lbl = tk.Label(
            status_frame, textvariable=status_var,
            font=("Helvetica", 11),
            bg=_BG_NONE, fg=self._MUTED,
            wraplength=460, justify="left", anchor="w",
            padx=10,
        )
        status_lbl.pack(fill="both", expand=True)

        def _set_status(state, text):
            bg = {
                "": _BG_NONE,
                "loading": _BG_LOAD,
                "success": _BG_OK,
                "error":   _BG_ERR,
            }.get(state, _BG_NONE)
            fg = {
                "": self._MUTED,
                "loading": self._AMBER,
                "success": self._GREEN,
                "error":   self._RED,
            }.get(state, self._MUTED)
            status_var.set(text)
            status_frame.configure(bg=bg)
            status_lbl.configure(bg=bg, fg=fg)

        # Row 8: Button row
        btn_row = tk.Frame(body, bg=self._LIGHT)
        btn_row.grid(row=8, column=0, columnspan=2,
                     sticky="ew", padx=28, pady=(14, 20))

        def _collect():
            cfg = {k: v.get().strip() for k, v in entry_vars.items()}
            cfg["verify_ssl"] = ssl_var.get()
            cfg.setdefault("station_uuid", self.config.get("station_uuid", ""))
            return cfg

        def on_test():
            cfg = _collect()
            if not cfg.get("odoo_url"):
                _set_status("error", "⚠  Enter the Odoo URL first.")
                return
            _set_status("loading", "⏳  Connecting…")
            root.update()

            def _do_test():
                try:
                    import requests as _req
                    resp = _req.post(
                        cfg["odoo_url"].rstrip("/") + "/ak_print/auth",
                        json={
                            "email":        cfg.get("email", ""),
                            "password":     cfg.get("password", ""),
                            "station_uuid": cfg.get("station_uuid", ""),
                            "station_name": cfg.get("station_name", APP_NAME),
                            "os":           platform.system(),
                            "app_version":  APP_VERSION,
                        },
                        timeout=15,
                        verify=cfg["verify_ssl"],
                    )
                    if resp.status_code == 200:
                        d = resp.json()
                        root.after(0, lambda: _set_status(
                            "success",
                            '✓  Connected!  Station "%s" registered (id=%d).'
                            % (d["station_name"], d["station_id"])
                        ))
                    elif resp.status_code == 401:
                        root.after(0, lambda: _set_status(
                            "error", "✗  Invalid credentials — check email and password."))
                    elif resp.status_code == 404:
                        root.after(0, lambda: _set_status(
                            "error", "✗  URL not found — confirm AK Direct Print is installed on that Odoo."))
                    else:
                        root.after(0, lambda: _set_status(
                            "error", "✗  Server returned HTTP %d." % resp.status_code))
                except ImportError:
                    root.after(0, lambda: _set_status(
                        "error", "✗  'requests' package missing — run: pip install requests"))
                except Exception as exc:
                    # requests' connection/timeout/SSL exceptions render as a
                    # long urllib3 internals dump — show a plain message for
                    # the common cases instead of that wall of text.
                    cls_name = type(exc).__name__
                    if "SSL" in cls_name:
                        msg = "SSL certificate error — try unchecking 'Verify SSL certificate'."
                    elif "ConnectionError" in cls_name:
                        msg = "Could not connect — check the URL and your network connection."
                    elif "Timeout" in cls_name:
                        msg = "Connection timed out — the server took too long to respond."
                    else:
                        msg = str(exc)[:160]
                    root.after(0, lambda: _set_status("error", "✗  " + msg))

            threading.Thread(target=_do_test, daemon=True).start()

        def on_save():
            cfg = _collect()
            if not cfg.get("odoo_url") or not cfg.get("email") or not cfg.get("password"):
                _set_status("error", "⚠  Odoo URL, Email and Password are required.")
                return
            if not cfg["station_uuid"]:
                cfg["station_uuid"] = _uuid_mod.uuid4().hex
            save_config(cfg)
            result_holder[0] = cfg
            root.destroy()

        self._styled_button(btn_row, "Test Connection", on_test, "outline", 15).pack(
            side="left", padx=(0, 10))
        self._styled_button(btn_row, "Save & Connect", on_save, "primary", 15).pack(
            side="left")

        # ── Center and show ────────────────────────────────────────────────
        root.update_idletasks()
        w, h = root.winfo_reqwidth(), root.winfo_reqheight()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (w, h, (sw - w) // 2, (sh - h) // 2))
        root.mainloop()
        return result_holder[0]


def _make_tray_icon(online=True):
    """Draw the tray icon: brand-purple when online, slate-grey when offline.

    Mirrors tray.py's Cairo-drawn Linux icon exactly — same 22x22 proportions
    (scaled up 4x here for Windows tray sharpness), same colors, same status
    dot — so the tray icon looks identical across every OS the agent runs on.
    """
    from PIL import Image, ImageDraw
    scale = 4
    size = 22 * scale  # 88px; base unit matches tray.py's 22x22 canvas
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    purple = (0x61, 0x2E, 0x9E, 255)  # brand purple  #612E9E
    grey   = (0x94, 0xA3, 0xB8, 255)  # slate grey    #94A3B8
    green  = (0x22, 0xC3, 0x5E, 255)  # status dot    #22C35E
    white  = (255, 255, 255, 255)
    bg = purple if online else grey

    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=4 * scale, fill=bg)

    draw.rectangle([6 * scale, 2 * scale, 16 * scale - 1, 5 * scale - 1], fill=white)   # paper-input tray
    draw.rectangle([3 * scale, 6 * scale, 19 * scale - 1, 15 * scale - 1], fill=white)  # printer body
    draw.rectangle([5 * scale, 9 * scale, 17 * scale - 1, 12 * scale - 1], fill=bg)     # paper-feed slot punch-out
    draw.rectangle([6 * scale, 17 * scale, 16 * scale - 1, 20 * scale - 1], fill=white) # paper output

    if online:
        cx = cy = (22 - 3.5) * scale
        r = 3 * scale
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=green)

    return img


def _open_log_file():
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(LOG_FILE)
        elif system == "Darwin":
            subprocess.Popen(["open", LOG_FILE])
        else:
            subprocess.Popen(["xdg-open", LOG_FILE])
    except Exception as exc:
        _logger.warning("Could not open log file: %s", exc)


def _run_tray(port, station_client=None):
    """System tray icon for Windows / macOS (pystray)."""
    try:
        import pystray
    except ImportError:
        _logger.warning("pystray not installed — running headless (no tray icon).")
        return

    # Build initial icon color based on connection state
    initial_online = bool(station_client and station_client.token)
    tray_icon_ref = [None]  # mutable so callbacks can reach it

    def on_toggle_autostart(icon, item):
        _set_autostart(not _is_autostart_enabled())

    def on_view_logs(icon, item):
        _open_log_file()

    def on_configure(icon, item):
        def _show():
            old_cfg = load_config()
            # Runs in an isolated subprocess — see the --_setup_subprocess
            # branch in main() for why (Tk can abort the whole process
            # natively on some macOS/Tcl-Tk combos).
            try:
                subprocess.run(
                    _get_launch_argv("--_setup_subprocess"),
                    timeout=600,
                )
            except Exception as exc:
                _logger.error("Setup dialog subprocess failed to run: %s", exc)
                return

            new_cfg = load_config()
            if new_cfg == old_cfg:
                return  # dialog was cancelled — nothing to apply

            connected = _apply_new_station_config(new_cfg)
            try:
                if connected:
                    icon.notify(
                        "Connected — station \"%s\" is online."
                        % new_cfg.get("station_name", APP_NAME),
                        APP_NAME,
                    )
                else:
                    icon.notify(
                        "Saved, but couldn't connect — check the URL and credentials.",
                        APP_NAME,
                    )
            except Exception:
                pass  # notifications aren't supported on every backend

        threading.Thread(target=_show, daemon=True, name="setup_dialog").start()

    def on_sync(icon, item):
        sc = _station_client_ref[0]
        if sc:
            threading.Thread(target=sc.sync_printers,
                             daemon=True, name="sync_printers").start()

    def on_quit(icon, item):
        sc = _station_client_ref[0]
        if sc:
            sc.stop()
        icon.stop()

    def _is_online():
        sc = _station_client_ref[0]
        return bool(sc and sc.token)

    def _station_label(item):
        cfg = load_config()
        url = cfg.get("odoo_url", "")
        if url:
            name = cfg.get("station_name", "Station")
            badge = "[Online]" if _is_online() else "[Offline]"
            dot = "●" if _is_online() else "○"
            return "%s %s  %s" % (dot, name, badge)
        return "○ Not configured"

    menu = pystray.Menu(
        pystray.MenuItem("%s v%s" % (APP_NAME, APP_VERSION), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_station_label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Configure Station…", on_configure),
        pystray.MenuItem(
            "Sync Printers",
            on_sync,
            enabled=lambda item: _is_online(),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Start on Login",
            on_toggle_autostart,
            checked=lambda item: _is_autostart_enabled(),
        ),
        pystray.MenuItem("View Logs", on_view_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(APP_ID, _make_tray_icon(initial_online), APP_NAME, menu)
    tray_icon_ref[0] = icon
    _logger.info("System tray icon started.")
    icon.run()


def _launch_linux_tray(port):
    _script = "/usr/local/lib/ak-direct-print/tray.py"
    _py3    = "/usr/bin/python3"
    if not os.path.exists(_script):
        _logger.info("Tray script not found at %s — headless mode.", _script)
        return
    if not os.path.exists(_py3):
        _logger.info("system python3 not found — headless mode.")
        return
    try:
        proc = subprocess.Popen(
            [_py3, _script, "--port", str(port), "--agent-pid", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _logger.info("Tray subprocess started (PID %d).", proc.pid)
    except Exception as exc:
        _logger.warning("Could not start tray subprocess: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="%s v%s" % (APP_NAME, APP_VERSION))
    parser.add_argument("--port",     type=int, default=7654,   help="Flask port (default: 7654)")
    parser.add_argument("--host",     default="127.0.0.1",      help="Flask bind address")
    parser.add_argument("--headless", action="store_true",       help="Skip tray icon and setup dialog")
    parser.add_argument("--setup",    action="store_true",       help="Open the Station Setup dialog")
    parser.add_argument("--debug",    action="store_true",       help="Flask debug mode")
    parser.add_argument("--_setup_subprocess", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # ── Internal: isolated child process that ONLY shows the setup dialog.
    # Some macOS/Tcl-Tk combinations abort the whole process natively on
    # tk.Tk() init (not a catchable Python exception) — running the dialog
    # here, in a disposable subprocess, keeps that from ever taking down
    # the Flask server / tray icon in the parent. SetupDialog.run() already
    # calls save_config() internally before returning, so the parent just
    # needs to reload config afterwards — no IPC return value needed. The
    # parent (main-run or the tray's on_configure) is responsible for
    # detecting the change and applying it live — no restart required, and
    # no popup here since this child has no idea whether that reconnect
    # will actually succeed.
    if args._setup_subprocess:
        cfg = load_config()
        try:
            dlg = SetupDialog(cfg)
            dlg.run()
        except Exception as exc:
            _logger.error("Setup dialog failed: %s", exc)
        return

    config = load_config()

    # ── First-run / --setup: show configuration dialog (isolated subprocess) ──
    if not args.headless and (args.setup or not config.get("odoo_url")):
        if args.setup or not config.get("odoo_url"):
            _logger.info("Opening setup dialog (first run or --setup flag).")
            try:
                subprocess.run(_get_launch_argv("--_setup_subprocess"), timeout=600)
            except Exception as exc:
                _logger.error("Setup dialog subprocess failed to run: %s", exc)
            config = load_config()
            if config.get("odoo_url"):
                _logger.info("Configuration saved via setup dialog.")
            else:
                _logger.info("Setup cancelled — running in legacy localhost mode.")

    _logger.info("=" * 55)
    _logger.info("  %s  v%s", APP_NAME, APP_VERSION)
    _logger.info("  Platform : %s", platform.system())
    _logger.info("  Log file : %s", LOG_FILE)

    station_client = None
    if config.get("odoo_url"):
        if not HAS_REQUESTS:
            _logger.error(
                "Station mode requires the 'requests' package. "
                "Run: pip install requests"
            )
        else:
            try:
                station_client = StationClient(config)
                station_client.run()
                _station_client_ref[0] = station_client
                _logger.info("  Mode     : Station (outbound polling → %s)", config["odoo_url"])
            except Exception as exc:
                _logger.error("Station client failed to start: %s", exc)
                station_client = None
    else:
        _logger.info("  Mode     : Legacy localhost (no config found at %s)", _get_config_path())

    _logger.info("  Flask    : http://%s:%d  (/health, /print, /check_printer)", args.host, args.port)
    _logger.info("=" * 55)

    # ── Linux: tray subprocess + Flask in main thread ──────────────────────
    if sys.platform.startswith("linux"):
        if not args.headless:
            _launch_linux_tray(args.port)
        app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        return

    # ── Windows / macOS: pystray on main thread, Flask in daemon thread ────
    tray_available = False
    if not args.headless:
        try:
            import pystray          # noqa: F401
            from PIL import Image   # noqa: F401
            tray_available = True
        except Exception as exc:
            _logger.info("Tray backend unavailable (%s) — headless mode.", exc)

    if tray_available:
        flask_thread = threading.Thread(
            target=lambda: app.run(
                host=args.host, port=args.port,
                debug=False, use_reloader=False,
            ),
            daemon=True,
            name="flask",
        )
        flask_thread.start()
        _run_tray(args.port, station_client=station_client)
    else:
        app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
