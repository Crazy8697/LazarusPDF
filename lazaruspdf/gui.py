"""Lazarus PDF: one main window with two modes (PDF Editor, Extraction) + the Import pop-up."""
import os
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QKeySequence, QPalette
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QStackedWidget, QStyle,
                               QStyleOptionToolBar,
                               QToolBar, QVBoxLayout, QWidget)

from . import APP_NAME, __version__, locked, modes, tess
from . import settings as S
from .extract import Cancelled
from .layoutmgr import LayoutManager
from .overview import OverviewMode
from .pdfeditor import PdfEditorMode
from .textmode import ExtractionMode

DEFAULT_OUT = str(Path.home() / "extract-out")
ICON = Path(__file__).resolve().parent / "assets" / "lazaruspdf.ico"


def local_pdf(mime):
    """First local file in a drag payload, if it is a PDF."""
    urls = [u for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []
    p = urls[0].toLocalFile() if urls else ""
    return p if p.lower().endswith(".pdf") else ""


def local_pdfs(mime):
    """Every local PDF in a drag payload."""
    urls = [u for u in mime.urls() if u.isLocalFile()] if mime.hasUrls() else []
    return [u.toLocalFile() for u in urls if u.toLocalFile().lower().endswith(".pdf")]


class Worker(QThread):
    progressed = Signal(int, int, str, int, int)
    finished_ok = Signal(object, dict, int, int)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, mode, src, out, password=None):
        super().__init__()
        self.mode, self.src, self.out = mode, src, out
        self.password = password            # memory only, for a password-protected PDF
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            pages, paths, (gaps, figs) = modes.run(self.mode, self.src, self.out,
                                                   lambda *a: self.progressed.emit(*a),
                                                   lambda: self._stop, self.password)
            self.finished_ok.emit(pages, {k: str(v) for k, v in paths.items()}, gaps, figs)
        except Cancelled:
            self.cancelled.emit()
        except Exception as e:  # shown on screen only, never written anywhere
            self.failed.emit(f"{e}\n\n{traceback.format_exc()}")


class DropZone(QLabel):
    def __init__(self):
        super().__init__("Drop a PDF here")
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(80)
        self.setObjectName("drop")


