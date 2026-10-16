"""Stage 10b: batch processing, the UI. File › Batch… asks for the inputs, the pipeline, the
output folder and a name pattern, then the job runs as its own tab in the tab bar (marked as
a job, not a document) while the other tabs stay usable.

The work happens in a worker PROCESS (batch.worker_main): the window never waits on it, also
not while another tab's find index holds the window locked. Its events (indices only, never
file names or contents) are read here on a timer and drive the job view: a progress bar,
"X of Y files", a per-file status list (queued / running / done / failed / cancelled /
skipped), Cancel, and the summary at the end. If the worker dies on a file (a file that
crashes MuPDF), that file is marked failed and a new worker carries on with the rest.

Batch never touches an open working copy: no undo entries anywhere. Passwords typed here are
passed to the worker in memory and never stored; the rest of the setup is remembered."""
import json
import multiprocessing
import os
import time
from pathlib import Path

import pymupdf
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QAction, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import APP_NAME
from . import batch as B

POLL_MS = 50
CANCEL_GRACE = 15.0          # seconds a cancelled worker gets to stop between steps before it is ended
SETUP_KEY = "batch_last"     # remembered setup (no passwords, no image data)
SECRET_KEYS = ("open_pw", "owner_pw", "image_png")

STATUS_TEXT = {"queued": "Queued", "running": "Running", "done": "Done", "failed": "Failed",
               "cancelled": "Cancelled", "skipped": "Skipped"}
STATUS_COLOR = {"done": "#7bd88f", "failed": "#ff8a80", "cancelled": "#ffcc80", "skipped": "#8a94a6",
                "running": "#9fc0ff", "queued": "#8a94a6"}


