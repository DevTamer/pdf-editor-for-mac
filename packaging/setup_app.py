"""py2app build script for PDF Editor.

Run it via build_app.sh, which builds in a fresh venv from the pinned
requirements.txt after running the tests. Running it by hand
(`python setup_app.py py2app` from this directory) also works, but it
refuses to build from an environment where pdfcore is an editable install
(e.g. a dev venv set up with `./setup.sh --with-local-pdfcore`): that would
bundle whatever is in your local pdfcore checkout instead of the pinned
release.

Produces dist/PDF Editor.app.

Bundling note: py2app correctly relocates the Tcl/Tk *dylibs* themselves
(libtcl8.6.dylib / libtk8.6.dylib land in Contents/Frameworks, linked via
@executable_path so they work from any install location). What it does
*not* do is bundle Tcl/Tk's script library (init.tcl, tk.tcl, and friends)
that those dylibs need at startup — Homebrew's tcl-tk isn't a macOS
.framework, so py2app has no recipe for it, and Tcl instead falls back to
a hardcoded build-machine path (the Homebrew Cellar) baked into the dylib.
That works on the machine that built it and nowhere else. So this script
also copies Tcl/Tk's `lib/tcl8.6` and `lib/tk8.6` directories into the app
bundle, and editor.py points TCL_LIBRARY/TK_LIBRARY at them when frozen
(via pdfcore.frozen.configure_bundled_tcl_tk).
"""

import json
import os
import subprocess
import sys
from importlib import metadata

from setuptools import setup

APP = ["../editor.py"]

# editor.py imports textedit.py from the repo root; make it findable when
# py2app traces imports from this directory.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def require_pinned_pdfcore() -> None:
    """Refuse to bundle an editable (local checkout) pdfcore."""
    try:
        dist = metadata.distribution("pdfcore")
    except metadata.PackageNotFoundError:
        sys.exit("pdfcore isn't installed; pip install -r ../requirements.txt")
    direct_url = json.loads(dist.read_text("direct_url.json") or "{}")
    if direct_url.get("dir_info", {}).get("editable"):
        sys.exit(
            "pdfcore is an editable install of a local checkout "
            f"({direct_url.get('url')}).\n"
            "Release builds must use the version pinned in requirements.txt "
            "-- run packaging/build_app.sh instead."
        )


require_pinned_pdfcore()


def brew_prefix(formula: str) -> str:
    try:
        return subprocess.check_output(
            ["brew", "--prefix", formula], text=True
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        sys.exit(
            f"Could not resolve Homebrew prefix for '{formula}' ({exc}).\n"
            f"Install it with: brew install {formula}"
        )


def bundle_dir_as_data_files(src_root: str, dest_root: str):
    """Build py2app data_files entries that recreate src_root's directory
    tree under dest_root (relative to Contents/Resources) in the bundle."""
    if not os.path.isdir(src_root):
        sys.exit(f"Expected Tcl/Tk library directory not found: {src_root}")
    entries = []
    for dirpath, _dirnames, filenames in os.walk(src_root):
        if not filenames:
            continue
        rel = os.path.relpath(dirpath, src_root)
        dest = dest_root if rel == "." else os.path.join(dest_root, rel)
        files = [os.path.join(dirpath, f) for f in filenames]
        entries.append((dest, files))
    return entries


TCL_TK_PREFIX = brew_prefix("tcl-tk@8")

DATA_FILES = []
DATA_FILES += bundle_dir_as_data_files(
    os.path.join(TCL_TK_PREFIX, "lib", "tcl8.6"), "tcl/tcl8.6")
DATA_FILES += bundle_dir_as_data_files(
    os.path.join(TCL_TK_PREFIX, "lib", "tk8.6"), "tcl/tk8.6")

OPTIONS = {
    "argv_emulation": False,
    "iconfile": "icon.icns",
    "packages": ["pymupdf", "PIL", "pdfcore", "fontTools"],
    "includes": ["textedit", "erase", "fontmatch", "findreplace"],
    "plist": {
        "CFBundleName": "PDF Editor",
        "CFBundleDisplayName": "PDF Editor",
        "CFBundleIdentifier": "com.devtamer.pdfeditor",
        "CFBundleVersion": "0.2.0",
        "CFBundleShortVersionString": "0.2.0",
        "NSHumanReadableCopyright": "PDF Editor",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "PDF Document",
                "CFBundleTypeRole": "Editor",
                "LSItemContentTypes": ["com.adobe.pdf"],
                "LSHandlerRank": "Alternate",
            }
        ],
    },
}

setup(
    app=APP,
    name="PDF Editor",
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
