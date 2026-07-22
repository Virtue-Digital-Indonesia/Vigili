#!/bin/bash
# ── Vigili one-click setup ──────────────────────────────────────────────
# Double-click this file. (First time: right-click ▸ Open, then click Open,
# because it isn't from the App Store.) It installs what Vigili needs and
# builds Vigili.app, which you can then double-click any time.
# ───────────────────────────────────────────────────────────────────────
cd "$(dirname "$0")" || exit 1
PROJECT="$(pwd)"

echo "Setting up Vigili in:"
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
[ -f assets/Vigili.icns ] || {
  ./.venv/bin/python3 tools/make_icon.py assets/Vigili.iconset >/dev/null 2>&1 &&
  iconutil -c icns assets/Vigili.iconset -o assets/Vigili.icns
}
bash tools/build_app.sh "$PROJECT"

echo
echo "✅ Done. Vigili.app is in the Vigili folder."
osascript <<OSA || true
set r to button returned of (display dialog "Vigili is ready! ✅

Double-click Vigili.app to start the proximity lock.

(The motion alarm needs your password — see \"Read Me First.txt\".)" buttons {"Reveal Vigili.app", "Done"} default button "Reveal Vigili.app" with title "Vigili")
if r is "Reveal Vigili.app" then tell application "Finder" to reveal POSIX file "$PROJECT/Vigili.app"
OSA