# ---------------- the job (a tab) ----------------
class JobSession:
    """A batch job in the tab bar. Quacks like tabs.Session where the editor needs it to (no
    document, no canvas, never dirty), and owns its worker process."""
    is_job = True
    ready = False
    work = None
    loader = None
    loading = None
    canvas = None
    doc_version = 0

    def __init__(self, ctrl, number, job):
        from .tabs import Session
        self.sid = next(Session._ids)
        self.ctrl, self.number, self.job = ctrl, number, job
        self.files = [Path(p) for p in job["files"]]
        n = len(self.files)
        self.status = ["queued"] * n
        self.detail = [""] * n
        self.outputs = [[] for _ in range(n)]
        self.find_index = None
        self.find_key = None
        self.thumb_rows, self.thumb_scroll, self.overview_sel, self.bookmarks = [], 0, [], None
        self.proc = None
        self.events = None
        self.cancel_ev = None
        self.started = None
        self.ended = None
        self.cancelling = False
        self.cancel_at = None
        self.ended_by_cancel = False
        self.bates = {}
        self.restarts = 0
        self.seen = []                     # every event, in order (the self-test reads it)
        self.timer = QTimer()
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.view = BatchView(self)

    # ---- what the editor asks of a tab ----
    @property
    def name(self):
        return f"Batch job {self.number}"

    @property
    def owner(self):
        return f"job:{self.sid}"          # (nothing registered with protect.py under it)

    def is_source(self, path):
        return False

    def dirty(self):
        return False

    @property
    def running(self):
        return self.ended is None and self.started is not None

    def counts(self):
        c = {k: 0 for k in STATUS_TEXT}
        for s in self.status:
            c[s] += 1
        return c

    def finished_count(self):
        return sum(1 for s in self.status if s in ("done", "failed", "cancelled", "skipped"))

    def tab_text(self):
        c = self.counts()
        n = len(self.files)
        if self.running:
            return f"{self.name}: {c['done'] + c['failed']} of {n}" + (" (cancelling)" if self.cancelling else "")
        if self.ended_by_cancel:
            return f"{self.name}: cancelled"
        return f"{self.name}: done" + (f", {c['failed']} failed" if c["failed"] else "")

    def confirm_close(self, parent):
        if not self.running:
            return True
        r = QMessageBox.question(parent, APP_NAME, f"{self.name} is still running. Stop it?\n\nFiles already "
                                 "finished keep their outputs; nothing is left half-written.",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if r != QMessageBox.Yes:
            return False
        self.cancel()
        self.wait(CANCEL_GRACE + 5)
        return True

    def close(self):
        if self.running:
            self.cancel()
            self.wait(CANCEL_GRACE + 5)
        self.timer.stop()
        self._kill()
        self.view.deleteLater()

    # ---- running ----
    def start(self):
        self.started = time.time()
        self._spawn(list(range(len(self.files))))
        self.timer.start()
        self.refresh()

    def _spawn(self, todo):
        ctx = multiprocessing.get_context("spawn")
        self.events, send = ctx.Pipe(duplex=False)
        self.cancel_ev = ctx.Event()
        if self.cancelling:
            self.cancel_ev.set()
        j = self.job
        spec = {"inputs": [str(f) for f in self.files], "todo": todo, "total": len(self.files),
                "out_dir": j["out_dir"], "pattern": j["pattern"], "pipeline": j["pipeline"],
                "open_password": j.get("open_password", ""), "save_pdf": j.get("save_pdf", True),
                "protected": self.ctrl.protected_paths(), "bates": dict(self.bates)}
        for k in ("test_delay", "test_crash_at"):
            if k in j:
                spec[k] = j[k]
        self.proc = ctx.Process(target=B.worker_main, args=(spec, send, self.cancel_ev), daemon=True)
        self.proc.start()
        send.close()                          # the worker holds it now; its end means EOF here

    def cancel(self):
        if not self.running or self.cancelling:
            return
        self.cancelling = True
        self.cancel_at = time.time()
        if self.cancel_ev is not None:
            self.cancel_ev.set()
        self.refresh()

    def wait(self, limit=60.0):
        """Run the event loop until the job has ended (tests, closing the window)."""
        from PySide6.QtCore import QEventLoop
        t0 = time.time()
        while self.running and time.time() - t0 < limit:
            loop = QEventLoop()
            QTimer.singleShot(20, loop.quit)
            loop.exec()
        return not self.running

    def _kill(self):
        p = self.proc
        if p is not None:
            if p.is_alive():
                p.terminate()
                p.join(5)
            self.proc = None
        self._close_pipe()

    def _close_pipe(self):
        if self.events is not None:
            try:
                self.events.close()
            except OSError:
                pass
            self.events = None

    def poll(self):
        got = False
        while True:
            try:
                if self.events is None or not self.events.poll():
                    break
                ev = self.events.recv()
            except (OSError, ValueError, EOFError):
                break
            got = True
            self._event(ev)
            if self.ended is not None:
                return
        p = self.proc
        if self.ended is None and p is not None and not p.is_alive():
            self._worker_gone(p.exitcode)
        elif self.cancelling and self.ended is None and time.time() - self.cancel_at > CANCEL_GRACE:
            self._kill()                                   # a step that won't stop: end the worker
            B.clean_partials(self.job["out_dir"])
            self._mark_running("cancelled", "Stopped by Cancel")
            self._finish(cancelled=True)
            return
        if got:
            self.refresh()

    def _event(self, ev):
        self.seen.append(ev[:2])
        kind = ev[0]
        if kind == "start":
            i = ev[1]
            self.status[i] = "running"
            self.detail[i] = "Opening"
        elif kind == "step":
            _, i, _k, text = ev
            self.detail[i] = text
        elif kind == "done":
            _, i, outs, notes, bates = ev
            self.status[i] = "done"
            self.outputs[i] = list(outs)
            self.detail[i] = ("Wrote " + ", ".join(outs) if outs else "Nothing written") + \
                (f"  ·  {'; '.join(notes)}" if notes else "")
            self.bates = dict(bates)
        elif kind == "failed":
            _, i, reason = ev
            self.status[i] = "failed"
            self.detail[i] = reason
        elif kind == "cancelled":
            i = ev[1]
            self.status[i] = "cancelled"
            self.detail[i] = "Stopped by Cancel (nothing written for this file)"
        elif kind == "end":
            if self.proc is not None:
                self.proc.join(5)
                self.proc = None
            self._close_pipe()
            self._finish(cancelled=bool(ev[1]) or self.cancelling)

    def _mark_running(self, status, text):
        for i, s in enumerate(self.status):
            if s == "running":
                self.status[i] = status
                self.detail[i] = text

    def _worker_gone(self, code):
        """The worker died without saying "end": a file crashed it. Mark that file failed and go
        on with the rest in a new worker."""
        self.proc = None
        self._close_pipe()
        B.clean_partials(self.job["out_dir"])
        self._mark_running("failed", f"The file stopped the worker (exit code {code}); it may be damaged")
        todo = [i for i, s in enumerate(self.status) if s == "queued"]
        if todo and not self.cancelling and self.restarts < len(self.files):
            self.restarts += 1
            self._spawn(todo)
            self.refresh()
        else:
            self._finish(cancelled=self.cancelling)

    def _finish(self, cancelled):
        self.timer.stop()
        for i, s in enumerate(self.status):
            if s in ("queued", "running"):
                self.status[i] = "skipped" if cancelled else "failed"
                self.detail[i] = "Not started (cancelled)" if cancelled else "Not processed"
        self.ended = time.time()
        self.ended_by_cancel = cancelled
        self.refresh()
        self.ctrl.job_finished(self)

    def summary(self):
        c = self.counts()
        head = (f"Cancelled: {c['done']} succeeded, {c['failed']} failed, {c['cancelled'] + c['skipped']} not done."
                if self.ended_by_cancel else f"Finished: {c['done']} succeeded, {c['failed']} failed.")
        return f"{head} Outputs are in {self.job['out_dir']}"

    def refresh(self):
        self.view.refresh()
        self.ctrl.tab_changed(self)


class BatchView(QWidget):
    """The job tab's page: what runs, progress, the per-file list, Cancel, the summary."""

    def __init__(self, job):
        super().__init__()
        self.job = job
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 18, 24, 18)
        head = QLabel(f"⚙  {job.name}")
        f = head.font()
        f.setPointSize(14)
        f.setBold(True)
        head.setFont(f)
        v.addWidget(head)
        steps = "  →  ".join(B.LABELS[s["op"]].split(" (")[0] for s in job.job["pipeline"]) or "(no operations)"
        info = QLabel(f"Pipeline: {steps}\nOutput folder: {job.job['out_dir']}   ·   Names: "
                      f"{job.job['pattern']}" + ("" if job.job.get("save_pdf", True) else "   ·   PDF not saved"))
        info.setObjectName("legend")
        info.setWordWrap(True)
        v.addWidget(info)
        row = QHBoxLayout()
        self.bar = QProgressBar()
        self.bar.setRange(0, max(1, len(job.files)))
        self.bar.setTextVisible(True)
        row.addWidget(self.bar, 1)
        self.count = QLabel("")
        self.count.setMinimumWidth(130)
        row.addWidget(self.count)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setToolTip("Stop after the current step. Finished files keep their outputs; the file being "
                                   "processed is not written.")
        self.btn_cancel.clicked.connect(job.cancel)
        row.addWidget(self.btn_cancel)
        self.btn_folder = QPushButton("Open output folder")
        self.btn_folder.clicked.connect(self.open_folder)
        row.addWidget(self.btn_folder)
        v.addLayout(row)
        self.now = QLabel("")
        self.now.setObjectName("legend")
        v.addWidget(self.now)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["#", "File", "Status", "Details"])
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        hd = self.list.header()
        hd.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hd.setSectionResizeMode(1, QHeaderView.Interactive)
        hd.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hd.setStretchLastSection(True)
        self.list.setColumnWidth(1, 260)
        for i, p in enumerate(job.files):
            it = QTreeWidgetItem([str(i + 1), p.name, "", ""])
            it.setToolTip(1, str(p))
            self.list.addTopLevelItem(it)
        v.addWidget(self.list, 1)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(self.summary)

    def refresh(self):
        j = self.job
        n = len(j.files)
        done = j.finished_count() if j.ended is not None else sum(1 for s in j.status if s in ("done", "failed"))
        self.bar.setValue(done)
        self.bar.setFormat(f"{done} / {n} files")
        self.count.setText(f"{done} of {n} files")
        cur = next((i for i, s in enumerate(j.status) if s == "running"), None)
        if j.ended is not None:
            self.now.setText("")
        elif j.cancelling:
            self.now.setText("Cancelling: stopping after the current step…")
        elif cur is not None:
            self.now.setText(f"File {cur + 1} of {n}: {j.detail[cur]}")
        else:
            self.now.setText("Starting…")
        for i in range(n):
            it = self.list.topLevelItem(i)
            st = j.status[i]
            if it.text(2) != STATUS_TEXT[st] or it.text(3) != j.detail[i]:
                it.setText(2, STATUS_TEXT[st])
                it.setForeground(2, QColorFor(st))
                it.setText(3, j.detail[i])
                it.setToolTip(3, j.detail[i])
        self.btn_cancel.setVisible(j.ended is None)
        self.btn_cancel.setEnabled(not j.cancelling)
        self.summary.setText(j.summary() if j.ended is not None else "")

    def open_folder(self):
        try:
            os.startfile(self.job.job["out_dir"])          # noqa: S606 (the user's own folder)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't open the folder:\n{e}")


