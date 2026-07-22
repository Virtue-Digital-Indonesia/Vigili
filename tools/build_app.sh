#!/usr/bin/env bash
# Build Vigili.app — a REAL, self-contained macOS app via py2app.
#
# Unlike the old shell-wrapper, this bundles its own Python + pyobjc, so the app
# runs without the project folder or a .venv present, shows up as "Vigili" (not
# "Python") in the menu bar / Force-Quit, and can be handed to someone else.
#
# Output: ./Vigili.app   (drag it to /Applications, or double-click in place)
set -euo pipefail

PROJECT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$PROJECT"

PY="$PROJECT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

# py2app is a build-time-only dependency (the finished app doesn't need it).
if ! "$PY" -c 'import py2app' >/dev/null 2>&1; then
  echo "· installing py2app (build tool)…"
  "$PY" -m pip install --quiet 'py2app>=0.28'
fi

# Generate the icon if it isn't already committed.
if [ ! -f assets/Vigili.icns ]; then
  echo "· rendering app icon…"
  "$PY" tools/make_icon.py assets/Vigili.iconset >/dev/null &&
    iconutil -c icns assets/Vigili.iconset -o assets/Vigili.icns
fi

echo "· building Vigili.app (py2app)…"
rm -rf build dist "$PROJECT/Vigili.app"
"$PY" setup.py py2app >/dev/null

# py2app ad-hoc-signs on Apple Silicon (required to launch); confirm it's valid.
codesign --verify --deep --strict dist/Vigili.app

mv dist/Vigili.app "$PROJECT/Vigili.app"
rm -rf build dist
touch "$PROJECT/Vigili.app"        # nudge Finder to refresh the icon
SIZE="$(du -sh "$PROJECT/Vigili.app" | cut -f1)"
echo "✅ built $PROJECT/Vigili.app  ($SIZE, ad-hoc signed, self-contained)"
