"""Startup and import hygiene.

The py2app Tcl/Tk fix-up must run before tkinter is first imported, or a
bundled app looks for Tcl's script library at the build machine's Homebrew
path and fails to start on anyone else's Mac. And PyMuPDF's deprecated
`fitz` alias must never be loaded.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(code: str) -> str:
    result = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_tcl_tk_fixup_runs_before_tkinter_is_imported():
    out = _run(
        "import sys\n"
        "import pdfcore.frozen as frozen\n"
        "calls = []\n"
        "real = frozen.configure_bundled_tcl_tk\n"
        "def spy(*a, **k):\n"
        "    calls.append('tkinter' in sys.modules or '_tkinter' in sys.modules)\n"
        "    return real(*a, **k)\n"
        "frozen.configure_bundled_tcl_tk = spy\n"
        "import editor\n"
        "print(calls)\n")
    assert out == "[False]"


def test_engine_does_not_need_tk():
    out = _run("import sys, textedit; print('tkinter' in sys.modules)")
    assert out == "False"


def test_uses_pymupdf_not_the_deprecated_fitz_alias():
    out = _run("import sys, editor; "
               "print('pymupdf' in sys.modules, 'fitz' in sys.modules)")
    assert out == "True False"