def QColorFor(status):
    from PySide6.QtGui import QBrush, QColor
    return QBrush(QColor(STATUS_COLOR.get(status, "#e6e6e6")))


# ---------------- the controller ----------------
class BatchController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.jobs = []
        self.numbers = 0
        self.act_batch = QAction("Batch…", win, shortcut="Ctrl+Shift+B", triggered=lambda _=False: self.new_job())
        self.act_batch.setToolTip("Run a pipeline of operations over many PDFs. Each result is a new file in a folder "
                                  "you choose; the job runs in its own tab while you keep working.")

    def protected_paths(self):
        from . import protect
        return [str(p) for p in protect._sources.values()]

    def new_job(self, job=None):
        """job: {files, pipeline, out_dir, pattern, open_password, save_pdf} (None asks)."""
        if job is None:
            job = ask_batch(self.win, self.win.settings)
            if job is None:
                return None
        return self.start(job)

    def start(self, job):
        Path(job["out_dir"]).mkdir(parents=True, exist_ok=True)
        self.numbers += 1
        s = JobSession(self, self.numbers, job)
        ed = self.ed
        ed.sessions.append(s)
        ed.center.addWidget(s.view)
        ed.tabbar.blockSignals(True)
        i = ed.tabbar.addTab(s.tab_text())
        from .pdfeditor import _icon
        ed.tabbar.setTabIcon(i, _icon("cog-play-outline"))
        ed.tabbar.setTabToolTip(i, f"{s.name}: a batch job (not a document)")
        ed.tabbar.blockSignals(False)
        self.win.set_mode(ed.key)
        ed.sync_tabbar()
        ed.activate(s)
        self.jobs.append(s)
        s.start()
        self.win.statusBar().showMessage(f"{s.name} started: {len(s.files)} file(s). Keep working in the other tabs "
                                         "meanwhile.", 6000)
        return s

    def tab_changed(self, s):
        ed = self.ed
        if s in ed.sessions:
            ed.tabbar.setTabText(ed.sessions.index(s), s.tab_text())

    def job_finished(self, s):
        if s in self.ed.sessions:
            self.win.statusBar().showMessage(f"{s.name}: {s.summary()}", 10000)


