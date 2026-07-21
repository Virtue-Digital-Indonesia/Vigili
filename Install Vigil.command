#!/bin/bash
# ── Vigil one-click setup ──────────────────────────────────────────────
# Double-click this file. (First time: right-click ▸ Open, then click Open,
# because it isn't from the App Store.) It installs what Vigil needs and
# builds Vigil.app, which you can then double-click any time.
# ───────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")" || exit 1
PROJECT="$(pwd)"

echo "Setting up Vigil in:"
echo "  $PROJECT"
echo

if ! command -v python3 >/dev/null 2>&1; then
  osascript -e 'display alert "Python 3 is required" message "Install the Xcode Command Line Tools (run: xcode-select --install) or Python from python.org, then run this again." as critical'
  exit 1
fi

echo "1/3  Creating a private Python environment…"
[ -x ".venv/bin/python3" ] || python3 -m venv .venv
./.venv/bin/python3 -m pip install --upgrade pip >/dev/null 2>&1 || true

echo "2/3  Installing dependencies (this can take a minute)…"
./.venv/bin/python3 -m pip install -r requirements.txt

echo "3/3  Building the app + icon…"
[ -f assets/Vigil.icns ] || {
  ./.venv/bin/python3 tools/make_icon.py assets/Vigil.iconset >/dev/null 2>&1 &&
  iconutil -c icns assets/Vigil.iconset -o assets/Vigil.icns
}
bash tools/build_app.sh "$PROJECT"

echo
echo "✅ Done. Vigil.app is in the Vigil folder."
osascript <<OSA || true
set r to button returned of (display dialog "Vigil is ready! ✅

Double-click Vigil.app to start the proximity lock.

(The motion alarm needs your password — see \"Read Me First.txt\".)" buttons {"Reveal Vigil.app", "Done"} default button "Reveal Vigil.app" with title "Vigil")
if r is "Reveal Vigil.app" then tell application "Finder" to reveal POSIX file "$PROJECT/Vigil.app"
OSA
