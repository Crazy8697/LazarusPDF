"""Find bar (Ctrl+F) for the PDF Editor and Overview. Read-only: nothing here changes the
document, the unsaved flag, or anything that gets saved or exported. Hit highlights are
view-only scene items on the canvas, not annotations.

The search runs in small chunks on a timer so a book-length PDF stays responsive. The hit
count updates live ("Searching… 120"), then reads "3 of 17".

Building the index (the first search of a document, or reaching pages not yet indexed):
if it is still running after LOCK_DELAY, the count area reads "Indexing X/Y" (pages indexed
so far, climbing as it goes) and the rest of
the window is locked (widgets AND shortcuts, tab switching included) so stray input can't
queue; only the query field and Cancel stay live, plus the window's close button, which
cancels first. Cancel (or Esc) stops between slices and KEEPS what is indexed: the count
reads "3 of 12+ · Partial (120/400)" and Enter/F3 step through those hits. The next search
re-matches the indexed pages from cache and indexes on from there; when it completes it is
a normal full result. A fully indexed document never locks anything."""
import re

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence, QPen, QShortcut
from PySide6.QtWidgets import QCheckBox, QDockWidget, QGraphicsRectItem, QLabel, QLineEdit, QToolBar

from .pdfeditor import _icon
from .search import SearchIndex, compile_query

import time

TICK_BUDGET = 0.03      # seconds of searching per timer tick (keeps the UI responsive)
DRAW_LIMIT = 2000       # highlight boxes drawn up front; the rest are drawn when navigated to
LOCK_DELAY = 250        # ms: an index build still running after this shows "Indexing…" + locks
HIT_FILL = QColor(255, 213, 0, 90)
CUR_FILL = QColor(255, 120, 0, 150)
CUR_PEN = QColor("#ff6d00")


