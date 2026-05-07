#!/usr/bin/env bash
set -euo pipefail

APP_NAME="UpstreamKit"
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-build.txt

rm -rf build "dist/${APP_NAME}.app" "dist/${APP_NAME}.dmg" "dist/dmg"

python -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name "${APP_NAME}" \
  api_relay_gui.py

mkdir -p "dist/dmg"
cp -R "dist/${APP_NAME}.app" "dist/dmg/"

hdiutil create \
  -volname "${APP_NAME}" \
  -srcfolder "dist/dmg" \
  -ov \
  -format UDZO \
  "dist/${APP_NAME}.dmg"

echo ""
echo "Build complete:"
echo "  dist/${APP_NAME}.app"
echo "  dist/${APP_NAME}.dmg"
