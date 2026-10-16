"""Stage 16 UI: File › Export › Word document (.docx)… for the open PDF (its working copy, unsaved
edits included). Options (remembered): page range, images, tables, keep columns, and what to do
afterwards (nothing / open the .docx / show it in its folder).

Before converting:
  - a PDF whose permissions forbid copying content, opened without its owner password, is
    blocked (the same rule as editing text, stage 11);
  - a password-protected PDF: a .docx can't carry its password, so the user is warned first;
  - pages without real text (scans): "Run OCR first" (on a TEMPORARY copy for this export only;
    the open PDF and its undo history are untouched) or "Convert anyway" (they come through as
    pictures).
The location is always asked for: a NEW .docx, never an original. The conversion runs in a
worker process (todocx.worker_main) with progress and Cancel; nothing half-written remains."""
import json
import multiprocessing
import os
import subprocess
import time
from pathlib import Path

import pymupdf
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QLabel,
                               QLineEdit, QMessageBox, QProgressDialog)

from . import APP_NAME
from . import todocx as X

SETTINGS_KEY = "export_docx"
GOOD_ENOUGH = ("Paragraphs, fonts where available, tables, images and basic layout come through; complex layouts "
               "may not convert perfectly.")


class WordDialog(QDialog):
    def __init__(self, parent, o):
        super().__init__(parent)
        self.setWindowTitle("Export as Word document")
        f = QFormLayout(self)
        self.range = QLineEdit(o.get("range", "all"))
        self.range.setPlaceholderText("all, or e.g. 1-3, 5")
        f.addRow("Pages", self.range)
        self.images = QCheckBox("Include images")
        self.images.setChecked(bool(o.get("images", True)))
        self.tables = QCheckBox("Detect tables")
        self.tables.setChecked(bool(o.get("tables", True)))
        self.columns = QCheckBox("Keep multi-column layout")
        self.columns.setChecked(bool(o.get("columns", True)))
        for cb in (self.images, self.tables, self.columns):
            f.addRow(cb)
        self.after = QComboBox()
        for k, lab in (("folder", "Show it in its folder"), ("open", "Open the .docx"), ("none", "Nothing")):
            self.after.addItem(lab, k)
        self.after.setCurrentIndex(max(0, self.after.findData(o.get("after", "folder"))))
        f.addRow("Afterwards", self.after)
        note = QLabel(GOOD_ENOUGH)
        note.setObjectName("legend")
        note.setWordWrap(True)
        f.addRow(note)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Choose file…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        return {"range": self.range.text().strip() or "all", "images": self.images.isChecked(),
                "tables": self.tables.isChecked(), "columns": self.columns.isChecked(),
                "after": self.after.currentData()}


def ask_options(parent, o):
    d = WordDialog(parent, o)
    return d.values() if d.exec() == QDialog.Accepted else None


def ask_no_text(parent, n):
    """-> "ocr" | "anyway" | None."""
    box = QMessageBox(parent)
    box.setWindowTitle(APP_NAME)
    box.setText(f"{n} page(s) have no text that can be converted (scans?).\n\n"
                "Run OCR first? It runs on a temporary copy made for this export only: your PDF and its undo "
                "history are not changed. Or convert anyway: those pages come through as pictures.")
    ocr = box.addButton("Run OCR first", QMessageBox.AcceptRole)
    anyway = box.addButton("Convert anyway", QMessageBox.AcceptRole)
    box.addButton(QMessageBox.Cancel)
    box.exec()
    return "ocr" if box.clickedButton() is ocr else "anyway" if box.clickedButton() is anyway else None


def copy_blocked(work, what="Word"):
    """Why exporting the text isn't allowed (owner-only permission, owner password unknown), or None.
    what: the format named in the message (the stage 18b exports use the same rule)."""
    lock = getattr(work, "lock", None)
    if lock is None or lock.owner_known or lock.permissions & pymupdf.PDF_PERM_COPY:
        return None
    return ("This PDF's security settings don't allow copying its content, and its owner password isn't known here, "
            f"so it can't be exported to {what}. Open it again with the owner password to export it.")


