"""PDF Editor mode (stage 1 UI shell, hosted in the main window since stage 2).

The editor's frame: tabs, the page canvas (QGraphicsView) with zoom / fit / pan / page
navigation, the thumbnail and document panels, the tool groups and menus. Each tool group's
actions are made here and given their behaviour by its stage's controller (realize()); since
stage 21 none is left without one. The source PDF is opened read-only and never saved."""
import time
from pathlib import Path

import pymupdf
from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QImage, QKeySequence, QPainter, QPen,
                           QPixmap, QTransform)
from PySide6.QtWidgets import (QComboBox, QDockWidget, QFileDialog, QFrame, QGraphicsPixmapItem,
                               QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView, QGridLayout,
                               QBoxLayout, QGroupBox, QLabel, QListView, QListWidget, QListWidgetItem,
                               QMenu, QMessageBox, QSizePolicy, QSpinBox, QStackedWidget, QTabBar,
                               QToolBar, QToolButton, QVBoxLayout, QWidget)

from . import APP_NAME, export
from .icons import icon as qicon

ON = "#e6e6e6"       # icon colour for working controls
RENDER_BUDGET = 0.04  # s of page/thumbnail rendering per event-loop turn (heavy pages: one per turn)
_RENDER_GATE = [0.0]  # when the last render (any renderer) finished
_RENDER_BUSY = [False]  # the thumbnail thread is rendering right now (it holds the GIL meanwhile)


def render_turn_taken():
    """True if another renderer (canvas, thumbnails, Overview) rendered moments ago, i.e. in
    this same pass of the event loop, or the thumbnail thread is rendering now: yield so input
    and repaints get a turn in between."""
    return _RENDER_BUSY[0] or time.perf_counter() - _RENDER_GATE[0] < 0.005


def rendered_now():
    _RENDER_GATE[0] = time.perf_counter()
SOON = "#6b7484"     # icon colour for placeholders

# group -> [(key, mdi6 icon, label, what it will do)]
TOOLS = {
    "Pages": [
        ("reorder", "swap-vertical", "Reorder", "Drag pages into a new order"),
        ("rotate", "rotate-right", "Rotate", "Rotate the selected pages 90°"),
        ("delete", "delete-outline", "Delete", "Delete the selected pages"),
        ("insert", "file-plus-outline", "Insert", "Insert blank pages or pages from another PDF"),
        ("duplicate", "content-duplicate", "Duplicate", "Duplicate the selected pages"),
        ("extract", "file-export-outline", "Extract", "Save the selected pages as a new PDF"),
        ("merge", "call-merge", "Merge", "Append other PDFs to this one"),
        ("split", "call-split", "Split", "Split this PDF into several files"),
        ("crop", "crop", "Crop", "Crop page margins"),
    ],
    "Markup": [
        ("highlight", "marker", "Highlight", "Highlight text"),
        ("underline", "format-underline", "Underline", "Underline text"),
        ("strike", "format-strikethrough", "Strike", "Strikethrough text"),
        ("draw", "draw", "Draw", "Freehand ink drawing"),
        ("shapes", "shape-outline", "Shapes", "Rectangles, ellipses, lines and arrows"),
        ("textbox", "text-box-outline", "Text box", "Add a free-text box"),
        ("note", "note-text-outline", "Note", "Sticky note / comment"),
        ("stamp", "stamper", "Stamp", "Approved / Draft / custom stamps"),
        ("signature", "draw-pen", "Signature", "Place a drawn or image signature"),
        ("callout", "comment-arrow-right-outline", "Callout", "A text box with a leader line pointing at a spot"),
        ("measure", "ruler", "Measure", "Distance, area, scale calibration"),
    ],
    "Edit": [
        ("edittext", "square-edit-outline", "Edit text", "Edit existing text in place"),
        ("whiteout", "eraser-variant", "Whiteout", "Cover an area with white"),
        ("redact", "format-color-marker-cancel", "Redact", "Permanently remove content under a box"),
    ],
    "Insert": [
        ("image", "image-plus", "Image", "Insert an image"),
        ("link", "link-variant", "Link", "Add a link to a page or URL"),
        ("formfield", "form-textbox", "Form field", "Add a fillable form field"),
    ],
    "Document": [
        ("headerfooter", "page-layout-header-footer", "Header / footer", "Add a header and footer to every page"),
        ("pagenumbers", "numeric", "Page numbers", "Number the pages"),
        ("watermark", "watermark", "Watermark", "Add a text or image watermark"),
        ("metadata", "information-outline", "Metadata", "Edit title, author, subject, keywords"),
        ("encrypt", "lock-outline", "Password / encrypt", "Protect the PDF with a password"),
        ("flatten", "layers-triple-outline", "Flatten", "Bake annotations and form fields into the page"),
        ("compress", "zip-box-outline", "Compress", "Shrink the file size"),
        ("ocrpage", "text-recognition", "OCR page", "Add a searchable text layer to a scanned page"),
        ("bates", "pound-box-outline", "Bates numbers", "Stamp sequential Bates numbers on the pages"),
    ],
}

PAGE_GAP = 18.0     # points between pages on the canvas
SIDE = 20.0         # points of canvas margin left/right of the widest page
THUMB_W = 116


def _icon(name, on=True):
    return qicon("mdi6." + name, color=ON if on else SOON)


def _qimage(pix):
    fmt = QImage.Format_RGB888 if pix.n == 3 else QImage.Format_RGBA8888
    return QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()