class ImportDialog(QDialog):
    """Pick a PDF, run extraction. Emits imported(pages, paths) and closes itself when done."""
    imported = Signal(object, dict)

    def __init__(self, parent, settings):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — Import PDF")
        self.setModal(True)
        self.setAcceptDrops(True)
        self.resize(600, 380)
        self.settings = settings
        self.worker = None
        self.results = None
        self.last_pages = None

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)
        v.addWidget(QLabel(f"<b style='font-size:16px'>Import PDF</b> "
                           f"<span style='color:#8a94a6'>local only · v{__version__}</span>"))
        self.drop = DropZone()
        v.addWidget(self.drop)

        g = QGridLayout()
        g.setHorizontalSpacing(8)
        self.pdf_edit = QLineEdit()
        self.pdf_edit.setPlaceholderText("No PDF chosen")
        self.pdf_edit.setReadOnly(True)
        pick = QPushButton("Choose PDF…")
        pick.clicked.connect(self.choose_pdf)
        self.out_edit = QLineEdit(S.get(self.settings, "out_dir"))
        out_btn = QPushButton("Choose…")
        out_btn.clicked.connect(self.choose_out)
        # QLineEdit accepts text drops by default and would swallow the file drag
        self.pdf_edit.setAcceptDrops(False)
        self.out_edit.setAcceptDrops(False)
        g.addWidget(QLabel("PDF"), 0, 0)
        g.addWidget(self.pdf_edit, 0, 1)
        g.addWidget(pick, 0, 2)
        g.addWidget(QLabel("Output"), 1, 0)
        g.addWidget(self.out_edit, 1, 1)
        g.addWidget(out_btn, 1, 2)
        v.addLayout(g)

        row = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("start")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop)
        row.addWidget(self.start_btn)
        row.addWidget(self.stop_btn)
        row.addStretch(1)
        v.addLayout(row)

        self.bar = QProgressBar()
        v.addWidget(self.bar)
        stat = QHBoxLayout()
        self.status = QLabel("Ready.")
        self.gaps = QLabel("Gaps: 0")
        self.gaps.setObjectName("gaps")
        self.figs = QLabel("Figures: 0")
        stat.addWidget(self.status, 1)
        stat.addWidget(self.figs)
        stat.addSpacing(16)
        stat.addWidget(self.gaps)
        v.addLayout(stat)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        v.addWidget(line)
        res = QHBoxLayout()
        self.open_btn = QPushButton("Open results")
        self.open_btn.setToolTip("Load the last import into the editor")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self.open_results)
        self.folder_btn = QPushButton("Show folder")
        self.folder_btn.setEnabled(False)
        self.folder_btn.clicked.connect(self.show_folder)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        res.addWidget(self.open_btn)
        res.addWidget(self.folder_btn)
        res.addStretch(1)
        res.addWidget(self.close_btn)
        v.addLayout(res)

        try:
            _, ver = tess.configure()
            self.status.setText(f"Ready. Tesseract {ver.split()[0]}.")
        except Exception as e:
            self.status.setText(str(e))

    # --- file picking / drag-drop ---
    def busy(self):
        return self.worker is not None and self.worker.isRunning()

    def set_pdf(self, path):
        self.pdf_edit.setText(path)
        self.drop.setText("PDF loaded — press Start")
        self.bar.setValue(0)
        self.gaps.setText("Gaps: 0")
        self.figs.setText("Figures: 0")
        self.status.setText("Ready.")

    def load_pdf(self, p):
        """Single entry point for the Choose PDF button and drag-and-drop."""
        if not p or not p.lower().endswith(".pdf") or not Path(p).is_file() or self.busy():
            return False
        p = os.path.normpath(p)
        self.settings.setValue("last_dir", str(Path(p).parent))
        self.set_pdf(p)
        return True

    def choose_pdf(self):
        start = self.settings.value("last_dir", str(Path.home()))
        p, _ = QFileDialog.getOpenFileName(self, "Choose PDF", start, "PDF files (*.pdf)")
        self.load_pdf(p)

    def choose_out(self):
        p = QFileDialog.getExistingDirectory(self, "Output folder", self.out_edit.text())
        if p:
            self.out_edit.setText(os.path.normpath(p))

    def _drag_ok(self, e):
        if e.mimeData().hasUrls() and not self.busy():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragEnterEvent(self, e):
        self._drag_ok(e)

    def dragMoveEvent(self, e):
        self._drag_ok(e)

    def dropEvent(self, e):
        if self.load_pdf(local_pdf(e.mimeData())):   # non-PDFs are ignored quietly
            e.acceptProposedAction()
        else:
            e.ignore()

    # --- run ---
    def start(self):
        src = self.pdf_edit.text()
        if not src or not Path(src).is_file():
            QMessageBox.warning(self, APP_NAME, "Choose a PDF first.")
            return
        try:                       # a protected PDF: ask now, on the UI thread, before the run
            doc, lock = locked.open_pdf(src, locked.asker(self, src))
        except locked.PasswordCancelled:
            self.status.setText("Not started: no password given.")
            return
        except Exception as e:
            QMessageBox.warning(self, APP_NAME, f"Could not open the PDF:\n{e}")
            return
        doc.close()
        password = (lock.user_pw or None) if lock else None
        out = self.out_edit.text().strip() or DEFAULT_OUT
        self.settings.setValue("out_dir", out)
        self.last_password = password        # memory only: Searchable / Rebuilt need the original again
        self.worker = Worker(modes.EXTRACT, src, out, password)
        self.worker.progressed.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_done)
        self.worker.failed.connect(self.on_fail)
        self.worker.cancelled.connect(self.on_cancel)
        for b in (self.start_btn, self.open_btn, self.folder_btn, self.close_btn):
            b.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("Opening…")
        self.worker.start()

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.status.setText("Stopping after this page…")

    def _idle(self):
        self.start_btn.setEnabled(True)
        self.close_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def on_progress(self, done, total, status, gaps, figs):
        self.bar.setMaximum(max(total, 1))
        self.bar.setValue(done)
        self.status.setText(status)
        self.gaps.setText(f"Gaps: {gaps}")
        self.figs.setText(f"Figures: {figs}")

    def on_done(self, pages, paths, gaps, figs):
        self._idle()
        self.results, self.last_pages = paths, pages
        self.last_src = self.worker.src
        self.bar.setValue(self.bar.maximum())
        self.gaps.setText(f"Gaps: {gaps}")
        self.figs.setText(f"Figures: {figs}")
        self.status.setText("Done — loaded into the editor.")
        self.open_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)
        self.imported.emit(pages, paths)
        self.accept()     # auto-close; the editor behind now has the content

    def on_fail(self, msg):
        self._idle()
        self.status.setText("Failed.")
        QMessageBox.critical(self, APP_NAME, msg)

    def on_cancel(self):
        self._idle()
        self.status.setText("Stopped. No output written.")

    def open_results(self):
        if self.last_pages is not None:
            self.imported.emit(self.last_pages, self.results)
            self.accept()

    def show_folder(self):
        if self.results:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(self.results["review"])])

    def reject(self):
        if self.busy():
            return       # use Stop first
        super().reject()

    def closeEvent(self, e):
        if self.busy():
            e.ignore()
        else:
            super().closeEvent(e)


