"""Extraction mode, hosted in the main window: the editable review pane plus its
toolbar and Review menu. Same behaviour as before stage 2; only the host changed."""
import os
import subprocess
import traceback
from pathlib import Path

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QFileDialog, QLabel, QMenu, QMessageBox, QToolBar, QToolButton

from . import APP_NAME, export, output
from . import settings as S
from .editor import Editor


class ExtractionMode(QObject):
    key = "extract"
    title = "Extraction"

    def __init__(self, win, settings):
        super().__init__(win)
        self.win = win
        self.settings = settings
        self.active = False
        self.paths = None          # review output paths of the loaded document
        self.title_base = ""       # source base name (used inside outputs only)
        self.editor = Editor()
        self.widget = self.editor
        self._build()
        self.editor.gapsChanged.connect(self.on_gaps)
        self.editor.dirtyChanged.connect(lambda _: self.win.update_title())

    def status(self, msg, ms=0):
        self.win.statusBar().showMessage(msg, ms)

    # ---------- chrome ----------
    def _build(self):
        win = self.win
        # Ctrl+S / Save runs the default chosen in Settings (save in place, or Save As)
        self.act_save = QAction("Save", win, shortcut=QKeySequence.Save, triggered=self.save_default)
        self.act_save_as = QAction("Save As…", win, shortcut="Ctrl+Shift+S", triggered=self.save_as)
        self.act_save_as.setToolTip("Write the four review files to another folder and keep working there")
        self.act_save_inplace = QAction("Save in place", win, triggered=self.save)
        self.act_save_inplace.setToolTip("Overwrite the four review files where they are")
        self.act_next = QAction("Next gap ▶", win, shortcut="F3", triggered=lambda: self.goto_gap(True))
        self.act_prev = QAction("◀ Prev gap", win, shortcut="Shift+F3", triggered=lambda: self.goto_gap(False))
        self.act_folder = QAction("Show output folder", win, triggered=self.show_folder)
        self.act_headings = QAction("Include page headings in exports", win, checkable=True)
        self.act_headings.setChecked(S.get(self.settings, "export_headings"))
        self.act_headings.toggled.connect(lambda v: self.settings.setValue("export_headings", v))

        self.export_menu = QMenu("Export", win)
        for key, label, ext, filt in export.FORMATS:
            a = self.export_menu.addAction(label)
            a.triggered.connect(lambda _=False, k=key: self.do_export(k))
        self.export_menu.addSeparator()
        self.export_menu.addAction(self.act_headings)

        review = QMenu("Review", win)
        review.addAction(self.act_save)
        review.addAction(self.act_save_inplace)
        review.addAction(self.act_save_as)
        review.addMenu(self.export_menu)
        review.addSeparator()
        review.addAction(self.act_prev)
        review.addAction(self.act_next)
        review.addSeparator()
        review.addAction(self.act_folder)
        self.menus = [review]
        self.file_entries = [self.act_save, self.act_save_as, self.export_menu.menuAction(), self.act_folder]
        self.view_entries = []

        tb = QToolBar("Extraction", win)
        tb.setObjectName("extract_bar")
        tb.setMovable(False)
        tb.addAction(win.act_import)
        tb.addAction(self.act_save)
        self.export_btn = QToolButton()
        self.export_btn.setText("Export ▾")
        self.export_btn.setMenu(self.export_menu)
        self.export_btn.setPopupMode(QToolButton.InstantPopup)
        tb.addWidget(self.export_btn)
        tb.addSeparator()
        tb.addAction(self.act_prev)
        tb.addAction(self.act_next)
        tb.addSeparator()
        self.gap_label = QLabel("")
        self.gap_label.setObjectName("gaps")
        self.filled_label = QLabel("")
        self.filled_label.setObjectName("filled")
        tb.addWidget(self.gap_label)
        tb.addWidget(QLabel("  "))
        tb.addWidget(self.filled_label)
        self.top_bar = tb
        self.doc_actions = [self.act_save, self.act_save_as, self.act_save_inplace, self.act_next,
                            self.act_prev, self.act_folder]

    def toolbars(self):
        return [self.top_bar]

    def set_active(self, on):
        self.active = on
        self.top_bar.setVisible(on)
        for m in self.menus:
            m.menuAction().setVisible(on)
        for a in self.file_entries:
            a.setVisible(on)
        self._apply_enabled()
        if on and not self.editor.loaded:
            self.status("Extraction: import a PDF (File › Import PDF for extraction…, Ctrl+I).")

    def _apply_enabled(self):
        live = self.active and self.editor.loaded
        for a in self.doc_actions:
            a.setEnabled(live)
        self.export_btn.setEnabled(live)
        self.export_menu.setEnabled(live)
        self.gap_label.setVisible(self.editor.loaded)
        self.filled_label.setVisible(self.editor.loaded)

    def on_gaps(self, open_n, filled):
        self.gap_label.setText(f"  Open gaps: {open_n}  ")
        self.filled_label.setText(f"  Filled: {filled}  ")

    def is_dirty(self):
        return self.editor.loaded and self.editor.is_dirty()

    # ---------- load ----------
    def load(self, pages, paths):
        if not self.confirm_discard():
            return False
        self.paths = {k: Path(v) for k, v in paths.items()}
        self.title_base = self.paths["review"].name.removesuffix("-review.html")
        self.editor.load(pages)
        self.win.set_mode(self.key)
        self._apply_enabled()
        self.win.update_title()
        self.status("Loaded. Click a yellow gap (or press F3) and type to fill it.")
        return True

    def confirm_discard(self):
        if not self.is_dirty():
            return True
        r = QMessageBox.question(self.win, APP_NAME, "Save your extraction edits first?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                                 QMessageBox.Save)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    # ---------- settings ----------
    def apply_settings(self):
        s = self.settings
        self.act_headings.setChecked(S.get(s, "export_headings"))
        as_ = S.get(s, "save_default") == "save_as"
        self.act_save.setText("Save As…" if as_ else "Save")
        self.act_save.setToolTip("Ctrl+S: " + ("Save As… (choose a folder)" if as_ else "save in place"))
        open_c, filled_c = S.get(s, "gap_open_color"), S.get(s, "gap_filled_color")
        self.editor.set_gap_colors(open_c, filled_c)
        from . import output
        output.set_gap_colors(open_c, filled_c)        # the written review HTML / PDF too (17)
        from .editor import STYLES
        for lab, kind in ((self.gap_label, "gap_open"), (self.filled_label, "gap_filled")):
            st = STYLES[kind]
            lab.setStyleSheet(f"background:{st['bg']};color:{st['fg']};font-weight:700;"
                              "padding:2px 8px;border-radius:4px")

    # ---------- save / export ----------
    def save_default(self):
        if S.get(self.settings, "save_default") == "save_as":
            return self.save_as()
        return self.save()

    def save_as(self):
        if not self.editor.loaded or not self.paths:
            return False
        start = str(self.paths["review"].parent)
        folder = QFileDialog.getExistingDirectory(self.win, "Save review files to folder", start)
        if not folder:
            return False
        new = output.output_paths(self.title_base + ".pdf", folder)   # helper takes a source path
        from . import protect
        if any(protect.is_protected(p) for p in new.values()):
            QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
            return False
        if Path(folder).resolve() != self.paths["review"].parent.resolve() and \
                any(p.exists() for p in new.values()):
            r = QMessageBox.question(self.win, APP_NAME,
                                     "Review files with this name already exist in that folder. Overwrite them?",
                                     QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return False
        old = self.paths
        self.paths = new
        if not self.save():
            self.paths = old
            return False
        self.status("Saved to the new folder — further saves go there.", 6000)
        return True

    def save(self):
        if not self.editor.loaded or not self.paths:
            return False
        self.editor.flush()
        try:
            output.write_review(self.editor.pages(), self.paths, self.title_base)
        except Exception as e:
            QMessageBox.critical(self.win, APP_NAME, f"Save failed:\n{e}")
            return False
        self.editor.mark_saved()
        self.win.update_title()
        self.status("Saved — review outputs updated in place.", 6000)
        return True

    def do_export(self, fmt):
        if not self.editor.loaded:
            return
        self.editor.flush()
        open_n, _ = self.editor.gap_counts()
        if open_n:
            r = QMessageBox.warning(self.win, APP_NAME,
                                    f"You still have {open_n} gap(s) unfilled. Export anyway?",
                                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if r != QMessageBox.Yes:
                return
        from . import protect
        key, label, ext, filt = next(f for f in export.FORMATS if f[0] == fmt)
        start_dir = self.settings.value("export_dir", str(self.paths["review"].parent))
        start = str(Path(start_dir) / (self.title_base + ext))
        while True:
            target, _ = QFileDialog.getSaveFileName(self.win, f"Export as {label}", start, filt)
            if not target:
                return
            if not target.lower().endswith(ext):
                target += ext
            if protect.is_protected(target):
                QMessageBox.warning(self.win, APP_NAME, protect.MESSAGE)
                start = str(Path(start_dir) / (self.title_base + "-export" + ext))
                continue
            break
        self.settings.setValue("export_dir", str(Path(target).parent))
        try:
            export.export(fmt, self.editor.pages(), target, self.title_base,
                          self.act_headings.isChecked())
        except Exception as e:
            QMessageBox.critical(self.win, APP_NAME, f"Export failed:\n{e}\n\n{traceback.format_exc()}")
            return
        self.status(f"Exported {label}.", 6000)

    def goto_gap(self, forward):
        if not self.editor.goto_gap(forward):
            self.status("No open gaps.", 4000)
        self.editor.setFocus()

    def show_folder(self):
        if self.paths:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(str(self.paths["review"]))])