class PageCanvas(QGraphicsView):
    """Stacked page view. Scene units are PDF points of each page's displayed (rotated)
    rect, so scene <-> PDF is a translation per page (scene_to_page / page_to_scene).
    In Select mode, click-drag selects text (textsel.PageText hit-testing)."""
    pageChanged = Signal(int)
    zoomChanged = Signal(float)
    selectionChanged = Signal(int)         # number of selected characters
    cropCancelled = Signal()

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#2a2e36"))
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setFrameShape(QFrame.NoFrame)
        self.doc = None
        self.pages = []           # [{rect, pix_item, scale}]
        self.zoom = 1.0
        self._cur = -1
        self._render_timer = QTimer(self, singleShot=True, interval=60)
        self._render_timer.timeout.connect(self.render_visible)
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.horizontalScrollBar().valueChanged.connect(lambda _: self._render_timer.start())
        self._text = {}             # page index -> PageText (cleared on any document change)
        self.selection = None       # (page, anchor char, cursor char)
        self._sel_items = []
        self._dragging = False
        self.interact = None        # markup.MarkupController: annotation tools get events first

    # ---------- coordinates ----------
    def page_at(self, scene_pt):
        for i, p in enumerate(self.pages):
            if p["rect"].contains(scene_pt):
                return i
        return None

    def scene_to_page(self, i, scene_pt):
        """Scene point -> PDF point in page i's displayed (rotated) space."""
        r = self.pages[i]["rect"]
        return pymupdf.Point(scene_pt.x() - r.x(), scene_pt.y() - r.y())

    def page_to_scene(self, i, rect):
        """PDF rect in page i's displayed space -> scene QRectF."""
        o = self.pages[i]["rect"].topLeft()
        return QRectF(o.x() + rect.x0, o.y() + rect.y0, rect.width, rect.height)

    # ---------- text selection ----------
    def page_text(self, i):
        from .textsel import PageText
        if i not in self._text:
            self._text[i] = PageText(self.doc, i)
        return self._text[i]

    def clear_selection(self):
        for it in self._sel_items:
            self.scene().removeItem(it)
        self._sel_items = []
        if self.selection is not None:
            self.selection = None
            self.selectionChanged.emit(0)

    def _draw_selection(self):
        for it in self._sel_items:
            self.scene().removeItem(it)
        self._sel_items = []
        if self.selection is None:
            return
        i, a, b = self.selection
        for r in self.page_text(i).segments(a, b):
            it = QGraphicsRectItem(self.page_to_scene(i, r))
            it.setBrush(QColor(45, 108, 223, 80))
            it.setPen(Qt.NoPen)
            it.setZValue(3)
            self.scene().addItem(it)
            self._sel_items.append(it)
        lo, hi = sorted((a, b))
        self.selectionChanged.emit(hi - lo + 1)

    def selected_text(self):
        if self.selection is None:
            return ""
        i, a, b = self.selection
        return self.page_text(i).text(a, b)

    def _char_at(self, i, scene_pt):
        r = self.pages[i]["rect"]
        clamped = QPointF(min(max(scene_pt.x(), r.left()), r.right()), min(max(scene_pt.y(), r.top()), r.bottom()))
        return self.page_text(i).nearest(self.scene_to_page(i, clamped))

    # ---------- crop rectangle (Page › Crop) ----------
    def start_crop(self, callback):
        """Next drag on a page draws a rectangle; callback(page, pymupdf.Rect displayed) on release."""
        self.clear_selection()
        self._crop_cb = callback
        self.viewport().setCursor(Qt.CrossCursor)

    def cancel_crop(self):
        self._crop_cb = None
        self._crop_press = None
        if getattr(self, "_crop_item", None) is not None:
            self.scene().removeItem(self._crop_item)
            self._crop_item = None
        self.viewport().unsetCursor()

    def cropping(self):
        return getattr(self, "_crop_cb", None) is not None

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.cropping():
            self.cancel_crop()
            self.cropCancelled.emit()
            return
        if self.interact is not None and self.doc is not None and self.interact.key(self, e):
            e.accept()
            return
        super().keyPressEvent(e)

    def mouseDoubleClickEvent(self, e):
        if self.interact is not None and self.doc is not None and not self.cropping() \
                and self.interact.double(self, e):
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def mousePressEvent(self, e):
        if self.cropping() and e.button() == Qt.LeftButton:
            pt = self.mapToScene(e.position().toPoint())
            i = self.page_at(pt)
            if i is not None:
                self._crop_press = (i, pt)
                if getattr(self, "_crop_item", None) is None:
                    self._crop_item = QGraphicsRectItem()
                    self._crop_item.setPen(QPen(QColor("#ff9800"), 0, Qt.DashLine))
                    self._crop_item.setBrush(QColor(255, 152, 0, 40))
                    self._crop_item.setZValue(5)
                    self.scene().addItem(self._crop_item)
                self._crop_item.setRect(QRectF(pt, pt))
            e.accept()
            return
        if self.interact is not None and self.doc is not None and self.interact.press(self, e):
            self.setFocus()
            e.accept()
            return
        if self.doc is not None and e.button() == Qt.LeftButton and self.dragMode() == QGraphicsView.NoDrag:
            pt = self.mapToScene(e.position().toPoint())
            i = self.page_at(pt)
            self.clear_selection()
            if i is not None:
                try:
                    c = self._char_at(i, pt)
                except Exception:          # never let text extraction take the app down
                    c = None
                    self.selectionChanged.emit(-1)
                if c is not None:
                    self._dragging = True
                    self._press = (i, c, e.position().toPoint())
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self.cropping() and getattr(self, "_crop_press", None):
            i, start = self._crop_press
            r = self.pages[i]["rect"]
            pt = self.mapToScene(e.position().toPoint())
            pt = QPointF(min(max(pt.x(), r.left()), r.right()), min(max(pt.y(), r.top()), r.bottom()))
            self._crop_item.setRect(QRectF(start, pt).normalized())
            e.accept()
            return
        if self.interact is not None and self.doc is not None and self.interact.move(self, e):
            e.accept()
            return
        if self._dragging:
            i, anchor, start = self._press
            if (e.position().toPoint() - start).manhattanLength() >= 3:
                c = self._char_at(i, self.mapToScene(e.position().toPoint()))
                self.selection = (i, anchor, c)
                self._draw_selection()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self.cropping() and getattr(self, "_crop_press", None):
            i, _ = self._crop_press
            sr = self._crop_item.rect()
            o = self.pages[i]["rect"].topLeft()
            rect = pymupdf.Rect(sr.left() - o.x(), sr.top() - o.y(), sr.right() - o.x(), sr.bottom() - o.y())
            cb = self._crop_cb
            self.cancel_crop()
            if rect.width > 4 and rect.height > 4:
                cb(i, rect)
            else:
                self.cropCancelled.emit()
            e.accept()
            return
        if self.interact is not None and self.doc is not None and self.interact.release(self, e):
            e.accept()
            return
        if self._dragging:
            self._dragging = False
            if self.interact is not None:
                self.interact.text_released(self)      # e.g. a Highlight tool marks the selection
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def select_chars(self, i, a, b):
        """Programmatic selection (used by tests and later tools)."""
        self.selection = (i, a, b)
        self._draw_selection()

    # ---------- after the document changes ----------
    def invalidate(self, pages=None):
        """Re-render pages (all if None) and drop cached text geometry."""
        self._text.clear()
        for i, p in enumerate(self.pages):
            if pages is None or i in pages:
                p["scale"] = 0.0
        self.render_visible()

    def show_hint(self, on):
        """Centered hint while nothing is open."""
        if on and not self.pages:
            self.scene().clear()
            hint = QGraphicsSimpleTextItem("Open a PDF to view it here\n\n"
                                           "File › Open PDF in Editor…  (Ctrl+O)\nor drop a PDF on the window")
            hint.setBrush(QColor("#6b7484"))
            f = hint.font()
            f.setPointSize(13)
            hint.setFont(f)
            r = hint.boundingRect()
            hint.setPos(-r.width() / 2, 160)
            self.scene().addItem(hint)
            self.scene().setSceneRect(QRectF(-400, 0, 800, 600))
            self.resetTransform()

    def set_document(self, doc, rects=None):
        """rects: every page's rect, measured by the background loader (big files: no page is
        touched here)."""
        self.scene().clear()
        self._sel_items, self.selection, self._text = [], None, {}
        self.doc, self.pages, self._cur = doc, [], -1
        y = 0.0
        for i in range(doc.page_count):
            r = rects[i] if rects is not None and i < len(rects) else doc[i].rect   # reflects /Rotate
            x = -r.width / 2
            shadow = QGraphicsRectItem(QRectF(x + 3, y + 3, r.width, r.height))
            shadow.setBrush(QColor(0, 0, 0, 90))
            shadow.setPen(Qt.NoPen)
            paper = QGraphicsRectItem(QRectF(x, y, r.width, r.height))
            paper.setBrush(QBrush(Qt.white))
            paper.setPen(QPen(QColor("#3a4150"), 0))
            self.scene().addItem(shadow)
            self.scene().addItem(paper)
            pix = QGraphicsPixmapItem()
            pix.setTransformationMode(Qt.SmoothTransformation)
            pix.setPos(x, y)
            pix.setZValue(1)
            self.scene().addItem(pix)
            self.pages.append({"rect": QRectF(x, y, r.width, r.height), "item": pix, "scale": 0.0})
            y += r.height + PAGE_GAP
        w = max((p["rect"].width() for p in self.pages), default=612)
        self.scene().setSceneRect(QRectF(-w / 2 - SIDE, -PAGE_GAP, w + 2 * SIDE, y + PAGE_GAP))
        self.fit_width()
        self.goto(0)

    # ---------- zoom ----------
    def set_zoom(self, z, under_mouse=False):
        """Buttons zoom about the view centre; Ctrl+wheel zooms about the cursor."""
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse if under_mouse
                                     else QGraphicsView.AnchorViewCenter)
        z = max(0.1, min(8.0, z))
        self.zoom = z
        self.setTransform(QTransform.fromScale(z, z))
        self.zoomChanged.emit(z)
        self._render_timer.start()

    def zoom_by(self, f, under_mouse=False):
        self.set_zoom(self.zoom * f, under_mouse)

    def fit_page(self):
        """Whole first page in view."""
        if not self.pages:
            return
        r = self.pages[0]["rect"]
        w = max(p["rect"].width() for p in self.pages)
        vw, vh = self.viewport().width() - 2, self.viewport().height() - 2
        if vw > 50 and vh > 50:
            self.set_zoom(min(vw / (w + 2 * SIDE), vh / (r.height() + 2 * PAGE_GAP)))

    def apply_zoom_mode(self, mode):
        """Settings › default zoom: fit_width | fit_page | a percentage."""
        if mode == "fit_page":
            self.fit_page()
        elif mode == "fit_width":
            self.fit_width()
        else:
            try:
                self.set_zoom(float(mode) / 100)
            except ValueError:
                self.fit_width()

    def fit_width(self):
        if not self.pages:
            return
        w = max(p["rect"].width() for p in self.pages)
        avail = self.viewport().width() - 2
        self.set_zoom(avail / (w + 2 * SIDE) if avail > 50 else 1.0)

    def wheelEvent(self, e):
        if e.modifiers() & Qt.ControlModifier:
            self.zoom_by(1.15 if e.angleDelta().y() > 0 else 1 / 1.15, under_mouse=True)
            e.accept()
        else:
            super().wheelEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._render_timer.start()

    # ---------- navigation ----------
    def visible_rect(self):
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def current_page(self):
        if not self.pages:
            return -1
        vr = self.visible_rect()
        probe = vr.top() + vr.height() * 0.3
        for i, p in enumerate(self.pages):
            if probe <= p["rect"].bottom() + PAGE_GAP:
                return i
        return len(self.pages) - 1

    def goto(self, i):
        if not self.pages:
            return
        i = max(0, min(len(self.pages) - 1, i))
        r = self.pages[i]["rect"]
        vr = self.visible_rect()
        self.centerOn(r.center().x(), r.top() - PAGE_GAP / 2 + vr.height() / 2)
        self._scrolled()

    def _scrolled(self, *_):
        self._render_timer.start()
        c = self.current_page()
        if c != self._cur:
            self._cur = c
            self.pageChanged.emit(c)

    # ---------- rendering ----------
    def render_visible(self):
        if self.doc is None or self.doc.is_closed:     # closed with the window: nothing to draw
            return
        vr = self.visible_rect()
        near = vr.adjusted(0, -vr.height(), 0, vr.height())
        far = vr.adjusted(0, -4 * vr.height(), 0, 4 * vr.height())
        scale = self.zoom * self.devicePixelRatioF()
        todo = []
        for i, p in enumerate(self.pages):
            if p["rect"].intersects(near):
                if abs(p["scale"] - scale) > 1e-3:
                    todo.append(i)
            elif p["scale"] and not p["rect"].intersects(far):
                p["item"].setPixmap(QPixmap())    # free memory far off-screen
                p["scale"] = 0.0
        # visible pages first (nearest the middle of the view), then the ones just off-screen;
        # a time budget per event-loop turn, so heavy pages never stall the window in a burst
        cy = vr.center().y()
        todo.sort(key=lambda i: (not self.pages[i]["rect"].intersects(vr), abs(self.pages[i]["rect"].center().y() - cy)))
        if todo and render_turn_taken():
            QTimer.singleShot(5, self.render_visible)          # another renderer just ran: next turn
            return
        t0 = time.perf_counter()
        for n, i in enumerate(todo):
            if n and time.perf_counter() - t0 > RENDER_BUDGET:
                QTimer.singleShot(0, self.render_visible)      # carry on next turn
                return
            p = self.pages[i]
            pix = self.doc[i].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            p["item"].setPixmap(QPixmap.fromImage(_qimage(pix)))
            p["item"].setScale(1 / scale)
            p["scale"] = scale
            rendered_now()
            from . import startup
            startup.first_page()                   # start-up clock: the first page is on screen


THUMB_AHEAD = 8      # thumbnails rendered beyond the visible ones, each way


class ThumbWorker(QThread):
    """Renders thumbnails off the UI thread from its OWN read-only copy of the PDF (opened from
    the file's bytes, never the working document), newest request first. Results come back as
    QImages; the list turns them into icons."""
    rendered = Signal(int, int, QImage)       # generation, page, image

    def __init__(self, data, parent=None):
        super().__init__(parent)
        import threading
        self.data = data
        self.cv = threading.Condition()
        self.queue = []
        self.gen = 0
        self.width_px = THUMB_W
        self._stop = False
        self.threads = set()                  # thread ids that rendered (the self-test reads it)
        self.count = 0
        self.turn = threading.Semaphore(0)    # one page per turn the UI thread hands out
        self.waiting = False

    def request(self, gen, pages, width_px):
        with self.cv:
            self.gen, self.queue, self.width_px = gen, list(pages), width_px
            self.cv.notify()

    def stop(self):
        with self.cv:
            self._stop = True
            self.queue = []
            self.cv.notify()
        self.turn.release()

    def give_turn(self):
        """UI thread: let the worker render one page (only when it is waiting for one)."""
        if self.waiting:
            self.waiting = False
            self.turn.release()
            return True
        return False

    def busy(self):
        return bool(self.queue) or self.waiting

    def run(self):
        import threading
        doc = pymupdf.open(stream=self.data, filetype="pdf")
        try:
            while True:
                with self.cv:
                    while not self.queue and not self._stop:
                        self.cv.wait()
                    if self._stop:
                        return
                    gen, i, wpx = self.gen, self.queue.pop(0), self.width_px
                if not 0 <= i < doc.page_count:
                    continue
                # PyMuPDF holds the GIL while it renders: wait for a turn the UI thread hands out,
                # so a heavy page never lands on top of a canvas render (one heavy page per turn)
                self.waiting = True
                self.turn.acquire()
                if self._stop:
                    return
                _RENDER_BUSY[0] = True
                try:
                    page = doc[i]
                    s = wpx / page.rect.width
                    pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
                    del page
                finally:
                    _RENDER_BUSY[0] = False
                    _RENDER_GATE[0] = time.perf_counter()
                img = _qimage(pix)
                self.threads.add(threading.get_ident())
                self.count += 1
                self.rendered.emit(gen, i, img)
        finally:
            doc.close()


