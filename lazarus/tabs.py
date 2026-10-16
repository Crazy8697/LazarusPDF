"""Multi-document: one Session per open PDF (shown as a tab) and the compare view.

A Session owns everything that belongs to ONE open PDF: its working copy with its single
undo history (journal + snapshot steps), its protected-file registration, its own canvas
(zoom, page, text selection live there), the find index, and the view state of the shared
side panels (thumbnail selection/scroll, Overview selection, bookmark tree state), saved
when its tab is left and restored when it comes back. The snapshot memory budget stays
global (memguard), not per session.

The compare view splits the central area into two panes, a session on each side (two PDFs
or the same one twice), with optional synced scrolling. It is the shell only: the diff logic
arrives with stage 13."""
import itertools
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QProgressBar, QSplitter, QToolButton,
                               QVBoxLayout, QWidget)

from .pdfeditor import PAGE_GAP, PageCanvas, _icon


class LoadingView(QWidget):
    """Shown in a tab while its PDF loads in the background: real progress (bytes read, then
    pages measured); an animated busy bar while PyMuPDF parses (no measure exists there)."""

    def __init__(self, name):
        super().__init__()
        v = QVBoxLayout(self)
        v.addStretch(2)
        self.title = QLabel(f"Loading {name}…")
        self.title.setAlignment(Qt.AlignCenter)
        f = self.title.font()
        f.setPointSize(13)
        self.title.setFont(f)
        v.addWidget(self.title)
        self.bar = QProgressBar()
        self.bar.setFixedWidth(360)
        self.bar.setRange(0, 0)          # busy (animated) until a real measure arrives
        self.bar.setTextVisible(True)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.bar)
        row.addStretch(1)
        v.addLayout(row)
        self.phase = QLabel("Reading the file…")
        self.phase.setObjectName("legend")
        self.phase.setAlignment(Qt.AlignCenter)
        v.addWidget(self.phase)
        v.addStretch(3)

    def show_progress(self, phase, done, total):
        if phase == "read":
            self.phase.setText(f"Reading the file… {done / 2 ** 20:.0f} of {total / 2 ** 20:.0f} MB")
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(1000 * done / max(1, total)))
            self.bar.setFormat("%p%")
        elif phase == "parse":
            self.phase.setText("Opening the PDF…")
            self.bar.setRange(0, 0)       # animated: the parse is one PyMuPDF call
        else:
            self.phase.setText(f"Preparing pages… {done} of {total}")
            self.bar.setRange(0, max(1, total))
            self.bar.setValue(done)
            self.bar.setFormat(f"{done} / {total} pages")


class Session:
    _ids = itertools.count(1)
    is_job = False                        # a batch job's tab (batch_ui.JobSession) says True

    def __init__(self, path):
        self.sid = next(Session._ids)
        self.source = Path(path)
        self.work = None                  # WorkingDoc (working copy + unified undo history), once loaded
        self.loader = None                # loader.DocLoader while loading
        self.loading = LoadingView(self.source.name)
        self.canvas = PageCanvas()        # its own canvas: zoom, page and text selection stay with it
        self.canvas.setAcceptDrops(False)     # file drops are handled by the main window
        self.doc_version = 0              # bumps on every change (views re-render)
        # side-panel view state, saved when the tab is left
        self.thumb_rows = []
        self.thumb_scroll = 0
        self.overview_sel = []
        self.bookmarks = None
        # find (Ctrl+F) index for this PDF; kept while the tab is open
        self.find_index = None
        self.find_key = None

    @property
    def owner(self):
        """protect.py owner key for this session's original."""
        return f"editor:{self.sid}"

    @property
    def name(self):
        return self.source.name

    @property
    def ready(self):
        return self.work is not None

    def is_source(self, path):
        try:
            return Path(path).resolve() == self.source.resolve()
        except OSError:
            return False

    def dirty(self):
        return self.work is not None and self.work.dirty()

    def close(self):
        if self.loader is not None:
            self.loader.cancel()          # the loader closes whatever it opened
            self.loader = None
        if self.find_index is not None:
            self.find_index.close()
            self.find_index = None
        self.canvas.doc = None            # nothing left to render
        if self.work is not None:
            self.work.close()
        self.canvas.deleteLater()
        if self.loading is not None:
            self.loading.deleteLater()
            self.loading = None


