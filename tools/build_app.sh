#!/usr/bin/env bash
# Build Vigil.app — a double-clickable wrapper around the window GUI.
# Bakes this project's absolute path into the launcher so the .app works even if
# you move it to /Applications. Run via "Install Vigil.command" (or directly).
set -euo pipefail

PROJECT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP="$PROJECT/Vigil.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Vigil</string>
  <key>CFBundleDisplayName</key><string>Vigil</string>
  <key>CFBundleIdentifier</key><string>id.val.vigil</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleExecutable</key><string>Vigil</string>
  <key>CFBundleIconFile</key><string>Vigil</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSBluetoothAlwaysUsageDescription</key><string>Vigil watches your paired device's Bluetooth signal so it can lock the screen when you walk away.</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict></plist>
PLIST

# Launcher (PROJECT is baked in at build time; other vars stay runtime).
cat > "$APP/Contents/MacOS/Vigil" <<LAUNCH
#!/bin/bash
PROJECT="$PROJECT"
PY="\$PROJECT/.venv/bin/python3"
if [ ! -x "\$PY" ]; then
  osascript -e 'display alert "Vigil needs setup" message "Open the Vigil folder and run \"Install Vigil.command\" first." as critical'
  exit 1
fi
exec "\$PY" "\$PROJECT/vigil.py" "\$@"
LAUNCH
chmod +x "$APP/Contents/MacOS/Vigil"

printf 'APPL????' > "$APP/Contents/PkgInfo"

if [ -f "$PROJECT/assets/Vigil.icns" ]; then
  cp "$PROJECT/assets/Vigil.icns" "$APP/Contents/Resources/Vigil.icns"
fi

touch "$APP"                      # nudge Finder to refresh the icon
echo "built $APP"