# ---------------- setup ----------------
def sample_doc():
    """A plain one-page sample for previews of the stamping steps (there is no document yet)."""
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((72, 90), "Sample page", fontsize=22, fontname="hebo", color=(0.35, 0.35, 0.4))
    body = "The preview shows where this step puts its text or image on a page. " * 2
    y = 130
    for _ in range(22):
        pg.insert_text((72, y), body[:88], fontsize=10, color=(0.55, 0.55, 0.6))
        y += 22
    return d


def _langs():
    try:
        from . import tess
        tess.configure()
        import pytesseract
        langs = [x for x in pytesseract.get_languages(config="") if x != "osd"]
    except Exception:
        langs = []
    return [(x, x) for x in (langs or ["eng"])]


def fields_for(op):
    """(fields for stage10_ui.ToolDialog, note, preview?) of one operation's settings."""
    from . import export, locked
    from . import stage10 as T
    from .stage10_ui import Stage10Controller as C
    if op == "header_footer":
        return C.HF_FIELDS, "Placeholders: {page}, {pages}, {date}.", True
    if op == "page_numbers":
        return C.PN_FIELDS, "Numbers count through the page range from the start number, in every file.", True
    if op == "bates":
        return C.BATES_FIELDS + [("continue", "Continue numbering from file to file", "check", True, None)], \
            "With 'continue' on, each file starts where the previous one ended (only files that were written " \
            "count). Off: every file starts at the start number.", True
    if op == "watermark":
        return C.WM_FIELDS, "", True
    if op == "flatten":
        return C.FLAT_FIELDS, "Annotations and form fields become part of the page. Redaction marks are not " \
                              "flattened.", False
    if op == "ocr":
        return [("range", "Pages", "range", "all", None), ("lang", "Language", "choice", "eng", _langs()),
                ("force", "Also pages that already have text", "check", False, None)], \
            "Adds an invisible text layer: the pages look the same, but can be searched and selected.", False
    if op == "redact_terms":
        return [("terms", "Search terms (one per line)", "lines", "", "e.g. a name, an account number…"),
                ("match_case", "Match case", "check", False, None),
                ("whole_word", "Whole words only", "check", False, None),
                ("regex", "Terms are regular expressions", "check", False, None),
                ("fill", "Fill colour", "color", (0, 0, 0), None),
                ("apply", "Apply them (remove what is under the marks)", "check", True, None)], \
            "Every hit is marked and, with Apply on, removed (text, image pixels and drawings under it), then " \
            "verified: a file where text could still be extracted under a mark is NOT written. Redaction marks " \
            "already in a file are applied too.", False
    if op == "find_replace":
        return [("find", "Find", "text", "", None), ("replace", "Replace with", "text", "", None),
                ("match_case", "Match case", "check", False, None),
                ("whole_word", "Whole words only", "check", False, None),
                ("regex", "Regular expression ($1 or \\1 in the replacement = a group)", "check", False, None),
                ("range", "Pages", "range", "all", None)], \
            "The same write path as Edit text: the text keeps its font where the file allows (else the closest " \
            "standard font), reflows in its block (shrinking if it must), and is verified. Hits that can't be " \
            "replaced cleanly are skipped and counted.", False
    if op == "metadata":
        return [("mode", "Action", "choice", "set", [("set", "Set the fields below (blank = leave as is)"),
                                                      ("remove", "Remove all metadata (Info and XMP)")])] + \
            [(k, lab, "text", "", None) for k, lab in T.META_FIELDS], "", False
    if op == "bookmarks":
        return [("levels", "Heading levels", "int", 3, (1, 6)),
                ("threshold", "Larger than body text by at least (pt)", "float", 0.5, (0, 20)),
                ("mode", "If a file already has bookmarks", "choice", "replace",
                 [("replace", "Replace them"), ("append", "Add after them"),
                  ("under", "Nest under a \"Generated bookmarks\" entry")])], \
            "Headings found by font size and style (best effort, no preview in a batch). A scan without text gets " \
            "none: put OCR earlier in the pipeline.", False
    if op == "extract":
        return [("format", "Format", "choice", "txt", [(k, lab) for k, lab, _e, _f in export.FORMATS]),
                ("page_headings", "Page headings", "check", False, None)], \
            "The existing extraction (text layer, OCR where there is none) of each file as it is at this point of " \
            "the pipeline, written next to its PDF with the same name.", False
    if op == "to_word":
        return [("range", "Pages", "range", "all", None), ("images", "Include images", "check", True, None),
                ("tables", "Detect tables", "check", True, None),
                ("columns", "Keep multi-column layout", "check", True, None),
                ("ocr_first", "OCR pages without text first (a temporary copy)", "check", False, None)], \
            "Each file as it is at this point of the pipeline becomes a .docx with the batch's name pattern, next to " \
            "its PDF; with 'Save the processed PDF' off, only the .docx is written. Paragraphs, fonts, tables, images " \
            "and basic layout come through; complex layouts may not convert perfectly.", False
    if op == "compress":
        return [("downsample", "Downsample images", "check", False, None),
                ("threshold", "Images above (dpi)", "int", 225, (50, 2400)),
                ("target", "…are resampled to (dpi)", "int", 150, (36, 1200)),
                ("quality", "JPEG quality", "int", 75, (10, 100))], \
            "Always: unused objects removed, every stream compressed, content cleaned (when the PDF is saved).", False
    if op == "password":
        return [("mode", "Protection", "choice", "set",
                 [("set", "Set passwords"), ("match", "One password (opens and owns the file)"),
                  ("clear", "No password"), ("keep", "Keep each file's own")]),
                ("open_pw", "Password to open", "password", "", None),
                ("owner_pw", "Owner password (permissions)", "password", "", None)] + \
            [(f"perm_{k}", lab, "check", True, None) for k, lab, _f in locked.PERMISSIONS], \
            "Applies to every PDF the batch writes (AES-256). Permissions need an owner password. Passwords are " \
            "kept in memory for this job only. Without this step each file keeps its own protection.", False
    raise ValueError(op)


