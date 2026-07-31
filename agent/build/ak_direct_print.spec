# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for AK Direct Print Agent
#
# Build commands:
#   Windows : pyinstaller build\ak_direct_print.spec
#   macOS   : pyinstaller build/ak_direct_print.spec
#   Linux   : pyinstaller build/ak_direct_print.spec
#
# Output:  dist/AKDirectPrint(.exe on Windows)

import sys
import os

block_cipher = None

# Hidden imports needed by flask, pystray, and platform-specific backends
hidden_imports = [
    # Flask internals
    "flask",
    "flask.json",
    "werkzeug",
    "werkzeug.serving",
    "werkzeug.routing",
    "jinja2",
    "click",
    "itsdangerous",
    # Pillow — needed for tray icon drawing
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    # pystray backends (include all; unused ones are skipped at runtime)
    "pystray",
    "pystray._base",
    # stdlib modules that PyInstaller drops when unittest/pydoc are excluded
    # but which are still needed by the system gi import chain at runtime
    "optparse",
    "textwrap",
    "gettext",
]

# Platform-specific hidden imports
if sys.platform == "win32":
    hidden_imports += [
        "win32print",
        "win32api",
        "win32gui",
        "winreg",
        "pystray._win32",
        "fitz",
    ]
elif sys.platform == "darwin":
    hidden_imports += [
        "pystray._darwin",
        "AppKit",
        "Foundation",
        "objc",
    ]
else:
    # Linux: the tray runs as a separate system python3 subprocess (tray.py),
    # so NO pystray backends or gi/GObject modules are needed in this bundle.
    #
    # Why: PyInstaller bundles its own libpython.  System gi's _gi.so is compiled
    # against the SYSTEM libpython.  When pystray._appindicator or pystray._gtk
    # was analysed here, PyInstaller pulled gi binaries into the bundle, creating
    # two incompatible libpython instances in one process.  That causes the
    # "partially initialised module 'gi'" circular import at runtime.
    # tray.py (under /usr/bin/python3) avoids this entirely — zero gi in bundle.
    pass

# Modules excluded from the bundle on all platforms
# NOTE: tkinter must NOT be excluded — SetupDialog (the "Configure Station…"
# first-run dialog) is built on it; excluding it silently breaks that flow
# in every packaged build ("tkinter not available — cannot show setup dialog").
_excludes = ["unittest", "pydoc"]

# Windows: pywin32's runtime DLLs (pywintypes3xx.dll, pythoncom3xx.dll) live in
# site-packages/pywin32_system32/.  Depending on the installed PyInstaller /
# pyinstaller-hooks-contrib version, the auto-generated hook sometimes bundles
# them nested under a "pywin32_system32/" subfolder instead of the bundle root.
# Windows' DLL search order does not look in that subfolder, so win32print.pyd
# fails to load its pywintypes dependency at runtime — surfacing in Python as
# "ImportError" even though pywin32 is correctly installed.  Explicitly
# collecting these DLLs to the bundle root ('.') sidesteps the hook entirely.
pywin32_binaries = []
if sys.platform == "win32":
    try:
        import win32
        _win32_pkg_dir = list(win32.__path__)[0]  # win32 is a namespace pkg — no __file__
        _pywin32_system32 = os.path.join(
            os.path.dirname(_win32_pkg_dir), "pywin32_system32"
        )
        if os.path.isdir(_pywin32_system32):
            for _f in os.listdir(_pywin32_system32):
                if _f.lower().endswith(".dll"):
                    pywin32_binaries.append((os.path.join(_pywin32_system32, _f), "."))
    except ImportError:
        pass

a = Analysis(
    [os.path.join("..", "ak_direct_print_agent.py")],
    pathex=[os.path.join("..", )],
    binaries=pywin32_binaries,
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# macOS: build onedir (exclude_binaries=True + COLLECT) instead of onefile.
# Onefile self-extracts to a fresh temp dir on EVERY launch, and macOS
# re-verifies the freshly-written binaries' code signature each time —
# this made a menu-bar app take ~25s to show its tray icon. Onedir ships
# the binaries inside the .app permanently, so launches are near-instant.
# PyInstaller also deprecates onefile+BUNDLE (macOS .app) combinations
# and will make it an error in v7.0.
if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="AKDirectPrint",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,       # macOS: keep False to avoid argv issues
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="AKDirectPrint",
    )
    app = BUNDLE(
        coll,
        name="AKDirectPrint.app",
        bundle_identifier="com.aktivsoftware.ak-direct-print",
        info_plist={
            "CFBundleName": "AK Direct Print",
            "CFBundleDisplayName": "AK Direct Print",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "LSUIElement": True,        # hide from Dock — tray-only app
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="AKDirectPrint",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        # --windowed: no console window on Windows.
        # Users see only the tray icon; logs go to ~/.ak_direct_print/agent.log
        console=False,
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
