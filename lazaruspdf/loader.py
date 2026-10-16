"""Background loading of a PDF for a new tab, so a big file never blocks the window.

DocLoader (a QThread) does the slow part of opening off the UI thread, in small steps:
  1. read the file in chunks (progress by bytes; the read releases the GIL),
  2. parse it (PyMuPDF open) and, unless it needs a password, measure every page's size
     (progress by pages) so the canvas can lay pages out without touching them.
PyMuPDF holds the GIL inside each call, so the work is split into many short calls; the UI
thread gets its turn between them. Cancel stops at the next step; whatever was opened is
closed and nothing is emitted.

A password-protected file stops after step 1 (needPassword): the UI asks on the UI thread,
authenticates, and starts measure() with the opened document.

The document object is only touched by the loader until `ready` is emitted; then it belongs
to the UI thread (WorkingDoc)."""
import os
import time

import pymupdf
from PySide6.QtCore import QThread, Signal

from . import locked

CHUNK = 4 * 1024 * 1024        # file read step
SLOW_STEP = 0.0                # test hook: seconds to sleep per step (simulates a slow disk / file)


class Cancelled(Exception):
    pass


class DocLoader(QThread):
    progress = Signal(str, int, int)          # phase ("read" | "parse" | "pages"), done, total
    needPassword = Signal(bytes)              # file bytes; the UI asks and calls measure()
    ready = Signal(object, object, object, bytes)   # doc, lock, page rects, file bytes
    failed = Signal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path
        self._cancel = False
        self._doc = None              # set when only measuring (password files)
        self._lock = None
        self._data = None

    def cancel(self):
        self._cancel = True

    def measure(self, doc, lock, data):
        """Second run for a password-protected file: doc is opened and authenticated."""
        self._doc, self._lock, self._data = doc, lock, data
        self.start()

    def _check(self):
        if SLOW_STEP:
            time.sleep(SLOW_STEP)
        if self._cancel:
            raise Cancelled()

    def run(self):
        doc = None
        try:
            if self._doc is None:
                total = os.path.getsize(self.path)
                parts, done = [], 0
                with open(self.path, "rb") as f:
                    while True:
                        self._check()
                        b = f.read(CHUNK)
                        if not b:
                            break
                        parts.append(b)
                        done += len(b)
                        self.progress.emit("read", done, total)
                data = b"".join(parts)
                del parts
                self.progress.emit("parse", 0, 0)
                self._check()
                doc = pymupdf.open(stream=data, filetype="pdf")
                if doc.needs_pass:           # (needs_pass only before authenticate: locked.py)
                    doc.close()
                    doc = None
                    if not self._cancel:
                        self.needPassword.emit(data)
                    return
                lock = locked.lock_for(doc)
            else:
                doc, lock, data = self._doc, self._lock, self._data
            n = doc.page_count
            rects = []
            for i in range(n):
                if i % 8 == 0:
                    self._check()
                    self.progress.emit("pages", i, n)
                rects.append(doc[i].rect)     # already reflects /Rotate
            doc._reset_page_refs()
            self._check()
            self.progress.emit("pages", n, n)
            self.ready.emit(doc, lock, rects, data)
            doc = None                        # handed over
        except Cancelled:
            pass
        except Exception as e:                # shown on screen only, never logged
            if not self._cancel:
                self.failed.emit(str(e))
        finally:
            if doc is not None and not doc.is_closed:
                doc.close()
