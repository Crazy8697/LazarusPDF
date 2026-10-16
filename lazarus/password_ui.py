"""Dialogs for File > Save With Password (Keep / Set / Match / Clear).

Each ask_* function returns the user's answer or None when cancelled. They are module-level
so the self-test can answer them without showing a window."""
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QLabel,
                               QLineEdit, QMessageBox, QVBoxLayout)

from . import APP_NAME, locked


def _pw_field():
    f = QLineEdit()
    f.setEchoMode(QLineEdit.Password)
    f.setMinimumWidth(260)
    return f


def _show_toggle(fields):
    cb = QCheckBox("Show passwords")
    cb.toggled.connect(lambda on: [f.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
                                   for f in fields])
    return cb


class SetPasswordsDialog(QDialog):
    """Open password + Owner password; blank = not set. Permission boxes apply only with an
    owner password, so they stay greyed until one is typed. All allowed by default."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Set passwords")
        v = QVBoxLayout(self)
        intro = QLabel("Saves a new file. Leave a password blank to not set it:\n"
                       "• both: the open password opens it; the owner password controls permissions\n"
                       "• open only: a password to open, no permission lock\n"
                       "• owner only: opens freely, permissions restricted\n"
                       "• both blank: no protection (same as Clear passwords)")
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.open_pw = _pw_field()
        self.owner_pw = _pw_field()
        form.addRow("Open password:", self.open_pw)
        form.addRow("Owner password:", self.owner_pw)
        v.addLayout(form)
        self.perm_box = QGroupBox("Permissions (need an owner password)")
        pv = QVBoxLayout(self.perm_box)
        self.perms = {}
        for key, label, _ in locked.PERMISSIONS:
            cb = QCheckBox(label)
            cb.setChecked(True)
            pv.addWidget(cb)
            self.perms[key] = cb
        v.addWidget(self.perm_box)
        v.addWidget(_show_toggle([self.open_pw, self.owner_pw]))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Choose file…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.owner_pw.textChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        self.perm_box.setEnabled(bool(self.owner_pw.text()))

    def values(self):
        return (self.open_pw.text(), self.owner_pw.text(),
                {k: cb.isChecked() for k, cb in self.perms.items()})


class MatchPasswordDialog(QDialog):
    """One password, used as both the open and the owner password. Typed twice."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Match passwords")
        v = QVBoxLayout(self)
        intro = QLabel("Saves a new file whose open and owner passwords are the same.")
        intro.setWordWrap(True)
        v.addWidget(intro)
        form = QFormLayout()
        self.pw = _pw_field()
        self.again = _pw_field()
        form.addRow("Password:", self.pw)
        form.addRow("Again:", self.again)
        v.addLayout(form)
        self.hint = QLabel("")
        v.addWidget(self.hint)
        v.addWidget(_show_toggle([self.pw, self.again]))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = bb.button(QDialogButtonBox.Ok)
        self.ok.setText("Choose file…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.pw.textChanged.connect(self._sync)
        self.again.textChanged.connect(self._sync)
        self._sync()

    def _sync(self):
        a, b = self.pw.text(), self.again.text()
        self.ok.setEnabled(bool(a) and a == b)
        self.hint.setText("" if not b or a == b else "The two passwords don't match.")


def ask_set_passwords(parent):
    """-> (open_pw, owner_pw, allowed) or None."""
    d = SetPasswordsDialog(parent)
    return d.values() if d.exec() == QDialog.Accepted else None


def ask_match_password(parent):
    """-> password or None."""
    d = MatchPasswordDialog(parent)
    return d.pw.text() if d.exec() == QDialog.Accepted else None


def warn_unknown_owner(parent):
    """Keep password when the owner password is unknown. -> "set", "continue" or None."""
    box = QMessageBox(QMessageBox.Warning, APP_NAME,
                      "The copy will keep its password and its restrictions, but its owner password "
                      "isn't known (the file was unlocked with its open password), so Lazarus would give "
                      "it a random one.\n\nNobody, including you, will be able to change the copy's "
                      "permissions later. Recommended: Set passwords, so you choose the owner password.",
                      QMessageBox.NoButton, parent)
    set_btn = box.addButton("Set passwords instead", QMessageBox.AcceptRole)
    go_btn = box.addButton("Continue anyway", QMessageBox.DestructiveRole)
    box.addButton(QMessageBox.Cancel)
    box.setDefaultButton(set_btn)
    box.exec()
    clicked = box.clickedButton()
    return "set" if clicked is set_btn else "continue" if clicked is go_btn else None


def confirm_clear(parent):
    """Clear passwords: warn first. -> True to go ahead."""
    r = QMessageBox.warning(parent, APP_NAME,
                            "Save a copy WITHOUT any password?\n\nAnyone who gets the new file can open "
                            "it, and its permission restrictions are removed. The original stays protected.",
                            QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel)
    return r == QMessageBox.Save
