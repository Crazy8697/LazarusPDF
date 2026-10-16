"""Stage 10 tools in the PDF Editor: the controller that turns the greyed placeholders into
working tools, and their dialogs. The operations themselves are in stage10.py."""
from pathlib import Path

import pymupdf
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCursor, QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSpinBox,
                               QToolButton, QVBoxLayout)

from . import APP_NAME
from . import stage10 as T


def realize(ed, key, icon, tip, handler=None, menu=None):
    """A placeholder action becomes a working one: its handler instead of the "not implemented
    yet" message, a lit icon, and every toolbar / panel button showing it restyled."""
    from .pdfeditor import _icon
    a = ed.soon[key]
    try:
        a.triggered.disconnect()
    except (RuntimeError, TypeError):
        pass
    a.setProperty("soon", False)
    a.setIcon(_icon(icon))
    a.setToolTip(tip)
    a.setStatusTip(tip.replace("<b>", "").replace("</b>", "").replace("<br>", " "))
    if handler is not None:
        a.triggered.connect(lambda _=False: handler())
    if menu is not None:
        a.setMenu(menu)
    buttons = []
    for bar in ed.groups:
        w = bar.widgetForAction(a)
        if w is not None:
            buttons.append(w)
    for _grid, bs in getattr(ed, "_doc_grids", []):
        buttons += [b for b in bs if b.defaultAction() is a]
    buttons += [b for b in ed.page_buttons.values() if b.defaultAction() is a]
    for w in buttons:
        w.setProperty("soon", False)
        if menu is not None and isinstance(w, QToolButton):
            w.setPopupMode(QToolButton.MenuButtonPopup)
        w.style().unpolish(w)
        w.style().polish(w)
    return a