def validate(op, p):
    """An error text for settings that can't work, or None."""
    if op == "find_replace" and not (p.get("find") or "").strip():
        return "Give the text to find."
    if op == "redact_terms" and not (p.get("terms") or "").strip():
        return "Give at least one search term."
    if op == "watermark" and p.get("kind") == "image" and not (p.get("image") or p.get("image_png")):
        return "Choose an image for the watermark."
    if op == "flatten" and not (p.get("annots") or p.get("widgets")):
        return "Choose annotations, form fields, or both."
    if op == "password":
        if p.get("mode") == "set" and not (p.get("open_pw") or p.get("owner_pw")):
            return "Give a password to open, an owner password, or both."
        if p.get("mode") == "match" and not p.get("open_pw"):
            return "Give the password (in 'Password to open')."
    if "range" in p:
        from . import stage10 as T
        try:
            T.parse_range(p["range"], 10 ** 6)
        except ValueError as e:
            return str(e)
    return None


def preview_for(op):
    def preview(values, _page):
        p = dict(values)
        if op == "watermark" and p.get("kind") == "image":
            if not p.get("image"):
                return None
            from .contentedit import load_image
            p["image_png"] = load_image(p["image"])[0]
        d = sample_doc()
        try:
            ctx = {"tick": lambda *_: None, "step_key": 0, "bates": {}, "bates_pending": {}}
            B.RUN[op](d, p, ctx)
            pix = d[0].get_pixmap(matrix=pymupdf.Matrix(0.6, 0.6), alpha=False)
        finally:
            d.close()
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        return QPixmap.fromImage(img)
    return preview