class Main(QMainWindow):
    """The one Lazarus PDF window. Central area = a stack holding one widget per mode.
    Switching modes swaps the central widget and shows/hides each mode's toolbars,
    docks, menus and File/View entries (and disables the hidden mode's shortcuts)."""

    def __init__(self):
        super().__init__()
        if ICON.exists():
            self.setWindowIcon(QIcon(str(ICON)))
        self.setAcceptDrops(True)
        self.resize(1280, 860)
        self.settings = S.store()            # QSettings("LazarusPDF", "LazarusPDF"), or an INI in tests
        S.purge_retired(self.settings)
        self.mode = None

        # shared actions (work from either mode)
        self.act_import = QAction("Import PDF…", self, shortcut="Ctrl+I", triggered=self.open_import)
        self.act_import.setToolTip("Import a PDF for text extraction (opens the import pop-up)")
        self.act_quit = QAction("Quit", self, shortcut=QKeySequence.Quit, triggered=self.close)

        # mode switcher: always visible, first on the top row
        self.mode_bar = QToolBar("Mode", self)
        self.mode_bar.setObjectName("modebar")
        self.mode_bar.setMovable(False)
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_actions = {}

        # the three modes (switcher order: Overview | PDF Editor | Extraction)
        self.importer = ImportDialog(self, self.settings)
        self.importer.imported.connect(self.on_imported)
        self.editor_mode = PdfEditorMode(self)
        from .batch_ui import BatchController
        self.batch = BatchController(self)      # File › Batch…: jobs run as tabs next to the PDFs
        from .images_ui import ImagesController
        self.images = ImagesController(self)    # File › New PDF from Images…, Insert › Images as Pages… (14)
        from .scan_ui import ScanController
        self.scanner = ScanController(self)     # File › Scan…, Insert › Scan as Pages… (15, WIA)
        from .todocx_ui import WordController
        self.word = WordController(self)        # File › Export › Word document (.docx)… (16)
        from .docexport_ui import ExportController
        self.exporter = ExportController(self)  # File › Export › PDF / EPUB / Markdown / HTML / text (18b)
        self.overview_mode = OverviewMode(self, self.editor_mode)
        self.text_mode = ExtractionMode(self, self.settings)
        self.modes = {m.key: m for m in (self.overview_mode, self.editor_mode, self.text_mode)}

        self.stack = QStackedWidget()
        for m in self.modes.values():
            self.stack.addWidget(m.widget)
        # the PDF tabs sit above every mode's area (shown in the PDF Editor and Overview)
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self.editor_mode.tabbar)
        cv.addWidget(self.stack, 1)
        from .update_ui import UpdateController
        self.updates = UpdateController(self, cv)   # 19b: the update bar sits above the tabs
        self.setCentralWidget(central)

        lab = QLabel(" MODE ")
        lab.setObjectName("grouplabel")
        self.mode_bar.addWidget(lab)
        for i, m in enumerate((self.overview_mode, self.editor_mode, self.text_mode), 1):
            a = QAction(m.title, self, checkable=True, shortcut=f"Ctrl+Shift+{i}",
                        triggered=lambda _=False, k=m.key: self.set_mode(k))
            a.setToolTip(f"Switch to {m.title} mode (Ctrl+Shift+{i})")
            self.mode_group.addAction(a)
            self.mode_bar.addAction(a)
            self.mode_bar.widgetForAction(a).setObjectName("modebtn")
            self.mode_actions[m.key] = a

        # toolbars: placed and re-packed by the layout manager (rows, sides, compact form, lock)
        self.layout_mgr = LayoutManager(
            self,
            row1_fixed=[self.mode_bar, self.overview_mode.top_bar, self.editor_mode.top_bar,
                        self.text_mode.top_bar],
            groups_by_mode={"editor": self.editor_mode.groups, "overview": self.overview_mode.groups},
            docks=self.editor_mode.docks)
        self.layout_mgr.repack("editor")

        # menus: File, Mode, View, then each mode's own menus
        self.act_settings = QAction("Settings…", self, shortcut="Ctrl+,", triggered=self.open_settings)
        self.act_settings.setToolTip("Output folder, save behaviour, gap colours, default zoom")
        self.act_about = QAction(f"About {APP_NAME}", self, triggered=lambda _=False: self.show_about())
        self.act_licenses = QAction("Licenses…", self, triggered=lambda _=False: self.show_licenses())
        self.act_licenses.setToolTip("Lazarus PDF's license (AGPL-3.0) and the third-party notices")
        self.act_support = QAction("Support DarkRelay", self, triggered=lambda _=False: self.support())
        self.act_support.setToolTip("Opens DarkRelay's Buy Me a Coffee page in your browser")
        fm = self.menuBar().addMenu("File")
        self.file_menu = fm
        fm.addAction(self.editor_mode.act_open)
        from .recent import RecentMenu
        self.recent = RecentMenu(self)          # File › Open Recent (18c): a pick list, nothing automatic
        fm.addMenu(self.recent.menu)
        fm.addAction(self.act_import)
        fm.addAction(self.batch.act_batch)
        fm.addAction(self.images.act_new)
        fm.addAction(self.scanner.act_scan)
        fm.addSeparator()
        for m in self.modes.values():
            for a in m.file_entries:
                fm.addAction(a)
        fm.addSeparator()
        fm.addAction(self.act_settings)
        fm.addAction(self.act_about)
        fm.addSeparator()
        fm.addAction(self.act_quit)
        mm = self.menuBar().addMenu("Mode")
        for a in self.mode_actions.values():
            mm.addAction(a)
        vm = self.menuBar().addMenu("View")
        self.view_menu = vm
        for a in self.editor_mode.view_entries + [None] + self.overview_mode.view_entries:
            if a is None:
                vm.addSeparator()
            else:
                vm.addAction(a)
        vm.addSeparator()
        self.group_toggles = {}             # mode key -> that mode's toolbar toggles
        for mode_key, groups in (("editor", self.editor_mode.groups), ("overview", self.overview_mode.groups)):
            self.group_toggles[mode_key] = []
            for g in groups:
                a = g.toggleViewAction()
                a.setText(f"{g.windowTitle()} toolbar")
                # triggered = user clicks only (toggled also fires when repack hides/re-adds bars)
                a.triggered.connect(lambda _: QTimer.singleShot(0, self._regroup))
                vm.addAction(a)
                self.group_toggles[mode_key].append(a)
        vm.addSeparator()
        self.panel_toggles = []
        for d in self.editor_mode.docks:
            a = d.toggleViewAction()
            a.setText(f"{d.windowTitle()} panel")
            vm.addAction(a)
            self.panel_toggles.append(a)
        vm.addSeparator()
        self.act_lock = QAction("Lock layout", self, checkable=True)
        self.act_lock.setToolTip("Freeze all toolbars and panels in place")
        self.act_lock.toggled.connect(self.lock_layout)
        vm.addAction(self.act_lock)
        for m in self.modes.values():
            for menu in m.menus:
                self.menuBar().addMenu(menu)
        hm = self.menuBar().addMenu("Help")
        hm.addAction(self.updates.act_check)
        hm.addAction(self.act_licenses)
        hm.addAction(self.act_support)
        hm.addSeparator()
        hm.addAction(self.act_about)

        # undo/redo shortcuts live on the window, so they work in Overview (Edit menu is editor-only)
        self.addAction(self.editor_mode.act_undo)
        self.addAction(self.editor_mode.act_redo)
        self.addAction(self.editor_mode.bookmarks.act_add)     # Ctrl+B in the editor and Overview
        from .pageops_ui import PageOpsController
        self.pageops = PageOpsController(self)     # page ops: Overview PAGES group, Pages panel, Page menu
        from .pageclip_ui import PageClipController
        self.pageclip = PageClipController(self)   # pages between tabs: copy / cut / paste / drag (17)
        from .searchbar import FindController
        self.find = FindController(self)            # Ctrl+F (read-only)
        from .textedit_ui import ReplaceController
        self.replace = ReplaceController(self)      # Ctrl+H: Replace / Replace All (stage 11)
        from .compare_ui import CompareController
        self.compare_ctl = CompareController(self)  # the diff on the compare view (stage 13)
        em_edit = self.editor_mode.menus[2]
        first = em_edit.actions()[0] if em_edit.actions() else None
        for a in (self.find.act_find, self.find.act_next, self.find.act_prev, self.replace.act_replace):
            em_edit.insertAction(first, a)
        em_edit.insertSeparator(first)
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.find.act_find)
        self.set_mode(self.editor_mode.key)    # the editor is the face of the app
        self.apply_settings()
        self.statusBar().clearMessage()

    # ---------- modes ----------
    def set_mode(self, key):
        if key == self.mode:
            return
        self.statusBar().clearMessage()         # no stale messages from the other mode
        for k, m in self.modes.items():         # turn the old one off first so shortcuts never clash
            if k == key:
                continue
            if m is self.editor_mode:
                m.set_active(False, keep_docks=(key == self.overview_mode.key))   # Overview shares the panels
            else:
                m.set_active(False)
        self.modes[key].set_active(True)
        self.layout_mgr.repack(key)
        for mode_key, toggles in self.group_toggles.items():
            for a in toggles:
                a.setVisible(key == mode_key)
        for a in self.panel_toggles:
            a.setVisible(key in (self.editor_mode.key, self.overview_mode.key))
        # the Page menu works on Overview's selection too
        self.editor_mode.menus[0].menuAction().setVisible(key in (self.editor_mode.key, self.overview_mode.key))
        self.stack.setCurrentWidget(self.modes[key].widget)
        self.mode_actions[key].setChecked(True)
        self.mode = key
        self.editor_mode.sync_tabbar()          # tabs: PDF Editor and Overview
        if hasattr(self, "pageops"):
            self.pageops.update_enabled()       # after self.mode: the selection source depends on it
        self.editor_mode._apply_enabled()       # undo/redo are live in Overview too
        self.editor_mode.bookmarks.update_enabled()
        if hasattr(self, "find"):
            self.find.update_enabled()
        self.update_title()

    def update_title(self):
        t = f"{APP_NAME} — {self.modes[self.mode].title}" if self.mode else APP_NAME
        if self.editor_mode.any_dirty():
            t += " • unsaved PDF edits"
        if self.text_mode.is_dirty():
            t += " • unsaved extraction edits"
        self.setWindowTitle(t)

    # ---------- layout ----------
    def _regroup(self):
        self.layout_mgr.repack()

    def lock_layout(self, on):
        self.layout_mgr.set_locked(on)
        self.statusBar().showMessage("Layout locked." if on else "Layout unlocked.", 3000)

    # ---------- File › Settings / About ----------
    def open_settings(self):
        from .settings_dialog import SettingsDialog
        dlg = SettingsDialog(self, self.settings)
        dlg.applied.connect(self.apply_settings)
        dlg.exec()

    def apply_settings(self):
        """Push stored settings into the live UI (called at start-up and after OK / Apply)."""
        s = self.settings
        from . import memguard
        memguard.BUDGET.set_percent(S.get(s, "undo_ram_pct"))
        if not self.importer.busy():
            self.importer.out_edit.setText(S.get(s, "out_dir"))
        self.text_mode.apply_settings()
        self.statusBar().showMessage("Settings saved.", 3000)

    def show_about(self, show=True):
        from .about_ui import AboutDialog
        dlg = AboutDialog(self, licenses=self.show_licenses, support=self.support)
        self._about = dlg
        if show:
            dlg.exec()
        return dlg

    def show_licenses(self, show=True):
        from .about_ui import LicensesDialog
        dlg = LicensesDialog(self)
        self._licenses = dlg
        if show:
            dlg.exec()
        return dlg

    def support(self):
        from .about_ui import open_support
        return open_support()

    # ---------- import (extraction) ----------
    def open_import(self, pdf=None):
        if not self.importer.busy():
            self.importer.out_edit.setText(S.get(self.settings, "out_dir"))
        if pdf:
            self.importer.load_pdf(pdf)
        self.importer.show()
        self.importer.raise_()
        self.importer.activateWindow()

    def on_imported(self, pages, paths):
        if self.text_mode.load(pages, paths):   # switches to Extraction mode
            src = getattr(self.importer, "last_src", None)
            self.text_mode.source = src                       # Searchable / Rebuilt read it again (21)
            self.text_mode.source_password = getattr(self.importer, "last_password", None)
            if src:
                from . import protect
                protect.register("extract", src)   # exports and review saves may never write to it

    # ---------- drag-drop: editor mode opens it visually, extraction mode imports it ----------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and not self.importer.busy():
            e.acceptProposedAction()
        else:
            e.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e):
        p = local_pdf(e.mimeData())
        if not p:
            from .images import is_image
            urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()] if e.mimeData().hasUrls() else []
            imgs = [u for u in urls if is_image(u) or os.path.isdir(u)]
            if imgs and any(is_image(u) for u in urls):           # images, no PDF: make a PDF of them
                e.acceptProposedAction()
                QTimer.singleShot(0, lambda: self.images.dropped([os.path.normpath(u) for u in imgs]))
                return
            e.ignore()
            return
        e.acceptProposedAction()
        if self.mode == self.editor_mode.key:
            for q in local_pdfs(e.mimeData()):        # each PDF dropped loads in its own tab
                self.editor_mode.open_pdf(os.path.normpath(q), wait=False)
        else:
            self.open_import(p)

    def closeEvent(self, e):
        if getattr(self, "_update_exit", False):        # closing for an update: already asked (19b)
            self.editor_mode.close_doc()
            e.accept()
            return
        if self.find.building() or self.find.locked:
            self.find.cancel()            # an index build: stop it (and unlock) before the prompts
        if self.importer.busy():
            self.importer.worker.stop()
            self.importer.worker.wait(30000)
        if not self.text_mode.confirm_discard() or not self.editor_mode.confirm_all():
            e.ignore()
            return
        self.editor_mode.close_doc()
        e.accept()


