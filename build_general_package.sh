#!/usr/bin/env bash
# Build the sled-general-agent Lambda zip WITHOUT Docker.
#
# Uses pip's --platform to fetch Linux (manylinux) wheels for the Lambda runtime
# so this works from macOS. Bundles general_agent/ AND scoring_agent/ (the
# BedrockClient wrapper is imported from it). Produces build/general_agent.zip.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
PKG="$BUILD/general_pkg"
PY_VERSION="${PY_VERSION:-3.12}"
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"

echo ">> Cleaning $PKG"
rm -rf "$PKG" "$BUILD/general_agent.zip"
mkdir -p "$PKG"

echo ">> Installing deps ($PLATFORM, py$PY_VERSION) into $PKG"
python3 -m pip install \
  --platform "$PLATFORM" \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --target "$PKG" \
  -r "$HERE/general_agent/requirements-general.txt"

echo ">> Copying general_agent + scoring_agent packages"
cp -R "$HERE/general_agent" "$PKG/general_agent"
cp -R "$HERE/scoring_agent" "$PKG/scoring_agent"
rm -f "$PKG/general_agent/requirements-general.txt" \
      "$PKG/scoring_agent/requirements-scoring.txt"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true

echo ">> Zipping"
( cd "$PKG" && zip -q -r -X "$BUILD/general_agent.zip" . )
SIZE=$(du -h "$BUILD/general_agent.zip" | cut -f1)
echo ">> Built $BUILD/general_agent.zip ($SIZE)"