class FindController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self._canvas = None         # the canvas the highlight boxes are on
        self.hits = []              # [(page, first char, last char)] in document order
        self.cur = -1
        self._items = {}            # hit index -> [QGraphicsRectItem]
        self._scan = None
        self._pattern = None
        self.partial = None         # (pages indexed, total) after Cancel; None = a full result
        self.locked = False
        self.on_close = []          # called when the bar closes (the Replace row follows it)
        self.lock_count = 0         # times the lock engaged (self-test: small builds never lock)
        self._locked_widgets, self._locked_actions, self._locked_shortcuts = [], [], []
        self._build()
        self._lock_timer = QTimer(self, singleShot=True, interval=LOCK_DELAY)
        self._lock_timer.timeout.connect(self._maybe_lock)
        self._debounce = QTimer(self, singleShot=True, interval=150)
        self._debounce.timeout.connect(self.start_search)
        self._tick = QTimer(self, interval=0)
        self._tick.timeout.connect(self._scan_some)
        self.ed.docChanged.connect(self._doc_changed)
        self.ed.sessionChanged.connect(self._session_changed)

    # ---------- the index lives in the active tab's Session ----------
    @property
    def index(self):
        return self.ed.cur.find_index if self.ed.cur else None

    @index.setter
    def index(self, v):
        if self.ed.cur:
            self.ed.cur.find_index = v

    @property
    def _index_key(self):
        return self.ed.cur.find_key if self.ed.cur else None

    @_index_key.setter
    def _index_key(self, v):
        if self.ed.cur:
            self.ed.cur.find_key = v

    def _session_changed(self):
        """Another tab: drop the old tab's hits and boxes; search the new one (its own index)."""
        self._tick.stop()
        self._set_building(False)
        self.partial = None
        self._clear_items()
        self.hits, self.cur = [], -1
        self.count.setText("")
        if self.ed.doc is None:
            if self.bar.isVisible():
                self.close()
            return
        if self.bar.isVisible() and self.edit.text().strip():
            self.start_search()
        self.update_enabled()

    # ---------- UI ----------
    def _build(self):
        win = self.win
        bar = QToolBar("Find", win)
        bar.setObjectName("findbar")
        bar.setMovable(False)
        lab = QLabel(" FIND ")
        lab.setObjectName("grouplabel")
        bar.addWidget(lab)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Find text…  (Enter: next, Shift+Enter: previous)")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(280)
        self.edit.textChanged.connect(lambda _: self._debounce.start())
        self.edit.installEventFilter(self)
        bar.addWidget(self.edit)
        self.count = QLabel("")
        self.count.setMinimumWidth(110)
        self.count.setAlignment(Qt.AlignCenter)
        bar.addWidget(self.count)
        self.act_cancel = QAction(_icon("close-circle-outline"), "Cancel indexing", win)
        self.act_cancel.setToolTip("Stop indexing now (Esc). What is indexed so far is kept; "
                                   "the next search carries on from there.")
        self.act_cancel.triggered.connect(lambda _=False: self.cancel())
        bar.addAction(self.act_cancel)
        self.act_cancel.setVisible(False)
        self.act_prev = QAction(_icon("chevron-up"), "Previous match", win, shortcut="Shift+F3")
        self.act_next = QAction(_icon("chevron-down"), "Next match", win, shortcut="F3")
        self.act_prev.triggered.connect(lambda _=False: self.step(-1))
        self.act_next.triggered.connect(lambda _=False: self.step(1))
        bar.addAction(self.act_prev)
        bar.addAction(self.act_next)
        bar.addSeparator()
        self.case = QCheckBox("Match case")
        self.word = QCheckBox("Whole word")
        self.regex = QCheckBox("Regex")
        for cb in (self.case, self.word, self.regex):
            cb.toggled.connect(lambda _: self.start_search())
            bar.addWidget(cb)
        bar.addSeparator()
        self.act_redact = QAction(_icon("format-color-marker-cancel"), "Mark all hits for redaction", win)
        self.act_redact.setToolTip("Mark every hit for redaction (one undo step); review, then Edit › Apply redactions")
        self.act_redact.triggered.connect(lambda _=False: self.win.editor_mode.stage10.redact_hits())
        bar.addAction(self.act_redact)
        close = QAction(_icon("close"), "Close find bar (Esc)", win)
        close.triggered.connect(lambda _=False: self.close())
        bar.addAction(close)
        self.act_close = close
        self._esc = QShortcut(QKeySequence(Qt.Key_Escape), bar, activated=self.escape,
                              context=Qt.WidgetWithChildrenShortcut)
        win.addToolBar(Qt.BottomToolBarArea, bar)
        bar.hide()
        self.bar = bar
        self.act_find = QAction(_icon("magnify"), "Find…", win, shortcut=QKeySequence.Find)
        self.act_find.setToolTip("Find text in the PDF (Ctrl+F)")
        self.act_find.triggered.connect(lambda _=False: self.open())
        for a in (self.act_find, self.act_next, self.act_prev):
            win.addAction(a)            # window-level: works in the editor and Overview
        self.update_enabled()

    def eventFilter(self, obj, ev):
        if obj is self.edit and ev.type() == QEvent.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.step(-1 if ev.modifiers() & Qt.ShiftModifier else 1)
                return True
            if ev.key() == Qt.Key_Escape:
                self.escape()
                return True
        return False

    def escape(self):
        """Esc: during an index build it cancels (keeping the partial index); otherwise it
        closes the bar."""
        if self.building():
            self.cancel()
        else:
            self.close()

    def update_enabled(self):
        ok = getattr(self.win, "mode", None) in ("editor", "overview") and self.ed.doc is not None
        self.act_find.setEnabled(ok)
        live = ok and self.bar.isVisible() and bool(self.hits)
        self.act_next.setEnabled(live)
        self.act_prev.setEnabled(live)
        self.act_redact.setEnabled(live and self.partial is None and getattr(self.win, "mode", None) == "editor")
        if not ok and self.bar.isVisible():
            self.close()

    def open(self):
        if self.ed.doc is None:
            return
        self.bar.show()
        self.edit.setFocus()
        self.edit.selectAll()
        if self.edit.text():
            self.start_search()
        self.update_enabled()

    def close(self):
        for f in self.on_close:
            f()
        self._tick.stop()
        self._set_building(False)
        self.partial = None
        self._clear_items()
        self.hits, self.cur = [], -1
        self.count.setText("")
        self.bar.hide()
        self.update_enabled()
        if self.win.mode == "editor":
            self.ed.canvas.setFocus()

    # ---------- searching ----------
    def _ensure_index(self):
        key = (id(self.ed.doc), self.ed.doc_version)
        if self.index is None or self._index_key != key:
            if self.index is not None:
                self.index.close()
            known = self.ed.work.known_bytes() if self.ed.work is not None else None
            self.index = SearchIndex(self.ed.doc, known)   # read-only snapshot of the working copy
            self._index_key = key

    def start_search(self, keep=None):
        """(Re)start matching from page 1. Indexed pages are matched from cache; indexing goes
        on from the first page not yet indexed. A query change mid-build lands here too: the
        lock (if engaged) stays, nothing indexed is redone."""
        self._tick.stop()
        self._clear_items()
        self.hits, self.cur = [], -1
        self.partial = None
        q = self.edit.text()
        if not self.bar.isVisible() or self.ed.doc is None or not q.strip():
            self._set_building(False)
            self.count.setText("")
            self.update_enabled()
            return
        try:
            self._pattern = compile_query(q, self.case.isChecked(), self.word.isChecked(), self.regex.isChecked())
        except re.error as e:
            self._set_building(False)
            self.count.setText("Bad regex")
            self.count.setToolTip(str(e))
            self.update_enabled()
            return
        self.count.setToolTip("")
        if self._pattern is None:
            self._set_building(False)
            self.count.setText("")
            return
        self._ensure_index()
        self._scan = 0
        self._keep = keep
        self.count.setText(self._indexing_text() if self.locked else "Searching…")
        if not self.index.complete() and not self.locked:
            self._lock_timer.start()          # still building after LOCK_DELAY: indicator + lock
        self._tick.start()

    def _scan_some(self):
        idx = self.index
        t0 = time.perf_counter()
        p = self._scan
        while p < idx.page_count and time.perf_counter() - t0 < TICK_BUDGET:
            for a, b in idx.find(p, self._pattern):
                self.hits.append((p, a, b))
                if len(self._items) < DRAW_LIMIT:
                    self._draw(len(self.hits) - 1)
            p += 1
        self._scan = stop = p
        if stop < idx.page_count:
            self.count.setText(self._indexing_text() if self.locked else f"Searching… {len(self.hits)}")
            return
        self._tick.stop()
        self._set_building(False)                # indexed in full: unlock the instant it's done
        self._finish()

    def _finish(self):
        """Show the result of the scan so far (full, or partial after Cancel)."""
        if not self.hits:
            self.count.setText("No matches" if self.partial is None else
                               f"No matches yet · Partial ({self.partial[0]}/{self.partial[1]})")
            self.update_enabled()
            return
        if self._keep is not None:
            first = min(self._keep, len(self.hits) - 1)
        else:
            here = max(0, self.ed.canvas.current_page())
            first = next((k for k, h in enumerate(self.hits) if h[0] >= here), 0)
        self.go(first)
        self.update_enabled()

    def step(self, d):
        if not self.bar.isVisible():
            return
        if not self.hits:
            if self.edit.text().strip() and not self._tick.isActive():
                self.start_search()
            return
        self.go((self.cur + d) % len(self.hits))          # wraps at both ends

    def go(self, k):
        if self.cur >= 0:
            self._style(self.cur, False)
        self.cur = k
        self._style(k, True)
        self.count.setText(self._count_text(k))
        p, a, b = self.hits[k]
        for j, h in enumerate(self.hits):            # boxes for every hit on this page exist
            if h[0] == p and j not in self._items:
                self._draw(j)
        self._style(k, True)
        rects = self.index.page(p).rects(a, b)
        canvas = self.ed.canvas
        if rects and p < len(canvas.pages):
            if canvas.current_page() != p:
                canvas.goto(p)                       # the hit's page becomes the current page
            sr = canvas.page_to_scene(p, rects[0])
            canvas.ensureVisible(sr, 60, 120)
        if self.win.mode == "overview":
            grid = self.win.overview_mode.grid
            if grid.doc is not None and p < len(grid.items):
                grid.set_selection([p])
                grid.ensureVisible(grid.items[p]["frame"])

    def _count_text(self, k):
        if self.partial is None:
            return f"{k + 1} of {len(self.hits)}"
        x, y = self.partial
        return f"{k + 1} of {len(self.hits)}+ · Partial ({x}/{y})"   # never reads as a full result

    # ---------- index builds: indicator, lock, cancel ----------
    def building(self):
        """An index build is running: the scan is active on a not-fully-indexed document."""
        idx = self.index
        return self._tick.isActive() and idx is not None and not idx.complete()

    def _maybe_lock(self):
        if self.building():
            self._set_building(True)
            self.count.setText(self._indexing_text())

    def _indexing_text(self):
        """Real progress: pages indexed so far / total (same page measure as "Partial (X/Y)")."""
        idx = self.index
        return f"Indexing {idx.indexed()}/{idx.page_count}" if idx is not None else "Indexing"

    def cancel(self):
        """Stop between slices; KEEP the partial index and show the partial result."""
        if not self._tick.isActive():
            return
        self._tick.stop()
        idx = self.index
        self.partial = (idx.indexed(), idx.page_count) if idx is not None and not idx.complete() else None
        self._set_building(False)
        self._keep = None
        self._finish()

    def _set_building(self, on):
        """Lock (on) / unlock the window around an index build."""
        if not on:
            self._lock_timer.stop()
        if on == self.locked:
            return
        self.locked = on
        win = self.win
        self.act_cancel.setVisible(on)
        if on:
            self.lock_count += 1
            keep_actions = {self.act_cancel}
            widgets = [win.centralWidget(), win.menuBar()]
            widgets += [d for d in win.findChildren(QDockWidget)]
            widgets += [t for t in win.findChildren(QToolBar) if t is not self.bar]
            widgets += [self.case, self.word, self.regex]
            self._locked_widgets = [w for w in widgets if w is not None and w.isEnabled()]
            for w in self._locked_widgets:
                w.setEnabled(False)
            # the find bar's own actions stay (the query field sits in one: a QWidgetAction),
            # except stepping and closing
            bar_actions = set(self.bar.actions()) - {self.act_prev, self.act_next, self.act_close}
            self._locked_actions = [a for a in win.findChildren(QAction)
                                    if a.isEnabled() and a not in keep_actions and a not in bar_actions]
            for a in self._locked_actions:
                a.setEnabled(False)              # shortcuts (Ctrl+Z/S/O, modes, tabs…) can't fire
            self._locked_shortcuts = [s for s in win.findChildren(QShortcut)
                                      if s.isEnabled() and s is not self._esc]
            for s in self._locked_shortcuts:
                s.setEnabled(False)
            self.edit.setFocus()
        else:
            for w in self._locked_widgets:
                w.setEnabled(True)
            for a in self._locked_actions:
                a.setEnabled(True)
            for s in self._locked_shortcuts:
                s.setEnabled(True)
            self._locked_widgets, self._locked_actions, self._locked_shortcuts = [], [], []
            self.update_enabled()

    # ---------- highlights (view-only scene items) ----------
    def _draw(self, k):
        p, a, b = self.hits[k]
        canvas = self.ed.canvas
        if p >= len(canvas.pages):
            return
        items = []
        self._canvas = canvas
        for r in self.index.page(p).rects(a, b):
            it = QGraphicsRectItem(canvas.page_to_scene(p, r).adjusted(-1, -1, 1, 1))
            it.setBrush(HIT_FILL)
            it.setPen(Qt.NoPen)
            it.setZValue(2)
            canvas.scene().addItem(it)
            items.append(it)
        self._items[k] = items

    def _style(self, k, current):
        for it in self._items.get(k, []):
            try:
                it.setBrush(CUR_FILL if current else HIT_FILL)
                it.setPen(QPen(CUR_PEN, 0) if current else Qt.NoPen)
                it.setZValue(3 if current else 2)
            except RuntimeError:          # scene was rebuilt underneath us
                pass

    def _clear_items(self):
        try:
            scene = self._canvas.scene() if self._canvas is not None else None
        except RuntimeError:              # that tab's canvas is gone
            scene = None
        for items in self._items.values():
            for it in items:
                try:
                    if it.scene() is scene:
                        scene.removeItem(it)
                except RuntimeError:
                    pass
        self._items = {}

    def _doc_changed(self):
        """The working copy changed (edit, page op, undo, new PDF): re-index and re-run."""
        if self.index is not None and self._index_key != (id(self.ed.doc), self.ed.doc_version):
            self.index.close()
            self.index = None
        if self.ed.doc is None:
            self.close()
            return
        if self.bar.isVisible() and self.edit.text().strip():
            keep = self.cur if self.cur >= 0 else None
            self.partial = None           # that index is gone (partial or full)
            self._clear_items()           # removes live boxes; skips any the scene rebuild already deleted
            QTimer.singleShot(0, lambda: self.start_search(keep=keep))
        self.update_enabled()