STYLE = """
QWidget{font-family:'Segoe UI';font-size:13px}
#drop{border:2px dashed #3a4150;border-radius:10px;color:#8a94a6;font-size:14px}
QPushButton,QToolButton{padding:5px 12px;border:1px solid #3a4150;border-radius:6px;background:#252a33}
QPushButton:hover,QToolButton:hover{background:#2e3440}
QPushButton:disabled,QToolButton:disabled{color:#5c6370;background:#1f232a}
QPushButton#start{background:#2d6cdf;border-color:#2d6cdf;color:white;font-weight:600}
QPushButton#start:disabled{background:#28344a;color:#7c8799}
QLineEdit,QComboBox{padding:5px;border:1px solid #3a4150;border-radius:5px;background:#1b1f26}
QTextEdit{background:#1b1f26;border:none;selection-background-color:#2d6cdf}
QToolBar{background:#16181d;border-bottom:1px solid #2c313a;spacing:6px;padding:4px}
QMenuBar{background:#16181d}
QMenuBar::item:selected,QMenu::item:selected{background:#2d6cdf}
QMenu{background:#1e2128;border:1px solid #3a4150}
QProgressBar{border:1px solid #3a4150;border-radius:5px;text-align:center;height:18px;background:#1b1f26}
QProgressBar::chunk{background:#2d6cdf;border-radius:4px}
#gaps{background:#ffeb3b;color:#000;font-weight:700;padding:2px 8px;border-radius:4px}
#filled{background:#24452c;color:#a5d6a7;font-weight:600;padding:2px 8px;border-radius:4px}
QToolButton#modebtn{padding:4px 10px;border:1px solid #3a4150;background:#1b1f26;color:#9aa0a6;font-weight:600}
QToolButton#modebtn:checked{background:#2d6cdf;border-color:#2d6cdf;color:#ffffff}
QLabel#drophint{background:rgba(45,108,223,0.18);border:2px dashed #2d6cdf;border-radius:6px;color:#9fc0ff;font-weight:600}
QLabel#modelabel{background:#2d6cdf;color:#fff;font-weight:600;padding:1px 8px;border-radius:3px}
"""


