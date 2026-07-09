#!/usr/bin/env bash
# Build the sled-scoring-agent Lambda deployment zip WITHOUT Docker.
#
# Uses pip's --platform to fetch Linux (manylinux) wheels for the Lambda runtime
# so this works from macOS. Produces build/scoring_agent.zip.
#
# Usage:  ./build_scoring_package.sh [--template /path/to/Scorecard.pptx]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$HERE/build"
PKG="$BUILD/pkg"
PY_VERSION="${PY_VERSION:-3.12}"
PLATFORM="${PLATFORM:-manylinux2014_x86_64}"
TEMPLATE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --template) TEMPLATE="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

echo ">> Cleaning $PKG"
rm -rf "$PKG" "$BUILD/scoring_agent.zip"
mkdir -p "$PKG"

echo ">> Installing deps ($PLATFORM, py$PY_VERSION) into $PKG"
python3 -m pip install \
  --platform "$PLATFORM" \
  --python-version "$PY_VERSION" \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --target "$PKG" \
  -r "$HERE/scoring_agent/requirements-scoring.txt"

echo ">> Copying scoring_agent package"
cp -R "$HERE/scoring_agent" "$PKG/scoring_agent"
# don't ship the local requirements or caches
rm -f "$PKG/scoring_agent/requirements-scoring.txt"
find "$PKG" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true
find "$PKG" -type d -name "tests" -prune -exec rm -rf {} + 2>/dev/null || true

if [[ -n "$TEMPLATE" ]]; then
  if [[ -f "$TEMPLATE" ]]; then
    echo ">> Bundling PPTX template"
    mkdir -p "$PKG/scoring_agent/assets"
    cp "$TEMPLATE" "$PKG/scoring_agent/assets/template.pptx"
  else
    echo "!! template not found: $TEMPLATE" >&2
  fi
fi

echo ">> Zipping"
( cd "$PKG" && zip -q -r -X "$BUILD/scoring_agent.zip" . )
SIZE=$(du -h "$BUILD/scoring_agent.zip" | cut -f1)
echo ">> Built $BUILD/scoring_agent.zip ($SIZE)"
echo "   (Lambda unzipped limit is 250 MB; if exceeded, move deps to a Lambda layer"
echo "    or switch to a container image.)"
