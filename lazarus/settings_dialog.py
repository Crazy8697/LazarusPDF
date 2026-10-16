"""File › Settings… dialog. Changes apply on OK / Apply; Cancel discards them."""
import os
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from . import APP_NAME
from . import settings as S


class ColorButton(QPushButton):
    changed = Signal(str)

    def __init__(self, title):
        super().__init__()
        self.title = title
        self.color = "#000000"
        self.setFixedWidth(110)
        self.clicked.connect(self.pick)

    def set_color(self, c):
        self.color = QColor(c).name()
        fg = "#000000" if QColor(c).lightnessF() > 0.5 else "#ffffff"
        self.setText(self.color)
        self.setStyleSheet(f"background:{self.color};color:{fg};border:1px solid #3a4150;border-radius:5px;padding:4px")

    def pick(self):
        c = QColorDialog.getColor(QColor(self.color), self, self.title)
        if c.isValid():
            self.set_color(c.name())
            self.changed.emit(self.color)


def _folder_row(edit, parent, title):
    w = QWidget()
    h = QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.addWidget(edit, 1)
    b = QPushButton("Choose…")

    def pick():
        start = edit.text() if Path(edit.text()).is_dir() else str(Path.home())
        p = QFileDialog.getExistingDirectory(parent, title, start)
        if p:
            edit.setText(os.path.normpath(p))
    b.clicked.connect(pick)
    h.addWidget(b)
    return w


