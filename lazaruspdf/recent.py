"""Stage 18c: File › Open Recent. A manual pick list of the PDFs most recently opened in the PDF
Editor; nothing is reopened automatically (no session restore).

The list lives in the user's own settings store (QSettings, key "recent_files", a JSON list of
absolute paths, newest first, at most MAX). It is fed by PdfEditorMode.docOpened, which fires
once a PDF has opened as a tab: Open PDF (Ctrl+O), a dropped PDF, a PDF given on the command
line or handed over by another launch (the default-reader double-click), and Open Recent
itself. Untitled documents (Paste Pages as New PDF, images, scans) have no file and never count.
A file that is gone shows greyed (kept: the drive may come back); "Clear recent files" empties
the list."""
import json
import os

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMenu

KEY = "recent_files"
MAX = 10


def entries(settings):
    try:
        v = json.loads(settings.value(KEY, "[]", type=str) or "[]")
        return [str(p) for p in v if isinstance(p, str)][:MAX]
    except Exception:
        return []


def _store(settings, paths):
    settings.setValue(KEY, json.dumps(paths[:MAX]))


def add(settings, path):
    p = os.path.normpath(os.path.abspath(path))
    key = os.path.normcase(p)
    rest = [x for x in entries(settings) if os.path.normcase(x) != key]
    _store(settings, [p] + rest)


def remove(settings, path):
    key = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    _store(settings, [x for x in entries(settings) if os.path.normcase(x) != key])


def clear(settings):
    settings.remove(KEY)


class RecentMenu(QObject):
    """File › Open Recent: rebuilt each time it opens."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.menu = QMenu("Open Recent", win)
        self.menu.setToolTipsVisible(True)
        self.menu.aboutToShow.connect(self.rebuild)
        self.ed.docOpened.connect(lambda p: add(self.win.settings, p))
        self.rebuild()

    def rebuild(self):
        m = self.menu
        m.clear()
        paths = entries(self.win.settings)
        for i, p in enumerate(paths, 1):
            name = os.path.basename(p).replace("&", "&&")          # (a literal & in a menu label)
            a = m.addAction(f"&{i % 10}  {name}")                     # 1..9, 0: keyboard accelerators
            a.setData(p)
            if os.path.isfile(p):
                a.setToolTip(p)
                a.triggered.connect(lambda _=False, p=p: self.open(p))
            else:
                a.setEnabled(False)
                a.setToolTip(f"{p}\n(not found: moved, renamed, or on a drive that isn't connected)")
        if not paths:
            m.addAction("(no recent files)").setEnabled(False)
        m.addSeparator()
        clear_act = m.addAction("Clear recent files")
        clear_act.setEnabled(bool(paths))
        clear_act.triggered.connect(self.clear)

    def open(self, p):
        if not os.path.isfile(p):
            self.win.statusBar().showMessage("That file isn't there any more; it was taken off the recent list.", 5000)
            remove(self.win.settings, p)
            return False
        self.win.set_mode(self.ed.key)
        self.ed.open_pdf(p, wait=False)
        return True

    def clear(self):
        clear(self.win.settings)
        self.rebuild()
        self.win.statusBar().showMessage("Recent files cleared.", 3000)
