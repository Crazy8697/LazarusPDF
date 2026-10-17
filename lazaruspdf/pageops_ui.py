"""Page operations UI: one controller drives the PAGES group (Overview), the Pages panel
(editor), and the Page menu from the same selection.

Selection source: in Overview it's the grid selection; in the PDF Editor it's the thumbnail
selection, or the current page if nothing is selected. Every op runs on the editor's
working copy via pageops (one undoable step each). Afterwards the editor rebuilds and
Overview refreshes with the resulting pages selected."""
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QInputDialog,
                               QMenu, QMessageBox, QRadioButton, QSpinBox, QToolButton, QVBoxLayout)

from . import APP_NAME, locked, pageops as P, protect
from .icons import icon as qicon
from .pdfeditor import ON, TOOLS

TIPS = {
    "reorder": "Move the selected pages (or drag them in Overview / the thumbnails)",
    "rotate": "Rotate the selected pages: right, left or 180°",
    "delete": "Delete the selected pages (asks first)",
    "insert": "Insert a blank page or pages from another PDF, before or after the selection",
    "duplicate": "Duplicate the selected pages (copies go right after them)",
    "extract": "Save the selected pages as a NEW PDF (the working copy is unchanged)",
    "merge": "Add all pages of other PDFs, at the end or at a chosen position",
    "split": "Split the document into new PDFs, every N pages or after the selected pages",
    "crop": "Drag a crop box on a page in the PDF Editor; apply to selected or all pages",
}


class _Cancelled(Exception):
    pass


