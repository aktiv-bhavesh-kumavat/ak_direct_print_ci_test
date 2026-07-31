@echo off
REM Build AKDirectPrint.exe for Windows
REM Usage:  cd agent && build\build.bat
REM Output: agent\dist\AKDirectPrint.exe

cd /d "%~dp0\.."
echo === AK Direct Print - Windows Build ===
echo Python: & python --version

REM 1. Create virtual environment if missing
if not exist ".venv" (
    python -m venv .venv
    echo Created .venv
)

call .venv\Scripts\activate.bat
echo Activated .venv

REM 2. Install dependencies
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt -r requirements-build.txt --quiet
if %errorlevel% neq 0 (
    echo ERROR: pip install failed - see output above.
    exit /b 1
)
echo Dependencies installed

REM 3. Build with PyInstaller
pyinstaller build\ak_direct_print.spec --clean --noconfirm

echo.
echo === Build complete ===
echo Output: %cd%\dist\AKDirectPrint.exe

REM 4. Quick smoke test
echo.
echo Running smoke test...
start /B dist\AKDirectPrint.exe --headless --port 7655
timeout /t 3 /nobreak >nul

curl -s --max-time 2 http://127.0.0.1:7655/health | findstr "ok" >nul
if %errorlevel% == 0 (
    echo Smoke test PASSED - agent responded on port 7655
) else (
    echo Smoke test FAILED - agent did not respond
)

taskkill /F /IM AKDirectPrint.exe >nul 2>&1
echo Done.
pause
