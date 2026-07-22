"""
py2app build configuration for Vigili — produces a self-contained Vigili.app
(its own embedded Python + pyobjc, so it no longer shows up as "Python" and
doesn't depend on the project folder or a .venv being present).

    ./.venv/bin/python setup.py py2app        # full standalone build -> dist/
    ./.venv/bin/python setup.py py2app -A     # fast alias build (dev only)

The root MOTION helper deliberately still runs under the *system* python3 — the
SPU accelerometer is root-only and the admin prompt spawns a clean shell that
can't see the app's embedded interpreter. So motion_helper.py and a copy of the
macimu *source* ride along as plain resources for that helper to consume; see
vigili.py:_stage_root_helper / resource_base.
"""
import os

from setuptools import setup

import macimu  # noqa: E402  — used to locate the package source to bundle

MACIMU_SRC = os.path.dirname(macimu.__file__)

APP = ["vigili.py"]

# Copied verbatim into Vigili.app/Contents/Resources/ (found via resource_base()).
DATA_FILES = [
    "assets",            # carbon_ui.html, fonts/, Vigili.icns
    "motion_helper.py",  # staged to the root helper at runtime (needs plain source)
    MACIMU_SRC,          # macimu/ source for the system-python root helper
]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "assets/Vigili.icns",
    # Imported lazily / inside functions, so name them explicitly for modulegraph.
    "packages": ["rumps", "macimu"],
    "includes": [
        "objc", "Foundation", "AppKit", "WebKit", "CoreBluetooth", "Quartz",
        "PyObjCTools", "PyObjCTools.AppHelper",
    ],
    "excludes": ["tkinter"],
    "plist": {
        "CFBundleName": "Vigili",
        "CFBundleDisplayName": "Vigili",
        "CFBundleIdentifier": "id.val.vigili",
        "CFBundleVersion": "1.1",
        "CFBundleShortVersionString": "1.1",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.utilities",
        "LSMinimumSystemVersion": "12.0",
        # Required: the app touches CoreBluetooth; without this string macOS
        # kills it the moment it scans. This is what the TCC prompt shows.
        "NSBluetoothAlwaysUsageDescription":
            "Vigili watches your paired device's Bluetooth signal so it can lock "
            "the screen when you walk away.",
        "NSHumanReadableCopyright":
            "MIT-licensed · Built by Virtue Digital Indonesia",
        # Apps launched from Finder/Dock get an ASCII locale, which breaks any
        # locale-dependent text decoding. Force UTF-8 mode so reads, subprocess
        # text, and stdout are all UTF-8 (belt to the explicit open(encoding=)).
        "LSEnvironment": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    },
}

setup(
    name="Vigili",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