class PageOpsController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.ov = win.overview_mode
        self._wire()
        self.ov.grid.selectionChanged.connect(self.update_enabled)
        self.ov.grid.reorderRequested.connect(self.move_pages)
        self.ed.thumbs.itemSelectionChanged.connect(self.update_enabled)
        self.ed.thumbs.reorderRequested.connect(self.move_pages)
        self.ed.pageChanged.connect(lambda _: self.update_enabled())       # the active tab's
        self.ed.pagesChanged.connect(self._after_pages_changed)
        self.ed.cropCancelled.connect(lambda: self.win.statusBar().showMessage("Crop cancelled.", 3000))
        self.act_select_all = QAction("Select all pages", win, shortcut=QKeySequence.SelectAll,
                                      triggered=self.select_all)
        win.addAction(self.act_select_all)
        self.update_enabled()

    # ---------- wiring placeholders into real actions ----------
    def _menu(self, items):
        m = QMenu(self.win)
        for label, fn in items:
            m.addAction(label, fn)
        return m

    def _wire(self):
        handlers = {
            "reorder": (self.reorder_dialog, None),
            "rotate": (lambda: self.rotate(90), [("Rotate right 90°", lambda: self.rotate(90)),
                                                  ("Rotate left 90°", lambda: self.rotate(-90)),
                                                  ("Rotate 180°", lambda: self.rotate(180))]),
            "delete": (self.delete_selected, None),
            "insert": (lambda: self.insert_blank(after=True),
                       [("Blank page before selection", lambda: self.insert_blank(after=False)),
                        ("Blank page after selection", lambda: self.insert_blank(after=True)),
                        ("Pages from another PDF, before selection…", lambda: self.insert_pdf(after=False)),
                        ("Pages from another PDF, after selection…", lambda: self.insert_pdf(after=True))]),
            "duplicate": (self.duplicate, None),
            "extract": (self.extract, None),
            "merge": (self.merge, None),
            "split": (self.split_dialog, None),
            "crop": (self.crop_start, None),
        }
        self.actions = {}
        icons = {k: icon for k, icon, *_ in TOOLS["Pages"]}
        for key, (fn, menu) in handlers.items():
            acts = [a for a in (self.ed.soon.get(key), self.ov.soon.get(key)) if a is not None]
            for a in acts:
                try:
                    a.triggered.disconnect()
                except (TypeError, RuntimeError):
                    pass
                # never pass triggered(bool checked) through: it would land in an optional
                # parameter (e.g. delete_selected(confirm=False) skipping the confirmation)
                a.triggered.connect(lambda _=False, f=fn: f())
                a.setIcon(qicon("mdi6." + icons[key], color=ON))
                a.setProperty("soon", False)
                a.setToolTip(f"<b>{a.text()}</b><br>{TIPS[key]}")
                a.setStatusTip(TIPS[key])
                if menu:
                    a.setMenu(self._menu(menu))
            self.actions[key] = acts
        # restyle the widgets that showed these as greyed placeholders
        for w in self.win.findChildren(QToolButton):
            act = w.defaultAction()
            if act is not None and any(act in acts for acts in self.actions.values()):
                w.setProperty("soon", False)
                if act.menu() is not None:
                    w.setPopupMode(QToolButton.MenuButtonPopup)
                w.style().unpolish(w)
                w.style().polish(w)

    # ---------- selection ----------
    def selection(self):
        if self.ed.doc is None:
            return []
        if self.win.mode == self.ov.key:
            return sorted(self.ov.grid.selected)
        return self.ed.selected_pages()

    def select_all(self):
        if self.win.mode == self.ov.key:
            self.ov.grid.select_all()
        elif self.win.mode == self.ed.key and self.ed.doc is not None:
            self.ed.thumbs.selectAll()

    def update_enabled(self):
        doc = self.ed.doc
        n = doc.page_count if doc is not None else 0
        k = len(self.selection())
        rules = {"reorder": 0 < k < n, "rotate": k > 0, "delete": 0 < k < n, "insert": n > 0,
                 "duplicate": k > 0, "extract": k > 0, "merge": n > 0, "split": n >= 2, "crop": n > 0}
        for key, acts in self.actions.items():
            for a in acts:
                a.setEnabled(bool(rules[key]))
        self.act_select_all.setEnabled(n > 0 and self.win.mode in (self.ov.key, self.ed.key))

    # ---------- run an op and refresh every view ----------
    def _run(self, fn, msg):
        if self.ed.work is None:
            return None
        try:
            select = fn()
        except (ValueError, RuntimeError) as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return None
        self.ed.rebuild(select)
        self.win.statusBar().showMessage(msg + " Ctrl+Z undoes it.", 5000)
        return select

    def _after_pages_changed(self, select):
        """Editor rebuilt (op or structural undo/redo): Overview follows, same selection."""
        self.ov.sync(select)
        self.update_enabled()

    def move_pages(self, pages, before):
        sel = sorted(set(pages))
        if not sel:
            return
        self._run(lambda: P.move(self.ed.work, sel, before), f"Moved {len(sel)} page(s).")

    def reorder_dialog(self):
        sel = self.selection()
        n = self.ed.doc.page_count
        v, ok = QInputDialog.getInt(self.win, "Move pages",
                                    f"Move the {len(sel)} selected page(s) to before page "
                                    f"(1–{n}; {n + 1} = after the last page):", 1, 1, n + 1)
        if ok:
            self.move_pages(sel, v - 1)

    def rotate(self, deg):
        sel = self.selection()
        if sel:
            self._run(lambda: P.rotate(self.ed.work, sel, deg), f"Rotated {len(sel)} page(s).")

    def delete_selected(self, confirm=True):
        sel = self.selection()
        n = self.ed.doc.page_count
        if not sel:
            return
        if len(sel) >= n:
            QMessageBox.warning(self.win, APP_NAME, "Can't delete every page; a PDF needs at least one.")
            return
        if confirm:
            r = QMessageBox.question(self.win, APP_NAME, f"Delete {len(sel)} page(s)? (Ctrl+Z brings them back.)",
                                     QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return
        self._run(lambda: P.delete(self.ed.work, sel), f"Deleted {len(sel)} page(s).")

    def _anchor(self, after):
        sel = self.selection()
        n = self.ed.doc.page_count
        if not sel:
            return (n, n - 1) if after else (0, 0)
        return (max(sel) + 1, max(sel)) if after else (min(sel), min(sel))

    def insert_blank(self, after=True):
        at, like = self._anchor(after)
        self._run(lambda: P.insert_blank(self.ed.work, at, like), "Inserted a blank page.")

    def _pick_pdfs(self, title, multi=False):
        start = str(self.ed.work.source.parent)
        if multi:
            paths, _ = QFileDialog.getOpenFileNames(self.win, title, start, "PDF files (*.pdf)")
            return paths
        p, _ = QFileDialog.getOpenFileName(self.win, title, start, "PDF files (*.pdf)")
        return [p] if p else []

    # ---------- other PDFs, read in the background (17) ----------
    def _read_refs(self, paths, title, fail_msg):
        """Open other PDFs for Insert / Merge (read-only, registered as protected) off the UI
        thread with loader.DocLoader: a progress dialog with Cancel, and the window keeps
        painting meanwhile (the dialog is window-modal, so the document can't change under it).
        Returns the opened documents in order, or None (cancelled, no password, or failed)."""
        from PySide6.QtCore import QEventLoop, Qt
        from PySide6.QtWidgets import QProgressDialog
        from .loader import DocLoader
        dlg = self._ref_dialog = QProgressDialog(title, "Cancel", 0, 1000, self.win)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)
        docs, err = [], None
        weight = {"read": (0.0, 0.5), "parse": (0.5, 0.0), "pages": (0.5, 0.5)}
        try:
            for k, p in enumerate(paths):
                protect.register(f"ref:{Path(p).resolve()}", p)
                res = {}

                def progress(ph, d, t, k=k):
                    if self._ref_dialog is not dlg:          # (a late signal after the dialog went)
                        return
                    a, b = weight.get(ph, (0.0, 0.0))
                    dlg.setValue(int(1000 * (k + a + (b * d / t if t else 0)) / len(paths)))

                def start(ld):
                    ld.progress.connect(progress)
                    ld.ready.connect(lambda doc, lock, rects, data: res.setdefault("doc", doc), Qt.DirectConnection)
                    ld.failed.connect(lambda msg: res.setdefault("err", msg), Qt.DirectConnection)
                    return ld

                def need_pw(data, p=p):
                    try:
                        doc, lock = locked.open_pdf(data, locked.asker(self.win, p))
                    except locked.PasswordCancelled:
                        res["cancel"] = True
                        return
                    except Exception as e:
                        res["err"] = str(e)
                        return
                    res["ld"] = start(DocLoader(str(p), self))
                    res["ld"].measure(doc, lock, data)

                ld = res["ld"] = start(DocLoader(str(p), self))
                ld.needPassword.connect(need_pw)
                ld.start()
                while not res.keys() & {"doc", "err", "cancel"}:
                    if dlg.wasCanceled():
                        res["cancel"] = True
                        res["ld"].cancel()
                        break
                    loop = QEventLoop()
                    QTimer.singleShot(15, loop.quit)
                    loop.exec()
                for l_ in {ld, res["ld"]}:
                    l_.wait()
                    l_.deleteLater()
                if "doc" in res and ("err" in res or res.get("cancel")):
                    res.pop("doc").close()
                if "doc" in res:
                    docs.append(res["doc"])
                    continue
                err = res.get("err")
                break
            else:
                return docs
        finally:
            # deleted, not just closed: a QProgressDialog that never showed still has its
            # "show after minimumDuration" timer running and pops up (window-modal) later
            self._ref_dialog = None
            dlg.close()
            dlg.deleteLater()
        for d in docs:
            d.close()
        if err is not None:
            QMessageBox.warning(self.win, APP_NAME, f"{fail_msg}\n{err}")
        else:
            self.win.statusBar().showMessage("Cancelled; nothing was added.", 4000)
        return None

    def _grafting(self, op, msg, title):
        """Run a big Insert / Merge in parts (one undo step): a progress dialog with Cancel shows
        if it takes a moment, and the window keeps painting between the parts. Cancel rolls back
        everything (nothing is added)."""
        from PySide6.QtCore import QCoreApplication, Qt
        from PySide6.QtWidgets import QProgressDialog
        dlg = self._graft_dialog = QProgressDialog(title, "Cancel", 0, 1000, self.win)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setValue(0)

        sel = self.selection()

        def between(progress):
            done, total = progress
            dlg.setValue(int(1000 * done / max(1, total)))
            QCoreApplication.processEvents()
            if dlg.wasCanceled():
                raise _Cancelled()
        try:
            return self._run(lambda: op(between), msg)
        except _Cancelled:
            self.ed.rebuild(sel or None)       # (rolled back; views drop anything painted meanwhile)
            self.win.statusBar().showMessage("Cancelled; nothing was added.", 4000)
            return None
        finally:
            self._graft_dialog = None
            dlg.close()
            dlg.deleteLater()                   # (see _read_refs)

    def insert_pdf(self, after=True, path=None):
        paths = [path] if path else self._pick_pdfs("Insert pages from PDF (read-only)")
        if not paths:
            return
        at, _ = self._anchor(after)
        got = self._read_refs(paths[:1], "Reading the PDF to insert…", "Couldn't open that PDF:")
        if got is None:
            return
        src = got[0]
        try:
            self._grafting(lambda between: P.insert_from(self.ed.work, src, at, between=between),
                           f"Inserted {src.page_count} page(s).", "Inserting pages…")
        finally:
            src.close()

    def duplicate(self):
        sel = self.selection()
        if sel:
            self._run(lambda: P.duplicate(self.ed.work, sel), f"Duplicated {len(sel)} page(s).")

    def merge(self, paths=None, where=None):
        paths = paths or self._pick_pdfs("Merge PDFs (read-only)", multi=True)
        if not paths:
            return
        n = self.ed.doc.page_count
        choices = ["At the end", "Before the first selected page", "After the last selected page"]
        if where is None:
            where, ok = QInputDialog.getItem(self.win, "Merge PDFs", "Where should the pages go?", choices, 0, False)
            if not ok:
                return
        sel = self.selection()
        at = {choices[0]: n, choices[1]: min(sel) if sel else 0, choices[2]: max(sel) + 1 if sel else n}[where]
        srcs = self._read_refs(paths, f"Reading {len(paths)} PDF(s) to merge…", "Couldn't open a PDF to merge:")
        if srcs is None:
            return
        try:
            total = sum(s.page_count for s in srcs)
            names = [Path(p).stem for p in paths]
            self._grafting(lambda between: P.merge(self.ed.work, srcs, at, names, between=between),
                           f"Merged {len(srcs)} PDF(s), {total} page(s).", "Merging pages…")
        finally:
            for s in srcs:
                s.close()

    # ---------- new files: extract / split (never originals) ----------
    def _pick_new_pdf(self, title, suggested):
        start = str(suggested)
        while True:
            path, _ = QFileDialog.getSaveFileName(self.win, title, start, "PDF files (*.pdf)")
            if not path:
                return None
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            if protect.is_protected(path) or self.ed.work.is_source(path):
                QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
                continue
            return path

    def extract(self, dest=None):
        sel = self.selection()
        if not sel:
            return False
        src = self.ed.work.source
        label = f"{sel[0] + 1}" if len(sel) == 1 else f"{sel[0] + 1}-{sel[-1] + 1}"
        dest = dest or self._pick_new_pdf("Extract pages to a new PDF",
                                          src.with_name(f"{src.stem}-pages-{label}.pdf"))
        if not dest:
            return False
        try:
            P.extract(self.ed.work, sel, dest)
        except Exception as e:
            QMessageBox.critical(self.win, APP_NAME, f"Extract failed:\n{e}")
            return False
        self.win.statusBar().showMessage(f"Extracted {len(sel)} page(s) to a new PDF.", 5000)
        return True

    def split_dialog(self):
        n = self.ed.doc.page_count
        sel = self.selection()
        dlg = QDialog(self.win)
        dlg.setWindowTitle("Split into new PDFs")
        v = QVBoxLayout(dlg)
        f = QFormLayout()
        every = QRadioButton("Every N pages")
        every.setChecked(True)
        nspin = QSpinBox()
        nspin.setRange(1, max(1, n - 1))
        nspin.setValue(min(10, max(1, n // 2)))
        after = QRadioButton(f"After each selected page ({len(sel)} selected)")
        after.setEnabled(bool([p for p in sel if p < n - 1]))
        f.addRow(every, nspin)
        f.addRow(after)
        v.addLayout(f)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        ranges = P.split_ranges(n, every=nspin.value()) if every.isChecked() else P.split_ranges(n, after=sel)
        self.split(ranges)

    def split(self, ranges, folder=None):
        src = self.ed.work.source
        if folder is None:
            folder = QFileDialog.getExistingDirectory(self.win, "Folder for the split PDFs", str(src.parent))
            if not folder:
                return False
        dests = [Path(folder) / f"{src.stem}-part-{i + 1:02d}.pdf" for i in range(len(ranges))]
        if any(protect.is_protected(d) or self.ed.work.is_source(d) for d in dests):
            QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
            return False
        existing = [d for d in dests if d.exists()]
        if existing:
            r = QMessageBox.question(self.win, APP_NAME,
                                     f"{len(existing)} of the {len(dests)} part files already exist. Overwrite them?",
                                     QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return False
        try:
            P.split(self.ed.work, ranges, dests)
        except Exception as e:
            QMessageBox.critical(self.win, APP_NAME, f"Split failed:\n{e}")
            return False
        self.win.statusBar().showMessage(f"Split into {len(dests)} new PDFs.", 5000)
        return True

    # ---------- crop ----------
    def crop_start(self):
        if self.ed.doc is None:
            return
        self._crop_pages = self.selection()
        if self.win.mode != self.ed.key:
            self.win.set_mode(self.ed.key)
            if self._crop_pages:
                self.ed.canvas.goto(self._crop_pages[0])
        self.ed.canvas.start_crop(self._crop_drawn)
        self.win.statusBar().showMessage("Crop: drag a box on a page (Esc cancels). Crop hides content outside "
                                         "the box; nothing is deleted.", 8000)

    def _crop_drawn(self, page, rect, scope=None, mode=None):
        sel = [p for p in (getattr(self, "_crop_pages", None) or self.ed.selected_pages())]
        n = self.ed.doc.page_count
        asked = scope is None                        # the interactive path (a programmatic scope: same box)
        if scope is None:
            box = QMessageBox(self.win)
            box.setWindowTitle(APP_NAME)
            box.setText("Apply this crop box to which pages?\n\nCrop hides content outside the box; "
                        "nothing is deleted, and Ctrl+Z undoes it.")
            b_sel = box.addButton(f"Selected pages ({len(sel)})", QMessageBox.AcceptRole)
            b_this = box.addButton(f"This page only (page {page + 1})", QMessageBox.AcceptRole)
            b_all = box.addButton(f"All pages ({n})", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            clicked = box.clickedButton()
            scope = {b_sel: "selected", b_this: "this", b_all: "all"}.get(clicked)
            if scope is None:
                return
        pages = {"selected": sel or [page], "this": [page], "all": list(range(n))}[scope]
        sizes = {(round(self.ed.doc[p].rect.width), round(self.ed.doc[p].rect.height)) for p in pages}
        if mode is None and asked and len(sizes) > 1:   # pages of mixed sizes: how should the box scale? (17)
            box = QMessageBox(self.win)
            box.setWindowTitle(APP_NAME)
            box.setText("These pages are not all the same size. Apply the crop as:")
            box.setInformativeText("Same margins: every page loses the same distance from each edge.\n"
                                   "Same proportion: every page keeps the same share of its width and height.\n"
                                   "Same box: the drawn box as-is on every page.")
            b_m = box.addButton("Same margins", QMessageBox.AcceptRole)
            b_p = box.addButton("Same proportion", QMessageBox.AcceptRole)
            b_b = box.addButton("Same box", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            mode = {b_m: "margins", b_p: "proportion", b_b: "box"}.get(box.clickedButton())
            if mode is None:
                return
        mode = mode or "box"
        self._run(lambda: P.crop(self.ed.work, pages, rect, page, mode), f"Cropped {len(pages)} page(s).")
