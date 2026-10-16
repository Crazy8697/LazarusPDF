"""Stage 14 UI: File › New PDF from Images… (and images dropped on the window), and
Insert › Images as pages… for the open PDF.

The setup dialog lists the images in order with thumbnails (made off the UI thread): drag or
Up / Down to reorder, Remove, rotate each, sort by name / date taken / date modified. Page size
(fit to each image, or Letter / Legal / A4 / A5 / custom, orientation), fit / fill / stretch with
margins and alignment, images per page (1, 2, 4, 6) with spacing, quality (keep original or
recompress), OCR afterwards. The settings are remembered.

The build runs in a worker process (images.worker_main) with "X of Y images" and Cancel. A new
PDF opens as a new tab, unsaved: nothing is written until the user saves it, to a file they
choose. Inserting into the open PDF is one undo step (declared in oprules.py)."""
import json
import multiprocessing
import time
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QImage, QPixmap, QTransform
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QListView, QListWidget, QListWidgetItem, QMessageBox, QProgressDialog, QPushButton,
                               QSpinBox, QVBoxLayout)

from . import APP_NAME
from . import images as I

SETTINGS_KEY = "images_to_pdf"
FILTER = ("Images (*.png *.jpg *.jpeg *.jpe *.jfif *.webp *.gif *.bmp *.dib *.tif *.tiff *.svg *.ico *.tga "
          "*.pcx *.ppm *.pgm *.pbm);;All files (*)")
THUMB = 96


class ThumbLoader(QThread):
    """Thumbnails for the list, off the UI thread (Pillow's decoding mostly runs without the GIL)."""
    ready = Signal(int, QImage)

    def __init__(self, items):
        super().__init__()
        self.items = [dict(i) for i in items]
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from PIL import Image, ImageOps
        for k, it in enumerate(self.items):
            if self._stop:
                return
            try:
                p = Path(it["path"])
                if p.suffix.lower() == ".svg":
                    import pymupdf
                    d = pymupdf.open(str(p))
                    s = THUMB / max(d[0].rect.width, d[0].rect.height)
                    pix = d[0].get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
                    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
                    d.close()
                else:
                    with Image.open(p) as im:
                        if it.get("frame"):
                            im.seek(it["frame"])
                        im.draft("RGB", (THUMB * 2, THUMB * 2))
                        im = ImageOps.exif_transpose(im)
                        im.thumbnail((THUMB, THUMB))
                        im = im.convert("RGB")
                        img = QImage(im.tobytes(), im.width, im.height, im.width * 3, QImage.Format_RGB888).copy()
                self.ready.emit(k, img)
            except Exception:
                continue