def _prefill(fields, params):
    out = []
    for k, lab, kind, d, extra in fields:
        v = params.get(k, d) if kind != "file" else d
        if kind == "color" and isinstance(v, list):
            v = tuple(v)                           # (remembered as JSON)
        out.append((k, lab, kind, v, extra))
    return out


def ask_step(parent, op, params=None):
    """Settings for one step -> params, or None. A remembered step fills the form."""
    from .stage10_ui import ToolDialog
    fields, note, has_preview = fields_for(op)
    params = dict(params or {})
    fields = _prefill(fields, params)
    while True:
        dlg = ToolDialog(parent, f"Batch step: {B.LABELS[op]}", fields, preview_for(op) if has_preview else None, 1, 0,
                         note)
        if op == "watermark" and params.get("image"):
            kind, wd = dlg.w["image"]
            wd.path = params["image"]
            wd.setText(Path(params["image"]).name)
            dlg.refresh()
        dlg.button_box = dlg.findChild(QDialogButtonBox)
        dlg.button_box.button(QDialogButtonBox.Ok).setText("OK")
        if dlg.exec() != QDialog.Accepted:
            return None
        vals = dlg.values()
        err = validate(op, vals)
        if err is None:
            return vals
        QMessageBox.warning(parent, APP_NAME, err)
        params = vals
        fields = _prefill(fields, vals)


def summarize(op, p):
    """One line for the pipeline list (never a password)."""
    lab = B.LABELS[op].split(" (")[0]
    if op == "watermark":
        return f"{lab}: " + (f"image {Path(p['image']).name}" if p.get("kind") == "image" and p.get("image")
                             else f"“{p.get('text', '')}”")
    if op == "header_footer":
        parts = [p[k] for k in ("tl", "tc", "tr", "bl", "bc", "br") if p.get(k)]
        return f"{lab}: " + " | ".join(parts)
    if op == "bates":
        from . import stage10 as T
        return f"{lab}: {T.bates_label(p.get('prefix', ''), p.get('start', 1), p.get('digits', 6), p.get('suffix', ''))}…" \
            + (" (continues across files)" if p.get("continue", True) else " (each file restarts)")
    if op == "find_replace":
        return f"{lab}: “{p.get('find', '')}” → “{p.get('replace', '')}”"
    if op == "redact_terms":
        n = len([t for t in (p.get("terms") or "").splitlines() if t.strip()])
        return f"{lab}: {n} term(s)" + ("" if p.get("apply", True) else " (mark only)")
    if op == "metadata":
        return f"{lab}: " + ("remove all" if p.get("mode") == "remove" else "set fields")
    if op == "password":
        return f"{lab}: " + {"set": "set passwords", "match": "one password", "clear": "none",
                             "keep": "keep each file's own"}[p.get("mode", "set")]
    if op == "extract":
        return f"{lab}: {p.get('format', 'txt')}"
    if op == "to_word":
        return f"{lab}" + (" (OCR first)" if p.get("ocr_first") else "")
    if op == "compress":
        return f"{lab}" + (f": images over {p.get('threshold')} dpi to {p.get('target')} dpi" if p.get("downsample") else "")
    if op == "bookmarks":
        return f"{lab}: {p.get('levels', 3)} level(s), {p.get('mode', 'replace')}"
    if op == "ocr":
        return f"{lab}: {p.get('lang', 'eng')}" + (", every page" if p.get("force") else "")
    return lab


