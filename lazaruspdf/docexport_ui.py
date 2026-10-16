"""Stage 18b UI: File › Export › PDF / EPUB / Markdown / HTML / Plain text for the open PDF (its
working copy, unsaved edits included). Word (.docx) is stage 16 (todocx_ui) in the same menu.

Before exporting:
  - text-bearing formats (EPUB, Markdown, HTML, text) from a PDF whose permissions forbid copying
    content, opened without its owner password, are blocked with the reason (the stage 16 rule);
  - text-bearing formats from a password-protected PDF: the output can't carry the password, so
    the user is warned first (as for Word);
  - PDF follows Save As exactly: the same password choice (the "unknown owner password" warning
    included) and the same writer (workdoc.write_pdf); unlike Save As it doesn't become where
    Ctrl+S saves, and the document stays "unsaved" if it was.
The location is always asked for: a NEW file, never an open original or protected file
(protect.py). The export runs in a worker process (docexport.worker_main) with progress and
Cancel; a cancelled or failed export leaves nothing behind."""
import json
import multiprocessing
import os
import subprocess
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel,
                               QLineEdit, QMessageBox, QProgressDialog)

from . import APP_NAME
from . import docexport as X

SETTINGS_KEY = "export_doc"
ICONS = {"pdf": "file-pdf-box", "epub": "book-open-page-variant-outline", "md": "language-markdown-outline",
         "html": "language-html5", "txt": "file-document-outline"}
TIPS = {
    "pdf": "A new PDF of the document as it is now (unsaved edits included), with the same password choice as "
           "Save As. A page range exports only those pages.",
    "epub": "A reflowable e-book: chapters from the bookmarks (or detected headings), text flowed, images carried. "
            "Best effort: tables and multi-column pages flow as plain text; a page without text comes as a picture.",
    "md": "Markdown: headings (detected by size / weight), paragraphs and lists. Best effort: no tables, no "
          "inline emphasis, images left out.",
    "html": "One self-contained HTML file: each page's positioned text, with its images inlined.",
    "txt": "Plain text in reading order, optionally with page markers.",
}
NOTES = {"epub": TIPS["epub"], "md": TIPS["md"],
         "html": "Images are inlined into the one file (data URIs); no folder next to it."}


class ExportDialog(QDialog):
    def __init__(self, parent, fmt, o):
        super().__init__(parent)
        label = X.FORMATS[fmt][0]
        self.setWindowTitle(f"Export as {label}")
        f = QFormLayout(self)
        self.range = QLineEdit(o.get("range", "all"))
        self.range.setPlaceholderText("all, or e.g. 1-3, 5")
        f.addRow("Pages", self.range)
        self.marks = QCheckBox("Mark where each page starts")
        self.marks.setChecked(bool(o.get("page_marks", True)))
        if fmt == "txt":
            f.addRow(self.marks)
        self.after = QComboBox()
        for k, lab in (("folder", "Show it in its folder"), ("open", "Open it"), ("none", "Nothing")):
            self.after.addItem(lab, k)
        self.after.setCurrentIndex(max(0, self.after.findData(o.get("after", "folder"))))
        f.addRow("Afterwards", self.after)
        if fmt in NOTES:
            note = QLabel(NOTES[fmt])
            note.setObjectName("legend")
            note.setWordWrap(True)
            f.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Choose file…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        return {"range": self.range.text().strip() or "all", "page_marks": self.marks.isChecked(),
                "after": self.after.currentData()}


def ask_options(parent, fmt, o):
    d = ExportDialog(parent, fmt, o)
    return d.values() if d.exec() == QDialog.Accepted else None


