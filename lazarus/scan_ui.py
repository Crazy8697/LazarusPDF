"""Stage 15 UI: File › Scan… and Insert › Scan as Pages… (WIA scanners, Lazarus's own dialog).

The dialog lists the WIA scanners (the last one used is picked again; Refresh), and offers only
what the chosen scanner reports: its sources (flatbed / feeder / duplex), colour modes,
resolutions (300 dpi by default), page sizes that fit, brightness / contrast where it has them.
Flatbed: Scan, then "Scan another page" until Done; feeder / duplex: every page in one pass, and
"feeder empty" is simply the end. Scanned pages collect in a list (thumbnails): reorder, rotate,
delete, rescan one. Options: skip blank pages (threshold), deskew, crop to content, image quality,
OCR afterwards. Settings are remembered. "Advanced…" opens the stock WIA dialog for a device that
needs it.

Every scanner call runs on a worker thread that owns the device (COM apartments); the window keeps
working. A device error keeps the pages already scanned and says what happened. Done builds the
pages with the stage 14 engine (worker process): a new unsaved tab, or pages inserted into the
open PDF (one undo step, op insert_scan_pages)."""
import io
import json
import time

from PySide6.QtCore import QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QListView, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSlider, QSpinBox, QVBoxLayout)

from . import APP_NAME
from . import wia

SETTINGS_KEY = "scan"
THUMB = 90
SOURCE_LABEL = {"flatbed": "Flatbed (glass)", "feeder": "Feeder (one side)", "duplex": "Feeder, both sides (duplex)"}
MODE_LABEL = {"color": "Colour", "gray": "Greyscale", "bw": "Black and white"}
SIZE_LABEL = {"letter": "Letter", "legal": "Legal", "a4": "A4", "max": "Whole scan area"}


class Job(QThread):
    """fn(backend) on a thread of its own (the WIA objects belong to the thread that made them).
    Pages a scan delivers come out as `page` while it runs."""
    page = Signal(bytes)
    done = Signal(object, object)              # result, error (wia.ScanError or another exception)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._stop = False

    def stop(self):
        self._stop = True

    def stopped(self):
        return self._stop

    def run(self):
        try:
            b = wia.backend()
            res = self.fn(b, self)
            self.done.emit(res, None)
        except Exception as e:                  # shown in the dialog
            self.done.emit(None, e)