# ---------- page drags between tabs / panes (stage 17) ----------
PAGES_MIME = "application/x-lazaruspdf-pages"
DROP_BLUE = "#4f8cff"                     # the insertion bar (as Overview's, stage 6)


def page_mime(sid, pages):
    """Drag data for pages of the tab with session id sid: which tab and which pages, nothing else
    (the pages are copied from that tab when they are dropped)."""
    import json
    from PySide6.QtCore import QMimeData
    md = QMimeData()
    md.setData(PAGES_MIME, json.dumps({"sid": sid, "pages": [int(p) for p in pages]}).encode())
    return md


def page_payload(md):
    """{"sid", "pages"} from drag data, or None if it isn't a page drag."""
    import json
    if md is None or not md.hasFormat(PAGES_MIME):
        return None
    try:
        d = json.loads(bytes(md.data(PAGES_MIME)).decode())
        return {"sid": d["sid"], "pages": [int(p) for p in d["pages"]]}
    except Exception:
        return None


class ThumbList(QListWidget):
    """Page thumbnails. Extended selection (click / Ctrl / Shift / Ctrl+A); dragging selected
    thumbnails onto a new spot asks for a reorder. The list never moves items itself; the
    page op runs and the list is rebuilt from the document.

    Rendered lazily: only the visible thumbnails plus THUMB_AHEAD each way, as you scroll, and
    each once (kept until the document changes). While the document is as it was opened, a
    worker thread renders them from its own copy of the file; edited pages, and any document
    after a page operation, render on the UI thread a page per turn."""
    reorderRequested = Signal(list, int)     # rows, insert-before index
    pagesDropped = Signal(dict, int)         # pages dragged from another tab / pane: payload, insert-before (17)

    def __init__(self):
        super().__init__()
        self.origin = None                   # f() -> the shown tab's session id (set by the editor)
        self._bar = QFrame(self.viewport())  # the blue insertion bar while pages are dragged over
        self._bar.setStyleSheet(f"background:{DROP_BLUE}")
        self._bar.hide()
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.TopToBottom)
        self.setWrapping(False)
        self.setMovement(QListView.Snap)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.setDragDropMode(QListWidget.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDropIndicatorShown(True)
        self.setResizeMode(QListView.Adjust)
        self.setIconSize(QSize(THUMB_W, int(THUMB_W * 1.42)))
        self.setSpacing(6)
        self.setUniformItemSizes(False)
        self.doc = None
        self._gen = 0
        self._done = set()            # pages with a current thumbnail
        self._main = set()            # pages the UI thread renders (edited since the worker's copy)
        self._queue = []              # UI-thread renders still to do
        self._worker = None
        self._source = None
        self.stats = {"worker": 0, "main": 0}
        self._timer = QTimer(self, interval=0)
        self._timer.timeout.connect(self._render_some)
        self._sched = QTimer(self, singleShot=True, interval=15)
        self._sched.timeout.connect(self._schedule)
        self._pace = QTimer(self, interval=0)          # hands the worker its turns
        self._pace.timeout.connect(self._give_turn)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._sched.start())
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self._sched.start())

    def stop(self):
        self._timer.stop()
        self._set_source(None)
        self.doc = None

    def _set_source(self, data):
        if data is self._source and (data is None or self._worker is not None):
            return
        if self._worker is not None:
            self._pace.stop()
            self._worker.stop()
            self._worker.wait(5000)
            self._worker = None
        self._source = data
        if data is not None:
            self._worker = ThumbWorker(data, self)
            self._worker.rendered.connect(self._from_worker)
            self._worker.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._sched.start()

    def showEvent(self, e):
        super().showEvent(e)
        self._sched.start()

    def wanted(self):
        """Rows to have thumbnails for now: the visible ones plus THUMB_AHEAD each way."""
        n = self.count()
        if not n:
            return []
        vp = self.viewport().rect()
        horizontal = self.flow() == QListView.LeftToRight
        span = vp.width() if horizontal else vp.height()

        def edge(k, end):
            r = self.visualItemRect(self.item(k))
            return (r.right() if end else r.left()) if horizontal else (r.bottom() if end else r.top())
        lo_, hi_ = 0, n - 1                    # first item that ends inside the view
        while lo_ < hi_:
            m = (lo_ + hi_) // 2
            if edge(m, True) < 0:
                lo_ = m + 1
            else:
                hi_ = m
        first = lo_
        lo_, hi_ = first, n - 1                # last item that starts inside the view
        while lo_ < hi_:
            m = (lo_ + hi_ + 1) // 2
            if edge(m, False) > span:
                hi_ = m - 1
            else:
                lo_ = m
        last = lo_
        lo, hi = max(0, first - THUMB_AHEAD), min(n - 1, last + THUMB_AHEAD)
        mid = (first + last) / 2
        return sorted(range(lo, hi + 1), key=lambda i: (not first <= i <= last, abs(i - mid)))

    def _schedule(self):
        if self.doc is None or self.doc.is_closed:
            return
        todo = [i for i in self.wanted() if i not in self._done]
        to_worker = [i for i in todo if i not in self._main] if self._worker is not None else []
        self._queue = [i for i in todo if i not in to_worker]
        if self._worker is not None:
            self._worker.request(self._gen, to_worker, int(THUMB_W * self.devicePixelRatioF()))
            if to_worker:
                self._pace.start()
        if self._queue:
            self._timer.start()

    def _give_turn(self):
        w = self._worker
        if w is None or not w.busy():
            self._pace.stop()
            return
        if not render_turn_taken():
            w.give_turn()

    def _from_worker(self, gen, i, img):
        if gen != self._gen or i in self._main or i >= self.count():
            return
        qp = QPixmap.fromImage(img)
        qp.setDevicePixelRatio(self.devicePixelRatioF())
        self.item(i).setIcon(qp)
        self._done.add(i)
        self.stats["worker"] += 1

    def selected_rows(self):
        return sorted({i.row() for i in self.selectedIndexes()})

    def drop_target(self, pos):
        """Insert-before index for a drop at viewport position pos."""
        idx = self.indexAt(pos)
        if not idx.isValid():
            return self.count()
        r = self.visualRect(idx)
        horizontal = self.flow() == QListView.LeftToRight
        after = pos.x() > r.center().x() if horizontal else pos.y() > r.center().y()
        return idx.row() + (1 if after else 0)

    def _sid(self):
        return self.origin() if self.origin is not None else None

    def startDrag(self, actions):
        """Our own drag (17): it carries the tab and the pages, so it can be dropped here (a
        reorder), on another tab, on Overview or on a compare pane."""
        from PySide6.QtGui import QDrag
        rows = self.selected_rows()
        if not rows:
            return
        drag = QDrag(self)
        drag.setMimeData(page_mime(self._sid(), rows))
        it = self.item(rows[0])
        if it is not None:
            drag.setPixmap(it.icon().pixmap(40, 56))
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
        self._bar.hide()

    def _show_bar(self, pos):
        k = self.drop_target(pos)
        n = self.count()
        horizontal = self.flow() == QListView.LeftToRight
        if n == 0:
            r = QRectF(0, 0, 0, 0).toRect()
        else:
            ref = self.visualItemRect(self.item(min(k, n - 1)))
            r = ref
        if horizontal:
            x = (r.left() if k < n else r.right()) - 3
            self._bar.setGeometry(x, r.top(), 4, max(r.height(), 20))
        else:
            y = (r.top() if k < n else r.bottom()) - 3
            self._bar.setGeometry(4, y, max(self.viewport().width() - 8, 20), 4)
        self._bar.show()
        self._bar.raise_()
        return k

    def dragEnterEvent(self, e):
        if page_payload(e.mimeData()) is not None:
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self._show_bar(e.position().toPoint())
            return
        super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if page_payload(e.mimeData()) is not None:
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self._show_bar(e.position().toPoint())
            return
        super().dragMoveEvent(e)

    def dragLeaveEvent(self, e):
        self._bar.hide()
        super().dragLeaveEvent(e)

    def dropEvent(self, e):
        self._bar.hide()
        payload = page_payload(e.mimeData())
        if payload is None:
            e.ignore()
            return
        target = self.drop_target(e.position().toPoint())
        e.setDropAction(Qt.CopyAction)               # we change the document, never the list itself
        e.accept()
        if payload["sid"] == self._sid():                # its own pages: a reorder
            if payload["pages"]:
                self.reorderRequested.emit(payload["pages"], target)
            return
        self.pagesDropped.emit(payload, target)      # from another tab or pane: a paste here

    def set_orientation(self, horizontal):
        """Vertical strip in a side panel, horizontal strip when the panel sits at the top."""
        self.setFlow(QListView.LeftToRight if horizontal else QListView.TopToBottom)
        w = 44 if horizontal else THUMB_W
        self.setIconSize(QSize(w, int(w * 1.42)))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded if horizontal else Qt.ScrollBarAlwaysOff)

    def set_document(self, doc, source=None):
        """source: the PDF's bytes when the document is exactly as it was opened (unedited, not
        password-protected); the worker thread then renders from its own copy of them."""
        self._timer.stop()
        self._gen += 1
        self._done = set()
        self._main = set()
        self._queue = []
        self._set_source(source if doc is not None else None)
        # rebuilding moves the current row (clear() emits currentRowChanged), which would
        # scroll the canvas to another page; keep that quiet
        self.blockSignals(True)
        self.clear()
        self.doc = doc
        blank = QPixmap(THUMB_W, int(THUMB_W * 1.3))
        blank.fill(QColor("#e8e8e8"))
        for i in range(doc.page_count):
            it = QListWidgetItem(blank, str(i + 1))
            it.setTextAlignment(Qt.AlignHCenter)
            self.addItem(it)
        if doc.page_count:
            self.setCurrentRow(0)
        self.blockSignals(False)
        self._sched.start()

    def refresh(self, pages=None):
        """After an edit / undo: those pages (None: all) render again, on the UI thread (the
        worker's copy no longer matches them), lazily, as they come into view."""
        if self.doc is None or self.doc.is_closed:
            return
        if pages is None:
            self._set_source(None)
            self._gen += 1
            self._done = set()
            self._sched.start()
            return
        for i in pages:
            if 0 <= i < self.count():
                self._main.add(i)
                self._done.discard(i)
                if i in self.wanted():
                    self._render_one(i)
                    self._done.add(i)

    def _render_one(self, i):
        page = self.doc[i]
        dpr = self.devicePixelRatioF()
        s = THUMB_W * dpr / page.rect.width
        pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
        qp = QPixmap.fromImage(_qimage(pix))
        qp.setDevicePixelRatio(dpr)
        self.item(i).setIcon(qp)
        self.stats["main"] += 1

    def _render_some(self):
        """UI-thread thumbnails (edited pages, documents after page ops): a time budget per
        tick (at least one), only rows still wanted."""
        if render_turn_taken():
            return                                   # the timer comes back next turn
        t0 = time.perf_counter()
        while True:
            if self.doc is None or self.doc.is_closed or not self._queue:
                self._timer.stop()
                return
            i = self._queue.pop(0)
            if i in self._done or i >= self.doc.page_count:
                continue
            self._render_one(i)
            self._done.add(i)
            rendered_now()
            if time.perf_counter() - t0 > RENDER_BUDGET:
                return


