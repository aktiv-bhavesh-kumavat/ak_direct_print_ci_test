#!/usr/bin/env bash
# Download the latest successful CI release artifacts and place them directly
# into the Odoo module's static/agent/ folder with the correct filenames.
#
# Usage:  ./fetch_builds.sh
# Requires: curl, unzip, python3

set -euo pipefail

REPO="aktiv-bhavesh-kumavat/ak_direct_print_ci_test"
# Read token from git remote URL (stored there already) or GITHUB_TOKEN env var
TOKEN="${GITHUB_TOKEN:-$(git -C "$(dirname "$0")" remote get-url origin 2>/dev/null | sed 's|.*://[^:]*:\([^@]*\)@.*|\1|')}"
if [[ -z "$TOKEN" ]]; then
  echo "ERROR: no GitHub token found. Set GITHUB_TOKEN env var or ensure the remote URL contains credentials."
  exit 1
fi
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATIC="$(cd "$SCRIPT_DIR/../aktiv_contributions_19/ak_direct_print/static/agent" && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Target folder: $STATIC"
echo "Repo:          $REPO"
echo ""

api() { curl -sf -H "Authorization: token $TOKEN" "$@"; }

# ── Find the latest successful run for each workflow ──────────────────────────
echo "Fetching latest run IDs..."
RUNS=$(api "https://api.github.com/repos/$REPO/actions/runs?per_page=20&status=success")

macos_run=$(echo "$RUNS" | python3 -c "
import sys, json
runs = json.load(sys.stdin)['workflow_runs']
r = next((r for r in runs if 'macOS' in r['name'] or 'macos' in r['name'].lower()), None)
print(r['id'] if r else '')
")
windows_run=$(echo "$RUNS" | python3 -c "
import sys, json
runs = json.load(sys.stdin)['workflow_runs']
r = next((r for r in runs if 'Windows' in r['name'] or 'windows' in r['name'].lower()), None)
print(r['id'] if r else '')
")

echo "  macOS  run: $macos_run"
echo "  Windows run: $windows_run"
echo ""

# ── Map artifact names to final filenames ─────────────────────────────────────
# artifact-name → final-filename
declare -A WANT=(
  ["release-macos-arm64"]="AKDirectPrint_arm64.dmg"
  ["release-windows-x64"]="AKDirectPrint_Setup.exe"
  ["release-windows-x86"]="AKDirectPrint_Setup_x86.exe"
)

for RUN_ID in "$macos_run" "$windows_run"; do
  [[ -z "$RUN_ID" ]] && continue
  ARTIFACTS=$(api "https://api.github.com/repos/$REPO/actions/runs/$RUN_ID/artifacts")
  echo "$ARTIFACTS" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for a in data.get('artifacts', []):
    print(a['id'], a['name'])
" | while read -r AID ANAME; do
    FINAL="${WANT[$ANAME]:-}"
    [[ -z "$FINAL" ]] && continue
    echo "Downloading  $ANAME  →  $FINAL ..."
    curl -sL -H "Authorization: token $TOKEN" \
      "https://api.github.com/repos/$REPO/actions/artifacts/$AID/zip" \
      -o "$TMP/$AID.zip"
    unzip -q -o "$TMP/$AID.zip" -d "$TMP/$AID"
    SRC=$(find "$TMP/$AID" -type f | head -1)
    cp "$SRC" "$STATIC/$FINAL"
    echo "  ✓  $FINAL ($(du -h "$STATIC/$FINAL" | cut -f1))"
  done
done

echo ""
echo "Done. Current static/agent/ contents:"
ls -lh "$STATIC"
