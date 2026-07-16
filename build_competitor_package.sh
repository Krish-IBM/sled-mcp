#!/usr/bin/env bash
# Build the sled-competitor-analysis-agent Lambda zip WITHOUT Docker.
#
# Uses pip's --platform to fetch Linux (manylinux) wheels for the Lambda runtime
# so this works from macOS. Bundles competitor_analysis/ AND scoring_agent/
# (document ingestion + Bedrock client are imported from it). Produces
# build/competitor_analysis.zip.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
PKG="$BUILD/ca_pkg"
PY_VERSION="${PY_VERSION:-3.12}"
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"

echo ">> Cleaning $PKG"
rm -rf "$PKG" "$BUILD/competitor_analysis.zip"
mkdir -p "$PKG"

echo ">> Installing deps ($PLATFORM, py$PY_VERSION) into $PKG"
python3 -m pip install \
  --platform "$PLATFORM" \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --target "$PKG" \
  -r "$HERE/competitor_analysis/requirements-ca.txt"

echo ">> Copying competitor_analysis + scoring_agent packages"
cp -R "$HERE/competitor_analysis" "$PKG/competitor_analysis"
cp -R "$HERE/scoring_agent" "$PKG/scoring_agent"
rm -f "$PKG/competitor_analysis/requirements-ca.txt" \
      "$PKG/scoring_agent/requirements-scoring.txt"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true

echo ">> Zipping"
( cd "$PKG" && zip -q -r -X "$BUILD/competitor_analysis.zip" . )
SIZE=$(du -h "$BUILD/competitor_analysis.zip" | cut -f1)
echo ">> Built $BUILD/competitor_analysis.zip ($SIZE)"