class CompareView(QWidget):
    """Two panes side by side. The left pane follows the active tab (so the side panels and
    tools stay on it); the right pane shows any open PDF, or the same one again. View only.
    The diff itself (stage 13) is compare_ui.CompareController, which adds its own row."""
    closeRequested = Signal()

    def __init__(self, ed):
        super().__init__()
        self.ed = ed
        self.sessions = [None, None]
        self._syncing = False
        self.page_map = None          # ({left page: right page}, {right: left}) from a compare (stage 13)
        self.on_changed = None        # f(session): a shown document changed (compare results clear)
        self.on_pair = None           # f(): another PDF is shown on a side
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("comparebar")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 3, 8, 3)
        lab = QLabel(" COMPARE ")
        lab.setObjectName("grouplabel")
        h.addWidget(lab)
        self.combos = [QComboBox(), QComboBox()]
        for k, c in enumerate(self.combos):
            c.setMinimumWidth(220)
            c.setSizeAdjustPolicy(QComboBox.AdjustToContents)
            c.activated.connect(lambda i, k=k: self._picked(k, i))
        h.addWidget(QLabel("Left:"))
        h.addWidget(self.combos[0])
        h.addSpacing(12)
        h.addWidget(QLabel("Right:"))
        h.addWidget(self.combos[1])
        h.addSpacing(12)
        self.sync = QCheckBox("Synced scrolling")
        self.sync.setChecked(True)
        self.sync.setToolTip("Scroll both panes together (same page, same position on it)")
        h.addWidget(self.sync)
        h.addStretch(1)
        note = QLabel("Compare runs from the row below (F7: next change).")
        note.setObjectName("legend")
        h.addWidget(note)
        close = QToolButton()
        close.setIcon(_icon("close"))
        close.setIconSize(QSize(16, 16))
        close.setToolTip("Close the compare view")
        close.clicked.connect(self.closeRequested.emit)
        h.addWidget(close)
        v.addWidget(bar)
        self.split = QSplitter(Qt.Horizontal)
        self.panes = [PageCanvas(), PageCanvas()]
        for k, p in enumerate(self.panes):
            p.setAcceptDrops(False)
            p.verticalScrollBar().valueChanged.connect(lambda _v, k=k: self._sync(k))
            self.split.addWidget(p)
        v.addWidget(self.split, 1)

    # ---------- which sessions ----------
    def _fill(self):
        for k, c in enumerate(self.combos):
            c.blockSignals(True)
            c.clear()
            for s in self.ed.sessions:
                if s.ready:
                    c.addItem(s.name, s.sid)
            s = self.sessions[k]
            if s is not None:
                c.setCurrentIndex(max(0, c.findData(s.sid)))
            c.blockSignals(False)

    def open(self, left, right):
        self.sessions = [None, None]
        self.show_pair(left, right)

    def show_pair(self, left, right):
        if self.on_pair is not None and (left, right) != tuple(self.sessions) and any(self.sessions):
            self.on_pair()
        for k, s in enumerate((left, right)):
            if s is not self.sessions[k]:
                self.sessions[k] = s
                self._bind(k)
        self._fill()

    def _picked(self, k, i):
        sid = self.combos[k].itemData(i)
        s = next((x for x in self.ed.sessions if x.sid == sid), None)
        if s is None:
            return
        if k == 0:
            self.ed.activate(s)            # the left pane is the active tab
        else:
            self.show_pair(self.sessions[0], s)

    def _bind(self, k):
        s, p = self.sessions[k], self.panes[k]
        if s is None:
            p.doc, p.pages = None, []
            p.scene().clear()
            return
        p.set_document(s.work.doc)

    # ---------- keep up with the sessions ----------
    def refresh(self, session):
        """session's document changed (an edit, undo/redo, a snapshot swap)."""
        if self.on_changed is not None:
            self.on_changed(session)
        for k, s in enumerate(self.sessions):
            if s is not session:
                continue
            p = self.panes[k]
            doc = s.work.doc
            if p.doc is not doc or len(p.pages) != doc.page_count:
                zoom, cur = p.zoom, max(0, p.current_page())
                p.set_document(doc)
                p.set_zoom(zoom)
                p.goto(min(cur, doc.page_count - 1))
            else:
                p.invalidate()

    def session_closed(self, session):
        if session in self.sessions:
            rest = [s for s in self.ed.sessions if s is not session and s.ready]
            if not rest:
                self.closeRequested.emit()
                return
            left = self.sessions[0] if self.sessions[0] is not session else rest[0]
            right = self.sessions[1] if self.sessions[1] is not session else left
            self.show_pair(left, right)
        else:
            self._fill()

    # ---------- synced scrolling ----------
    def _sync(self, k):
        if self._syncing or not self.sync.isChecked():
            return
        src, dst = self.panes[k], self.panes[1 - k]
        if not src.pages or not dst.pages:
            return
        top = src.visible_rect().top()
        i = next((n for n, pg in enumerate(src.pages) if top <= pg["rect"].bottom() + PAGE_GAP), len(src.pages) - 1)
        r = src.pages[i]["rect"]
        frac = (top - r.top()) / max(1.0, r.height())
        j = min(i, len(dst.pages) - 1)
        if self.page_map is not None:                  # follow the compare's page alignment
            m = self.page_map[0] if k == 0 else self.page_map[1]
            base = next((p for p in range(i, -1, -1) if p in m), None)
            if base is not None:
                j = max(0, min(m[base] + (i - base), len(dst.pages) - 1))
        dr = dst.pages[j]["rect"]
        dv = dst.visible_rect()
        self._syncing = True
        try:
            dst.centerOn(dv.center().x(), dr.top() + frac * dr.height() + dv.height() / 2)
        finally:
            self._syncing = False
