#!/bin/bash
# Build dist/Murmur.app and dist/Murmur-<version>-macOS-arm64.zip
# Usage: packaging/build.sh <path-to-uv-binary> [version]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UV_BIN="${1:?path to the uv binary (aarch64-apple-darwin)}"
VERSION="${2:-0.1.0}"
APP="$ROOT/dist/Murmur.app"
rm -rf "$ROOT/dist" && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/native"
R="$APP/Contents/Resources"

sed "s/VERSION/$VERSION/g" "$ROOT/packaging/Info.plist" > "$APP/Contents/Info.plist"
cp "$ROOT/packaging/launcher.sh" "$APP/Contents/MacOS/Murmur"
chmod +x "$APP/Contents/MacOS/Murmur"

cp -R "$ROOT/server" "$ROOT/web" "$R/"
find "$R" -name __pycache__ -prune -exec rm -rf {} +
cp "$ROOT/packaging/bootstrap.py" "$ROOT/requirements.lock" "$ROOT/packaging/THIRD_PARTY_NOTICES.md" "$R/"
cp "$UV_BIN" "$R/uv" && chmod +x "$R/uv"
swiftc -O -target arm64-apple-macos13.0 -o "$R/native/syscap" "$ROOT/native/syscap.swift"

# icon
T="$(mktemp -d)"; ICON="$T/Murmur.iconset"; mkdir -p "$ICON"
"$ROOT/.venv/bin/python" "$ROOT/packaging/make_icon.py" "$T/icon.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$T/icon.png" --out "$ICON/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$T/icon.png" --out "$ICON/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICON" -o "$R/Murmur.icns"
rm -rf "$T"

# ad-hoc signature (no Apple Developer ID): inner binaries first, then the bundle
codesign --force -s - "$R/uv" "$R/native/syscap"
codesign --force -s - "$APP"
codesign --verify --deep --strict "$APP"

ZIP="$ROOT/dist/Murmur-$VERSION-macOS-arm64.zip"
ditto -c -k --norsrc --noextattr --keepParent "$APP" "$ZIP"
echo "built $APP"
cp "$ZIP" "$ROOT/dist/Murmur-macOS-arm64.zip"   # stable name for releases/latest/download links
echo "built $ZIP ($(du -h "$ZIP" | cut -f1))"