class ImagesDialog(QDialog):
    def __init__(self, parent, settings, paths=None, insert_pages=None):
        """insert_pages: page count of the open PDF when inserting into it (adds a position)."""
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Insert images as pages" if insert_pages else "New PDF from images")
        self.resize(900, 700)
        self.items = []
        self._loader = None
        last = self._remembered()
        v = QVBoxLayout(self)
        g = QGroupBox("Images, in page order")
        gv = QVBoxLayout(g)
        self.list = QListWidget()
        self.list.setViewMode(QListView.IconMode)
        self.list.setIconSize(QSize(THUMB, THUMB))
        self.list.setGridSize(QSize(THUMB + 30, THUMB + 34))
        self.list.setMovement(QListView.Snap)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setResizeMode(QListView.Adjust)
        self.list.model().rowsMoved.connect(lambda *_: self._sync_from_list())
        gv.addWidget(self.list, 1)
        row = QHBoxLayout()
        for lab, fn in (("Add files…", self.add_files), ("Add folder…", self.add_folder), ("Remove", self.remove),
                        ("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1)),
                        ("Rotate left", lambda: self.rotate(90)), ("Rotate right", lambda: self.rotate(-90))):
            b = QPushButton(lab)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.recurse = QCheckBox("Subfolders")
        self.recurse.setChecked(bool(last.get("recurse", False)))
        row.addWidget(self.recurse)
        row.addStretch(1)
        self.sort = QComboBox()
        for k, lab in (("name", "Sort by name"), ("taken", "Sort by date taken"), ("modified", "Sort by date modified")):
            self.sort.addItem(lab, k)
        bs = QPushButton("Sort")
        bs.clicked.connect(lambda: self.sort_by(self.sort.currentData()))
        row.addWidget(self.sort)
        row.addWidget(bs)
        self.count = QLabel("")
        row.addWidget(self.count)
        gv.addLayout(row)
        v.addWidget(g, 1)
        g = QGroupBox("Pages")
        f = QFormLayout(g)
        self.page = QComboBox()
        for k, lab in (("fit", "Fit each page to its image"), ("letter", "Letter"), ("legal", "Legal"), ("a4", "A4"),
                       ("a5", "A5"), ("custom", "Custom (mm)")):
            self.page.addItem(lab, k)
        f.addRow("Page size", self.page)
        cr = QHBoxLayout()
        self.cw, self.ch = QDoubleSpinBox(), QDoubleSpinBox()
        for s_ in (self.cw, self.ch):
            s_.setRange(20, 2000)
            s_.setSuffix(" mm")
            cr.addWidget(s_)
        cr.addStretch(1)
        f.addRow("Custom width / height", cr)
        self.orient = QComboBox()
        for k, lab in (("auto", "Auto (as the image)"), ("portrait", "Portrait"), ("landscape", "Landscape")):
            self.orient.addItem(lab, k)
        f.addRow("Orientation", self.orient)
        self.placement = QComboBox()
        for k, lab in (("fit", "Fit within the margins (keep proportions)"), ("fill", "Fill (crop to the page)"),
                       ("stretch", "Stretch")):
            self.placement.addItem(lab, k)
        f.addRow("Image on the page", self.placement)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 100)
        self.margin.setSuffix(" mm")
        f.addRow("Margin", self.margin)
        self.align = QComboBox()
        for k in ("center", "top-left", "top-center", "top-right", "center-left", "center-right", "bottom-left",
                  "bottom-center", "bottom-right"):
            self.align.addItem(k.replace("-", " ").capitalize(), k)
        f.addRow("Alignment", self.align)
        self.per_page = QComboBox()
        for n in (1, 2, 4, 6):
            self.per_page.addItem(f"{n} per page" if n > 1 else "One image per page", n)
        self.spacing = QDoubleSpinBox()
        self.spacing.setRange(0, 50)
        self.spacing.setSuffix(" mm")
        f.addRow("Images per page", self.per_page)
        f.addRow("Spacing (grid)", self.spacing)
        v.addWidget(g)
        g = QGroupBox("Quality")
        f = QFormLayout(g)
        self.quality = QComboBox()
        self.quality.addItem("Keep the originals (JPEGs embedded as they are)", "keep")
        self.quality.addItem("Recompress to shrink the file", "recompress")
        f.addRow(self.quality)
        self.jq = QSpinBox()
        self.jq.setRange(10, 100)
        self.mdpi = QSpinBox()
        self.mdpi.setRange(36, 1200)
        self.mdpi.setSuffix(" dpi")
        f.addRow("JPEG quality", self.jq)
        f.addRow("No sharper than", self.mdpi)
        self.ocr = QCheckBox("Then run OCR to searchable on the new pages")
        f.addRow(self.ocr)
        v.addWidget(g)
        self.at = None
        if insert_pages:
            self.at = QSpinBox()
            self.at.setRange(1, insert_pages + 1)
            self.at.setValue(insert_pages + 1)
            f.addRow("Insert before page (last + 1 = at the end)", self.at)
            self.ocr.hide()
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setText("Insert" if insert_pages else "Create PDF")
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.load_options(last)
        self.quality.currentIndexChanged.connect(self._sync)
        self.page.currentIndexChanged.connect(self._sync)
        if paths:
            self.add_paths(paths)
        self._sync()

    # ---- remembered settings ----
    def _remembered(self):
        try:
            return json.loads(self.settings.value(SETTINGS_KEY, "", type=str) or "{}")
        except Exception:
            return {}

    def load_options(self, o):
        d = dict(I.DEFAULTS)
        d.update(o or {})
        for combo, k in ((self.page, "page"), (self.orient, "orientation"), (self.placement, "placement"),
                         (self.align, "align"), (self.per_page, "per_page"), (self.quality, "quality")):
            combo.setCurrentIndex(max(0, combo.findData(d[k])))
        self.cw.setValue(float(d["custom_w"]))
        self.ch.setValue(float(d["custom_h"]))
        self.margin.setValue(float(d["margin_mm"]))
        self.spacing.setValue(float(d["spacing_mm"]))
        self.jq.setValue(int(d["jpeg_quality"]))
        self.mdpi.setValue(int(d["max_dpi"]))
        self.ocr.setChecked(bool(d["ocr"]))

    def options(self):
        return {"page": self.page.currentData(), "custom_w": self.cw.value(), "custom_h": self.ch.value(),
                "orientation": self.orient.currentData(), "placement": self.placement.currentData(),
                "margin_mm": self.margin.value(), "align": self.align.currentData(),
                "per_page": self.per_page.currentData(), "spacing_mm": self.spacing.value(),
                "quality": self.quality.currentData(), "jpeg_quality": self.jq.value(), "max_dpi": self.mdpi.value(),
                "ocr": self.ocr.isChecked() and not self.ocr.isHidden()}

    def remember(self):
        o = self.options()
        o["recurse"] = self.recurse.isChecked()
        self.settings.setValue(SETTINGS_KEY, json.dumps(o))

    def _sync(self, *_):
        rec = self.quality.currentData() == "recompress"
        self.jq.setEnabled(rec)
        self.mdpi.setEnabled(rec)
        fixed = self.page.currentData() != "fit"
        for wd in (self.orient, self.placement, self.margin, self.align):
            wd.setEnabled(fixed or self.per_page.currentData() > 1)
        self.cw.setEnabled(self.page.currentData() == "custom")
        self.ch.setEnabled(self.page.currentData() == "custom")
        self.count.setText(f"{len(self.items)} page(s) of images")
        self.ok.setEnabled(bool(self.items))

    # ---- the list ----
    def add_paths(self, paths, recurse=None):
        rec = self.recurse.isChecked() if recurse is None else recurse
        new = I.items_for(I.collect(paths, rec))
        self.items += new
        self._fill()

    def add_files(self):
        ps, _ = QFileDialog.getOpenFileNames(self, "Add images", str(Path.home()), FILTER)
        if ps:
            self.add_paths(ps)

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder of images", str(Path.home()))
        if d:
            self.add_paths([d])

    def _fill(self, select=None):
        if self._loader is not None:
            self._loader.stop()
            self._loader.wait(3000)
        self.list.clear()
        blank = QPixmap(THUMB, THUMB)
        blank.fill(Qt.lightGray)
        for k, it in enumerate(self.items):
            label = Path(it["path"]).name + (f" [{it['frame'] + 1}]" if it.get("frame") else "")
            li = QListWidgetItem(QIcon(blank), label)
            li.setData(Qt.UserRole, k)
            li.setToolTip(label)
            self.list.addItem(li)
        for r in select or []:
            if 0 <= r < self.list.count():
                self.list.item(r).setSelected(True)
        self._loader = ThumbLoader(self.items)
        self._loader.ready.connect(self._thumb)
        self._loader.start()
        self._sync()

    def _thumb(self, k, img):
        if k < self.list.count():
            rot = self.items[k].get("rotate", 0)
            pm = QPixmap.fromImage(img)
            if rot:
                pm = pm.transformed(QTransform().rotate(-rot))
            self.list.item(k).setIcon(QIcon(pm))

    def _sync_from_list(self):
        order = [self.list.item(r).data(Qt.UserRole) for r in range(self.list.count())]
        self.items = [self.items[k] for k in order]
        sel = [r for r in range(self.list.count()) if self.list.item(r).isSelected()]
        QTimer.singleShot(0, lambda: self._fill(sel))

    def selected(self):
        return sorted(self.list.row(i) for i in self.list.selectedItems())

    def remove(self):
        rows = set(self.selected())
        self.items = [it for k, it in enumerate(self.items) if k not in rows]
        self._fill()

    def move(self, d):
        rows = self.selected()
        if not rows:
            return
        if d < 0 and rows[0] == 0 or d > 0 and rows[-1] == len(self.items) - 1:
            return
        for r in (rows if d < 0 else reversed(rows)):
            self.items[r], self.items[r + d] = self.items[r + d], self.items[r]
        self._fill([r + d for r in rows])

    def rotate(self, deg):
        """deg: anticlockwise (Rotate left = 90, Rotate right = -90)."""
        rows = self.selected() or list(range(len(self.items)))
        for r in rows:
            self.items[r]["rotate"] = (self.items[r].get("rotate", 0) + deg) % 360
        self._fill(rows)

    def sort_by(self, key):
        self.items = I.sort_items(self.items, key)
        self._fill()

    def _ok(self):
        if not self.items:
            return
        self.remember()
        self.accept()

    def done(self, r):
        if self._loader is not None:
            self._loader.stop()
            self._loader.wait(3000)
        super().done(r)


