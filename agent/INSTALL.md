# AK Direct Print — Local Agent Installation Guide

The Local Agent runs on the **client's machine** and bridges Odoo (including
Odoo.sh / Odoo Online) to the client's local printer.  
The agent listens on `http://localhost:7654`.

---

## 1. What the Client Receives

| Platform | File to deliver |
|----------|----------------|
| Windows  | `AKDirectPrint.exe` |
| macOS    | `AKDirectPrint.app` |
| Linux    | `AKDirectPrint` (binary) |

Built by the vendor using PyInstaller (see Section 4 — Vendor Build Steps).

---

## 2. Client Installation — Windows

**System packages required:** None (everything is bundled in the .exe).

**Steps:**
1. Copy `AKDirectPrint.exe` to any folder (e.g. `C:\Program Files\AKDirectPrint\`)
2. Double-click `AKDirectPrint.exe`
3. A green printer icon appears in the system tray (bottom-right)
4. Right-click the tray icon → **Start on Login** → enable it
5. Done — the agent starts automatically on every login

**Printing backends available:**
- IPP (direct WiFi/network printer) — works out of the box
- Windows printers — works via Win32 API (bundled)

---

## 3. Client Installation — Ubuntu / Linux

Linux requires a few system packages before the agent tray icon works correctly.

### 3a. Required System Packages

Run these once on the client machine (requires sudo):

```bash
# Printing backend — CUPS (lp command + lpstat)
sudo apt install cups cups-client

# WiFi/mDNS printer discovery (avahi-browse, avahi-resolve)
sudo apt install avahi-utils

# System tray icon support on GNOME
sudo apt install \
    python3-gi \
    gir1.2-appindicator3-0.1 \
    gnome-shell-extension-appindicator
```

> **Why these packages?**
> 
> | Package | Used for |
> |---------|----------|
> | `cups`, `cups-client` | Sending jobs via `lp` command; auto-fallback for older printers |
> | `avahi-utils` | mDNS discovery — finds WiFi printers on the LAN (`avahi-browse`, `avahi-resolve`) |
> | `python3-gi` | GObject Introspection Python bindings — required by pystray on Linux |
> | `gir1.2-appindicator3-0.1` | AppIndicator3 typelib — enables right-click tray menu on GNOME |
> | `gnome-shell-extension-appindicator` | GNOME Shell extension that shows tray icons in the top bar |

### 3b. After Installing System Packages

**Log out and log back in** (the GNOME extension only activates after re-login).

### 3c. Run the Agent

```bash
# Make executable (first time only)
chmod +x AKDirectPrint

# Run
./AKDirectPrint
```

A green tray icon appears in the top bar.  
Right-click → **Start on Login** to enable auto-start.

### 3d. One-Line Install Script (optional — for tech-savvy clients)

```bash
sudo apt install -y cups cups-client avahi-utils python3-gi \
    gir1.2-appindicator3-0.1 gnome-shell-extension-appindicator \
    && echo "Done — log out and back in, then run ./AKDirectPrint"
```

---

## 4. Client Installation — macOS

**System packages required:** None (CUPS is built into macOS; Bonjour is native).

**Steps:**
1. Drag `AKDirectPrint.app` to `/Applications`
2. Double-click to launch — a green tray icon appears in the menu bar
3. Right-click → **Start on Login** → enable it
4. Done

### 4a. "AKDirectPrint cannot be opened because the developer cannot be verified"

This is expected — the app isn't yet signed with an Apple Developer ID, so
macOS Gatekeeper blocks apps downloaded from a browser by default. This is a
one-time step per machine; it does **not** happen again after the first
successful launch. Two ways to get past it:

**Option A — System Settings (no Terminal needed):**
1. Click **Cancel** on the warning (not "Move to Bin").
2. Open **System Settings → Privacy & Security**.
3. Scroll down — you'll see a message that AKDirectPrint was blocked, with
   an **Open Anyway** button next to it. Click it (enter your Mac password
   or Touch ID if asked).
4. Open `AKDirectPrint` from `/Applications` again — this time click
   **Open** on the (now much milder) confirmation dialog.

**Option B — Terminal (one command, faster):**
```bash
xattr -d com.apple.quarantine /Applications/AKDirectPrint.app
```
Then open the app normally.

---

## 5. Tray Icon — Menu Reference

| Menu Item | Action |
|-----------|--------|
| AK Direct Print \| port 7654 | Status label (not clickable) |
| **Start on Login** | Toggle auto-start on user login |
| **View Logs** | Open `~/.ak_direct_print/agent.log` in default viewer |
| **Quit** | Stop the agent |

**Log file location:**

| Platform | Path |
|----------|------|
| Windows  | `C:\Users\<user>\.ak_direct_print\agent.log` |
| macOS    | `~/.ak_direct_print/agent.log` |
| Linux    | `~/.ak_direct_print/agent.log` |

---

## 6. Headless / Server Mode (no tray icon)

For servers or automated environments where there is no desktop:

```bash
./AKDirectPrint --headless
# or
./AKDirectPrint --headless --port 7654 --host 127.0.0.1
```

---

## 7. Vendor Build Steps (one per OS)

> Run these on the target OS — PyInstaller can only build for the OS it runs on.

### 7a. Build on Ubuntu / Linux

```bash
cd agent/

# Install system packages needed to BUILD (not just run)
sudo apt install -y cups cups-client avahi-utils python3-gi \
    gir1.2-appindicator3-0.1 gnome-shell-extension-appindicator

# Create venv and add system gi to its path
python3 -m venv .venv
source .venv/bin/activate
echo "/usr/lib/python3/dist-packages" > .venv/lib/python3.*/site-packages/system-gi.pth

# Install Python dependencies
pip install -r requirements.txt -r requirements-build.txt

# Build
pyinstaller build/ak_direct_print.spec --clean --noconfirm

# Output: dist/AKDirectPrint
```

### 7b. Build on Windows

```bat
cd agent\

:: Create venv and install
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-build.txt

:: Build
pyinstaller build\ak_direct_print.spec --clean --noconfirm

:: Output: dist\AKDirectPrint.exe
```

### 7c. Build on macOS

```bash
cd agent/

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-build.txt

pyinstaller build/ak_direct_print.spec --clean --noconfirm

# Output: dist/AKDirectPrint.app
```

---

## 8. Odoo Module Setup (server-side — done once per Odoo instance)

1. Copy `ak_direct_print/` to your Odoo addons path
2. Restart Odoo, update apps list
3. Install **Direct Print** from Apps
4. Go to **Direct Print → Configuration → Printers**
5. Create a printer, set **Print Mode** to **Local Agent**
6. Set **Connection Type** to CUPS or IPP (WiFi)
7. Click **Discover from Agent** to auto-detect printers from the running agent
8. Go to **Direct Print → Configuration → Print Rules**
9. Create a rule linking a report to the printer

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Tray icon not clickable on Linux | Missing AppIndicator or GNOME extension | Install packages from Section 3a, re-login |
| "Agent not reachable" in Odoo | Agent not running or wrong port | Start the agent; check port matches printer config (default 7654) |
| Printer not found via Discover | avahi-browse not installed | `sudo apt install avahi-utils` |
| CUPS printer not printing | CUPS not installed or printer not added | `sudo apt install cups`, then add printer via `http://localhost:631` |
| Agent starts but no tray icon | Running on headless server | Use `--headless` flag |