class PdfEditorMode(QObject):
    pagesChanged = Signal(list)          # after a page op / structural undo: pages to select
    docChanged = Signal()                # the working copy's content changed (search re-indexes)
    sessionChanged = Signal()            # another tab became active (or the last one closed)
    docOpened = Signal(str)              # a PDF file opened as a tab (File › Open Recent, stage 18c)
    pageChanged = Signal(int)            # the active tab's current page
    cropCancelled = Signal()             # the active tab's crop rectangle was cancelled
    """PDF Editor mode, hosted in the main window.

    Several PDFs can be open, one per tab (tabs.Session). canvas / work / doc / doc_version
    always mean the ACTIVE tab's; the side panels (thumbnails, bookmarks, Overview, page ops,
    find) follow the active tab.

    Builds the canvas (the central widget for this mode), the top and ribbon toolbars,
    the two docks, and the mode's menus. The main window places the toolbars and menus;
    set_active() shows/hides and enables/disables all of them with the mode."""
    key = "editor"
    title = "PDF Editor"

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.active = False
        self.sessions = []                     # one tabs.Session per open PDF, in tab order
        self.last_edit_result = None           # what the last run_edit's fn returned (e.g. an xref)
        self.loaders = set()                   # running background loads (window close waits)
        self.cur = None                        # the active Session (None: nothing open)
        self.hint_canvas = PageCanvas()        # shown while no PDF is open
        self.hint_canvas.setAcceptDrops(False)     # file drops are handled by the main window
        self.soon = {}                         # key -> placeholder QAction
        self.page_buttons = {}                 # key -> Pages-panel button (page ops)
        from . import memguard
        memguard.BUDGET.notice = lambda text: QMessageBox.information(self.win, APP_NAME, text)
        self._dock_vis = {}
        self._bar_vis = {}
        self._was_on = False
        self._docks_shown = True
        self._build()
        self._build_central()
        self.hint_canvas.show_hint(True)

    # ---------- the active tab ----------
    @property
    def canvas(self):
        return self.cur.canvas if self.cur and self.cur.canvas is not None else self.hint_canvas

    @property
    def work(self):
        return self.cur.work if self.cur else None          # None while the tab is loading

    @property
    def doc(self):
        return self.cur.work.doc if self.cur and self.cur.work else None

    @property
    def doc_version(self):
        return self.cur.doc_version if self.cur else 0

    @doc_version.setter
    def doc_version(self, v):
        if self.cur:
            self.cur.doc_version = v

    @property
    def session(self):
        """Identity of the active PDF (stays the same when a snapshot step swaps its document)."""
        return self.cur.sid if self.cur else 0

    def status(self, msg, ms=0):
        self.win.statusBar().showMessage(msg, ms)

    # ---------- placeholders ----------
    def _placeholder(self, key, icon, label, what):
        a = QAction(_icon(icon, on=False), label, self.win)
        a.setToolTip(f"<b>{label}</b><br>{what}<br><i>Not implemented yet</i>")
        a.setStatusTip(f"{label}: {what} (not implemented yet)")
        a.triggered.connect(lambda: self.status(f"Not implemented yet: {label}, {what}.", 5000))
        a.setProperty("soon", True)
        self.soon[key] = a
        return a

    @staticmethod
    def _mark_soon(toolbar):
        for a in toolbar.actions():
            w = toolbar.widgetForAction(a)
            if w is not None and a.property("soon"):
                w.setProperty("soon", True)
                w.style().unpolish(w)
                w.style().polish(w)

    # ---------- layout ----------
    def _build(self):
        win = self.win
        for group, items in TOOLS.items():
            for key, icon, label, what in items:
                self._placeholder(key, icon, label, what)
        # saving (real): Save writes the working copy; the first Save asks where (Save As)
        self.act_save_pdf = QAction(_icon("content-save-outline"), "Save PDF", win,
                                    shortcut=QKeySequence.Save, triggered=self.save)
        self.act_save_pdf.setToolTip("Save the edited PDF (asks for a new file the first time; "
                                     "the original is never changed)")
        self.act_saveas = QAction(_icon("content-save-edit-outline"), "Save PDF As…", win,
                                  shortcut="Ctrl+Shift+S", triggered=self.save_as)
        self.act_saveas.setToolTip("Save the edited PDF to a new file")
        self.soon["saveas"] = self.act_saveas
        # one password menu; every entry writes a NEW file, and Ctrl+S then repeats that choice
        self.pw_menu = QMenu("Save With Password", win)
        self.pw_menu.setIcon(_icon("lock-outline"))
        self.pw_actions = {}
        for kind, label, tip in (
                ("keep", "Keep Password…", "Carry the open password (and permissions) over to a new file"),
                ("set", "Set Passwords…", "Choose an open password and/or an owner password with permissions"),
                ("match", "Match Passwords…", "One password used as both the open and the owner password"),
                ("clear", "Clear Passwords…", "Save a new file with no password at all")):
            a = self.pw_menu.addAction(label)
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, k=kind: self.save_with_password(k))
            self.pw_actions[kind] = a
        # editing (real)
        self.act_undo = QAction(_icon("undo"), "Undo", win, shortcut=QKeySequence.Undo, triggered=self.undo)
        self.act_redo = QAction(_icon("redo"), "Redo", win, triggered=self.redo)
        self.act_redo.setShortcuts([QKeySequence.Redo, QKeySequence("Ctrl+Shift+Z")])
        self.act_copy = QAction("Copy selected text", win, shortcut=QKeySequence.Copy, triggered=self.copy)
        self.act_test_mark = QAction("Highlight selection (pipeline test)", win, triggered=self.highlight_selection)
        self.act_test_mark.setToolTip("Minimal real edit to prove selection, undo/redo and saving. "
                                      "The Highlight tool proper arrives with the markup stage.")
        self.edit_actions = [self.act_undo, self.act_redo, self.act_copy, self.act_test_mark,
                             self.act_save_pdf, self.act_saveas]
        self.export_menu = QMenu("Export", win)
        for key, label, ext, _ in export.FORMATS:
            self.export_menu.addAction(self._placeholder(f"export_{key}", "export-variant", label,
                                                         f"Export the document as {label}"))

        # working actions
        self.act_open = QAction(_icon("folder-open-outline"), "Open PDF in Editor…", win,
                                shortcut=QKeySequence.Open, triggered=self.choose_pdf)
        self.act_open.setIconText("Open PDF")
        self.act_zin = QAction(_icon("magnify-plus-outline"), "Zoom in", win,
                               shortcut=QKeySequence.ZoomIn, triggered=lambda: self.canvas.zoom_by(1.25))
        self.act_zout = QAction(_icon("magnify-minus-outline"), "Zoom out", win,
                                shortcut=QKeySequence.ZoomOut, triggered=lambda: self.canvas.zoom_by(0.8))
        self.act_fit = QAction(_icon("arrow-expand-horizontal"), "Fit width", win,
                               shortcut="Ctrl+0", triggered=lambda: self.canvas.fit_width())
        self.act_100 = QAction("Actual size (100%)", win, shortcut="Ctrl+1",
                               triggered=lambda: self.canvas.set_zoom(1.0))
        self.act_prev = QAction(_icon("chevron-left"), "Previous page", win, shortcut="PgUp",
                                triggered=lambda: self.canvas.goto(self.canvas.current_page() - 1))
        self.act_next = QAction(_icon("chevron-right"), "Next page", win, shortcut="PgDown",
                                triggered=lambda: self.canvas.goto(self.canvas.current_page() + 1))
        self.act_select = QAction(_icon("cursor-default-outline"), "Select", win, checkable=True,
                                  checked=True, triggered=lambda: self._set_pan(False))
        self.act_select.setToolTip("Select (pointer)")
        self.act_hand = QAction(_icon("hand-back-right-outline"), "Pan", win, checkable=True,
                                triggered=lambda: self._set_pan(True))
        self.act_hand.setToolTip("Pan: drag the pages around")
        for a in (self.act_open, self.act_zin, self.act_zout, self.act_fit, self.act_prev, self.act_next):
            a.setToolTip(a.text().replace("…", ""))
        # tabs (one per open PDF) and the compare view
        self.act_close_tab = QAction(_icon("close"), "Close Tab", win, shortcut=QKeySequence.Close,
                                     triggered=lambda _=False: self.close_session(self.cur))
        self.act_close_tab.setToolTip("Close this PDF's tab (asks first if it has unsaved edits)")
        self.act_next_tab = QAction("Next Tab", win, shortcut="Ctrl+Tab",
                                    triggered=lambda _=False: self.cycle_tab(1))
        self.act_prev_tab = QAction("Previous Tab", win, shortcut="Ctrl+Shift+Tab",
                                    triggered=lambda _=False: self.cycle_tab(-1))
        self.tab_actions = [self.act_close_tab, self.act_next_tab, self.act_prev_tab]   # editor + Overview
        self.act_compare = QAction(_icon("compare-horizontal"), "Compare Side by Side", win, checkable=True,
                                   shortcut="Ctrl+Alt+C", triggered=lambda on: self.show_compare(on))
        self.act_compare.setToolTip("Two open PDFs (or the same one twice) side by side, optionally "
                                    "scrolling together")
        self.view_doc_actions = [self.act_zin, self.act_zout, self.act_fit, self.act_100,
                                 self.act_prev, self.act_next, self.act_select, self.act_hand]


        # mode menus (placed on the menu bar by the main window)
        self.menus = []
        for group in ("Pages", "Markup", "Edit", "Insert", "Document"):
            m = QMenu("Page" if group == "Pages" else group, win)
            if group == "Edit":
                for a in (self.act_undo, self.act_redo, None, self.act_copy, self.act_test_mark, None):
                    m.addSeparator() if a is None else m.addAction(a)
            if group == "Pages":                 # the Page menu is also shown in Overview
                m.addAction(self.act_undo)
                m.addAction(self.act_redo)
                m.addSeparator()
            for key, *_ in TOOLS[group]:
                m.addAction(self.soon[key])
            self.menus.append(m)
        # entries this mode contributes to the shared File / View menus
        self.file_entries = [self.act_save_pdf, self.act_saveas, self.pw_menu.menuAction(),
                             self.export_menu.menuAction(), self.act_close_tab]

        # top toolbar: file + view (working)
        tb = QToolBar("PDF Editor", win)
        tb.setObjectName("editor_top")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        tb.setToolButtonStyle(Qt.ToolButtonIconOnly)
        tb.addAction(self.act_open)
        tb.addAction(self.act_save_pdf)
        self.export_btn = QToolButton()
        self.export_btn.setIcon(_icon("export-variant", on=False))
        self.export_btn.setText("Export")
        self.export_btn.setToolTip("<b>Export</b><br>PDF, Word, EPUB, Markdown, HTML, text<br><i>Not implemented yet</i>")
        self.export_btn.setMenu(self.export_menu)
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        self.export_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.export_btn.setProperty("soon", True)
        tb.addWidget(self.export_btn)
        tb.addSeparator()
        tb.addAction(self.act_select)
        tb.addAction(self.act_hand)
        tb.addSeparator()
        tb.addAction(self.act_zout)
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.setFixedWidth(84)
        self.zoom_box.addItems(["50%", "75%", "100%", "125%", "150%", "200%", "300%", "400%"])
        self.zoom_box.setToolTip("Zoom")
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_typed)
        self.zoom_box.activated.connect(lambda _: self._zoom_typed())
        tb.addWidget(self.zoom_box)
        tb.addAction(self.act_zin)
        tb.addAction(self.act_fit)
        tb.addSeparator()
        tb.addAction(self.act_prev)
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setToolTip("Go to page")
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda v: self.canvas.goto(v - 1))
        tb.addWidget(self.page_spin)
        self.page_total = QLabel(" / 0 ")
        tb.addWidget(self.page_total)
        tb.addAction(self.act_next)
        tb.widgetForAction(self.act_open).setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._mark_soon(tb)
        self.top_bar = tb

        # ribbon: one movable toolbar per tool group (Qt's native toolbar dragging),
        # so the groups can be reordered along the top; positions hold for the session
        self.groups = []
        for group in ("Markup", "Edit", "Insert"):
            bar = QToolBar(group, win)
            bar.setObjectName("tools")
            bar.setMovable(True)
            bar.setFloatable(False)
            bar.setAllowedAreas(Qt.TopToolBarArea | Qt.LeftToolBarArea | Qt.RightToolBarArea)
            bar.setIconSize(QSize(20, 20))
            bar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            bar.setToolTip(f"{group} tools: drag the dotted handle, along a row, up to row 1, or to a side")
            lab = QLabel(f" {group.upper()} ")
            lab.setObjectName("grouplabel")
            bar.addWidget(lab)
            for key, *_ in TOOLS[group]:
                bar.addAction(self.soon[key])
            self._mark_soon(bar)
            self.groups.append(bar)

        # left dock: page ops + thumbnails
        left = QWidget()
        lv = QBoxLayout(QBoxLayout.TopToBottom, left)
        lv.setContentsMargins(6, 6, 6, 6)
        lv.setSpacing(6)
        ops_w = QWidget()
        ops = QGridLayout(ops_w)
        ops.setContentsMargins(0, 0, 0, 0)
        ops.setSpacing(3)
        for n, (key, *_rest) in enumerate(TOOLS["Pages"]):
            b = QToolButton()
            b.setDefaultAction(self.soon[key])
            b.setIconSize(QSize(18, 18))
            b.setProperty("soon", True)
            self.page_buttons[key] = b
            ops.addWidget(b, n // 5, n % 5)
        ops_w.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        lv.addWidget(ops_w, 0, Qt.AlignLeft | Qt.AlignTop)
        self._pages_layout = lv
        self.thumbs = ThumbList()
        self.thumbs.itemClicked.connect(lambda it: self.canvas.goto(self.thumbs.row(it)))
        self.thumbs.currentRowChanged.connect(self._thumb_row)
        lv.addWidget(self.thumbs, 1)
        dock = QDockWidget("Pages", win)
        dock.setObjectName("pagesdock")
        dock.setWidget(left)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        win.addDockWidget(Qt.LeftDockWidgetArea, dock)
        dock.dockLocationChanged.connect(self._pages_area)

        # right dock: document tools (placeholders)
        right = QWidget()
        rv = QBoxLayout(QBoxLayout.TopToBottom, right)
        rv.setContentsMargins(8, 8, 8, 8)
        self._doc_grids = []
        for title, keys in (("Document", [k for k, *_ in TOOLS["Document"]]), ("Output", ["saveas"])):
            box = QGroupBox(title)
            bv = QGridLayout(box)
            bv.setSpacing(4)
            buttons = []
            for key in keys:
                b = QToolButton()
                b.setDefaultAction(self.soon[key])
                b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
                b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                b.setProperty("soon", bool(self.soon[key].property("soon")))
                buttons.append(b)
            self._doc_grids.append((bv, buttons))
            rv.addWidget(box, 0, Qt.AlignTop)
        rv.addStretch(1)
        info = QLabel("Edits happen on an in-memory copy. The original PDF is never written; saves always "
                      "go to a new file.")
        info.setObjectName("legend")
        info.setWordWrap(True)
        rv.addWidget(info)
        self._doc_info = info
        self._doc_layout = rv
        rdock = QDockWidget("Document", win)
        rdock.setObjectName("docdock")
        rdock.setWidget(right)
        rdock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        win.addDockWidget(Qt.RightDockWidgetArea, rdock)
        rdock.dockLocationChanged.connect(self._doc_area)
        self.docks = [dock, rdock]

        # Bookmarks: a tab next to Pages (same dock behaviour: movable, View toggle, Lock layout)
        from .bookmarks import BookmarksPanel
        self.bookmarks = BookmarksPanel(self, win)
        bdock = self.bookmarks.dock
        bdock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        win.addDockWidget(Qt.LeftDockWidgetArea, bdock)
        win.tabifyDockWidget(dock, bdock)
        dock.raise_()
        self.docks.append(bdock)
        self.menus[0].addSeparator()
        self.menus[0].addAction(self.bookmarks.act_add)

        # stage 9a: annotation tools, selection, Properties panel (tabbed with Document)
        from .markup import MarkupController
        self.markup = MarkupController(self, win)
        pdock = self.markup.dock
        pdock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        win.addDockWidget(Qt.RightDockWidgetArea, pdock)
        win.tabifyDockWidget(rdock, pdock)
        rdock.raise_()
        self.docks.append(pdock)
        self.menus[1].addSeparator()
        self.menus[1].addAction(self.markup.act_delete)
        # stage 18c: the Annotations panel, a tab next to Pages and Bookmarks
        from .annotlist import AnnotationsPanel
        self.annots = AnnotationsPanel(self, win)
        adock = self.annots.dock
        adock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)
        win.addDockWidget(Qt.LeftDockWidgetArea, adock)
        win.tabifyDockWidget(bdock, adock)
        dock.raise_()
        self.docks.append(adock)
        # stage 10 document tools
        from .stage10_ui import Stage10Controller
        self.stage10 = Stage10Controller(self, win)
        from .textedit_ui import TextEditController
        self.texted = TextEditController(self, win)       # stage 11: Edit text (in place)
        self._pages_area(Qt.LeftDockWidgetArea)
        self._doc_area(Qt.RightDockWidgetArea)

        # View-menu entries for this mode. The panel toggles are added by the main window,
        # because Overview shares the panels.
        self.view_entries = [self.act_zin, self.act_zout, self.act_fit, self.act_100, None,
                             self.act_select, self.act_hand, None, self.act_prev, self.act_next, None,
                             self.act_next_tab, self.act_prev_tab, self.act_compare]

        self.status_page = QLabel("")
        win.statusBar().addPermanentWidget(self.status_page)

    # ---------- mode on/off ----------
    def toolbars(self):
        return [self.top_bar] + self.groups

    def set_active(self, on, keep_docks=False):
        """keep_docks: Overview shares the Pages / Document panels."""
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
        self.show_docks(on or keep_docks)
        for m in self.menus:
            m.menuAction().setVisible(on)
        for a in self.file_entries + [a for a in self.view_entries if a is not None and a not in self.tab_actions]:
            if a in self.tab_actions:
                continue
            a.setVisible(on)
        self.act_open.setEnabled(True)       # Open PDF in Editor works from either mode
        self.status_page.setVisible(on)
        self._apply_enabled()
        if on and not self.doc:
            self.status("PDF Editor: open a PDF (File › Open PDF in Editor…, Ctrl+O) or drop one on the window.")

    def show_docks(self, on):
        for d in self.docks:
            if on:
                d.setVisible(self._dock_vis.get(d, True))
            else:
                if self._docks_shown:
                    self._dock_vis[d] = not d.isHidden()
                d.hide()
        self._docks_shown = on

    # ---------- panels re-flow when docked at the top or bottom ----------
    def _pages_area(self, area):
        horiz = area in (Qt.TopDockWidgetArea, Qt.BottomDockWidgetArea)
        self._pages_layout.setDirection(QBoxLayout.LeftToRight if horiz else QBoxLayout.TopToBottom)
        self.thumbs.set_orientation(horiz)
        self.docks[0].setMinimumWidth(0 if horiz else THUMB_W + 70)
        self.docks[0].setMaximumHeight(165 if horiz else 16777215)

    def _doc_area(self, area):
        horiz = area in (Qt.TopDockWidgetArea, Qt.BottomDockWidgetArea)
        self._doc_layout.setDirection(QBoxLayout.LeftToRight if horiz else QBoxLayout.TopToBottom)
        cols = 5 if horiz else 1
        for grid, buttons in self._doc_grids:
            for b in buttons:
                grid.removeWidget(b)
            for i, b in enumerate(buttons):
                grid.addWidget(b, i // cols, i % cols)
        self._doc_info.setVisible(not horiz)
        self.docks[1].setMinimumWidth(0 if horiz else 200)
        self.docks[1].setMaximumHeight(185 if horiz else 16777215)

    def _apply_enabled(self):
        live = self.active and self.doc is not None
        for a in self.view_doc_actions:
            a.setEnabled(live)
        w = self.work
        # undo/redo also work in Overview, where the page operations happen
        hist = self.doc is not None and (self.active or getattr(self.win, "mode", None) == "overview")
        self.act_undo.setEnabled(hist and w is not None and w.can_undo())
        self.act_redo.setEnabled(hist and w is not None and w.can_redo())
        has_sel = live and self.canvas.selection is not None
        self.act_copy.setEnabled(has_sel)
        self.act_test_mark.setEnabled(has_sel)
        for a in (self.act_save_pdf, self.act_saveas):
            a.setEnabled(live)
        self.pw_menu.setEnabled(live)
        locked_src = live and w is not None and w.lock is not None
        self.pw_actions["keep"].setEnabled(locked_src)
        self.pw_actions["clear"].setEnabled(live and w is not None and (w.lock is not None or w.protection is not None))
        self.zoom_box.setEnabled(live)
        self.page_spin.setEnabled(live)
        tabs = self.active or getattr(self.win, "mode", None) == "overview"
        self.act_close_tab.setEnabled(tabs and self.cur is not None)    # a loading tab too
        many = tabs and len(self.sessions) > 1
        self.act_next_tab.setEnabled(many)
        self.act_prev_tab.setEnabled(many)
        self.act_compare.setEnabled(live)

    # ---------- open ----------
    def choose_pdf(self):
        """Ctrl+O: one or more PDFs, each in its own tab."""
        paths, _ = QFileDialog.getOpenFileNames(self.win, "Open PDF(s) in editor", str(Path.home()),
                                                "PDF files (*.pdf)")
        for p in paths:
            self.open_pdf(p, wait=False)         # each loads in the background in its own tab

    def open_pdf(self, path, wait=True):
        """Open a PDF in a NEW tab. If it is already open, switch to its tab.
        The tab opens at once in a loading state; the file is read, parsed and measured in the
        background (loader.DocLoader), so a big PDF never blocks the window, and the tab fills
        in when ready. wait=False (the UI: Ctrl+O, drops, the command line) returns at once;
        wait=True returns when the load has finished (the event loop keeps running meanwhile),
        True if the PDF is open."""
        from . import protect
        from .loader import DocLoader
        from .tabs import Session
        for s in self.sessions:
            if s.is_source(path):
                self.win.set_mode(self.key)
                self.activate(s)
                self.status("That PDF is already open: switched to its tab.", 4000)
                return True if not wait else self._wait_loaded(s)
        s = Session(path)
        protect.register(s.owner, s.source)        # no save or export may write to it
        c = s.canvas
        c.pageChanged.connect(lambda i, s=s: self._on_page(i) if s is self.cur else None)
        c.selectionChanged.connect(lambda n, s=s: self._on_selection(n) if s is self.cur else None)
        c.zoomChanged.connect(lambda z, s=s: self._on_zoom(z) if s is self.cur else None)
        c.cropCancelled.connect(lambda s=s: self.cropCancelled.emit() if s is self.cur else None)
        c.interact = self.markup
        self.sessions.append(s)
        self.center.addWidget(s.loading)
        self.center.addWidget(c)
        self.tabbar.blockSignals(True)
        i = self.tabbar.addTab(f"Loading… {s.name}")
        self.tabbar.setTabToolTip(i, str(s.source))
        self.tabbar.blockSignals(False)
        self.win.set_mode(self.key)           # opening a PDF visually always lands in this mode
        self.sync_tabbar()
        self.activate(s)
        ld = s.loader = DocLoader(str(s.source), self)
        self._track(ld)
        ld.progress.connect(lambda ph, d, t, s=s: s.loading.show_progress(ph, d, t) if s.loader is ld else None)
        ld.needPassword.connect(lambda data, s=s, ld=ld: self._load_password(s, ld, data))
        ld.ready.connect(lambda doc, lock, rects, data, s=s, ld=ld: self._load_ready(s, ld, doc, lock, rects, data))
        ld.failed.connect(lambda msg, s=s, ld=ld: self._load_failed(s, ld, msg))
        ld.finished.connect(ld.deleteLater)
        ld.start()
        return self._wait_loaded(s) if wait else True

    def open_built(self, data, name, tip="A new PDF (not saved yet)"):
        """A NEW document made in memory (e.g. from images) as a new tab: unsaved and marked as
        changed until the user saves it; Save asks where (there is no file behind it)."""
        from .tabs import Session
        s = Session(Path.home() / name)
        s.untitled = True
        c = s.canvas
        c.pageChanged.connect(lambda i, s=s: self._on_page(i) if s is self.cur else None)
        c.selectionChanged.connect(lambda n, s=s: self._on_selection(n) if s is self.cur else None)
        c.zoomChanged.connect(lambda z, s=s: self._on_zoom(z) if s is self.cur else None)
        c.cropCancelled.connect(lambda s=s: self.cropCancelled.emit() if s is self.cur else None)
        c.interact = self.markup
        self.sessions.append(s)
        self.center.addWidget(s.loading)
        self.center.addWidget(c)
        self.tabbar.blockSignals(True)
        i = self.tabbar.addTab(s.name)
        self.tabbar.setTabToolTip(i, tip)
        self.tabbar.blockSignals(False)
        self.win.set_mode(self.key)
        self.sync_tabbar()
        self.activate(s)
        doc = pymupdf.open(stream=data, filetype="pdf")
        rects = [doc[k].rect for k in range(doc.page_count)]
        doc._reset_page_refs()
        self._load_ready(s, None, doc, None, rects, data)
        if s.work is not None:
            s.work._saved_sig = ("new document, never saved",)      # changed until saved
        self._status()
        self.win.update_title()
        self._apply_enabled()
        return s

    def _track(self, ld):
        self.loaders.add(ld)
        ld.finished.connect(lambda ld=ld: self.loaders.discard(ld))

    def _wait_loaded(self, s):
        """Run the event loop until s has loaded (or failed / was closed)."""
        from PySide6.QtCore import QEventLoop
        while s in self.sessions and not s.ready:
            loop = QEventLoop()
            QTimer.singleShot(20, loop.quit)
            loop.exec()
        return s in self.sessions and s.ready

    def _load_password(self, s, ld, data):
        """Loader step 1 found a password-protected file: ask here (UI thread), then measure."""
        from . import locked
        if s.loader is not ld or s not in self.sessions:
            return
        self.activate(s)
        try:
            doc, lock = locked.open_pdf(data, locked.asker(self.win, s.source))
        except locked.PasswordCancelled:
            s.loader = None
            self.close_session(s, ask=False)
            self.status("Not opened: no password given.", 4000)
            return
        except Exception as e:
            self._load_failed(s, ld, str(e))
            return
        from .loader import DocLoader
        ld2 = s.loader = DocLoader(str(s.source), self)
        self._track(ld2)
        ld2.progress.connect(lambda ph, d, t, s=s: s.loading.show_progress(ph, d, t) if s.loader is ld2 else None)
        ld2.ready.connect(lambda doc_, lock_, rects, data_, s=s, ld2=ld2: self._load_ready(s, ld2, doc_, lock_, rects))
        ld2.failed.connect(lambda msg, s=s, ld2=ld2: self._load_failed(s, ld2, msg))
        ld2.finished.connect(ld2.deleteLater)
        ld2.measure(doc, lock, data)

    def _load_failed(self, s, ld, msg):
        if s.loader is not ld or s not in self.sessions:
            return
        s.loader = None
        self.close_session(s, ask=False)
        QMessageBox.critical(self.win, APP_NAME, f"Could not open the PDF:\n{msg}")

    def _load_ready(self, s, ld, doc, lock, rects, data=None):
        """The loader finished: the tab becomes a working document."""
        s.data = data if lock is None else None       # thumbnails render from these (not encrypted)
        from . import settings as S
        from .workdoc import WorkingDoc
        if s.loader is not ld or s not in self.sessions:     # closed (or replaced) meanwhile
            doc.close()
            return
        s.loader = None
        try:
            s.work = WorkingDoc(s.source, opened=(doc, lock))
        except Exception as e:
            doc.close()
            self.close_session(s, ask=False)
            QMessageBox.critical(self.win, APP_NAME, f"Could not open the PDF:\n{e}")
            return
        c = s.canvas
        c.set_document(s.work.doc, rects)
        self.center.removeWidget(s.loading)
        s.loading.deleteLater()
        s.loading = None                      # gone: nothing may touch it again
        if s is self.cur and not self.comparing():
            self.center.setCurrentWidget(c)
        c.apply_zoom_mode(S.get(self.win.settings, "default_zoom"))
        c.goto(0)
        self._status()
        if s is self.cur:
            self.doc_version += 1             # a fresh document: announced once (Overview builds its grid once)
            self.activate(s, force=True)
            self.status("Opened in a new tab. Edits stay in memory until you save."
                        + (" Password-protected: saves keep the password (File › Save With Password "
                           "for other choices)." if s.work.lock else ""), 6000)
            self.docChanged.emit()
            self._changed(False)
            self.pagesChanged.emit([])        # Overview and the page-op buttons follow
        else:
            self.status(f"Loaded {s.name}.", 3000)
        if self.comparing():
            self.compare._fill()
        if not getattr(s, "untitled", False):
            self.docOpened.emit(str(s.source))

    # ---------- tabs ----------
    def _build_central(self):
        """Central widget: a stack of [hint, one canvas per tab, compare view]. The tab bar is
        built here but placed by the main window above every mode, so Overview shows it too."""
        from .tabs import CompareView
        self.widget = QWidget()
        v = QVBoxLayout(self.widget)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        tb = QTabBar()
        tb.setObjectName("doctabs")
        tb.setTabsClosable(True)
        tb.setMovable(True)
        tb.setDocumentMode(True)
        tb.setExpanding(False)
        tb.setElideMode(Qt.ElideMiddle)
        tb.setUsesScrollButtons(True)
        tb.currentChanged.connect(self._tab_selected)
        tb.tabCloseRequested.connect(lambda i: self.close_session(self.sessions[i]))
        tb.tabMoved.connect(self._tab_moved)
        tb.hide()
        self.tabbar = tb
        self.center = QStackedWidget()
        self.center.addWidget(self.hint_canvas)
        self.compare = CompareView(self)
        self.compare.closeRequested.connect(lambda: self.show_compare(False))
        self.center.addWidget(self.compare)
        v.addWidget(self.center, 1)

    def tabs_live(self):
        """Tabs are shown and switchable in the PDF Editor and in Overview."""
        return getattr(self.win, "mode", None) in ("editor", "overview")

    def sync_tabbar(self):
        live = self.tabs_live()
        self.tabbar.setVisible(bool(self.sessions) and live)
        for a in self.tab_actions:
            a.setVisible(live)
        self._apply_enabled()

    def _tab_selected(self, i):
        if 0 <= i < len(self.sessions):
            self.activate(self.sessions[i])

    def _tab_moved(self, frm, to):
        self.sessions.insert(to, self.sessions.pop(frm))

    def cycle_tab(self, d):
        if len(self.sessions) > 1 and self.cur is not None:
            i = self.sessions.index(self.cur)
            self.activate(self.sessions[(i + d) % len(self.sessions)])

    def _save_view(self):
        """Remember the side panels' state for the tab being left."""
        s = self.cur
        if s is None:
            return
        if not s.ready:
            return
        s.thumb_rows = self.thumbs.selected_rows()
        bar = (self.thumbs.horizontalScrollBar() if self.thumbs.flow() == QListView.LeftToRight
               else self.thumbs.verticalScrollBar())
        s.thumb_scroll = bar.value()
        grid = self.win.overview_mode.grid if hasattr(self.win, "overview_mode") else None
        if grid is not None and grid.doc is s.work.doc:
            s.overview_sel = sorted(grid.selected)
        s.bookmarks = self.bookmarks.state()

    def activate(self, s, force=False):
        """Make s the active tab: the canvas stack, the side panels, Overview, find and the
        page ops all rebind to it, each restoring that tab's own state. force: rebind even if
        s is already current (after the active tab was closed: s may be None)."""
        if s is self.cur and not force:
            return
        self._save_view()
        self.cur = s
        comparing = self.center.currentWidget() is self.compare
        if s is None:
            self.thumbs.stop()
            self.thumbs.clear()
            if not comparing:
                self.center.setCurrentWidget(self.hint_canvas)
            self.page_total.setText(" / 0 ")
        elif s.is_job:                        # a batch job: its job view; panels empty
            self.thumbs.stop()
            self.thumbs.clear()
            if comparing:
                self.act_compare.setChecked(False)
            self.center.setCurrentWidget(s.view)
            self.tabbar.blockSignals(True)
            self.tabbar.setCurrentIndex(self.sessions.index(s))
            self.tabbar.blockSignals(False)
            self.page_total.setText(" / 0 ")
            if getattr(self.win, "mode", None) == "overview":        # the job view lives in the editor's area
                QTimer.singleShot(0, lambda: self.cur is s and self.win.set_mode(self.key))
        elif not s.ready:                     # still loading: its loading view; panels empty
            self.thumbs.stop()
            self.thumbs.clear()
            if comparing:
                self.show_compare(False)
            self.center.setCurrentWidget(s.loading)        # (exists until the load finishes)
            self.tabbar.blockSignals(True)
            self.tabbar.setCurrentIndex(self.sessions.index(s))
            self.tabbar.blockSignals(False)
            self.page_total.setText(" / 0 ")
        else:
            if comparing:
                self.compare.show_pair(s, self.compare.sessions[1] or s)
            else:
                self.center.setCurrentWidget(s.canvas)
            self.tabbar.blockSignals(True)
            self.tabbar.setCurrentIndex(self.sessions.index(s))
            self.tabbar.blockSignals(False)
            doc = s.work.doc
            self.page_spin.blockSignals(True)
            self.page_spin.setMaximum(max(doc.page_count, 1))
            self.page_spin.blockSignals(False)
            self.page_total.setText(f" / {doc.page_count} ")
            self.thumbs.set_document(doc, self._thumb_source(s))
            self._restore_thumbs(s)
            self._on_zoom(s.canvas.zoom)
            self._on_page(max(0, s.canvas.current_page()))
        self.bookmarks.reload()
        if s is not None and s.ready:
            self.bookmarks.restore_state(s.bookmarks)
        self.sessionChanged.emit()
        self.pagesChanged.emit(list(s.overview_sel) if s else [])   # Overview + page ops follow
        self._apply_enabled()
        self._status()
        self.win.update_title()

    def _thumb_source(self, s):
        """The file's bytes for the thumbnail worker, if the document is still exactly them."""
        w = s.work
        data = getattr(s, "data", None)
        if data is None or w is None or w.step() != 0 or w._base:      # edited (or past the undo floor)
            return None
        return data

    def _restore_thumbs(self, s):
        from PySide6.QtCore import QItemSelectionModel
        t = self.thumbs
        t.blockSignals(True)
        t.clearSelection()
        for r in s.thumb_rows:
            it = t.item(r)
            if it is not None:
                it.setSelected(True)
        cur = s.thumb_rows[0] if s.thumb_rows else max(0, s.canvas.current_page())
        if t.item(cur) is not None:
            t.selectionModel().setCurrentIndex(t.indexFromItem(t.item(cur)), QItemSelectionModel.NoUpdate)
        t.blockSignals(False)
        val = s.thumb_scroll
        bar = t.horizontalScrollBar() if t.flow() == QListView.LeftToRight else t.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(val))

    def close_session(self, s, ask=True):
        """Close one tab (Save / Discard / Cancel first if it has unsaved edits). Returns False
        if cancelled."""
        from . import protect
        if s is None or s not in self.sessions:
            return True
        if ask and not self.confirm_discard(s):
            return False
        i = self.sessions.index(s)
        nxt = None
        if s is self.cur:
            rest = self.sessions[:i] + self.sessions[i + 1:]
            nxt = rest[min(i, len(rest) - 1)] if rest else None
        self.sessions.remove(s)
        self.tabbar.blockSignals(True)
        self.tabbar.removeTab(i)
        self.tabbar.blockSignals(False)
        self.sync_tabbar()
        protect.unregister(s.owner)
        self.compare.session_closed(s)
        if s is self.cur:
            self.cur = None                   # (its view state isn't needed any more)
            self.activate(nxt, force=True)
        if s.canvas is not None:
            self.center.removeWidget(s.canvas)
        if s.is_job:
            self.center.removeWidget(s.view)
        if s.loading is not None:
            self.center.removeWidget(s.loading)
        s.close()                             # cancels a running load; closes its document
        if self.cur is None:
            self.docChanged.emit()
        self._apply_enabled()
        self.win.update_title()
        return True

    def close_doc(self):
        """Close every tab without asking (the window is closing, after confirm_all). Loads
        still running are cancelled and waited for (they stop at their next step)."""
        for s in list(self.sessions):
            self.close_session(s, ask=False)
        for ld in list(self.loaders):
            ld.wait(10000)

    def show_compare(self, on):
        """Split the central area: the active tab on the left, another open PDF (or the same
        one) on the right. The diff itself is a later stage."""
        if on and self.cur is not None and self.cur.ready:
            others = [x for x in self.sessions if x is not self.cur and x.ready]
            self.compare.open(self.cur, others[0] if others else self.cur)
            self.center.setCurrentWidget(self.compare)
        else:
            on = False
            s = self.cur
            self.center.setCurrentWidget(s.view if s is not None and s.is_job else
                                         s.canvas if s is not None else self.hint_canvas)
        self.act_compare.setChecked(on)

    def comparing(self):
        return self.center.currentWidget() is self.compare

    # ---------- editing ----------
    def _changed(self, pages=None):
        """Something in the working copy changed (or was saved): refresh views and state."""
        if pages is not False:
            self.doc_version += 1
            self.canvas.invalidate(pages)
            self.thumbs.refresh(pages)
            if self.comparing():
                self.compare.refresh(self.cur)
            self.docChanged.emit()
        self._apply_enabled()
        self._status()
        self.win.update_title()

    def dirty(self):
        """The ACTIVE tab has unsaved edits."""
        return self.work is not None and self.work.dirty()

    def any_dirty(self):
        return any(s.dirty() for s in self.sessions)

    def highlight_selection(self):
        sel = self.canvas.selection
        if self.work is None or sel is None:
            return
        i, a, b = sel
        quads = self.canvas.page_text(i).quads(a, b)
        if self.run_edit("highlight", "Highlight", lambda doc: doc[i].add_highlight_annot(quads), [i]):
            self.status("Highlighted. Ctrl+Z undoes it.", 4000)

    def run_edit(self, op, label, fn, pages=(), raise_errors=False):
        """The one way an editing tool changes the document. op must be declared in
        oprules.OPS: a journal op is one journal operation; a snapshot op runs on a fresh copy
        (memory-budgeted) and swaps it in. Every view is refreshed. Returns True if applied.
        raise_errors: the caller handles a failed edit itself (nothing changed either way)."""
        from . import oprules
        if self.work is None:
            return False
        if oprules.kind(op) == oprules.JOURNAL:
            try:
                self.last_edit_result = self.work.edit(label, fn, pages)
            except Exception as e:                # rolled back by WorkingDoc.edit: nothing changed
                if raise_errors:
                    raise
                QMessageBox.warning(self.win, APP_NAME, f"That edit couldn't be made:\n{e}")
                return False
            self.canvas.clear_selection()
            if pages == "structural":
                self.rebuild()
            elif pages == "outline":                  # bookmarks only: nothing to re-render
                self._changed(False)
            else:
                self._changed(list(pages) or None)
            return True
        try:
            self.last_edit_result = self.work.snapshot_edit(label, fn, pages)
        except MemoryError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return False
        except Exception as e:                    # the fresh copy is discarded: nothing changed
            if raise_errors:
                raise
            QMessageBox.warning(self.win, APP_NAME, f"That edit couldn't be made:\n{e}")
            return False
        self.canvas.clear_selection()
        self.rebuild()                            # a different document object: rebind every view
        return True

    def undo(self):
        if self.work is None:
            return
        pages = self.work.pages_for_undo()
        before = self.work.doc
        if self.work.undo():
            self.canvas.clear_selection()
            self._after_history(pages, swapped=self.work.doc is not before)

    def redo(self):
        if self.work is None:
            return
        pages = self.work.pages_for_redo()
        before = self.work.doc
        if self.work.redo():
            self.canvas.clear_selection()
            self._after_history(pages, swapped=self.work.doc is not before)

    def _after_history(self, pages, swapped=False):
        from .bookmarks import OUTLINE
        from .pageops import STRUCTURAL
        if swapped or pages == STRUCTURAL:          # a snapshot step swaps the document object
            self.rebuild()
        elif pages == OUTLINE:
            self._changed(False)               # outline only: no page re-render needed
        else:
            self._changed(list(pages) or None)
        self.bookmarks.reload()

    def selected_pages(self):
        """Pages the editor's page ops act on: the thumbnail selection, else the current page."""
        rows = self.thumbs.selected_rows()
        if rows:
            return rows
        c = self.canvas.current_page()
        return [c] if c >= 0 else []

    def rebuild(self, select=None):
        """After a page op (or its undo): page count/order/geometry changed. Rebuild every view,
        keeping the zoom and the current page. Also rebinds every view to the working document
        object, which a snapshot step (or its undo/redo) replaces."""
        doc = self.work.doc
        cur = max(0, self.canvas.current_page())
        zoom = self.canvas.zoom
        self.doc_version += 1
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(doc.page_count, 1))
        self.page_spin.blockSignals(False)
        self.page_total.setText(f" / {doc.page_count} ")
        self.canvas.set_document(doc)
        self.canvas.set_zoom(zoom)
        target = select[0] if select else min(cur, doc.page_count - 1)
        self.canvas.goto(target)
        self.thumbs.set_document(doc)
        if select:
            from PySide6.QtCore import QItemSelectionModel
            self.thumbs.blockSignals(True)
            self.thumbs.clearSelection()
            for r in select:
                it = self.thumbs.item(r)
                if it is not None:
                    it.setSelected(True)
            self.thumbs.selectionModel().setCurrentIndex(self.thumbs.indexFromItem(self.thumbs.item(target)),
                                                         QItemSelectionModel.NoUpdate)
            self.thumbs.blockSignals(False)
        if self.comparing():
            self.compare.refresh(self.cur)
        self.pagesChanged.emit(select or [])
        self.docChanged.emit()
        self.bookmarks.reload()
        self._apply_enabled()
        self._status()
        self.win.update_title()

    def copy(self):
        from PySide6.QtWidgets import QApplication
        t = self.canvas.selected_text()
        if t:
            QApplication.clipboard().setText(t)
            self.status(f"Copied {len(t)} characters.", 3000)

    # ---------- saving ----------
    def _default_target(self):
        src = self.work.source
        cand = src.with_name(f"{src.stem}-edited.pdf")
        if getattr(self.cur, "untitled", False):          # a new document: its own name, not "-edited"
            cand = src
        n = 2
        while cand.exists() and not (self.work.target and cand == self.work.target):
            cand = src.with_name(f"{src.stem}-edited-{n}.pdf")
            n += 1
        return cand

    def save(self):
        """Ctrl+S: the first save this session asks for a new file; later saves go there,
        quietly repeating the last password choice."""
        if self.work is None:
            return False
        if self.work.target is None:
            return self.save_as()
        return self._finish_save(lambda: self.work.save())

    def save_as(self):
        """Ctrl+Shift+S: pick a new file, with the current password choice. The original (any
        open source) is refused."""
        if self.work is None:
            return False
        w = self.work
        if w.lock is not None and w.protection is w.lock:          # "Keep password"
            ok, prot = self._resolve_protection("keep", explicit=False)
        else:
            ok, prot = True, w.protection
        if not ok:
            return False
        return self._save_new(prot, "Save PDF as (a new file)", str(w.target or self._default_target()))

    def save_with_password(self, kind):
        """File > Save With Password: kind = "keep" | "set" | "match" | "clear". Always a new file."""
        if self.work is None:
            return False
        ok, prot = self._resolve_protection(kind, explicit=True)
        if not ok:
            return False
        src = self.work.source
        start = str(src.with_name(f"{src.stem}-unprotected.pdf")) if prot is None else str(self._default_target())
        return self._save_new(prot, "Save PDF with password (a new file)" if prot else
                              "Save PDF without password (a new file)", start)

    def _resolve_protection(self, kind, explicit):
        """-> (ok, protection). Asks whatever the choice needs; ok False = cancelled."""
        from . import locked, password_ui
        w = self.work
        if kind == "keep":
            prot = w.lock
            # the owner password is unknown: the copy would get a random one. Warn on the menu
            # choice every time; on plain Save As only until "Continue anyway" for this document.
            if prot is not None and not prot.owner_known and (explicit or not w.owner_warned):
                r = password_ui.warn_unknown_owner(self.win)
                if r == "continue":
                    w.owner_warned = True
                    return True, prot
                if r != "set":
                    return False, None
                kind = "set"
            else:
                return True, prot
        if kind == "set":
            vals = password_ui.ask_set_passwords(self.win)
            return (False, None) if vals is None else (True, locked.custom(*vals))
        if kind == "match":
            pw = password_ui.ask_match_password(self.win)
            return (False, None) if not pw else (True, locked.match(pw))
        if kind == "clear":
            return (True, None) if password_ui.confirm_clear(self.win) else (False, None)
        raise ValueError(kind)

    def _save_new(self, prot, title, start):
        """Ask for a NEW file (never an open original) and write it with protection prot."""
        from . import protect
        while True:
            path, _ = QFileDialog.getSaveFileName(self.win, title, start, "PDF files (*.pdf)")
            if not path:
                return False
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            if self.work.is_source(path) or protect.is_protected(path):
                QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
                start = str(self._default_target())
                continue
            return self._finish_save(lambda: self.work.save_as(path, prot))

    def _finish_save(self, fn):
        from . import locked
        try:
            fn()
        except Exception as e:
            QMessageBox.critical(self.win, APP_NAME, f"Save failed:\n{e}")
            return False
        self._changed(False)
        w = self.work
        self.status("Saved" + ("." if w.lock is None and w.protection is None
                               else f": {locked.describe(w.protection)}."), 4000)
        return True

    def confirm_discard(self, s=None):
        """Before closing a tab with unsaved edits (s, default the active one): Save / Discard /
        Cancel. Its tab is brought to the front first so the question is clear."""
        s = s or self.cur
        if s is not None and s.is_job:          # a batch job: stop it first? (finished: just close)
            return s.confirm_close(self.win)
        if s is None or not s.dirty():
            return True
        self.activate(s)
        r = QMessageBox.question(self.win, APP_NAME, f"“{s.name}” has unsaved edits. Save them first?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                 QMessageBox.Save)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def confirm_all(self):
        """Window closing: ask for every tab with unsaved edits. False = cancelled."""
        return all(self.confirm_discard(s) for s in list(self.sessions) if s.dirty() or (s.is_job and s.running))

    # ---------- sync ----------
    def _on_page(self, i):
        if i < 0:
            return
        self.pageChanged.emit(i)
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(i + 1)
        self.page_spin.blockSignals(False)
        self.thumbs.blockSignals(True)
        from PySide6.QtCore import QItemSelectionModel
        it = self.thumbs.item(i)
        if it is not None:
            if len(self.thumbs.selected_rows()) <= 1:
                self.thumbs.setCurrentRow(i)          # single selection follows the page
            else:
                self.thumbs.selectionModel().setCurrentIndex(self.thumbs.indexFromItem(it),
                                                             QItemSelectionModel.NoUpdate)
            self.thumbs.scrollToItem(it)
        self.thumbs.blockSignals(False)
        self._status()

    def _on_selection(self, n):
        self._apply_enabled()
        if n < 0:
            self.status("Couldn't read the text on this page for selection.", 5000)
        elif n:
            self.status(f"{n} characters selected, Ctrl+C copies; Edit › Highlight selection (pipeline test).", 4000)

    def _thumb_row(self, r):
        if r >= 0:
            self.canvas.goto(r)

    def _on_zoom(self, z):
        self.zoom_box.lineEdit().setText(f"{round(z * 100)}%")
        self._status()

    def _status(self):
        for i, s in enumerate(self.sessions):
            if s.is_job:
                self.tabbar.setTabText(i, s.tab_text())
                continue
            self.tabbar.setTabText(i, ("● " if s.dirty() else "") + s.name if s.ready else f"Loading… {s.name}")
        if self.doc:
            state = "unsaved edits" if self.dirty() else ("saved" if self.work and self.work.target else "no edits")
            if self.work and (self.work.lock or self.work.protection):
                from . import locked
                state += f"  ·  saves: {locked.describe(self.work.protection)}"
            self.status_page.setText(f"Page {self.canvas.current_page() + 1} of {self.doc.page_count}"
                                     f"  ·  {round(self.canvas.zoom * 100)}%  ·  {state}  ")

    def _zoom_typed(self):
        t = self.zoom_box.currentText().strip().rstrip("%")
        try:
            self.canvas.set_zoom(float(t) / 100)
        except ValueError:
            self._on_zoom(self.canvas.zoom)

    def _set_pan(self, on):
        self.act_hand.setChecked(on)
        self.act_select.setChecked(not on)
        if on:
            self.canvas.clear_selection()
        self.canvas.setDragMode(QGraphicsView.ScrollHandDrag if on else QGraphicsView.NoDrag)


EDITOR_STYLE = """
QToolBar QToolButton{padding:3px 6px;margin:0 1px}
QToolBar#tools QToolButton{padding:2px 2px;margin:0;font-size:11px;min-width:42px}
QToolBar#tools[compact="true"] QToolButton{min-width:0;padding:2px 3px}
QToolBar#tools::handle:vertical{height:6px;margin:3px 8px 2px 8px;border-top:2px dotted #5a6374;border-bottom:2px dotted #5a6374}
QToolBar#tools::handle:horizontal{width:6px;margin:8px 2px 8px 3px;border-left:2px dotted #5a6374;border-right:2px dotted #5a6374}
QToolBar#tools::handle:horizontal:hover{border-color:#2d6cdf}
QToolButton[soon="true"]{color:#7c8799}
QToolButton[popupMode="1"]{padding-right:17px}
QToolBar#tools QToolButton[popupMode="1"]{padding-right:17px}
QToolBar#tools[compact="true"] QToolButton[popupMode="1"]{padding-right:15px}
QToolButton::menu-button{width:13px;border:none;border-left:1px solid #3a4150;margin:3px 0}
QToolButton::menu-button:hover{background:#353c4a}
QToolButton[soon="true"]:hover{background:#262b34;border:1px dashed #4a5262}
QLabel#grouplabel{color:#8a94a6;font-size:9px;font-weight:700;letter-spacing:1px;padding:0 2px}
QLabel#legend{color:#6b7484;font-size:11px}
QListWidget{background:#1b1f26;border:none}
QListWidget::item{color:#9aa0a6;padding:4px;border-radius:4px}
QListWidget::item:selected{background:#2d6cdf;color:#fff}
QDockWidget{color:#9aa0a6}
QDockWidget::title{background:#16181d;padding:6px;font-weight:600}
QGroupBox{border:1px solid #2c313a;border-radius:6px;margin-top:14px;padding-top:8px;color:#8a94a6}
QGroupBox::title{subcontrol-origin:margin;left:8px;padding:0 4px}
QSpinBox{padding:3px;border:1px solid #3a4150;border-radius:5px;background:#1b1f26;min-width:52px}
"""
