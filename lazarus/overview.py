"""Overview mode: every page in a zoomable grid. This is the home of the page operations.

Selection: click selects a page, Ctrl+click toggles one, Shift+click selects a range,
Ctrl+A selects all. Dragging selected pages shows a drop bar and moves them there.
The selection drives the page-op buttons (PAGES group, Pages panel, Page menu).
With nothing open in the editor, the grid shows numbered placeholder pages."""
import time

import pymupdf
from PySide6.QtCore import QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView, QLabel, QSpinBox, QToolBar)

from .pdfeditor import RENDER_BUDGET, TOOLS, _icon, _qimage, render_turn_taken, rendered_now

CELL_W = 100.0            # scene units per page cell (page scaled to fit)
CELL_H = 130.0
GAP = 14.0
LABEL_H = 14.0
MAX_ACROSS, MAX_DOWN = 30, 10     # zoom-out floor: never more than ~30 x 10 pages in view
PLACEHOLDER_PAGES = 120
THUMB_PX = 140            # render width for real pages (plenty at overview sizes)
PRESETS = [(30, 10), (15, 5), (8, 3)]


class OverviewGrid(QGraphicsView):
    selectionChanged = Signal()
    reorderRequested = Signal(list, int)     # pages, insert-before index
    pagesDropped = Signal(dict, int)         # pages dragged from another tab / pane: payload, insert-before (17)

    def __init__(self, status):
        super().__init__()
        self.origin = None                   # f() -> the shown tab's session id (set by the editor)
        self.selected = set()
        self._anchor = None
        self._sel_items = []
        self._drop_bar = None
        self._press = None
        self.status = status
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#22262e"))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setFrameShape(QFrame.NoFrame)
        self.setAcceptDrops(True)            # page drags only (17); file drops go on to the main window
        self.cols = 8
        self.zoom = 1.0
        self.doc = None
        self.items = []           # [{"frame", "pix", "rendered"}]
        self.sizes = []           # (w, h) of each page in points
        self._render = QTimer(self, singleShot=True, interval=80)
        self._render.timeout.connect(self.render_visible)
        self.verticalScrollBar().valueChanged.connect(lambda _: self._render.start())

    # ---------- content ----------
    def set_document(self, doc, sizes=None):
        self.doc = doc
        if doc is not None:
            self.sizes = sizes or [(doc[i].rect.width, doc[i].rect.height) for i in range(doc.page_count)]
        else:
            self.sizes = [(612, 792)] * PLACEHOLDER_PAGES
        self.relayout()

    def relayout(self):
        sc = self.scene()
        sc.clear()
        self.items = []
        f = QFont()
        f.setPointSize(7)
        for i, (w, h) in enumerate(self.sizes):
            r, c = divmod(i, self.cols)
            s = min(CELL_W / w, CELL_H / h)
            pw, ph = w * s, h * s
            x = c * (CELL_W + GAP) + (CELL_W - pw) / 2
            y = r * (CELL_H + GAP + LABEL_H) + (CELL_H - ph)
            frame = QGraphicsRectItem(QRectF(x, y, pw, ph))
            frame.setBrush(QBrush(QColor("#f4f4f4") if self.doc else QColor("#3a404c")))
            frame.setPen(QPen(QColor("#4a5262"), 0))
            sc.addItem(frame)
            pix = QGraphicsPixmapItem()
            pix.setPos(x, y)
            pix.setTransformationMode(Qt.SmoothTransformation)
            pix.setZValue(1)
            sc.addItem(pix)
            lab = QGraphicsSimpleTextItem(str(i + 1))
            lab.setFont(f)
            lab.setBrush(QColor("#8a94a6"))
            lb = lab.boundingRect()
            lab.setPos(c * (CELL_W + GAP) + (CELL_W - lb.width()) / 2, y + ph + 2)
            sc.addItem(lab)
            if not self.doc:
                num = QGraphicsSimpleTextItem(str(i + 1))
                nf = QFont()
                nf.setPointSize(20)
                num.setFont(nf)
                num.setBrush(QColor("#5c6370"))
                nb = num.boundingRect()
                num.setPos(x + (pw - nb.width()) / 2, y + (ph - nb.height()) / 2)
                num.setZValue(2)
                sc.addItem(num)
            self.items.append({"frame": QRectF(x, y, pw, ph), "pix": pix, "rendered": False, "size": (pw, ph)})
        rows = (len(self.sizes) + self.cols - 1) // self.cols
        sc.setSceneRect(QRectF(-GAP, -GAP, self.cols * (CELL_W + GAP) + GAP,
                               rows * (CELL_H + GAP + LABEL_H) + GAP))
        self._sel_items, self._drop_bar = [], None
        self.selected = {i for i in self.selected if i < len(self.sizes)}
        self._draw_selection()
        self._render.start()

    def refresh(self, doc, select=None):
        """The document changed (page op / undo): rebuild in place, keeping zoom and scroll."""
        v, h = self.verticalScrollBar().value(), self.horizontalScrollBar().value()
        if select is not None:
            self.selected = set(select)
        sizes = [(doc[i].rect.width, doc[i].rect.height) for i in range(doc.page_count)] if doc is not None else None
        if doc is not None and doc is self.doc and sizes == self.sizes:
            for it in self.items:                  # same layout: only the pictures are out of date
                it["rendered"] = False
            self._draw_selection()
            self._render.start()
        else:
            self.set_document(doc, sizes)
        self.verticalScrollBar().setValue(v)
        self.horizontalScrollBar().setValue(h)
        self.selectionChanged.emit()

    # ---------- selection ----------
    def _cell(self, i):
        r, c = divmod(i, self.cols)
        return QRectF(c * (CELL_W + GAP) - GAP / 2, r * (CELL_H + GAP + LABEL_H) - GAP / 2,
                      CELL_W + GAP, CELL_H + GAP + LABEL_H)

    def index_at(self, pt):
        if not self.doc:
            return None
        for i in range(len(self.items)):
            if self._cell(i).contains(pt):
                return i
        return None

    def gap_at(self, pt):
        """Insert-before index for a drop at scene point pt."""
        n = len(self.items)
        row = int(max(0, pt.y()) // (CELL_H + GAP + LABEL_H))
        col = round(pt.x() / (CELL_W + GAP))
        col = max(0, min(self.cols, col))
        return max(0, min(n, row * self.cols + col))

    def _draw_selection(self):
        for it in self._sel_items:
            self.scene().removeItem(it)
        self._sel_items = []
        for i in sorted(self.selected):
            if i >= len(self.items):
                continue
            box = QGraphicsRectItem(self.items[i]["frame"].adjusted(-3, -3, 3, 3))
            box.setBrush(QColor(45, 108, 223, 60))
            box.setPen(QPen(QColor("#4f8cff"), 3))
            box.setZValue(4)
            self.scene().addItem(box)
            self._sel_items.append(box)

    def set_selection(self, pages):
        self.selected = set(pages)
        self._draw_selection()
        self.selectionChanged.emit()

    def select_all(self):
        if self.doc:
            self.set_selection(range(len(self.items)))

    def _show_drop(self, pt):
        k = self.gap_at(pt)
        r, c = divmod(k, self.cols)
        if c == 0 and k == len(self.items) and k:          # after the last page of a full row
            r, c = divmod(k - 1, self.cols)
            c += 1
        x = c * (CELL_W + GAP) - GAP / 2 - 2
        y = r * (CELL_H + GAP + LABEL_H)
        if self._drop_bar is None:
            self._drop_bar = QGraphicsRectItem()
            self._drop_bar.setBrush(QColor("#4f8cff"))
            self._drop_bar.setPen(Qt.NoPen)
            self._drop_bar.setZValue(6)
            self.scene().addItem(self._drop_bar)
        self._drop_bar.setRect(QRectF(x, y, 4, CELL_H))
        self._drop_bar.show()
        return k

    def _hide_drop(self):
        if self._drop_bar is not None:
            self._drop_bar.hide()

    def render_visible(self):
        if self.doc is None or self.doc.is_closed:   # the editor may have replaced/closed it
            return
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        vr = vr.adjusted(0, -vr.height(), 0, vr.height())
        dpr = self.devicePixelRatioF()
        if render_turn_taken():
            QTimer.singleShot(5, self.render_visible)      # another renderer just ran: next turn
            return
        t0 = time.perf_counter()
        for i, it in enumerate(self.items):
            if it["rendered"] or not it["frame"].intersects(vr):
                continue
            page = self.doc[i]
            s = THUMB_PX * dpr / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
            qp = QPixmap.fromImage(_qimage(pix))
            it["pix"].setPixmap(qp)
            it["pix"].setScale(it["size"][0] / qp.width())
            it["rendered"] = True
            rendered_now()
            if time.perf_counter() - t0 > RENDER_BUDGET:   # keep the UI responsive: carry on next turn
                QTimer.singleShot(0, self.render_visible)
                break

    # ---------- zoom / density ----------
    def floor_zoom(self):
        vw, vh = max(self.viewport().width(), 1), max(self.viewport().height(), 1)
        return max(vw / (MAX_ACROSS * (CELL_W + GAP)), vh / (MAX_DOWN * (CELL_H + GAP + LABEL_H)))

    def set_zoom(self, z):
        self.zoom = max(self.floor_zoom(), min(4.0, z))
        self.resetTransform()
        self.scale(self.zoom, self.zoom)
        self._render.start()

    def set_density(self, across, down):
        """Lay out `across` pages per row and zoom so about across x down pages fill the view."""
        self.cols = max(1, across)
        self.relayout()
        vw, vh = self.viewport().width() - 4, self.viewport().height() - 4
        z = min(vw / (across * (CELL_W + GAP) + GAP), vh / (down * (CELL_H + GAP + LABEL_H) + GAP))
        self.set_zoom(z)
        self.verticalScrollBar().setValue(0)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.set_zoom(self.zoom * (1.15 if e.angleDelta().y() > 0 else 1 / 1.15))
            e.accept()
        else:
            super().wheelEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.zoom < self.floor_zoom():
            self.set_zoom(self.zoom)
        self._render.start()

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.doc:
            return super().mousePressEvent(e)
        pt = self.mapToScene(e.position().toPoint())
        i = self.index_at(pt)
        mods = e.modifiers()
        if i is None:
            if not mods & (Qt.ControlModifier | Qt.ShiftModifier):
                self.set_selection([])
            self._press = None
            return
        if mods & Qt.ShiftModifier and self._anchor is not None:
            lo, hi = sorted((self._anchor, i))
            base = self.selected if mods & Qt.ControlModifier else set()
            self.set_selection(base | set(range(lo, hi + 1)))
            self._press = None
        elif mods & Qt.ControlModifier:
            self.set_selection(self.selected ^ {i})
            self._anchor = i
            self._press = None
        else:
            if i not in self.selected:
                self.set_selection([i])
            self._anchor = i
            self._press = (i, e.position().toPoint(), False)      # may become a drag
        e.accept()

    def mouseMoveEvent(self, e):
        if self._press and e.buttons() & Qt.LeftButton:
            i, start, dragging = self._press
            if dragging and not self.viewport().rect().contains(e.position().toPoint()):
                self._drag_out()                     # left the grid: on to another tab / pane (17)
                e.accept()
                return
            if dragging or (e.position().toPoint() - start).manhattanLength() > 6:
                self._press = (i, start, True)
                self.viewport().setCursor(Qt.ClosedHandCursor)
                self._show_drop(self.mapToScene(e.position().toPoint()))
            e.accept()
            return
        super().mouseMoveEvent(e)

    def _drag_out(self):
        """The reorder drag left the grid: carry on as a page drag that another tab (hover its tab
        to switch), the thumbnails or a compare pane can take; back here it is a reorder again."""
        from PySide6.QtGui import QDrag
        from .pdfeditor import page_mime
        self._press = None
        self._hide_drop()
        self.viewport().unsetCursor()
        drag = QDrag(self)
        drag.setMimeData(page_mime(self.origin() if self.origin else None, sorted(self.selected)))
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
        self._hide_drop()

    def _drop_ok(self, e):
        from .pdfeditor import page_payload
        p = page_payload(e.mimeData())
        if p is None or not self.doc:
            e.ignore()
            return None
        e.setDropAction(Qt.CopyAction)
        e.accept()
        return p

    def dragEnterEvent(self, e):
        if self._drop_ok(e) is not None:
            self._show_drop(self.mapToScene(e.position().toPoint()))

    def dragMoveEvent(self, e):
        if self._drop_ok(e) is not None:
            self._show_drop(self.mapToScene(e.position().toPoint()))

    def dragLeaveEvent(self, e):
        self._hide_drop()

    def dropEvent(self, e):
        self._hide_drop()
        p = self._drop_ok(e)
        if p is None:
            return
        k = self.gap_at(self.mapToScene(e.position().toPoint()))
        if p["sid"] == (self.origin() if self.origin else None):
            if p["pages"]:
                self.reorderRequested.emit(p["pages"], k)     # back onto its own grid: a reorder
            return
        self.pagesDropped.emit(p, k)

    def mouseReleaseEvent(self, e):
        if self._press:
            i, start, dragging = self._press
            self._press = None
            self.viewport().unsetCursor()
            if dragging:
                k = self.gap_at(self.mapToScene(e.position().toPoint()))
                self._hide_drop()
                self.reorderRequested.emit(sorted(self.selected), k)
            elif len(self.selected) > 1:
                self.set_selection([i])               # plain click inside a multi-selection
            e.accept()
            return
        super().mouseReleaseEvent(e)


class OverviewMode(QObject):
    key = "overview"
    title = "Overview"

    def __init__(self, win, editor_mode):
        super().__init__(win)
        self.win = win
        self.editor_mode = editor_mode
        self.active = False
        self._doc_shown = "unset"
        self.grid = OverviewGrid(self.status)
        self.widget = self.grid
        self.soon = {}
        self._build()

    def status(self, msg, ms=0):
        self.win.statusBar().showMessage(msg, ms)

    def _placeholder(self, key, icon, label, what):
        a = QAction(_icon(icon, on=False), label, self.win)
        a.setToolTip(f"<b>{label}</b><br>{what}<br><i>Not implemented yet</i>")
        a.triggered.connect(lambda: self.status(f"Not implemented yet: {label} — {what}.", 5000))
        a.setProperty("soon", True)
        self.soon[key] = a
        return a

    def _build(self):
        win = self.win
        # row 1: zoom for the grid
        self.act_zin = QAction(_icon("magnify-plus-outline"), "Zoom in (grid)", win,
                               triggered=lambda: self.grid.set_zoom(self.grid.zoom * 1.25))
        self.act_zout = QAction(_icon("magnify-minus-outline"), "Zoom out (grid)", win,
                                triggered=lambda: self.grid.set_zoom(self.grid.zoom * 0.8))
        self.act_fit = QAction(_icon("fit-to-screen-outline"), "Fit grid to view", win,
                               triggered=self.apply_density)
        top = QToolBar("Overview", win)      # parented: never a stray top-level window
        top.setObjectName("overview_top")
        top.setMovable(False)
        top.setIconSize(QSize(20, 20))
        top.addAction(self.act_zout)
        top.addAction(self.act_zin)
        top.addAction(self.act_fit)
        top.addSeparator()
        self.count_label = QLabel("")
        self.count_label.setObjectName("legend")
        top.addWidget(self.count_label)
        self.top_bar = top

        # row 2: two movable groups, GRID (density) and PAGES (page operations)
        def group(title):
            bar = QToolBar(title, win)
            bar.setObjectName("tools")
            bar.setMovable(True)
            bar.setFloatable(False)
            bar.setAllowedAreas(Qt.TopToolBarArea | Qt.LeftToolBarArea | Qt.RightToolBarArea)
            bar.setIconSize(QSize(20, 20))
            bar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            bar.setToolTip(f"{title} — drag the dotted handle: along a row, up to row 1, or to a side")
            lab = QLabel(f" {title.upper()} ")
            lab.setObjectName("grouplabel")
            bar.addWidget(lab)
            return bar

        grid = group("Grid")
        self.spin_w = QSpinBox()
        self.spin_w.setRange(1, MAX_ACROSS)
        self.spin_w.setValue(8)
        self.spin_w.setToolTip("Pages wide")
        self.spin_h = QSpinBox()
        self.spin_h.setRange(1, MAX_DOWN)
        self.spin_h.setValue(3)
        self.spin_h.setToolTip("Pages high")
        for sp in (self.spin_w, self.spin_h):
            sp.setKeyboardTracking(False)
            sp.valueChanged.connect(lambda _: self.apply_density())
        grid.addWidget(QLabel(" wide "))
        grid.addWidget(self.spin_w)
        grid.addWidget(QLabel(" × high "))
        grid.addWidget(self.spin_h)
        grid.addSeparator()
        for w_, h_ in PRESETS:
            # text-only, so the preset stays readable in the compact (icon-only) row-1 form
            a = QAction(f"{w_}×{h_}", win, triggered=lambda _=False, a_=w_, b_=h_: self.preset(a_, b_))
            a.setToolTip(f"Show about {w_} pages across × {h_} down")
            grid.addAction(a)

        pages = group("Pages")
        for key, icon, label, what in TOOLS["Pages"]:
            pages.addAction(self._placeholder(key, icon, label, what))
        for a in pages.actions():
            w = pages.widgetForAction(a)
            if w is not None and a.property("soon"):
                w.setProperty("soon", True)
        self.groups = [grid, pages]
        self._bar_vis = {}
        self._was_on = False

        self.menus = []
        self.file_entries = []
        self.view_entries = [self.act_zin, self.act_zout, self.act_fit]

    def toolbars(self):
        return [self.top_bar] + self.groups

    def preset(self, w, h):
        for sp, v in ((self.spin_w, w), (self.spin_h, h)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.blockSignals(False)
        self.apply_density()

    def apply_density(self):
        self.grid.set_density(self.spin_w.value(), self.spin_h.value())
        self.status(f"Overview grid: {self.spin_w.value()} wide × {self.spin_h.value()} high.", 3000)

    def set_active(self, on):
        self.active = on
        self.top_bar.setVisible(on)
        for g in self.groups:               # remember View-menu choices across mode switches
            if on:
                g.setVisible(self._bar_vis.get(g, True))
            else:
                if self._was_on:
                    self._bar_vis[g] = not g.isHidden()
                g.hide()
        self._was_on = on
        for a in self.view_entries:
            a.setVisible(on)
            a.setEnabled(on)
        if on:
            self.sync()
            if self.editor_mode.doc is None:
                self.status("Open a PDF in the editor (Ctrl+O) to work on its pages here.", 5000)
            else:
                self.status("Click, Ctrl+click or Shift+click pages; drag to reorder; Ctrl+A selects all.", 5000)

    def sync(self, select=None):
        """Bring the grid up to date with the editor's document."""
        doc = self.editor_mode.doc
        # session, not id(doc): an undo/redo of a snapshot step swaps the document object, but
        # it is still the same PDF (keep selection and zoom)
        key = (self.editor_mode.session if doc is not None else None, self.editor_mode.doc_version)
        if key != self._doc_shown:
            new_doc = self._doc_shown == "unset" or self._doc_shown[0] != key[0]
            self._doc_shown = key
            if new_doc:                      # another PDF (tab): its own saved selection
                self.grid.selected = set(select or [])
                self.grid.set_document(doc)
                QTimer.singleShot(0, self.apply_density)
            else:
                self.grid.refresh(doc, select)
        elif select is not None:
            self.grid.set_selection(select)
        n = doc.page_count if doc else PLACEHOLDER_PAGES
        self.count_label.setText(f"  {n} pages" + ("" if doc else " (no PDF open yet: open one in the editor)") + "  ")
