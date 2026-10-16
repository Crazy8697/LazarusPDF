"""Stage 13 UI: the diff on the compare view (View › Compare Side by Side, Ctrl+Alt+C).

A row under the compare bar: Text / Visual, Options…, Compare (re-run), Cancel, progress,
Previous / Next change (Shift+F7 / F7), Export report…, and the summary. A "Changes" panel lists
every change in order; clicking one jumps both panes to it and selects it. Marks are view-only
scene items on the two panes: they never touch either document (no undo entries). Synced
scrolling follows the page alignment. Results clear when either document changes.

The comparison runs in a worker process (compare.worker_main): the window stays responsive
whatever the size of the files, and Cancel ends it."""
import json
import multiprocessing
import os
import time

import pymupdf
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QKeySequence, QPen
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDockWidget,
                               QFileDialog, QFormLayout, QGraphicsRectItem, QGraphicsSimpleTextItem, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTreeWidget,
                               QTreeWidgetItem, QWidget)

from . import APP_NAME
from . import compare as C
from . import settings as S

OPTIONS_KEY = "compare_options"
POLL_MS = 40
CANCEL_GRACE = 5.0
KIND_TEXT = {"insert": "Inserted", "delete": "Deleted", "modify": "Changed", "added": "Page added",
             "removed": "Page removed", "moved": "Page moved", "notext": "No text"}


def colors(settings):
    return {"insert": S.get(settings, "compare_insert_color"), "delete": S.get(settings, "compare_delete_color"),
            "modify": S.get(settings, "compare_modify_color")}