class ExportController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.job = None
        self.last = None                          # what the last export reported (tests)
        from .stage10_ui import realize
        self.actions = {}
        for fmt, (label, ext, _flt) in X.FORMATS.items():
            a = realize(self.ed, f"export_{fmt}", ICONS[fmt], f"<b>{label} ({ext})</b><br>{TIPS[fmt]}",
                        handler=lambda fmt=fmt: self.export(fmt))
            a.setText({"txt": "Plain text (.txt)…"}.get(fmt, f"{label} ({ext})…"))
            self.actions[fmt] = a
        self._timer = QTimer(self, interval=40)
        self._timer.timeout.connect(self._poll)

    def options(self, fmt):
        o = {"range": "all", "page_marks": True, "after": "folder"}
        try:
            o.update(json.loads(self.win.settings.value(SETTINGS_KEY, "", type=str) or "{}").get(fmt, {}))
        except Exception:
            pass
        return o

    def _remember(self, fmt, o):
        try:
            allv = json.loads(self.win.settings.value(SETTINGS_KEY, "", type=str) or "{}")
        except Exception:
            allv = {}
        allv[fmt] = o
        self.win.settings.setValue(SETTINGS_KEY, json.dumps(allv))

    def export(self, fmt, options=None, dest=None, unprotected_ok=None, wait=False, test_delay=0.0):
        """options / dest / unprotected_ok answer the questions a real user gets (tests); None asks."""
        from . import protect
        from . import stage10 as T
        from .todocx_ui import copy_blocked
        work, s = self.ed.work, self.ed.cur
        if work is None or self.job is not None or fmt not in X.FORMATS:
            return False
        label, ext, flt = X.FORMATS[fmt]
        save_kwargs = None
        if fmt in X.TEXT_FORMATS:
            why = copy_blocked(work, label)
            if why:
                QMessageBox.warning(self.win, APP_NAME, why)
                self.last = {"ok": False, "why": "blocked"}
                return False
            if work.lock is not None or work.protection is not None:
                if unprotected_ok is None:
                    r = QMessageBox.question(self.win, APP_NAME,
                                             f"This PDF is password-protected. {label} can't carry its password: the "
                                             "exported file will NOT be protected.\n\nExport anyway?",
                                             QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
                    unprotected_ok = r == QMessageBox.Yes
                if not unprotected_ok:
                    self.last = {"ok": False, "why": "unprotected-declined"}
                    return False
        else:                                     # PDF: Save As's password choice, exactly
            if work.lock is not None and work.protection is work.lock:
                ok, prot = self.ed._resolve_protection("keep", explicit=False)
                if not ok:
                    return False
            else:
                prot = work.protection
            save_kwargs = work.save_options(prot)
        o = self.options(fmt)
        if options is None:
            options = ask_options(self.win, fmt, o)
            if options is None:
                return False
        o.update(options)
        try:
            pages = T.parse_range(o["range"], self.ed.doc.page_count)
        except ValueError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return False
        self._remember(fmt, o)
        while dest is None:
            stem = s.source.stem + ("-exported" if fmt == "pdf" else "")
            p, _ = QFileDialog.getSaveFileName(self.win, f"Export as {label} (a new file)",
                                               str(Path.home() / f"{stem}{ext}"), flt)
            if not p:
                return False
            if not p.lower().endswith(ext):
                p += ext
            if protect.is_protected(p) or any(x.is_source(p) for x in self.ed.sessions if not x.is_job):
                QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
                continue
            dest = p
        if protect.is_protected(dest) or any(x.is_source(dest) for x in self.ed.sessions if not x.is_job):
            QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
            self.last = {"ok": False, "why": "protected"}
            return False
        spec = {"fmt": fmt, "data": work.plain_bytes(), "dest": os.path.abspath(dest), "pages": pages,
                "options": o, "title": s.source.stem, "save_kwargs": save_kwargs, "test_delay": test_delay}
        return self._start(spec, label, o.get("after", "none"), wait)

    def _start(self, spec, label, after, wait):
        ctx = multiprocessing.get_context("spawn")
        conn, send = ctx.Pipe(duplex=False)
        cancel = ctx.Event()
        proc = ctx.Process(target=X.worker_main, args=(spec, send, cancel), daemon=True)
        proc.start()
        send.close()
        dlg = QProgressDialog("Starting…", "Cancel", 0, 100, self.win)
        dlg.setWindowTitle(f"Export as {label}")
        dlg.setWindowModality(Qt.NonModal)
        dlg.setMinimumDuration(400)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(cancel.set)
        self.job = {"proc": proc, "conn": conn, "cancel": cancel, "dlg": dlg, "dest": spec["dest"], "after": after,
                    "label": label, "last": time.perf_counter(), "gap": 0.0, "seen": [], "t0": time.time()}
        self.last = None
        self._timer.start()
        if wait:
            from PySide6.QtCore import QEventLoop
            t0 = time.time()
            while self.job is not None and time.time() - t0 < 900:
                loop = QEventLoop()
                QTimer.singleShot(15, loop.quit)
                loop.exec()
            return bool(self.last and self.last.get("ok"))
        return True

    def cancel(self):
        if self.job is not None:
            self.job["cancel"].set()

    def _poll(self):
        j = self.job
        if j is None:
            return
        now = time.perf_counter()
        j["gap"] = max(j["gap"], now - j["last"])
        j["last"] = now
        try:
            while j["conn"].poll():
                msg = j["conn"].recv()
                if msg[0] == "progress":
                    _k, done, total = msg
                    j["seen"].append((done, total))
                    j["dlg"].setMaximum(max(1, total))
                    j["dlg"].setValue(done)
                    j["dlg"].setLabelText(f"Exporting… page {min(done + 1, total)} of {total}")
                    continue
                self._finish(msg)
                return
        except (EOFError, OSError):
            self._finish(("failed", "the export stopped unexpectedly"))
            return
        if not j["proc"].is_alive() and not j["conn"].poll():
            self._finish(("failed", "the export stopped unexpectedly"))

    def _finish(self, msg):
        j, self.job = self.job, None
        self._timer.stop()
        j["dlg"].close()
        j["dlg"].deleteLater()                  # (a closed, never-shown progress dialog would still pop up later)
        j["proc"].join(5)
        if j["proc"].is_alive():
            j["proc"].terminate()
        try:
            j["conn"].close()
        except OSError:
            pass
        self.last = {"ok": msg[0] == "done", "why": msg[0], "gap_ms": round(j["gap"] * 1000),
                     "seconds": round(time.time() - j["t0"], 2), "progress": list(j["seen"]), "dest": j["dest"]}
        if msg[0] == "cancelled":
            self.win.statusBar().showMessage(f"{j['label']} export cancelled: nothing was written.", 5000)
            return
        if msg[0] == "failed":
            QMessageBox.warning(self.win, APP_NAME, f"The {j['label']} export didn't work:\n{msg[1]}")
            return
        self.win.statusBar().showMessage(f"Exported {msg[1]} page(s) as {j['label']}.", 8000)
        if j["after"] == "open":
            try:
                os.startfile(j["dest"])             # noqa: S606 (the user's own new file)
            except OSError:
                pass
        elif j["after"] == "folder":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(j["dest"])])