class WordController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.job = None
        self.last = None                          # what the last export reported (tests)
        from .stage10_ui import realize
        self.action = realize(self.ed, "export_docx", "file-word-outline",
                              "<b>Word document (.docx)</b><br>Convert this PDF (as it is now, unsaved edits "
                              "included) to an editable Word file. " + GOOD_ENOUGH, handler=lambda: self.export())
        self.action.setText("Word document (.docx)…")
        from .pdfeditor import _icon
        self.ed.export_btn.setIcon(_icon("export-variant"))
        self.ed.export_btn.setToolTip("<b>Export</b><br>PDF, Word (.docx), EPUB, Markdown, HTML or plain text, "
                                      "from the document as it is now (unsaved edits included)")
        self.ed.export_btn.setProperty("soon", False)
        self.ed.export_btn.style().unpolish(self.ed.export_btn)
        self.ed.export_btn.style().polish(self.ed.export_btn)
        self._timer = QTimer(self, interval=40)
        self._timer.timeout.connect(self._poll)

    def options(self):
        o = dict(X.DEFAULTS)
        try:
            o.update(json.loads(self.win.settings.value(SETTINGS_KEY, "", type=str) or "{}"))
        except Exception:
            pass
        return o

    def export(self, options=None, dest=None, no_text=None, unprotected_ok=None, wait=False, test_delay=0.0):
        """options / dest / no_text ("ocr" | "anyway") / unprotected_ok answer the questions a real
        user gets (tests); None asks."""
        from . import protect
        from . import stage10 as T
        work, s = self.ed.work, self.ed.cur
        if work is None or self.job is not None:
            return False
        why = copy_blocked(work)
        if why:
            QMessageBox.warning(self.win, APP_NAME, why)
            self.last = {"ok": False, "why": "blocked"}
            return False
        if work.lock is not None or work.protection is not None:
            if unprotected_ok is None:
                r = QMessageBox.question(self.win, APP_NAME,
                                         "This PDF is password-protected. A Word file can't carry its password: the "
                                         ".docx will NOT be protected.\n\nExport anyway?",
                                         QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
                unprotected_ok = r == QMessageBox.Yes
            if not unprotected_ok:
                self.last = {"ok": False, "why": "unprotected-declined"}
                return False
        o = self.options()
        if options is None:
            options = ask_options(self.win, o)
            if options is None:
                return False
        o.update(options)
        try:
            pages = T.parse_range(o["range"], self.ed.doc.page_count)
        except ValueError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return False
        self.win.settings.setValue(SETTINGS_KEY, json.dumps(o))
        while dest is None:
            start = str(Path.home() / f"{s.source.stem}.docx")
            p, _ = QFileDialog.getSaveFileName(self.win, "Export as Word document (a new file)", start,
                                               "Word documents (*.docx)")
            if not p:
                return False
            if not p.lower().endswith(".docx"):
                p += ".docx"
            if protect.is_protected(p) or s.is_source(p):
                QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
                continue
            dest = p
        if protect.is_protected(dest) or s.is_source(dest):
            QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
            return False
        # pages without text: OCR a temporary copy first, or convert anyway
        plain = pymupdf.open(stream=self._bytes(s), filetype="pdf")
        try:
            empty = X.no_text_pages(plain, pages)
            data = plain.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
        finally:
            plain.close()
        ocr_pages = []
        if empty:
            if no_text is None:
                no_text = ask_no_text(self.win, len(empty))
            if no_text is None:
                return False
            if no_text == "ocr":
                ocr_pages = empty
        return self._start({"data": data, "dest": os.path.abspath(dest), "pages": pages, "options": o,
                            "ocr_pages": ocr_pages, "test_delay": test_delay}, o.get("after", "none"), wait)

    def _bytes(self, s):
        w = s.work
        data = getattr(s, "data", None)
        if data is not None and w.lock is None and w.step() == 0 and not w._base:
            return data
        return w.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)

    def _start(self, spec, after, wait):
        ctx = multiprocessing.get_context("spawn")
        conn, send = ctx.Pipe(duplex=False)
        cancel = ctx.Event()
        proc = ctx.Process(target=X.worker_main, args=(spec, send, cancel), daemon=True)
        proc.start()
        send.close()
        dlg = QProgressDialog("Starting…", "Cancel", 0, 100, self.win)
        dlg.setWindowTitle("Export as Word document")
        dlg.setWindowModality(Qt.NonModal)
        dlg.setMinimumDuration(400)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(cancel.set)
        self.job = {"proc": proc, "conn": conn, "cancel": cancel, "dlg": dlg, "dest": spec["dest"], "after": after,
                    "last": time.perf_counter(), "gap": 0.0, "seen": [], "t0": time.time(),
                    "ocr": len(spec["ocr_pages"])}
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
                    if total < 0:
                        j["dlg"].setLabelText(f"OCR (temporary copy) page {-done} of {-total}…")
                    else:
                        j["dlg"].setMaximum(total)
                        j["dlg"].setValue(done)
                        j["dlg"].setLabelText(f"Converting… {(done + 1) // 2 if total else 0} of {total // 2} page(s)")
                    continue
                self._finish(msg)
                return
        except (EOFError, OSError):
            self._finish(("failed", "the conversion stopped unexpectedly"))
            return
        if not j["proc"].is_alive() and not j["conn"].poll():
            self._finish(("failed", "the conversion stopped unexpectedly"))

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
            self.win.statusBar().showMessage("Word export cancelled: nothing was written.", 5000)
            return
        if msg[0] == "failed":
            QMessageBox.warning(self.win, APP_NAME, f"The Word export didn't work:\n{msg[1]}")
            return
        self.win.statusBar().showMessage(f"Exported {msg[1]} page(s) to a Word document"
                                         + (" (OCR on a temporary copy first)" if j["ocr"] else "") + ".", 8000)
        if j["after"] == "open":
            try:
                os.startfile(j["dest"])             # noqa: S606 (the user's own new file)
            except OSError:
                pass
        elif j["after"] == "folder":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(j["dest"])])