def ask_images(parent, settings, paths=None, insert_pages=None):
    """-> (items, options, insert position or None) or None."""
    d = ImagesDialog(parent, settings, paths, insert_pages)
    if d.exec() != QDialog.Accepted:
        return None
    return [dict(i) for i in d.items], d.options(), (d.at.value() - 1 if d.at is not None else None)


class ImagesController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.numbers = 0
        self.job = None
        self.last = None              # what the last build reported (tests): size, peak memory, longest pause
        self.act_new = QAction("New PDF from Images…", win, triggered=lambda _=False: self.new_pdf())
        self.act_new.setToolTip("Make a new PDF from images (each a page, or several per page); it opens as a new "
                                "tab and is only written when you save it")
        self.act_insert = QAction("Images as Pages…", win, triggered=lambda _=False: self.insert())
        self.act_insert.setToolTip("Insert images as new pages into this PDF (one undo step)")
        self.ed.menus[3].addSeparator()
        self.ed.menus[3].addAction(self.act_insert)
        self._timer = QTimer(self, interval=40)
        self._timer.timeout.connect(self._poll)

    # ---- entry points ----
    def new_pdf(self, paths=None, answer=None, wait=False, test_delay=0.0):
        if answer is None:
            answer = ask_images(self.win, self.win.settings, paths)
            if answer is None:
                return False
        items, opts, _at = answer
        return self._start(items, opts, ("new", None), wait, test_delay)

    def insert(self, answer=None, wait=False):
        if self.ed.work is None:
            return False
        if answer is None:
            answer = ask_images(self.win, self.win.settings, None, self.ed.doc.page_count)
            if answer is None:
                return False
        items, opts, at = answer
        at = self.ed.doc.page_count if at is None else max(0, min(at, self.ed.doc.page_count))
        return self._start(items, opts, ("insert", (self.ed.cur, at)), wait)

    def dropped(self, paths):
        """Images dropped on the window: the New PDF from Images dialog, with them listed."""
        return self.new_pdf(paths=paths)

    # ---- the background build ----
    def _start(self, items, opts, purpose, wait, test_delay=0.0, prefix="Images", op="insert_image_pages",
               label="Insert images as pages"):
        if self.job is not None or not items:
            return False
        ctx = multiprocessing.get_context("spawn")
        conn, send = ctx.Pipe(duplex=False)
        cancel = ctx.Event()
        o = dict(opts)
        do_ocr = bool(o.pop("ocr", False))
        proc = ctx.Process(target=I.worker_main, args=({"items": items, "options": o, "test_delay": test_delay},
                                                        send, cancel), daemon=True)
        proc.start()
        send.close()
        dlg = QProgressDialog("Starting…", "Cancel", 0, len(items), self.win)
        dlg.setWindowTitle("Images to PDF")
        dlg.setWindowModality(Qt.NonModal)            # the window stays usable
        dlg.setMinimumDuration(400)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(cancel.set)
        self.job = {"proc": proc, "conn": conn, "cancel": cancel, "dlg": dlg, "purpose": purpose, "n": len(items),
                    "ocr": do_ocr, "last": time.perf_counter(), "gap": 0.0, "done": None, "seen": [],
                    "prefix": prefix, "op": op, "label": label}
        self.last = None
        self._timer.start()
        if wait:
            from PySide6.QtCore import QEventLoop
            t0 = time.time()
            while self.job is not None and time.time() - t0 < 900:
                loop = QEventLoop()
                QTimer.singleShot(15, loop.quit)
                loop.exec()
            return self.last is not None and self.last.get("ok", False)
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
                    j["seen"].append(done)
                    j["dlg"].setValue(done)
                    j["dlg"].setLabelText(f"{done} of {total} images")
                    continue
                if msg[0] == "done":
                    data = j["conn"].recv_bytes()
                    self._finish(True, data=data, peak=msg[2])
                    return
                self._finish(False, why=msg[0], detail=msg[1] if len(msg) > 1 else "")
                return
        except (EOFError, OSError):
            self._finish(False, why="failed", detail="the build stopped unexpectedly")
            return
        if not j["proc"].is_alive() and not j["conn"].poll():
            self._finish(False, why="failed", detail="the build stopped unexpectedly")

    def _finish(self, ok, data=None, peak=None, why="", detail=""):
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
        self.last = {"ok": ok, "why": why, "size": len(data) if data else 0, "peak": peak,
                     "gap_ms": round(j["gap"] * 1000), "progress": list(j["seen"])}
        if not ok:
            if why == "cancelled":
                self.win.statusBar().showMessage("Images to PDF cancelled: nothing was made.", 5000)
            else:
                QMessageBox.warning(self.win, APP_NAME, f"The PDF couldn't be made from the images:\n{detail}")
            return
        kind, info = j["purpose"]
        if kind == "new":
            self.numbers += 1
            s = self.ed.open_built(data, f"{j['prefix']} {self.numbers}.pdf",
                                   f"A new PDF from {'a scan' if j['prefix'] == 'Scan' else 'images'} (not saved yet)")
            self.last["session"] = s
            self.win.statusBar().showMessage(f"New PDF from {j['n']} image page(s): edit it, then save it where you "
                                             "like (Ctrl+S).", 8000)
            if j["ocr"]:
                QTimer.singleShot(0, lambda: self.ed.stage10.ocr({"range": "all", "lang": "eng", "force": False}))
        else:
            sess, at = info
            if self.ed.cur is not sess:
                self.ed.activate(sess)
            import pymupdf
            from . import pageops
            src = pymupdf.open(stream=data, filetype="pdf")
            n = src.page_count
            try:
                ok = self.ed.run_edit(j["op"], j["label"],
                                      lambda d: pageops._graft(d, src, list(range(n)), at), "structural")
            finally:
                src.close()
            self.last["inserted"] = (at, n) if ok else None
            if ok:
                self.win.statusBar().showMessage(f"Inserted {n} page(s). Ctrl+Z undoes it.", 6000)