class ScanDialog(QDialog):
    def __init__(self, parent, settings, insert_pages=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Scan as pages" if insert_pages else "Scan")
        self.resize(940, 720)
        self.pages = []                          # [{"data": png, "rotate": deg}]
        self.devices = []
        self.caps = None
        self.job = None
        self.max_gap = 0.0
        self.last_error = None                   # the last scan's error kind (tests)
        self.last_note = ""
        self.skipped = 0
        self._beat_last = None
        self.remembered = self._remembered()
        v = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Scanner"))
        self.device = QComboBox()
        self.device.setMinimumWidth(320)
        self.device.currentIndexChanged.connect(lambda _i: self._device_chosen())
        row.addWidget(self.device, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(lambda: self.refresh())
        row.addWidget(b)
        self.btn_adv = QPushButton("Advanced…")
        self.btn_adv.setToolTip("The scanner's own Windows dialog, for a device that needs it")
        self.btn_adv.clicked.connect(self.advanced)
        row.addWidget(self.btn_adv)
        v.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        mid = QHBoxLayout()
        g = QGroupBox("Settings")
        f = QFormLayout(g)
        self.source = QComboBox()
        self.source.currentIndexChanged.connect(lambda _i: self._source_chosen())
        self.mode = QComboBox()
        self.dpi = QComboBox()
        self.size = QComboBox()
        self.bright = QSlider(Qt.Horizontal)
        self.contrast = QSlider(Qt.Horizontal)
        f.addRow("Source", self.source)
        f.addRow("Colour", self.mode)
        f.addRow("Resolution", self.dpi)
        f.addRow("Page size", self.size)
        self.bright_row = QLabel("Brightness")
        self.contrast_row = QLabel("Contrast")
        f.addRow(self.bright_row, self.bright)
        f.addRow(self.contrast_row, self.contrast)
        self.skip_blank = QCheckBox("Skip blank pages")
        self.blank_thr = QDoubleSpinBox()
        self.blank_thr.setRange(0.05, 10.0)
        self.blank_thr.setSingleStep(0.1)
        self.blank_thr.setSuffix(" % ink or less")
        br = QHBoxLayout()
        br.addWidget(self.skip_blank)
        br.addWidget(self.blank_thr)
        f.addRow(br)
        self.deskew = QCheckBox("Straighten (deskew)")
        self.crop = QCheckBox("Crop to the content")
        f.addRow(self.deskew)
        f.addRow(self.crop)
        self.quality = QComboBox()
        self.quality.addItem("Keep as scanned", "keep")
        self.quality.addItem("Recompress to shrink the file", "recompress")
        f.addRow("Image quality", self.quality)
        self.ocr = QCheckBox("Then run OCR to searchable")
        f.addRow(self.ocr)
        self.at = None
        if insert_pages:
            self.at = QSpinBox()
            self.at.setRange(1, insert_pages + 1)
            self.at.setValue(insert_pages + 1)
            f.addRow("Insert before page", self.at)
            self.ocr.hide()
        mid.addWidget(g)
        g = QGroupBox("Scanned pages")
        gv = QVBoxLayout(g)
        self.list = QListWidget()
        self.list.setViewMode(QListView.IconMode)
        self.list.setIconSize(QSize(THUMB, int(THUMB * 1.3)))
        self.list.setGridSize(QSize(THUMB + 24, int(THUMB * 1.3) + 30))
        self.list.setMovement(QListView.Snap)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setResizeMode(QListView.Adjust)
        self.list.model().rowsMoved.connect(lambda *_: QTimer.singleShot(0, self._sync_from_list))
        gv.addWidget(self.list, 1)
        pr = QHBoxLayout()
        for lab, fn in (("Up", lambda: self.move(-1)), ("Down", lambda: self.move(1)),
                        ("Rotate left", lambda: self.rotate(90)), ("Rotate right", lambda: self.rotate(-90)),
                        ("Delete", self.delete), ("Rescan", self.rescan)):
            bb = QPushButton(lab)
            bb.clicked.connect(fn)
            pr.addWidget(bb)
        gv.addLayout(pr)
        mid.addWidget(g, 1)
        v.addLayout(mid, 1)
        row = QHBoxLayout()
        self.btn_scan = QPushButton("Scan")
        self.btn_scan.clicked.connect(lambda: self.scan())
        self.btn_stop = QPushButton("Cancel scan")
        self.btn_stop.clicked.connect(self.cancel_scan)
        self.btn_stop.hide()
        self.progress = QLabel("")
        self.btn_done = QPushButton("Insert" if insert_pages else "Done: make the PDF")
        self.btn_done.clicked.connect(self._done)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        for wd in (self.btn_scan, self.btn_stop, self.progress):
            row.addWidget(wd)
        row.addStretch(1)
        row.addWidget(self.btn_done)
        row.addWidget(close)
        v.addLayout(row)
        self._beat = QTimer(self, interval=10)
        self._beat.timeout.connect(self._heartbeat)
        self._load_options()
        self._sync()
        QTimer.singleShot(0, self.refresh)

    # ---- remembered settings ----
    def _remembered(self):
        try:
            return json.loads(self.settings.value(SETTINGS_KEY, "", type=str) or "{}")
        except Exception:
            return {}

    def _load_options(self):
        r = self.remembered
        self.skip_blank.setChecked(bool(r.get("skip_blank", False)))
        self.blank_thr.setValue(float(r.get("blank_threshold", 0.5)))
        self.deskew.setChecked(bool(r.get("deskew", False)))
        self.crop.setChecked(bool(r.get("crop", False)))
        self.quality.setCurrentIndex(max(0, self.quality.findData(r.get("quality", "keep"))))
        self.ocr.setChecked(bool(r.get("ocr", False)))

    def remember(self):
        s = self.scan_settings()
        s.update({"device": self.device.currentData(), "skip_blank": self.skip_blank.isChecked(),
                  "blank_threshold": self.blank_thr.value(), "deskew": self.deskew.isChecked(),
                  "crop": self.crop.isChecked(), "quality": self.quality.currentData(),
                  "ocr": self.ocr.isChecked() and not self.ocr.isHidden()})
        self.settings.setValue(SETTINGS_KEY, json.dumps(s))

    # ---- devices and what they can do ----
    def _run(self, fn, after, scanning=False):
        if self.job is not None:
            return False
        j = Job(fn)
        self.job = j
        if scanning:
            j.page.connect(self._page_arrived)
        j.done.connect(lambda res, err, j=j: self._job_done(j, res, err, after))
        self._beat_last = time.perf_counter()
        self._beat.start()
        j.start()
        self._sync()
        return True

    def _job_done(self, j, res, err, after):
        j.wait(5000)
        self.job = None
        self._beat.stop()
        after(res, err)
        self._sync()

    def wait(self, limit=120):
        """Run the event loop until the running job is done (tests)."""
        from PySide6.QtCore import QEventLoop
        t0 = time.time()
        while self.job is not None and time.time() - t0 < limit:
            loop = QEventLoop()
            QTimer.singleShot(10, loop.quit)
            loop.exec()
        return self.job is None

    def _heartbeat(self):
        now = time.perf_counter()
        if self._beat_last is not None:
            self.max_gap = max(self.max_gap, now - self._beat_last)
        self._beat_last = now

    def refresh(self, wait=False):
        self.status.setText("Looking for scanners…")
        self._run(lambda b, j: b.devices(), self._devices_listed)
        if wait:
            self.wait()

    def _devices_listed(self, res, err):
        self.device.blockSignals(True)
        self.device.clear()
        self.devices = list(res or [])
        for dev_id, name in self.devices:
            self.device.addItem(name, dev_id)
        self.device.blockSignals(False)
        if err is not None:
            self.status.setText(f"Windows' scanner service (WIA) couldn't be reached: {err}")
        elif not self.devices:
            self.status.setText("No scanner found. Check that it is switched on and connected, that its driver is "
                                "installed (it should show in Windows' Printers & scanners), and that no other program "
                                "is using it. Then press Refresh.")
            self.caps = None
            self._fill_caps()
        else:
            last = self.remembered.get("device")
            k = self.device.findData(last) if last else -1
            self.device.setCurrentIndex(max(0, k))
            self.status.setText(f"{len(self.devices)} scanner(s) found.")
            self._device_chosen()

    def _device_chosen(self, wait=False):
        dev = self.device.currentData()
        if dev is None:
            return
        self.status.setText("Reading what the scanner can do…")
        self._run(lambda b, j: b.caps(dev), self._caps_read)
        if wait:
            self.wait()

    def _caps_read(self, res, err):
        if err is not None:
            self.caps = None
            self.status.setText(str(err))
        else:
            self.caps = res
            self.status.setText("Ready.")
        self._fill_caps()

    def _fill_caps(self):
        c, r = self.caps, self.remembered
        for combo in (self.source, self.mode, self.dpi, self.size):
            combo.blockSignals(True)
            combo.clear()
        if c is not None:
            for s in c.sources:
                self.source.addItem(SOURCE_LABEL[s], s)
            for m in c.modes:
                self.mode.addItem(MODE_LABEL[m], m)
            for d in c.resolutions:
                self.dpi.addItem(f"{d} dpi", d)
            self.source.setCurrentIndex(max(0, self.source.findData(r.get("source"))))
            self.mode.setCurrentIndex(max(0, self.mode.findData(r.get("mode", "color"))))
            want = r.get("dpi", c.default_dpi)
            self.dpi.setCurrentIndex(max(0, self.dpi.findData(want if want in c.resolutions else c.default_dpi)))
        for combo in (self.source, self.mode, self.dpi):
            combo.blockSignals(False)
        for sl, row, rng, key in ((self.bright, self.bright_row, c.brightness if c else None, "brightness"),
                                  (self.contrast, self.contrast_row, c.contrast if c else None, "contrast")):
            sl.setVisible(rng is not None)
            row.setVisible(rng is not None)
            if rng is not None:
                sl.setRange(rng[0], rng[1])
                sl.setValue(int(r.get(key, (rng[0] + rng[1]) // 2)) if rng[0] <= int(r.get(key, rng[0])) <= rng[1]
                            else (rng[0] + rng[1]) // 2)
        self._source_chosen()

    def _source_chosen(self):
        self.size.blockSignals(True)
        self.size.clear()
        if self.caps is not None and self.source.currentData():
            for s in self.caps.page_sizes(self.source.currentData()):
                self.size.addItem(SIZE_LABEL[s], s)
            self.size.setCurrentIndex(max(0, self.size.findData(self.remembered.get("size", "max"))))
        self.size.blockSignals(False)
        self._sync()

    def scan_settings(self):
        s = {"source": self.source.currentData(), "mode": self.mode.currentData(), "dpi": self.dpi.currentData(),
             "size": self.size.currentData()}
        if self.bright.isVisible() or (self.caps is not None and self.caps.brightness):
            s["brightness"] = self.bright.value()
        if self.contrast.isVisible() or (self.caps is not None and self.caps.contrast):
            s["contrast"] = self.contrast.value()
        return s

    # ---- scanning ----
    def scan(self, wait=False, replace=None):
        """Scan: one page (flatbed) or the whole feeder. replace: the page index a rescan replaces."""
        if self.caps is None or self.job is not None:
            return False
        dev = self.device.currentData()
        settings = self.scan_settings()
        if replace is not None:
            settings["source"] = "flatbed" if "flatbed" in self.caps.sources else settings["source"]
        post = {"skip_blank": self.skip_blank.isChecked(), "threshold": self.blank_thr.value(),
                "deskew": self.deskew.isChecked(), "crop": self.crop.isChecked()}
        self.last_error, self.last_note = None, ""
        self._replace = replace
        self._got = 0
        self.skipped = 0

        def fn(b, j):
            def on_page(data, dpi):
                if post["skip_blank"] and wia.is_blank(data, post["threshold"]):
                    j.page.emit(b"")                     # (a skipped blank page)
                    return
                if post["deskew"]:
                    data = wia.deskew(data)[0]
                if post["crop"]:
                    data = wia.crop(data)
                j.page.emit(data)
            return b.scan(dev, settings, on_page, j.stopped)
        self.progress.setText("Scanning…")
        self.remember()
        self._run(fn, self._scan_done, scanning=True)
        if wait:
            self.wait()
        return True

    def _page_arrived(self, data):
        if not data:
            self.skipped += 1
            self.progress.setText(f"Scanning… {self._got} page(s), {self.skipped} blank skipped")
            return
        self._got += 1
        if self._replace is not None and 0 <= self._replace < len(self.pages):
            self.pages[self._replace] = {"data": data, "rotate": 0}
            self._replace = None
        else:
            self.pages.append({"data": data, "rotate": 0})
        self._fill()
        self.progress.setText(f"Scanning… {self._got} page(s)")

    def _scan_done(self, res, err):
        note = f"{self._got} page(s) scanned" + (f", {self.skipped} blank skipped" if self.skipped else "")
        if err is None:
            self.progress.setText(note + (". Scan another page, or Done." if self.source.currentData() == "flatbed"
                                          else "."))
        elif isinstance(err, wia.ScanError) and err.kind == "empty":
            self.last_error = "empty"
            self.progress.setText(str(err))
        else:
            self.last_error = err.kind if isinstance(err, wia.ScanError) else "error"
            kept = f" The {len(self.pages)} page(s) scanned so far are kept." if self.pages else ""
            self.progress.setText(f"{note}. Stopped: {err}")
            QMessageBox.warning(self, APP_NAME, f"The scan stopped: {err}{kept}")
        self.last_note = self.progress.text()

    def cancel_scan(self):
        if self.job is not None:
            self.job.stop()
            self.progress.setText("Cancelling after this page…")

    def advanced(self):
        """The stock WIA dialog (for a device that needs its own settings): adds what it scans."""
        if self.job is not None:
            return
        try:
            data = wia.backend().acquire_with_stock_dialog() if hasattr(wia.backend(), "acquire_with_stock_dialog") \
                else None
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"The scanner's dialog couldn't be used:\n{e}")
            return
        if data:
            self.pages.append({"data": data, "rotate": 0})
            self._fill()

    # ---- the page list ----
    def _fill(self, select=None):
        self.list.clear()
        from PIL import Image
        for k, p in enumerate(self.pages):
            im = Image.open(io.BytesIO(p["data"]))
            im.draft("RGB", (THUMB * 2, THUMB * 2))
            im = im.convert("RGB")
            if p.get("rotate"):
                im = im.rotate(p["rotate"], expand=True)
            im.thumbnail((THUMB, int(THUMB * 1.3)))
            qi = QImage(im.tobytes(), im.width, im.height, im.width * 3, QImage.Format_RGB888).copy()
            it = QListWidgetItem(QIcon(QPixmap.fromImage(qi)), f"Page {k + 1}")
            it.setData(Qt.UserRole, k)
            self.list.addItem(it)
        for r in select or []:
            if 0 <= r < self.list.count():
                self.list.item(r).setSelected(True)
        self._sync()

    def _sync_from_list(self):
        order = [self.list.item(r).data(Qt.UserRole) for r in range(self.list.count())]
        if sorted(order) == list(range(len(self.pages))):
            self.pages = [self.pages[k] for k in order]
        self._fill()

    def selected(self):
        return sorted(self.list.row(i) for i in self.list.selectedItems())

    def move(self, d):
        rows = self.selected()
        if not rows or (d < 0 and rows[0] == 0) or (d > 0 and rows[-1] == len(self.pages) - 1):
            return
        for r in (rows if d < 0 else reversed(rows)):
            self.pages[r], self.pages[r + d] = self.pages[r + d], self.pages[r]
        self._fill([r + d for r in rows])

    def rotate(self, deg):
        rows = self.selected()
        for r in rows:
            self.pages[r]["rotate"] = (self.pages[r].get("rotate", 0) + deg) % 360
        self._fill(rows)

    def delete(self):
        rows = set(self.selected())
        self.pages = [p for k, p in enumerate(self.pages) if k not in rows]
        self._fill()

    def rescan(self, wait=False):
        rows = self.selected()
        if len(rows) != 1:
            self.progress.setText("Select one page to rescan it.")
            return False
        return self.scan(wait=wait, replace=rows[0])

    def _sync(self):
        busy = self.job is not None
        self.btn_scan.setEnabled(not busy and self.caps is not None)
        self.btn_stop.setVisible(busy)
        self.btn_done.setEnabled(not busy and bool(self.pages))
        flat = self.source.currentData() == "flatbed"
        self.btn_scan.setText("Scan another page" if flat and self.pages else
                              "Scan the feeder" if not flat and self.caps is not None else "Scan")

    def _done(self):
        if self.pages and self.job is None:
            self.remember()
            self.accept()

    def result_value(self):
        items = [{"data": p["data"], "frame": 0, "rotate": p.get("rotate", 0)} for p in self.pages]
        opts = {"page": "fit", "quality": self.quality.currentData(), "ocr": self.ocr.isChecked() and not self.ocr.isHidden()}
        return items, opts, (self.at.value() - 1 if self.at is not None else None)

    def done(self, r):
        if self.job is not None:
            self.job.stop()
            self.job.wait(30000)
        super().done(r)


def ask_scan(parent, settings, insert_pages=None):
    d = ScanDialog(parent, settings, insert_pages)
    if d.exec() != QDialog.Accepted:
        return None
    return d.result_value()


class ScanController:
    def __init__(self, win):
        self.win = win
        self.ed = win.editor_mode
        self.act_scan = QAction("Scan…", win, triggered=lambda _=False: self.scan_new())
        self.act_scan.setToolTip("Scan pages from a scanner (WIA) into a new PDF; it opens as a new tab, unsaved")
        self.act_insert = QAction("Scan as Pages…", win, triggered=lambda _=False: self.scan_insert())
        self.act_insert.setToolTip("Scan pages into this PDF at a position you pick (one undo step)")
        self.ed.menus[3].addAction(self.act_insert)

    def scan_new(self, answer=None, wait=False):
        if answer is None:
            answer = ask_scan(self.win, self.win.settings)
            if answer is None:
                return False
        items, opts, _at = answer
        return self.win.images._start(items, opts, ("new", None), wait, prefix="Scan", op="insert_scan_pages",
                                      label="Insert scanned pages")

    def scan_insert(self, answer=None, wait=False):
        if self.ed.work is None:
            return False
        if answer is None:
            answer = ask_scan(self.win, self.win.settings, self.ed.doc.page_count)
            if answer is None:
                return False
        items, opts, at = answer
        at = self.ed.doc.page_count if at is None else max(0, min(at, self.ed.doc.page_count))
        return self.win.images._start(items, opts, ("insert", (self.ed.cur, at)), wait, prefix="Scan",
                                      op="insert_scan_pages", label="Insert scanned pages")