class SettingsDialog(QDialog):
    applied = Signal()

    def __init__(self, parent, store):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setModal(True)
        self.resize(620, 420)
        self.s = store
        v = QVBoxLayout(self)
        v.setSpacing(10)

        # 1. Output
        g = QGroupBox("Output")
        f = QFormLayout(g)
        self.out_dir = QLineEdit()
        f.addRow("Default output folder", _folder_row(self.out_dir, self, "Default output folder"))
        self.save_default = QComboBox()
        for key, label in S.SAVE_CHOICES:
            self.save_default.addItem(label, key)
        f.addRow("Ctrl+S / Save does", self.save_default)
        v.addWidget(g)

        # 2. Extraction / gaps
        g = QGroupBox("Extraction / gaps")
        f = QFormLayout(g)
        self.open_color = ColorButton("Unfilled gap colour")
        self.filled_color = ColorButton("Filled gap colour")
        f.addRow("Unfilled gap highlight", self.open_color)
        f.addRow("Filled gap highlight", self.filled_color)
        self.headings = QCheckBox("Include page headings in exports")
        f.addRow("", self.headings)
        v.addWidget(g)

        # 3. View / zoom
        g = QGroupBox("View / zoom")
        f = QFormLayout(g)
        self.zoom = QComboBox()
        for key, label in S.ZOOM_CHOICES:
            self.zoom.addItem(label, key)
        f.addRow("When a PDF opens in the editor", self.zoom)
        self.cmp_colors = {}
        for key, label in (("compare_insert_color", "Compare: inserted"), ("compare_delete_color", "Compare: deleted"),
                           ("compare_modify_color", "Compare: changed")):
            b = ColorButton(label)
            self.cmp_colors[key] = b
            f.addRow(label, b)
        v.addWidget(g)

        # 4. Undo memory
        from . import memguard
        g = QGroupBox("Undo memory")
        f = QFormLayout(g)
        self.undo_pct = QSpinBox()
        self.undo_pct.setRange(memguard.MIN_PCT, memguard.MAX_PCT)
        self.undo_pct.setSuffix(" % of RAM")
        self.undo_gb = QLabel("")
        self.undo_gb.setObjectName("legend")
        row = QHBoxLayout()
        row.addWidget(self.undo_pct)
        row.addWidget(self.undo_gb, 1)
        box = QWidget()
        box.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        f.addRow("Undo steps for big edits may use", box)
        note = QLabel("Shared by all open PDFs. Free memory is also checked before each such step. "
                      "When full, the oldest undo steps are dropped (you're told once).")
        note.setObjectName("legend")
        note.setWordWrap(True)
        f.addRow("", note)
        self.undo_pct.valueChanged.connect(self._undo_gb)
        v.addWidget(g)

        # 5. Default PDF reader (Windows): acts at once, not on OK
        g = QGroupBox("Default PDF reader")
        f = QVBoxLayout(g)
        self.assoc_state = QLabel("")
        self.assoc_state.setWordWrap(True)
        f.addWidget(self.assoc_state)
        row = QHBoxLayout()
        self.btn_default = QPushButton("Make Lazarus the default PDF reader…")
        self.btn_default.setToolTip("Registers Lazarus for .pdf (your account only), then opens Windows' Default "
                                    "apps page, where you confirm it")
        self.btn_default.clicked.connect(lambda: self.make_default())
        self.btn_unreg = QPushButton("Remove Lazarus's registration")
        self.btn_unreg.setToolTip("Removes exactly what Lazarus added for .pdf; other programs' settings stay")
        self.btn_unreg.clicked.connect(lambda: self.unregister())
        row.addWidget(self.btn_default)
        row.addWidget(self.btn_unreg)
        row.addStretch(1)
        f.addLayout(row)
        note = QLabel("Windows only lets you choose the default yourself: after registering, confirm Lazarus in "
                      "Settings › Default apps. No admin rights needed.")
        note.setObjectName("legend")
        note.setWordWrap(True)
        f.addWidget(note)
        v.addWidget(g)
        self._assoc_state()

        # 6. Updates (stage 19b)
        g = QGroupBox("Updates")
        f = QVBoxLayout(g)
        self.update_check = QCheckBox("Check for updates at startup")
        self.update_check.setToolTip("Asks GitHub for the latest Lazarus release in the background after the "
                                     "window opens. Nothing else is sent.")
        f.addWidget(self.update_check)
        note = QLabel("The only request is GitHub's latest-release lookup for Lazarus (and, if you choose to update, "
                      "the installer download). No telemetry, nothing about your files. Help › Check for updates… "
                      "checks by hand.")
        note.setObjectName("legend")
        note.setWordWrap(True)
        f.addWidget(note)
        v.addWidget(g)

        v.addStretch(1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel
                              | QDialogButtonBox.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.Apply).clicked.connect(self.apply)
        bb.button(QDialogButtonBox.RestoreDefaults).clicked.connect(self.restore_defaults)
        v.addWidget(bb)
        self.buttons = bb
        self.load(lambda k: S.get(self.s, k))

    # ---------- default PDF reader (assoc.py) ----------
    def _assoc_state(self):
        from . import assoc as A
        reg = A.registry()
        if A.is_default(reg):
            t = "Lazarus is your default PDF reader."
        elif A.is_registered(reg):
            t = ("Lazarus is registered for PDFs (it is in \"Open with\"), but another program is still the default. "
                 "Confirm Lazarus in Settings › Default apps.")
        else:
            t = "Lazarus isn't registered for PDFs on this account."
        self.assoc_state.setText(t)
        self.btn_unreg.setEnabled(A.is_registered(reg))
        return t

    def make_default(self, open_settings=True):
        """Register (per user), then send the user to Windows' Default apps page to confirm."""
        from . import assoc as A
        reg = A.registry()
        try:
            A.register(reg)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't register Lazarus for PDFs:\n{e}")
            return False
        self._assoc_state()
        if open_settings and reg.root is None:
            A.open_default_apps()
        QMessageBox.information(self, APP_NAME, A.CONFIRM_NOTE)
        return True

    def unregister(self):
        from . import assoc as A
        reg = A.registry()
        A.unregister(reg)
        self._assoc_state()
        QMessageBox.information(self, APP_NAME, "Lazarus's registration for PDFs is removed. If Lazarus was your "
                                                "default, Windows will ask which program to use next time.")
        return True

    # ---------- fields <-> values ----------
    def load(self, value):
        self.out_dir.setText(value("out_dir"))
        self.save_default.setCurrentIndex(max(0, self.save_default.findData(value("save_default"))))
        self.open_color.set_color(value("gap_open_color"))
        self.filled_color.set_color(value("gap_filled_color"))
        self.headings.setChecked(bool(value("export_headings")))
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(value("default_zoom"))))
        for key, b in self.cmp_colors.items():
            b.set_color(value(key))
        self.undo_pct.setValue(int(value("undo_ram_pct")))
        self._undo_gb()
        self.update_check.setChecked(bool(value("update_check")))

    def _undo_gb(self, *_):
        from . import memguard
        try:
            total = memguard.BUDGET.total()
        except Exception:
            self.undo_gb.setText("")
            return
        self.undo_gb.setText(f"≈ {total * self.undo_pct.value() / 100 / 2 ** 30:.1f} GB "
                             f"of {total / 2 ** 30:.1f} GB")

    def values(self):
        return {
            "out_dir": os.path.normpath(self.out_dir.text().strip() or S.DEFAULTS["out_dir"]),
            "save_default": self.save_default.currentData(),
            "gap_open_color": self.open_color.color,
            "gap_filled_color": self.filled_color.color,
            "export_headings": self.headings.isChecked(),
            "default_zoom": self.zoom.currentData(),
            "undo_ram_pct": self.undo_pct.value(),
            "update_check": self.update_check.isChecked(),
            **{key: b.color for key, b in self.cmp_colors.items()},
        }

    def restore_defaults(self):
        """Fill the form with defaults; nothing is saved until OK / Apply."""
        self.load(lambda k: S.DEFAULTS[k])

    def validate(self, vals):
        return True

    def apply(self):
        vals = self.values()
        if not self.validate(vals):
            return False
        for k, v in vals.items():
            S.put(self.s, k, v)
        self.s.sync()
        self.applied.emit()
        return True

    def accept(self):
        if self.apply():
            super().accept()