class CompareController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.view = self.ed.compare
        self.result = None
        self.versions = None          # (session id, doc_version) of both sides the result is for
        self.items = []               # per change: [scene items]
        self.extra_items = []         # page labels (added / removed / moved / no text)
        self.cur = -1
        self.job = None               # {proc, conn, cancel, kind, started}
        self.max_gap = 0.0            # longest UI pause while the last run was going (tests)
        self.last_error = None
        self._build_bar()
        self._build_dock()
        self.act_next = QAction("Next change", win, shortcut=QKeySequence("F7"), triggered=lambda _=False: self.step(1))
        self.act_prev = QAction("Previous change", win, shortcut=QKeySequence("Shift+F7"),
                                triggered=lambda _=False: self.step(-1))
        for a in (self.act_next, self.act_prev):
            win.addAction(a)
        self.view.on_changed = self._doc_changed
        self.view.on_pair = lambda: self.clear("Another PDF is shown: compare again.")
        self.ed.act_compare.toggled.connect(self._shown)
        self._timer = QTimer(self, interval=POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._sync_enabled()

    # ---------- widgets ----------
    def _build_bar(self):
        bar = QWidget()
        bar.setObjectName("comparebar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 2, 8, 3)
        lab = QLabel(" DIFF ")
        lab.setObjectName("grouplabel")
        h.addWidget(lab)
        self.mode = QComboBox()
        self.mode.addItem("Text", "text")
        self.mode.addItem("Visual", "visual")
        self.mode.setToolTip("Text: word by word, at the exact boxes. Visual: what the pages look like (drawings, "
                             "scans, layout)")
        h.addWidget(self.mode)
        self.btn_opts = QPushButton("Options…")
        self.btn_opts.clicked.connect(lambda: self.ask_options())
        h.addWidget(self.btn_opts)
        self.btn_run = QPushButton("Compare")
        self.btn_run.setToolTip("Compare the two PDFs shown (again)")
        self.btn_run.clicked.connect(lambda: self.run())
        h.addWidget(self.btn_run)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_cancel.hide()
        h.addWidget(self.btn_cancel)
        self.progress = QLabel("")
        self.progress.setObjectName("legend")
        h.addWidget(self.progress)
        from .pdfeditor import _icon
        self.btn_prev = QPushButton(_icon("chevron-up"), "")
        self.btn_prev.setToolTip("Previous change (Shift+F7)")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next = QPushButton(_icon("chevron-down"), "")
        self.btn_next.setToolTip("Next change (F7)")
        self.btn_next.clicked.connect(lambda: self.step(1))
        h.addWidget(self.btn_prev)
        h.addWidget(self.btn_next)
        self.btn_export = QPushButton("Export report…")
        self.btn_export.clicked.connect(lambda: self.export_report())
        h.addWidget(self.btn_export)
        h.addStretch(1)
        self.summary = QLabel("")
        h.addWidget(self.summary)
        self.bar = bar
        self.view.layout().insertWidget(1, bar)

    def _build_dock(self):
        d = QDockWidget("Changes", self.win)
        d.setObjectName("comparechanges")
        t = QTreeWidget()
        t.setHeaderLabels(["#", "Type", "Left", "Right", "Text"])
        t.setRootIsDecorated(False)
        t.setUniformRowHeights(True)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        hd = t.header()
        for k in range(4):
            hd.setSectionResizeMode(k, QHeaderView.ResizeToContents)
        hd.setStretchLastSection(True)
        t.itemClicked.connect(lambda it, _c: self.select(it.data(0, Qt.UserRole)))
        t.itemActivated.connect(lambda it, _c: self.select(it.data(0, Qt.UserRole)))
        d.setWidget(t)
        self.win.addDockWidget(Qt.RightDockWidgetArea, d)
        d.hide()
        self.list = t
        self.dock = d

    def _shown(self, on):
        self.dock.setVisible(bool(on))
        if not on:
            self.cancel()
        self._sync_enabled()

    def _sync_enabled(self):
        has = self.result is not None and bool(self.result["changes"])
        running = self.job is not None
        for w in (self.btn_prev, self.btn_next):
            w.setEnabled(has)
        self.act_next.setEnabled(has and self.ed.comparing())
        self.act_prev.setEnabled(has and self.ed.comparing())
        self.btn_export.setEnabled(self.result is not None and not running)
        self.btn_run.setEnabled(not running)
        self.btn_cancel.setVisible(running)

    # ---------- options (remembered) ----------
    def options(self):
        o = dict(C.DEFAULT_OPTIONS)
        try:
            o.update(json.loads(self.win.settings.value(OPTIONS_KEY, "", type=str) or "{}"))
        except Exception:
            pass
        return o

    def set_options(self, o):
        self.win.settings.setValue(OPTIONS_KEY, json.dumps(o))

    def ask_options(self, answer=None):
        o = self.options()
        if answer is None:
            answer = ask_options(self.win, o)
            if answer is None:
                return None
        o.update(answer)
        self.set_options(o)
        return o

    # ---------- running ----------
    def sides(self):
        return self.view.sessions[0], self.view.sessions[1]

    def _bytes(self, s):
        """The document as it is now: the file's own bytes while unedited, else a plain copy."""
        w = s.work
        data = getattr(s, "data", None)
        if data is not None and w.step() == 0 and not w._base:
            return data
        return w.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)

    def run(self, wait=False, mode=None, options=None, test_delay=0.0):
        """Compare the two PDFs shown. wait=True returns when it has finished (tests)."""
        if self.job is not None:
            return False
        left, right = self.sides()
        if left is None or right is None or not left.ready or not right.ready:
            return False
        if mode is not None:
            self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        o = self.options()
        o.update(options or {})
        from . import stage10 as T
        try:
            T.parse_range(o["left_range"], left.work.doc.page_count)
            T.parse_range(o["right_range"], right.work.doc.page_count)
        except ValueError as e:
            QMessageBox.warning(self.win, APP_NAME, f"Page range: {e}")
            return False
        self.clear()
        spec = {"left": self._bytes(left), "right": self._bytes(right), "mode": self.mode.currentData(),
                "options": o, "test_delay": test_delay}
        self.versions = ((left.sid, left.doc_version), (right.sid, right.doc_version))
        self._start(spec, "compare")
        if wait:
            return self.wait()
        return True

    def _start(self, spec, kind):
        ctx = multiprocessing.get_context("spawn")
        conn, send = ctx.Pipe(duplex=False)
        cancel = ctx.Event()
        proc = ctx.Process(target=C.worker_main, args=(spec, send, cancel), daemon=True)
        proc.start()
        send.close()
        self.job = {"proc": proc, "conn": conn, "cancel": cancel, "kind": kind, "started": time.time(),
                    "cancel_at": None, "last": time.perf_counter()}
        self.max_gap = 0.0
        self.progress.setText("Starting…")
        self._timer.start()
        self._sync_enabled()

    def wait(self, limit=600):
        from PySide6.QtCore import QEventLoop
        t0 = time.time()
        while self.job is not None and time.time() - t0 < limit:
            loop = QEventLoop()
            QTimer.singleShot(15, loop.quit)
            loop.exec()
        return self.result is not None

    def cancel(self):
        j = self.job
        if j is None or j["cancel_at"] is not None:
            return
        j["cancel"].set()
        j["cancel_at"] = time.time()
        self.progress.setText("Cancelling…")

    def _end(self):
        j, self.job = self.job, None
        self._timer.stop()
        if j is not None:
            if j["proc"].is_alive():
                j["proc"].terminate()
            j["proc"].join(3)
            try:
                j["conn"].close()
            except OSError:
                pass
        self._sync_enabled()

    def _poll(self):
        j = self.job
        if j is None:
            return
        now = time.perf_counter()
        self.max_gap = max(self.max_gap, now - j["last"])
        j["last"] = now
        msg = None
        try:
            while j["conn"].poll():
                msg = j["conn"].recv()
                if msg[0] == "progress":
                    _k, phase, done, total = msg
                    self.progress.setText(f"{phase}… {done}/{total}" if total > 1 else f"{phase}…")
                    msg = None
                    continue
                break
        except (EOFError, OSError):
            msg = ("failed", "the comparison stopped unexpectedly") if msg is None else msg
        if msg is None:
            if j["cancel_at"] is not None and time.time() - j["cancel_at"] > CANCEL_GRACE:
                self._end()
                self.progress.setText("Cancelled.")
            elif not j["proc"].is_alive() and not j["conn"].poll():
                self._end()
                self.last_error = "the comparison stopped unexpectedly"
                self.progress.setText("Stopped unexpectedly.")
            return
        kind = j["kind"]
        self._end()
        if msg[0] == "cancelled":
            self.progress.setText("Cancelled.")
        elif msg[0] == "failed":
            self.last_error = msg[1]
            self.progress.setText("")
            QMessageBox.warning(self.win, APP_NAME, f"The comparison couldn't be made:\n{msg[1]}")
        elif kind == "report":
            self.progress.setText("Report written.")
            self.win.statusBar().showMessage("Comparison report written to a new file.", 6000)
        else:
            self.show_result(msg[1])

    # ---------- results ----------
    def clear(self, why=None):
        for items in self.items + [self.extra_items]:
            for it in items:
                try:
                    if it.scene() is not None:
                        it.scene().removeItem(it)
                except RuntimeError:
                    pass
        self.items, self.extra_items = [], []
        self.list.clear()
        had = self.result is not None
        self.result = None
        self.versions = None
        self.cur = -1
        self.view.page_map = None
        self.summary.setText("")
        if why and had:
            self.progress.setText(why)
        self._sync_enabled()

    def _doc_changed(self, session):
        if self.result is not None and self.versions is not None:
            ids = {self.versions[0][0], self.versions[1][0]}
            if session.sid in ids:
                self.clear("A document changed: results cleared (Compare runs it again).")

    def show_result(self, res):
        self.result = res
        col = {k: QColor(v) for k, v in colors(self.win.settings).items()}
        panes = {"left": self.view.panes[0], "right": self.view.panes[1]}
        self.items = []
        for c in res["changes"]:
            its = []
            for side in ("left", "right"):
                pane = panes[side]
                for p, rects in c[side].items():
                    if p >= len(pane.pages):
                        continue
                    for r in rects:
                        it = QGraphicsRectItem(pane.page_to_scene(p, pymupdf.Rect(r)).adjusted(-1, -1, 1, 1))
                        cc = QColor(col[c["kind"]])
                        cc.setAlpha(70)
                        it.setBrush(QBrush(cc))
                        it.setPen(QPen(col[c["kind"]], 0))
                        it.setZValue(4)
                        pane.scene().addItem(it)
                        its.append(it)
            self.items.append(its)
        # page events and pages without text: a label on the page
        def label(pane, p, text, color):
            if p is None or p >= len(pane.pages):
                return
            r = pane.pages[p]["rect"]
            fr = QGraphicsRectItem(r.adjusted(-3, -3, 3, 3))
            fr.setPen(QPen(color, 3))
            fr.setBrush(Qt.NoBrush)
            fr.setZValue(4)
            pane.scene().addItem(fr)
            t = QGraphicsSimpleTextItem(text)
            t.setBrush(color)
            f = t.font()
            f.setPointSize(14)
            f.setBold(True)
            t.setFont(f)
            t.setPos(r.left() + 8, r.top() + 6)
            t.setZValue(5)
            pane.scene().addItem(t)
            self.extra_items += [fr, t]
        for e in res["events"]:
            if e["kind"] == "removed":
                label(panes["left"], e["lpage"], "Page removed", col["delete"])
            elif e["kind"] == "added":
                label(panes["right"], e["rpage"], "Page added", col["insert"])
            else:
                label(panes["left"], e["lpage"], f"Moved: right page {e['rpage'] + 1}", col["modify"])
                label(panes["right"], e["rpage"], f"Page moved from left page {e['lpage'] + 1}", col["modify"])
        amber = QColor("#b08800")
        for side in ("left", "right"):
            for p in res["notext"].get(side, []):
                label(panes[side], p, "No text: OCR to searchable, or Visual mode", amber)
        lr, rl = C.page_map(res)
        self.view.page_map = (lr, rl)
        self._fill_list()
        s = res["summary"]
        self.summary.setText(f"Inserted {s['insert']} · Deleted {s['delete']} · Changed {s['modify']} · Pages: "
                             f"+{s['added']} −{s['removed']} moved {s['moved']}")
        n = len(res["changes"])
        self.progress.setText(f"{n} change(s)." if n else "No differences found.")
        self._sync_enabled()

    def rows(self):
        """The change list in order: (index into changes or None, type, left page, right page, text)."""
        res = self.result
        out = []
        for k, c in enumerate(res["changes"]):
            text = (f"{c['ltext']} → {c['rtext']}" if c["kind"] == "modify" and c["how"] == "text"
                    else c["ltext"] or c["rtext"] or "visual difference")
            out.append((k, KIND_TEXT[c["kind"]], c["lpage"], c["rpage"], text))
        for e in res["events"]:
            out.append((None, KIND_TEXT[e["kind"]], e["lpage"], e["rpage"], ""))
        for side in ("left", "right"):
            for p in res["notext"].get(side, []):
                out.append((None, KIND_TEXT["notext"], p if side == "left" else None, p if side == "right" else None,
                            "OCR to searchable, or use Visual mode"))
        return out

    def _fill_list(self):
        self.list.clear()
        for n, (k, typ, lp, rp, text) in enumerate(self.rows()):
            it = QTreeWidgetItem([str(n + 1), typ, "" if lp is None else str(lp + 1), "" if rp is None else str(rp + 1),
                                  text])
            it.setData(0, Qt.UserRole, k if k is not None else ("page", lp, rp))
            it.setToolTip(4, text)
            self.list.addTopLevelItem(it)

    # ---------- navigation ----------
    def step(self, d):
        if self.result is None or not self.result["changes"]:
            return
        n = len(self.result["changes"])
        self.select((self.cur + d) % n if self.cur >= 0 else (0 if d > 0 else n - 1))

    def select(self, k):
        """Jump both panes to change k (or a page row) and select it."""
        if self.result is None or k is None:
            return
        if isinstance(k, tuple):                         # a page row: show those pages
            _p, lp, rp = k
            self._jump(None, lp, None, rp)
            return
        if self.cur >= 0:
            self._style(self.cur, False)
        self.cur = k
        self._style(k, True)
        c = self.result["changes"][k]
        lr = next(iter(c["left"].values()))[0] if c["left"] else (c["lanchor"][1] if c["lanchor"] else None)
        rr = next(iter(c["right"].values()))[0] if c["right"] else (c["ranchor"][1] if c["ranchor"] else None)
        self._jump(lr, c["lpage"], rr, c["rpage"])
        for i in range(self.list.topLevelItemCount()):
            if self.list.topLevelItem(i).data(0, Qt.UserRole) == k:
                self.list.blockSignals(True)
                self.list.setCurrentItem(self.list.topLevelItem(i))
                self.list.blockSignals(False)
                break
        self.progress.setText(f"Change {k + 1} of {len(self.result['changes'])}")

    def _jump(self, lr, lp, rr, rp):
        v = self.view
        v._syncing = True                              # both panes go to their own spot
        try:
            for pane, rect, page in ((v.panes[0], lr, lp), (v.panes[1], rr, rp)):
                if page is None or page >= len(pane.pages):
                    continue
                if rect is not None:
                    sr = pane.page_to_scene(page, pymupdf.Rect(rect))
                    pane.centerOn(sr.center())
                else:
                    pane.goto(page)
        finally:
            v._syncing = False

    def _style(self, k, on):
        c = self.result["changes"][k]
        color = QColor(colors(self.win.settings)[c["kind"]])
        for it in self.items[k]:
            try:
                it.setPen(QPen(QColor("#ff6d00") if on else color, 2.5 if on else 0))
                it.setZValue(6 if on else 4)
            except RuntimeError:
                pass

    # ---------- the report ----------
    def export_report(self, dest=None, wait=False):
        """A new PDF: summary, change list, side-by-side snippets with the marks."""
        from . import protect
        if self.result is None or self.job is not None:
            return False
        left, right = self.sides()
        while dest is None:
            p, _ = QFileDialog.getSaveFileName(self.win, "Export comparison report (a new file)",
                                               str(left.source.with_name("comparison-report.pdf")), "PDF files (*.pdf)")
            if not p:
                return False
            if not p.lower().endswith(".pdf"):
                p += ".pdf"
            if protect.is_protected(p) or left.is_source(p) or right.is_source(p):
                QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
                continue
            dest = p
        if protect.is_protected(dest) or left.is_source(dest) or right.is_source(dest):
            QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
            return False
        spec = {"left": self._bytes(left), "right": self._bytes(right), "result": self.result,
                "report": os.path.abspath(dest), "colors": colors(self.win.settings)}
        keep = self.result
        self._start(spec, "report")
        self.result = keep
        if wait:
            from PySide6.QtCore import QEventLoop
            t0 = time.time()
            while self.job is not None and time.time() - t0 < 300:
                loop = QEventLoop()
                QTimer.singleShot(15, loop.quit)
                loop.exec()
        return True


class OptionsDialog(QDialog):
    def __init__(self, parent, o):
        super().__init__(parent)
        self.setWindowTitle("Compare options")
        f = QFormLayout(self)
        self.ws = QCheckBox("Ignore whitespace (spacing and line breaks)")
        self.case = QCheckBox("Ignore case")
        self.punct = QCheckBox("Ignore punctuation-only changes")
        self.running = QCheckBox("Ignore running headers / footers and page numbers")
        for cb, k in ((self.ws, "ignore_whitespace"), (self.case, "ignore_case"), (self.punct, "ignore_punct"),
                      (self.running, "ignore_running")):
            cb.setChecked(bool(o.get(k)))
            f.addRow(cb)
        self.lr = QLineEdit(o.get("left_range", "all"))
        self.rr = QLineEdit(o.get("right_range", "all"))
        for e in (self.lr, self.rr):
            e.setPlaceholderText("all, or e.g. 1-3, 5")
        f.addRow("Left pages", self.lr)
        f.addRow("Right pages", self.rr)
        self.thr = QSpinBox()
        self.thr.setRange(8, 200)
        self.thr.setValue(int(o.get("threshold", 48)))
        self.minr = QSpinBox()
        self.minr.setRange(1, 100)
        self.minr.setValue(int(o.get("min_region", 3)))
        f.addRow("Visual: grey-level difference that counts", self.thr)
        f.addRow("Visual: smallest region (cells of 4 px)", self.minr)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def values(self):
        return {"ignore_whitespace": self.ws.isChecked(), "ignore_case": self.case.isChecked(),
                "ignore_punct": self.punct.isChecked(), "ignore_running": self.running.isChecked(),
                "left_range": self.lr.text().strip() or "all", "right_range": self.rr.text().strip() or "all",
                "threshold": self.thr.value(), "min_region": self.minr.value()}


def ask_options(parent, o):
    d = OptionsDialog(parent, o)
    return d.values() if d.exec() == QDialog.Accepted else None