class BatchDialog(QDialog):
    """Inputs, pipeline, output. Returns the job via result_job()."""

    def __init__(self, parent, settings):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Batch")
        self.resize(860, 720)
        self.files = []
        self.steps = []                       # [{"op", "params"}]
        last = self._remembered()
        v = QVBoxLayout(self)
        # inputs
        g = QGroupBox("1. Input PDFs")
        gv = QVBoxLayout(g)
        self.in_list = QListWidget()
        self.in_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        gv.addWidget(self.in_list, 1)
        row = QHBoxLayout()
        for lab, fn in (("Add files…", self.add_files), ("Add folder…", self.add_folder),
                        ("Remove", self.remove_files), ("Clear", self.clear_files)):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.recurse = QCheckBox("Include subfolders")
        self.recurse.setChecked(bool(last.get("recurse", False)))
        row.addWidget(self.recurse)
        row.addStretch(1)
        self.in_count = QLabel("")
        row.addWidget(self.in_count)
        gv.addLayout(row)
        pr = QHBoxLayout()
        pr.addWidget(QLabel("Password for protected inputs (optional):"))
        self.in_pw = QLineEdit()
        self.in_pw.setEchoMode(QLineEdit.Password)
        self.in_pw.setPlaceholderText("tried once on each password-protected file")
        pr.addWidget(self.in_pw, 1)
        gv.addLayout(pr)
        v.addWidget(g, 2)
        # pipeline
        g = QGroupBox("2. Operations, run in this order on every file")
        gv = QVBoxLayout(g)
        row = QHBoxLayout()
        self.op_pick = QComboBox()
        for k, lab in B.OPS:
            self.op_pick.addItem(lab, k)
        row.addWidget(self.op_pick, 1)
        b = QPushButton("Add step…")
        b.clicked.connect(lambda: self.add_step())
        row.addWidget(b)
        gv.addLayout(row)
        self.step_list = QListWidget()
        self.step_list.itemDoubleClicked.connect(lambda _it: self.edit_step())
        gv.addWidget(self.step_list, 1)
        row = QHBoxLayout()
        for lab, fn in (("Settings…", self.edit_step), ("Up", lambda: self.move_step(-1)),
                        ("Down", lambda: self.move_step(1)), ("Remove", self.remove_step)):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        gv.addLayout(row)
        v.addWidget(g, 2)
        # output
        g = QGroupBox("3. Output")
        f = QFormLayout(g)
        orow = QHBoxLayout()
        self.out_dir = QLineEdit(last.get("out_dir", ""))
        self.out_dir.setPlaceholderText("a folder for the results (never the inputs themselves)")
        orow.addWidget(self.out_dir, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self.pick_out)
        orow.addWidget(b)
        f.addRow("Folder", orow)
        self.pattern = QLineEdit(last.get("pattern", B.PATTERN_DEFAULT))
        self.pattern.setToolTip("{name} = the input's name, {n} = its number in this batch, {date} = today")
        f.addRow("File names", self.pattern)
        self.example = QLabel("")
        self.example.setObjectName("legend")
        f.addRow("", self.example)
        self.save_pdf = QCheckBox("Save the processed PDF (off: only what Extract writes)")
        self.save_pdf.setChecked(bool(last.get("save_pdf", True)))
        f.addRow("", self.save_pdf)
        note = QLabel("Every result is a NEW file. An existing file is never replaced: the name gets (2), (3)… "
                      "Inputs are never written.")
        note.setObjectName("legend")
        note.setWordWrap(True)
        f.addRow(note)
        v.addWidget(g)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.run_btn = bb.button(QDialogButtonBox.Ok)
        self.run_btn.setText("Run batch")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        for st in last.get("pipeline", []):
            if st.get("op") in B.LABELS:
                self.steps.append({"op": st["op"], "params": dict(st.get("params") or {})})
        self.pattern.textChanged.connect(self._sync)
        self.out_dir.textChanged.connect(self._sync)
        self.save_pdf.toggled.connect(self._sync)
        self._fill_steps()
        self._sync()

    # ---- remembered setup (no passwords, no image data) ----
    def _remembered(self):
        try:
            return json.loads(self.settings.value(SETUP_KEY, "", type=str) or "{}")
        except Exception:
            return {}

    def remember(self, job):
        keep = {"out_dir": job["out_dir"], "pattern": job["pattern"], "save_pdf": job["save_pdf"],
                "recurse": self.recurse.isChecked(),
                "pipeline": [{"op": s["op"], "params": {k: v for k, v in s["params"].items() if k not in SECRET_KEYS}}
                             for s in job["pipeline"]]}
        self.settings.setValue(SETUP_KEY, json.dumps(keep))

    # ---- inputs ----
    def add_paths(self, paths, recurse=None):
        rec = self.recurse.isChecked() if recurse is None else recurse
        have = {os.path.normcase(str(p.resolve())) for p in self.files}
        for f in B.collect_inputs(paths, rec):
            if os.path.normcase(str(f.resolve())) not in have:
                self.files.append(f)
                have.add(os.path.normcase(str(f.resolve())))
                it = QListWidgetItem(f.name)
                it.setToolTip(str(f))
                self.in_list.addItem(it)
        self._sync()

    def add_files(self):
        ps, _ = QFileDialog.getOpenFileNames(self, "Add PDFs to the batch", str(Path.home()), "PDF files (*.pdf)")
        if ps:
            self.add_paths(ps)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of PDFs", str(Path.home()))
        if d:
            self.add_paths([d])

    def remove_files(self):
        rows = sorted({self.in_list.row(it) for it in self.in_list.selectedItems()}, reverse=True)
        for r in rows:
            self.in_list.takeItem(r)
            del self.files[r]
        self._sync()

    def clear_files(self):
        self.in_list.clear()
        self.files = []
        self._sync()

    # ---- pipeline ----
    def _fill_steps(self, select=None):
        self.step_list.clear()
        for k, s in enumerate(self.steps):
            it = QListWidgetItem(f"{k + 1}. {summarize(s['op'], s['params'])}")
            ok = validate(s["op"], s["params"]) is None
            if not ok:
                it.setText(it.text() + "   (needs settings)")
                it.setForeground(QColorFor("failed"))
            self.step_list.addItem(it)
        if select is not None and 0 <= select < self.step_list.count():
            self.step_list.setCurrentRow(select)
        self._sync()

    def add_step(self, op=None, params=None):
        op = op or self.op_pick.currentData()
        if params is None:
            params = ask_step(self, op)
            if params is None:
                return False
        self.steps.append({"op": op, "params": params})
        self._fill_steps(len(self.steps) - 1)
        return True

    def edit_step(self):
        k = self.step_list.currentRow()
        if k < 0:
            return
        s = self.steps[k]
        p = ask_step(self, s["op"], s["params"])
        if p is not None:
            s["params"] = p
            self._fill_steps(k)

    def move_step(self, d):
        k = self.step_list.currentRow()
        j = k + d
        if k < 0 or not 0 <= j < len(self.steps):
            return
        self.steps[k], self.steps[j] = self.steps[j], self.steps[k]
        self._fill_steps(j)

    def remove_step(self):
        k = self.step_list.currentRow()
        if k >= 0:
            del self.steps[k]
            self._fill_steps(min(k, len(self.steps) - 1))

    # ---- output ----
    def pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "Output folder", self.out_dir.text() or str(Path.home()))
        if d:
            self.out_dir.setText(d)

    def _sync(self, *_):
        n = len(self.files)
        self.in_count.setText(f"{n} file(s)")
        stem = self.files[0].stem if self.files else "report"
        ex = B.expand_pattern(self.pattern.text(), stem, 1, max(1, n))
        self.example.setText(f"e.g. {ex}.pdf" if self.save_pdf.isChecked() else f"e.g. {ex}.txt")
        self.run_btn.setEnabled(bool(n and self.steps and self.out_dir.text().strip()))

    def problem(self):
        if not self.files:
            return "Add at least one PDF."
        if not self.steps:
            return "Add at least one operation."
        for s in self.steps:
            err = validate(s["op"], s["params"])
            if err:
                return f"{B.LABELS[s['op']]}: {err}"
        if not self.out_dir.text().strip():
            return "Choose an output folder."
        if not self.save_pdf.isChecked() and not any(s["op"] in ("extract", "to_word") for s in self.steps):
            return "Nothing would be written: keep 'Save the processed PDF' on, or add an Extract or Word step."
        return None

    def result_job(self):
        steps = []
        for s in self.steps:
            p = dict(s["params"])
            if s["op"] == "watermark" and p.get("kind") == "image" and p.get("image"):
                from .contentedit import load_image
                p["image_png"] = load_image(p["image"])[0]
            steps.append({"op": s["op"], "params": p})
        return {"files": [str(f) for f in self.files], "pipeline": steps, "out_dir": self.out_dir.text().strip(),
                "pattern": self.pattern.text().strip() or B.PATTERN_DEFAULT, "open_password": self.in_pw.text(),
                "save_pdf": self.save_pdf.isChecked()}

    def _ok(self):
        err = self.problem()
        if err:
            QMessageBox.warning(self, APP_NAME, err)
            return
        try:
            job = self.result_job()
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't read the watermark image:\n{e}")
            return
        self.job = job
        self.remember(job)
        self.accept()


def ask_batch(parent, settings):
    """-> the job, or None (module-level so the self-test can answer it)."""
    d = BatchDialog(parent, settings)
    return d.job if d.exec() == QDialog.Accepted else None