class Stage10Controller(QObject):
    def __init__(self, ed, win):
        super().__init__(win)
        self.ed, self.win = ed, win
        self.last_verify = None             # characters found under applied marks (0 = clean)
        self.redact_fill = (0.0, 0.0, 0.0)
        self._redaction()
        self._forms()
        self._plain = None                  # (doc identity, plain copy) for previews
        realize(self.ed, "headerfooter", "page-layout-header-footer",
                "<b>Header / footer</b><br>Text in the left / centre / right of the header and footer, font, size, "
                "colour, margins and page range, with a preview first. One undo step.",
                handler=self.header_footer)
        realize(self.ed, "pagenumbers", "numeric",
                "<b>Page numbers</b><br>Position, format (1 / Page 1 / Page 1 of N), start number, font, size, "
                "colour and page range, with a preview first. One undo step.", handler=self.page_numbers)
        realize(self.ed, "watermark", "watermark",
                "<b>Watermark</b><br>Text or an image, centre / diagonal / corners, opacity, rotation, size, colour "
                "and font, page range, behind or in front of the content, with a preview first. One undo step.",
                handler=self.watermark)
        realize(self.ed, "metadata", "information-outline",
                "<b>Metadata</b><br>Title, author, subject, keywords, creator, producer; the dates; or remove all "
                "metadata (Info and XMP). One undo step.", handler=self.metadata)
        realize(self.ed, "flatten", "layers-triple-outline",
                "<b>Flatten</b><br>Bake annotations (highlights, drawings, whiteout, images, signatures, callouts, "
                "measurements…) and / or form fields into the page content, for a page range or the whole "
                "document. Whiteout still only covers: Redact is what removes content.", handler=self.flatten)
        self.last_compress = None           # (before, after) bytes of the last compressed copy
        realize(self.ed, "compress", "zip-box-outline",
                "<b>Compress</b><br>Save a compressed copy (a new file): garbage collection, compressed streams, "
                "and optionally images downsampled to a DPI with a JPEG quality. Shows the size before and after.",
                handler=self.compress)
        self.ocr_worker = None
        self.last_ocr = None                # (done pages, skipped pages) of the last OCR run
        realize(self.ed, "ocrpage", "text-recognition",
                "<b>OCR to searchable</b><br>Adds an invisible text layer to scanned pages (Tesseract, on this "
                "computer) so they can be searched and selected. Page range, language; pages that already have "
                "text are skipped unless forced. Runs in the background; one undo step.", handler=self.ocr)
        realize(self.ed, "bates", "pound-box-outline",
                "<b>Bates numbers</b><br>Prefix, start number, digits (zero padding), suffix, position, font, size, "
                "colour and page range, with a preview first. One undo step.", handler=self.bates)
        # 7. encrypt: File › Save With Password already does it (AES-256); the panel button opens that menu
        realize(self.ed, "encrypt", "lock-outline",
                "<b>Password / encrypt</b><br>Save a copy with a password: Keep / Set / Match / Clear (the same "
                "menu as File › Save With Password). Every protected copy uses AES-256.",
                handler=lambda: self.ed.pw_menu.popup(QCursor.pos()), menu=self.ed.pw_menu)

    # ---------------- 1. redaction ----------------
    def _redaction(self):
        ed, win = self.ed, self.win
        m = QMenu("Redact", win)
        m.addAction("Mark areas (drag a box)").triggered.connect(lambda: ed.markup.set_tool("redact"))
        m.addAction("Mark text (drag across it)").triggered.connect(lambda: ed.markup.set_tool("redact_text"))
        m.addAction("Mark all search hits (Ctrl+F first)").triggered.connect(self.redact_hits)
        m.addSeparator()
        self.act_apply = m.addAction("Apply redactions…")
        self.act_apply.triggered.connect(lambda: self.apply_redactions())
        m.addAction("Remove all marks").triggered.connect(self.remove_marks)
        self.redact_menu = m
        act = realize(ed, "redact", "format-color-marker-cancel",
                      "<b>Redact</b><br>Mark areas or text, review the marks, then Apply: what is under them "
                      "is permanently removed (text, image pixels, drawings). The arrow beside it has the rest.",
                      handler=lambda: ed.markup.set_tool("redact"), menu=m)
        act.setCheckable(True)
        ed.markup.group.addAction(act)
        ed.markup.actions["redact"] = act
        ed.menus[2].addSeparator()                         # the Edit menu
        ed.menus[2].addAction(self.act_apply)

    # ---------------- 2. forms ----------------
    def _forms(self):
        ed, win = self.ed, self.win
        m = QMenu("Form fields", win)
        m.addAction("Edit fields (select, move, resize, delete)").triggered.connect(
            lambda: ed.markup.set_tool("form_edit"))
        m.addSeparator()
        for k, label in (("form_text", "Add text field"), ("form_check", "Add checkbox"),
                         ("form_radio", "Add radio button"), ("form_combo", "Add dropdown"),
                         ("form_list", "Add list box")):
            m.addAction(label).triggered.connect(lambda _=False, k=k: ed.markup.set_tool(k))
        self.form_menu = m
        act = realize(ed, "formfield", "form-textbox",
                      "<b>Form fields</b><br>With the Select tool, click a field to fill it. Here: add text fields, "
                      "checkboxes, radio buttons, dropdowns, list boxes (drag a box), or edit fields (move, resize, "
                      "delete, rename, choices).", handler=lambda: ed.markup.set_tool("form_edit"), menu=m)
        act.setCheckable(True)
        ed.markup.group.addAction(act)
        ed.markup.actions["formfield"] = act

    # ---------------- previews and the shared apply ----------------
    def preview(self, fn, page):
        """Render page `page` with fn(doc, targets) applied to a one-page plain copy of it."""
        doc = self.ed.doc
        key = (self.ed.session, self.ed.doc_version)
        if self._plain is None or self._plain[0] != key:
            if self._plain is not None:
                self._plain[1].close()
            self._plain = (key, pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)))
        one = pymupdf.open()
        one.insert_pdf(self._plain[1], from_page=page, to_page=page)
        try:
            fn(one, [(0, page + 1, doc.page_count, 0)])
            pix = one[0].get_pixmap(matrix=pymupdf.Matrix(0.6, 0.6), alpha=False)
        finally:
            one.close()
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format_RGB888).copy()
        return QPixmap.fromImage(img)

    def run_tool(self, title, fields, op, label, make, values=None, note=""):
        """Ask (unless values are given), then apply make(values) -> fn(doc, targets) to the page
        range in one undo step. Returns the pages done, or None."""
        doc = self.ed.doc
        if doc is None:
            return None
        start = max(0, self.ed.canvas.current_page())
        if values is None:
            values = ask_tool(self.win, title, fields,
                              lambda v, p: self.preview(make(v), p), doc.page_count, start, note)
            if values is None:
                return None
        try:
            pages = T.parse_range(values.get("range", ""), doc.page_count)
        except ValueError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return None
        fn = make(values)
        tg = T.targets(pages, doc.page_count)
        if self.ed.run_edit(op, label, lambda d: fn(d, tg), pages):
            self.win.statusBar().showMessage(f"{label}: {len(pages)} page(s). Ctrl+Z undoes it.", 5000)
            return pages
        return None

    # ---------------- 3. header / footer ----------------
    HF_FIELDS = [("tl", "Header left", "text", "", None), ("tc", "Header centre", "text", "", None),
                 ("tr", "Header right", "text", "", None), ("bl", "Footer left", "text", "", None),
                 ("bc", "Footer centre", "text", "", None), ("br", "Footer right", "text", "Page {page} of {pages}", None),
                 ("font", "Font", "font", "helv", None), ("size", "Size", "float", 9.0, (4, 72)),
                 ("color", "Colour", "color", (0.2, 0.2, 0.2), None), ("mx", "Side margin (pt)", "float", 36.0, (0, 300)),
                 ("my", "Top / bottom margin (pt)", "float", 24.0, (0, 300)), ("range", "Pages", "range", "all", None)]

    def header_footer(self, values=None):
        def make(v):
            slots = {k: v[k] for k in T.SLOTS if v.get(k)}
            return lambda d, tg: T.header_footer(d, tg, slots, v["font"], v["size"], v["color"], v["mx"], v["my"])
        return self.run_tool("Header / footer", self.HF_FIELDS, "stamp_text", "Header / footer", make, values,
                             note="Placeholders: {page}, {pages}, {date}.")

    # ---------------- 4. page numbers ----------------
    PN_FIELDS = [("slot", "Position", "choice", "bc", list(T.SLOTS.items())),
                 ("fmt", "Format", "choice", "n", T.NUMBER_FORMATS), ("start", "Start at", "int", 1, (0, 999999)),
                 ("font", "Font", "font", "helv", None), ("size", "Size", "float", 10.0, (4, 72)),
                 ("color", "Colour", "color", (0, 0, 0), None), ("mx", "Side margin (pt)", "float", 36.0, (0, 300)),
                 ("my", "Top / bottom margin (pt)", "float", 24.0, (0, 300)), ("range", "Pages", "range", "all", None)]

    def page_numbers(self, values=None):
        def make(v):
            return lambda d, tg: T.page_numbers(d, tg, v["slot"], v["fmt"], v["start"], v["font"], v["size"],
                                                v["color"], v["mx"], v["my"])
        return self.run_tool("Page numbers", self.PN_FIELDS, "stamp_text", "Page numbers", make, values,
                             note="Numbers count through the page range from the start number.")

    # ---------------- 5. watermark ----------------
    WM_FIELDS = [("kind", "Watermark", "choice", "text", [("text", "Text"), ("image", "Image")]),
                 ("text", "Text", "text", "CONFIDENTIAL", None), ("image", "Image", "file", "", None),
                 ("position", "Position", "choice", "diagonal", T.WM_POSITIONS),
                 ("angle", "Rotation (° anticlockwise; diagonal: automatic)", "float", 0.0, (-360, 360)),
                 ("size", "Text size (pt)", "float", 60.0, (6, 400)), ("width", "Image width (% of page)", "float", 40.0, (1, 100)),
                 ("font", "Font", "font", "hebo", None), ("color", "Colour", "color", (0.75, 0.1, 0.1), None),
                 ("opacity", "Opacity (%)", "int", 25, (1, 100)), ("behind", "Behind the content", "check", True, None),
                 ("range", "Pages", "range", "all", None)]

    def watermark(self, values=None):
        def make(v):
            ang = None if v["position"] == "diagonal" else float(v["angle"])
            if v["kind"] == "image":
                if not v.get("image"):
                    raise ValueError("Choose an image for the watermark")
                from .contentedit import load_image
                png = load_image(v["image"])[0]
                return lambda d, tg: T.watermark_image(d, tg, png, v["position"], v["width"], v["opacity"] / 100,
                                                       float(v["angle"]), v["behind"])
            return lambda d, tg: T.watermark_text(d, tg, v["text"], v["position"], v["font"], v["size"], v["color"],
                                                  v["opacity"] / 100, ang, v["behind"])
        return self.run_tool("Watermark", self.WM_FIELDS, "watermark", "Watermark", make, values,
                             note="An image watermark accepts PNG, JPG, WebP, SVG and the other formats Insert image takes.")

    # ---------------- 6. metadata ----------------
    def metadata(self, answer=None):
        """answer: ("set", {field: value}) or ("remove",); None asks."""
        doc = self.ed.doc
        if doc is None:
            return False
        if answer is None:
            answer = ask_metadata(self.win, dict(doc.metadata or {}), bool(doc.get_xml_metadata()))
            if answer is None:
                return False
        if answer[0] == "remove":
            ok = self.ed.run_edit("remove_metadata", "Remove all metadata", T.remove_metadata, ())
            msg = "All metadata removed (Info and XMP). Ctrl+Z undoes it."
        else:
            vals = answer[1]
            ok = self.ed.run_edit("set_metadata", "Edit metadata", lambda d: T.set_metadata(d, vals), ())
            msg = "Metadata updated. Ctrl+Z undoes it."
        if ok:
            self.win.statusBar().showMessage(msg, 5000)
        return ok

    # ---------------- 8. flatten ----------------
    FLAT_FIELDS = [("annots", "Annotations", "check", True, None), ("widgets", "Form fields", "check", True, None),
                   ("range", "Pages", "range", "all", None)]

    def flatten(self, values=None):
        def make(v):
            if not (v["annots"] or v["widgets"]):
                raise ValueError("Choose annotations, form fields, or both")
            return lambda d, tg: T.flatten(d, [t_[0] for t_ in tg], v["annots"], v["widgets"])
        return self.run_tool("Flatten", self.FLAT_FIELDS, "flatten", "Flatten", make, values,
                             note="Flattened items become part of the page and can no longer be edited, moved or "
                                  "filled. Whiteout still only COVERS what is under it (the text can still be "
                                  "extracted); Redact is what removes content. Redaction marks are not flattened.")

    # ---------------- 9. compress ----------------
    def compress(self, values=None, dest=None):
        """values: {downsample, threshold, target, quality}; dest: the new file (asks if None)."""
        work = self.ed.work
        if work is None:
            return None
        if values is None:
            values = ask_compress(self.win)
            if values is None:
                return None
        from PySide6.QtWidgets import QFileDialog
        from . import protect
        src = work.source
        start = str(src.with_name(f"{src.stem}-compressed.pdf"))
        while dest is None:
            p, _ = QFileDialog.getSaveFileName(self.win, "Save compressed copy (a new file)", start, "PDF files (*.pdf)")
            if not p:
                return None
            if not p.lower().endswith(".pdf"):
                p += ".pdf"
            if work.is_source(p) or protect.is_protected(p):
                QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
                continue
            dest = p
        before = src.stat().st_size if src.exists() else len(work.doc.tobytes())
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            after = T.compress_to(work, dest, values["downsample"], values["threshold"], values["target"],
                                  values["quality"])
        except Exception as e:
            QMessageBox.warning(self.win, APP_NAME, f"Couldn't write the compressed copy:\n{e}")
            return None
        finally:
            QApplication.restoreOverrideCursor()
        self.last_compress = (before, after)
        pct = 100 * (1 - after / before) if before else 0
        QMessageBox.information(self.win, APP_NAME, f"Compressed copy saved.\n\nOriginal file: {T.human(before)}\n"
                                                    f"Compressed copy: {T.human(after)}  ({pct:.0f}% smaller)"
                                if pct > 0 else f"Compressed copy saved: {T.human(after)} (the original was already "
                                                f"{T.human(before)}; nothing more to squeeze).")
        return self.last_compress

    # ---------------- 10. OCR to searchable ----------------
    def ocr(self, values=None, wait=False):
        """values: {range, lang, force}. Runs Tesseract in the background (progress + Cancel), then
        adds the text layers in one undo step. wait=True returns when it has finished (tests)."""
        from . import tess
        doc = self.ed.doc
        if doc is None or (self.ocr_worker is not None and self.ocr_worker.isRunning()):
            return None
        try:
            tess.configure()
            import pytesseract
            langs = [x for x in pytesseract.get_languages(config="") if x != "osd"] or ["eng"]
        except Exception as e:
            QMessageBox.warning(self.win, APP_NAME, f"Tesseract isn't available:\n{e}")
            return None
        if values is None:
            values = ask_ocr(self.win, langs)
            if values is None:
                return None
        try:
            pages = T.parse_range(values.get("range", ""), doc.page_count)
        except ValueError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return None
        from PySide6.QtWidgets import QProgressDialog
        sess = self.ed.cur
        prog = QProgressDialog("Reading the pages…", "Cancel", 0, len(pages), self.win)
        prog.setWindowTitle("OCR to searchable")
        prog.setWindowModality(Qt.WindowModal)        # no edits meanwhile; the window keeps painting
        prog.setMinimumDuration(0)
        prog.setValue(0)
        wk = self.ocr_worker = OCRWorker(doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE), pages,
                                         values.get("lang", "eng"), bool(values.get("force")))
        state = {"done": False}
        wk.progress.connect(lambda k, n, pno: (prog.setValue(k), prog.setLabelText(f"OCR page {pno + 1} ({k} of {n})…")))
        prog.canceled.connect(wk.cancel)

        def finished(layers, skipped, cancelled):
            prog.close()
            prog.deleteLater()
            state["done"] = True
            if cancelled:
                self.last_ocr = ([], skipped)
                self.win.statusBar().showMessage("OCR cancelled; nothing was changed.", 5000)
                return
            if self.ed.cur is not sess or not layers:
                self.last_ocr = ([], skipped)
                self.win.statusBar().showMessage(
                    f"OCR: nothing to add ({len(skipped)} page(s) already have text)." if skipped else "OCR: nothing to add.", 6000)
                return
            if self.ed.run_edit("ocr_overlay", "OCR text layer", lambda d: T.add_ocr_layers(d, layers), sorted(layers)):
                self.last_ocr = (sorted(layers), skipped)
                self.win.statusBar().showMessage(f"OCR: text layer added to {len(layers)} page(s)"
                                                 + (f"; {len(skipped)} skipped (already text)" if skipped else "")
                                                 + ". Ctrl+Z undoes it.", 8000)
        wk.done.connect(finished)
        wk.failed.connect(lambda msg: (prog.close(), prog.deleteLater(), state.update(done=True),
                                       QMessageBox.warning(self.win, APP_NAME, f"OCR failed:\n{msg}")))
        wk.start()
        if wait:
            from PySide6.QtCore import QEventLoop
            while not state["done"]:
                loop = QEventLoop()
                QTimer.singleShot(20, loop.quit)
                loop.exec()
        return wk

    # ---------------- 11. Bates numbering ----------------
    BATES_FIELDS = [("prefix", "Prefix", "text", "ABC", None), ("start", "Start number", "int", 1, (0, 999999999)),
                    ("digits", "Digits (zero padding)", "int", 6, (1, 12)), ("suffix", "Suffix", "text", "", None),
                    ("slot", "Position", "choice", "br", list(T.SLOTS.items())), ("font", "Font", "font", "helv", None),
                    ("size", "Size", "float", 9.0, (4, 72)), ("color", "Colour", "color", (0, 0, 0), None),
                    ("mx", "Side margin (pt)", "float", 36.0, (0, 300)),
                    ("my", "Top / bottom margin (pt)", "float", 20.0, (0, 300)), ("range", "Pages", "range", "all", None)]

    def bates(self, values=None):
        def make(v):
            return lambda d, tg: T.bates(d, tg, v["prefix"], v["start"], v["digits"], v["suffix"], v["slot"],
                                         v["font"], v["size"], v["color"], v["mx"], v["my"])
        return self.run_tool("Bates numbers", self.BATES_FIELDS, "stamp_text", "Bates numbers", make, values,
                             note="E.g. prefix ABC, start 1, 6 digits: ABC000001, ABC000002, …")

    def redact_hits(self):
        """Every current Ctrl+F hit becomes a redaction mark (one undo step)."""
        fc = getattr(self.win, "find", None)
        if fc is None or not fc.hits or fc.index is None:
            QMessageBox.information(self.win, APP_NAME, "Search first (Ctrl+F): every hit can then be marked "
                                                        "for redaction in one go.")
            return 0
        marks = []
        for p, a, b in fc.hits:
            for r in fc.index.page(p).rects(a, b):         # displayed coordinates
                marks.append((p, r))
        pages = sorted({p for p, _ in marks})
        fill = self.redact_fill
        if self.ed.run_edit("redact_mark", "Mark search hits for redaction",
                            lambda doc: T.mark_redactions(doc, marks, fill), pages):
            self.win.statusBar().showMessage(f"Marked {len(marks)} hit area(s) on {len(pages)} page(s) for redaction. "
                                             "Review, then Edit › Apply redactions.", 6000)
            return len(marks)
        return 0

    def mark_count(self):
        doc = self.ed.doc
        return sum(len(v) for v in T.redaction_marks(doc).values()) if doc is not None else 0

    def apply_redactions(self, confirm=True):
        doc = self.ed.doc
        if doc is None:
            return False
        marks = T.redaction_marks(doc)
        n = sum(len(v) for v in marks.values())
        if not n:
            QMessageBox.information(self.win, APP_NAME, "There are no redaction marks to apply.")
            return False
        if confirm:
            r = QMessageBox.question(self.win, APP_NAME,
                                     f"Apply {n} redaction mark(s) on {len(marks)} page(s)?\n\n"
                                     "Everything under them is removed: text, image pixels and drawings. "
                                     "Ctrl+Z can undo it in this session; a saved copy no longer contains it.",
                                     QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return False
        applied = {}

        def fn(d):
            applied.update(T.apply_redactions(d))
            return applied
        if not self.ed.run_edit("apply_redactions", "Apply redactions", fn, sorted(marks)):
            return False
        self.last_verify = T.verify_redactions(self.ed.doc, applied)
        if self.last_verify == 0:
            self.win.statusBar().showMessage(f"Applied {n} redaction(s). Verified: no text left under them.", 8000)
        else:
            QMessageBox.warning(self.win, APP_NAME, f"Applied {n} redaction(s), but {self.last_verify} character(s) "
                                                    "can still be extracted under them. Do not share this copy.")
        return True

    def remove_marks(self):
        if self.ed.doc is None or not self.mark_count():
            return 0
        n = self.mark_count()
        if self.ed.run_edit("delete_annot", "Remove redaction marks", T.remove_marks, list(range(self.ed.doc.page_count))):
            return n
        return 0


# ---------------- a settings dialog with a live preview (headers, numbers, watermark, Bates) ----------------
class ToolDialog(QDialog):
    """fields: [(key, label, kind, default, extra)]; kind: text | int | float | color | font |
    choice (extra = [(value, label)]) | range | check | file | password | lines.
    preview(values, page) -> QPixmap, or None for no preview (batch settings)."""

    def __init__(self, parent, title, fields, preview, page_count, start_page=0, note=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.preview_fn = preview
        self.w = {}
        h = QHBoxLayout(self)
        left = QVBoxLayout()
        form = QFormLayout()
        for key, label, kind, default, extra in fields:
            wd = self._make(kind, default, extra)
            self.w[key] = (kind, wd)
            form.addRow(label, wd)
        left.addLayout(form)
        if note:
            n = QLabel(note)
            n.setWordWrap(True)
            n.setObjectName("legend")
            left.addWidget(n)
        left.addStretch(1)
        self.page = QSpinBox()
        self.page.setVisible(preview is not None)
        self.page.setRange(1, max(1, page_count))
        self.page.setValue(start_page + 1)
        self.page.valueChanged.connect(self._soon)
        pr = QHBoxLayout()
        if preview is not None:
            pr.addWidget(QLabel("Preview page"))
        pr.addWidget(self.page)
        pr.addStretch(1)
        left.addLayout(pr)
        self.err = QLabel("")
        self.err.setStyleSheet("color:#ff8a80")
        left.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        left.addWidget(bb)
        h.addLayout(left, 1)
        self.view = QLabel()
        self.view.setFixedSize(360, 480)
        self.view.setAlignment(Qt.AlignCenter)
        self.view.setStyleSheet("background:#2a2e36;border:1px solid #3a4150")
        h.addWidget(self.view)
        self.view.setVisible(preview is not None)
        self._timer = QTimer(self, singleShot=True, interval=200)
        self._timer.timeout.connect(self.refresh)
        self.refresh()

    def _make(self, kind, default, extra):
        if kind in ("text", "password"):
            wd = QLineEdit(str(default))
            if kind == "password":
                from PySide6.QtWidgets import QLineEdit as _LE
                wd.setEchoMode(_LE.Password)
            wd.textChanged.connect(self._soon)
        elif kind == "lines":
            from PySide6.QtWidgets import QPlainTextEdit
            wd = QPlainTextEdit(str(default))
            wd.setPlaceholderText(extra or "")
            wd.setFixedHeight(90)
            wd.textChanged.connect(self._soon)
        elif kind in ("int", "float"):
            wd = QSpinBox() if kind == "int" else QDoubleSpinBox()
            lo, hi = extra or (0, 9999)
            wd.setRange(lo, hi)
            wd.setValue(default)
            wd.valueChanged.connect(self._soon)
        elif kind == "color":
            from .markup import ColorButton
            wd = ColorButton()
            wd.set(default)
            wd.changed.connect(self._soon)
        elif kind in ("font", "choice"):
            wd = QComboBox()
            for v, lab in (T.FONTS if kind == "font" else extra):
                wd.addItem(lab, v)
            wd.setCurrentIndex(max(0, wd.findData(default)))
            wd.currentIndexChanged.connect(self._soon)
        elif kind == "range":
            wd = QLineEdit(default)
            wd.setPlaceholderText("all, or e.g. 1-3, 5, 8-")
            wd.textChanged.connect(self._soon)
        elif kind == "check":
            wd = QCheckBox()
            wd.setChecked(bool(default))
            wd.toggled.connect(self._soon)
        elif kind == "file":
            wd = QPushButton(default or "Choose image…")
            wd.path = None

            def pick(_=False, wd=wd):
                from .markup import ask_image_file
                p = ask_image_file(self)
                if p:
                    wd.path = p
                    wd.setText(Path(p).name)
                    self._soon()
            wd.clicked.connect(pick)
        else:
            raise ValueError(kind)
        return wd

    def _soon(self, *_):
        self._timer.start()

    def values(self):
        out = {}
        for key, (kind, wd) in self.w.items():
            if kind in ("text", "range", "password"):
                out[key] = wd.text()
            elif kind == "lines":
                out[key] = wd.toPlainText()
            elif kind in ("int", "float"):
                out[key] = wd.value()
            elif kind == "color":
                out[key] = wd.color or (0, 0, 0)
            elif kind in ("font", "choice"):
                out[key] = wd.currentData()
            elif kind == "check":
                out[key] = wd.isChecked()
            elif kind == "file":
                out[key] = wd.path
        return out

    def refresh(self):
        if self.preview_fn is None:
            return
        try:
            pix = self.preview_fn(self.values(), self.page.value() - 1)
            self.err.setText("")
        except Exception as e:
            pix = None
            self.err.setText(str(e))
        if pix is not None:
            self.view.setPixmap(pix.scaled(self.view.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))


def ask_tool(parent, title, fields, preview, page_count, start_page=0, note=""):
    """-> values or None (module-level so the self-test can answer it)."""
    d = ToolDialog(parent, title, fields, preview, page_count, start_page, note)
    return d.values() if d.exec() == QDialog.Accepted else None


class MetadataDialog(QDialog):
    def __init__(self, parent, meta, has_xmp):
        super().__init__(parent)
        self.setWindowTitle("Metadata")
        self.choice = None
        v = QVBoxLayout(self)
        form = QFormLayout()
        self.edits = {}
        for key, label in T.META_FIELDS:
            e = QLineEdit(meta.get(key, "") or "")
            form.addRow(label, e)
            self.edits[key] = e
        for key, label in (("creationDate", "Created"), ("modDate", "Modified")):
            form.addRow(label, QLabel(T.pdf_date(meta.get(key, "")) or "(none)"))
        if has_xmp:
            form.addRow("", QLabel("This PDF also has XMP metadata; Remove all clears it too."))
        v.addLayout(form)
        rm = QPushButton("Remove all metadata…")
        rm.setToolTip("Clear the document information (title, author, dates, ...) and the XMP metadata")
        rm.clicked.connect(self._remove)
        v.addWidget(rm)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _ok(self):
        self.choice = ("set", {k: e.text() for k, e in self.edits.items()})
        self.accept()

    def _remove(self):
        r = QMessageBox.question(self, APP_NAME, "Remove all metadata (title, author, dates, keywords, XMP)?",
                                 QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if r == QMessageBox.Yes:
            self.choice = ("remove",)
            self.accept()


def ask_metadata(parent, meta, has_xmp):
    d = MetadataDialog(parent, meta, has_xmp)
    return d.choice if d.exec() == QDialog.Accepted else None


def ask_compress(parent):
    """-> {downsample, threshold, target, quality} or None."""
    d = QDialog(parent)
    d.setWindowTitle("Save compressed copy")
    f = QFormLayout(d)
    note = QLabel("Always: unused objects removed, every stream compressed, content cleaned.\n"
                  "Optional: shrink images that are sharper than needed.")
    note.setWordWrap(True)
    f.addRow(note)
    ds = QCheckBox("Downsample images")
    thr = QSpinBox()
    thr.setRange(50, 2400)
    thr.setValue(225)
    thr.setSuffix(" dpi")
    tgt = QSpinBox()
    tgt.setRange(36, 1200)
    tgt.setValue(150)
    tgt.setSuffix(" dpi")
    q = QSpinBox()
    q.setRange(10, 100)
    q.setValue(75)
    f.addRow(ds)
    f.addRow("Images above", thr)
    f.addRow("…are resampled to", tgt)
    f.addRow("JPEG quality", q)
    for wd in (thr, tgt, q):
        wd.setEnabled(False)
        ds.toggled.connect(wd.setEnabled)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.button(QDialogButtonBox.Ok).setText("Choose file…")
    bb.accepted.connect(d.accept)
    bb.rejected.connect(d.reject)
    f.addRow(bb)
    if d.exec() != QDialog.Accepted:
        return None
    return {"downsample": ds.isChecked(), "threshold": thr.value(), "target": tgt.value(), "quality": q.value()}


class OCRWorker(QThread):
    """Tesseract over the chosen pages of a plain copy, in the background. Tesseract runs as its own
    process (the GIL is free meanwhile); rendering each page is one short PyMuPDF call."""
    progress = Signal(int, int, int)            # pages done, of, current page
    done = Signal(object, object, bool)         # {page: text-only pdf}, [skipped pages], cancelled
    failed = Signal(str)

    def __init__(self, data, pages, lang, force):
        super().__init__()
        self.data, self.pages, self.lang, self.force = data, list(pages), lang, force
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        layers, skipped = {}, []
        doc = None
        try:
            doc = pymupdf.open("pdf", self.data)
            for k, pno in enumerate(self.pages):
                if self._cancel:
                    self.done.emit({}, skipped, True)
                    return
                self.progress.emit(k, len(self.pages), pno)
                pg = doc[pno]
                if not self.force and T.has_real_text(pg):
                    skipped.append(pno)
                    continue
                layers[pno] = T.ocr_page_pdf(pg, self.lang)
            self.progress.emit(len(self.pages), len(self.pages), self.pages[-1] if self.pages else 0)
            self.done.emit({} if self._cancel else layers, skipped, self._cancel)
        except Exception as e:                  # shown on screen only
            self.failed.emit(str(e))
        finally:
            if doc is not None:
                doc.close()


def ask_ocr(parent, langs):
    """-> {range, lang, force} or None."""
    d = QDialog(parent)
    d.setWindowTitle("OCR to searchable")
    f = QFormLayout(d)
    rng = QLineEdit("all")
    rng.setPlaceholderText("all, or e.g. 1-3, 5, 8-")
    lang = QComboBox()
    for x in langs:
        lang.addItem(x)
    force = QCheckBox("Also pages that already have text")
    f.addRow("Pages", rng)
    f.addRow("Language", lang)
    f.addRow("", force)
    note = QLabel("Adds an invisible text layer: the pages look the same, but can be searched and selected.")
    note.setWordWrap(True)
    f.addRow(note)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.button(QDialogButtonBox.Ok).setText("Start")
    bb.accepted.connect(d.accept)
    bb.rejected.connect(d.reject)
    f.addRow(bb)
    if d.exec() != QDialog.Accepted:
        return None
    return {"range": rng.text(), "lang": lang.currentText(), "force": force.isChecked()}
