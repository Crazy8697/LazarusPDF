"""Locate the Tesseract binary.

Search order (first hit wins):
  1. LAZARUS_TESSERACT env var (full path to tesseract.exe)
  2. tesseract\\ bundled next to the app (PyInstaller _MEIPASS or the exe's folder)
  3. Program Files / LocalAppData installs (UB-Mannheim installer default)
  4. tesseract on PATH
"""
import os
import shutil
import sys
from pathlib import Path



def _candidates():
    env = os.environ.get("LAZARUS_TESSERACT")
    if env:
        yield Path(env)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield Path(meipass) / "tesseract" / "tesseract.exe"
    if getattr(sys, "frozen", False):
        yield Path(sys.executable).parent / "tesseract" / "tesseract.exe"
    yield Path(__file__).resolve().parent.parent / "tesseract" / "tesseract.exe"
    for var in ("ProgramFiles", "ProgramFiles(x86)"):
        pf = os.environ.get(var)
        if pf:
            yield Path(pf) / "Tesseract-OCR" / "tesseract.exe"
    lad = os.environ.get("LOCALAPPDATA")
    if lad:
        yield Path(lad) / "Programs" / "Tesseract-OCR" / "tesseract.exe"
    which = shutil.which("tesseract")
    if which:
        yield Path(which)


def find_tesseract():
    for c in _candidates():
        if c.is_file():
            return c
    return None


def _windowless_subprocess():
    """pytesseract's view of `subprocess`, with every launch windowless (CREATE_NO_WINDOW).
    tesseract.exe is a console program: started from the windowed exe (which has no console),
    Windows would give it a console of its own, and on Windows 11 that is a visible terminal
    window; pytesseract only asks for it hidden (which a terminal can ignore), and its version
    and language queries don't even do that."""
    import subprocess
    import types
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    def quiet(fn):
        def run(*a, **k):
            k["creationflags"] = k.get("creationflags", 0) | flag
            return fn(*a, **k)
        return run
    shim = types.SimpleNamespace(**{n: getattr(subprocess, n) for n in dir(subprocess) if not n.startswith("__")})
    for name in ("Popen", "run", "call", "check_call", "check_output"):
        setattr(shim, name, quiet(getattr(subprocess, name)))
    return shim


def configure():
    """Point pytesseract at the binary. Returns (path, version). Raises if missing."""
    import pytesseract                      # (imported here: start-up doesn't need it)
    if sys.platform == "win32" and getattr(pytesseract.pytesseract.subprocess, "__name__", "") == "subprocess":
        pytesseract.pytesseract.subprocess = _windowless_subprocess()     # no console window, ever
    exe = find_tesseract()
    if exe is None:
        raise RuntimeError(
            "Tesseract not found. Install it (winget install UB-Mannheim.TesseractOCR) "
            "or set LAZARUS_TESSERACT to tesseract.exe."
        )
    pytesseract.pytesseract.tesseract_cmd = str(exe)
    tessdata = exe.parent / "tessdata"
    if tessdata.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(tessdata)
    return exe, str(pytesseract.get_tesseract_version())
