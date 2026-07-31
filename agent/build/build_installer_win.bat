@echo off
REM Build AKDirectPrint_Setup.exe for Windows
REM
REM Steps:
REM   1. Creates Python venv and installs dependencies
REM   2. Runs PyInstaller → dist\AKDirectPrint.exe
REM   3. Runs Inno Setup compiler → dist\AKDirectPrint_Setup.exe
REM
REM Requirements on the build machine:
REM   - Python 3.10+ (https://python.org)
REM   - Inno Setup 6  (https://jrsoftware.org/isdl.php)
REM
REM Usage:  cd agent && build\build_installer_win.bat
REM Output: agent\dist\AKDirectPrint_Setup.exe

cd /d "%~dp0\.."
echo === AK Direct Print - Windows Installer Build ===
echo Python: & python --version
echo.

REM ── 1. Create virtual environment ────────────────────────────────────────────
if not exist ".venv" (
    python -m venv .venv
    echo Created .venv
)
call .venv\Scripts\activate.bat
echo Activated .venv

REM ── 2. Install dependencies ───────────────────────────────────────────────────
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
python -m pip install pyinstaller --quiet
echo Dependencies installed

REM ── 3. PyInstaller build ──────────────────────────────────────────────────────
echo.
echo --- Running PyInstaller ---
pyinstaller build\ak_direct_print.spec --clean --noconfirm
if errorlevel 1 (
    echo ERROR: PyInstaller failed.
    pause
    exit /b 1
)
echo PyInstaller OK — dist\AKDirectPrint.exe

REM ── 4. Smoke test the exe ─────────────────────────────────────────────────────
echo.
echo --- Smoke test ---
start /B dist\AKDirectPrint.exe --headless --port 7655
timeout /t 4 /nobreak >nul
curl -s --max-time 3 http://127.0.0.1:7655/health | findstr /C:"ok" >nul
if %errorlevel% == 0 (
    echo Smoke test PASSED
) else (
    echo Smoke test FAILED - check dist\AKDirectPrint.exe manually
)
taskkill /F /IM AKDirectPrint.exe >nul 2>&1

REM ── 5. Locate Inno Setup compiler ─────────────────────────────────────────────
echo.
echo --- Locating Inno Setup ---
set ISCC=""
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"
) else (
    echo ERROR: Inno Setup 6 not found.
    echo Download from: https://jrsoftware.org/isdl.php
    echo Then re-run this script.
    pause
    exit /b 1
)
echo Found: %ISCC%

REM ── 6. Build the installer ────────────────────────────────────────────────────
echo.
echo --- Running Inno Setup ---
%ISCC% build\windows\ak_direct_print.iss
if errorlevel 1 (
    echo ERROR: Inno Setup compilation failed.
    pause
    exit /b 1
)

REM ── 7. Done ───────────────────────────────────────────────────────────────────
echo.
echo === Build complete ===
echo.
echo Executable : dist\AKDirectPrint.exe
echo Installer  : dist\AKDirectPrint_Setup.exe
echo.
echo Share dist\AKDirectPrint_Setup.exe with clients.
echo Client install: double-click setup ^ next ^ next ^ finish.
echo.
pause