def _dark(app):
    app.setStyle("Fusion")
    p = QPalette()
    for role, c in [(QPalette.Window, "#16181d"), (QPalette.WindowText, "#e6e6e6"),
                    (QPalette.Base, "#1b1f26"), (QPalette.AlternateBase, "#22262e"),
                    (QPalette.Text, "#e6e6e6"), (QPalette.Button, "#252a33"),
                    (QPalette.ButtonText, "#e6e6e6"), (QPalette.Highlight, "#2d6cdf"),
                    (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, "#252a33"),
                    (QPalette.ToolTipText, "#e6e6e6"), (QPalette.PlaceholderText, "#6b7280")]:
        p.setColor(role, QColor(c))
    p.setColor(QPalette.Disabled, QPalette.Text, QColor("#5c6370"))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#5c6370"))
    app.setPalette(p)
    from .pdfeditor import EDITOR_STYLE
    app.setStyleSheet(STYLE + EDITOR_STYLE)


def run(paths=None):
    from . import startup
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LazarusPDF.App")
        except Exception:
            pass
    app = QApplication(sys.argv)
    from . import APP_ID, migrate
    app.setApplicationName(APP_ID)          # identifier: %APPDATA%\LazarusPDF (saved signatures)
    app.setApplicationDisplayName(APP_NAME)
    migrate.run()                           # stage 22: a Lazarus 0.21.0 user's data, once
    # single instance: this one listens for later launches (double-clicked PDFs open as tabs here)
    from .instance import InstanceServer, OpenQueue, claim, pdf_args, wait_for_instance
    paths = pdf_args(sys.argv[1:]) if paths is None else paths
    server = None
    if claim():                               # this process is the instance: listen for later launches
        server = InstanceServer()
        if not server.listen():
            server = None
    elif wait_for_instance(paths):            # another launch is the instance (starting a moment ago)
        os._exit(0)                           # (see __main__: nothing to clean up, never hang)
    if ICON.exists():
        app.setWindowIcon(QIcon(str(ICON)))
    _dark(app)
    w = Main()
    w.show()                              # starts clean in PDF Editor mode; Import is on demand
    w.updates.startup()                   # 19b: a delayed background check, if Settings allow it
    startup.mark("window_shown")
    w.open_queue = OpenQueue(w)
    w.instance_server = server
    if server is not None:
        server.openRequested.connect(w.open_queue.add)
    for a in paths:                       # PDFs given on the command line open as tabs
        w.editor_mode.open_pdf(os.path.normpath(a), wait=False)
    return app.exec()
