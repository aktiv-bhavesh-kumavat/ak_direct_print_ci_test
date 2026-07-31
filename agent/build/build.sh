#!/usr/bin/env bash
# Build AKDirectPrint for Linux or macOS
# Usage:  cd agent && bash build/build.sh
# Output: agent/dist/AKDirectPrint  (Linux)
#         agent/dist/AKDirectPrint.app  (macOS)

set -e
cd "$(dirname "$0")/.."   # always run from the agent/ directory

echo "=== AK Direct Print — build ==="
echo "Platform : $(uname -s)"
echo "Python   : $(python3 --version)"

# 1. Create / activate a clean virtual environment
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    echo "Created .venv"
fi

source .venv/bin/activate
echo "Activated .venv"

# 2. Install dependencies
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
echo "Dependencies installed"

# 3. Run PyInstaller
pyinstaller build/ak_direct_print.spec --clean --noconfirm

echo ""
echo "=== Build complete ==="
if [ "$(uname)" = "Darwin" ]; then
    echo "Output: $(pwd)/dist/AKDirectPrint.app"
else
    echo "Output: $(pwd)/dist/AKDirectPrint"
fi

# 4. Quick smoke test — start agent for 3 seconds, check /health
echo ""
echo "Running smoke test..."
if [ "$(uname)" = "Darwin" ]; then
    BINARY="dist/AKDirectPrint.app/Contents/MacOS/AKDirectPrint"
else
    BINARY="dist/AKDirectPrint"
fi

"$BINARY" --headless --port 7655 &
AGENT_PID=$!
sleep 3

if curl -s --max-time 2 http://127.0.0.1:7655/health | grep -q '"ok"'; then
    echo "Smoke test PASSED — agent responded on port 7655"
else
    echo "Smoke test FAILED — agent did not respond"
fi

kill $AGENT_PID 2>/dev/null || true

# 5. macOS: build DMG for distribution
if [ "$(uname)" = "Darwin" ]; then
    echo ""
    echo "--- Building AKDirectPrint.dmg ---"
    bash build/build_dmg.sh
fi

echo "Done."
