#!/usr/bin/env bash
# Download the latest successful CI release artifacts and place them directly
# into the Odoo module's static/agent/ folder with the correct filenames.
#
# Usage:  ./fetch_builds.sh
# Requires: gh CLI logged in  (gh auth status)

set -euo pipefail

REPO="aktiv-bhavesh-kumavat/ak_direct_print_ci_test"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATIC="$(cd "$SCRIPT_DIR/../../aktiv_contributions_19/ak_direct_print/static/agent" && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Target folder: $STATIC"
echo "Repo:          $REPO"
echo ""

# ── Artifact map: <artifact-name-in-CI>  <final-filename-in-static/agent>
declare -A ARTIFACTS=(
  ["release-macos-arm64"]="AKDirectPrint_arm64.dmg"
  ["release-windows-x64"]="AKDirectPrint_Setup.exe"
  ["release-windows-x86"]="AKDirectPrint_Setup_x86.exe"
)

for artifact in "${!ARTIFACTS[@]}"; do
  final="${ARTIFACTS[$artifact]}"
  dl_dir="$TMP/$artifact"
  mkdir -p "$dl_dir"

  echo "Downloading  $artifact  →  $final ..."
  if gh run download --repo "$REPO" --name "$artifact" -D "$dl_dir" 2>/dev/null; then
    # The downloaded file keeps the name we gave it in the workflow
    src=$(find "$dl_dir" -type f | head -1)
    if [ -z "$src" ]; then
      echo "  WARNING: artifact downloaded but no file found — skipping"
      continue
    fi
    cp "$src" "$STATIC/$final"
    echo "  ✓  $STATIC/$final"
  else
    echo "  WARNING: artifact '$artifact' not found in the latest run — skipping"
  fi
done

echo ""
echo "Done. Current static/agent/ contents:"
ls -lh "$STATIC"
