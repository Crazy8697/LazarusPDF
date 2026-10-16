"""Start-up timing (stage 12). The clock starts when Windows created the process (so it
includes the exe's own unpacking and Python's start), and "first page" is the moment the
first page of the first opened PDF is painted on the canvas.

Only measured, never logged: LAZARUS_STARTUP_REPORT=<file> writes the timings there as JSON
(numbers only), and LAZARUS_EXIT_AFTER_FIRST_PAGE=1 quits right after (the self-test)."""
import json
import os
import sys
import time


def process_start_epoch():
    """When this process was created (seconds since the epoch), from Windows; else now."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
            if k32.GetProcessTimes(k32.GetCurrentProcess(), ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt),
                                   ctypes.byref(ut)):
                t100 = (c.dwHighDateTime << 32) | c.dwLowDateTime
                return t100 / 1e7 - 11644473600.0          # FILETIME (1601) -> Unix epoch
        except Exception:
            pass
    return time.time()


T0 = process_start_epoch()
marks = {}
_done = False


def mark(name):
    if name not in marks:
        marks[name] = round(time.time() - T0, 3)


def first_page():
    """Called by the canvas when it has painted a page: the first time, the clock stops."""
    global _done
    if _done:
        return
    _done = True
    mark("first_page")
    out = os.environ.get("LAZARUS_STARTUP_REPORT")
    if out:
        try:
            with open(out, "w", encoding="utf-8") as f:
                json.dump(marks, f)
        except OSError:
            pass
    if os.environ.get("LAZARUS_EXIT_AFTER_FIRST_PAGE"):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        QTimer.singleShot(0, lambda: (QApplication.closeAllWindows(), QApplication.quit()))
