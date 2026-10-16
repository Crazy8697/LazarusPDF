"""Stage 19b UI: the update check, the update notice and Download and install.

- At startup (Settings › "Check for updates at startup", on by default) the check runs in a
  background thread a few seconds after the window is up: it never delays the launch and never
  takes the focus. Any failure (offline, rate limit, a bad reply) is silent then.
- Help › Check for updates… checks by hand and always says how it went.
- A newer release shows a bar at the top of the window (never a pop-up): the version, its release
  notes (Release notes), and Download and install / Skip this version / Later. A skipped
  version is remembered (QSettings "update_skip"); a newer one still shows.
- Download and install: the installer is downloaded to %TEMP% with progress and Cancel and
  checked against the release's SHA-256; then any unsaved work is asked about (Save / Discard /
  Cancel, never saved by itself), Setup is started (it upgrades this installation in place and
  starts Lazarus again afterwards) and Lazarus closes. A source / dev run checks but never
  installs."""
import os

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressDialog, QPushButton, QTextBrowser,
                               QVBoxLayout)

from . import APP_NAME, __version__
from . import settings as S
from . import updater as U

SKIP_KEY = "update_skip"
STARTUP_DELAY_MS = 4000


class _Check(QThread):
    done = Signal(dict)

    def run(self):
        self.done.emit(U.check())


class _Download(QThread):
    progress = Signal(int, int)
    done = Signal(object, str)          # path or None, error text ("" = ok, "cancelled")

    def __init__(self, info):
        super().__init__()
        self.info = info
        self.cancelled = False

    def run(self):
        try:
            p = U.download(self.info, lambda d, t: self.progress.emit(d, t), lambda: self.cancelled)
            self.done.emit(p, "")
        except U.Cancelled:
            self.done.emit(None, "cancelled")
        except U.UpdateError as e:
            self.done.emit(None, str(e))


class UpdateBar(QFrame):
    """The update notice: a bar at the top of the window (it never takes the focus)."""

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.setObjectName("updatebar")
        self.setStyleSheet("#updatebar{background:#1f3352;border-bottom:1px solid #2d6cdf}"
                           "#updatebar QLabel{color:#e6eefc}")
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 6, 10, 6)
        row = QHBoxLayout()
        self.label = QLabel("")
        self.label.setTextFormat(Qt.RichText)
        row.addWidget(self.label, 1)
        self.btn_notes = QPushButton("Release notes")
        self.btn_install = QPushButton("Download and install")
        self.btn_skip = QPushButton("Skip this version")
        self.btn_later = QPushButton("Later")
        for b in (self.btn_notes, self.btn_install, self.btn_skip, self.btn_later):
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
        v.addLayout(row)
        self.notes = QTextBrowser()
        self.notes.setOpenExternalLinks(True)
        self.notes.setMaximumHeight(180)
        self.notes.setFocusPolicy(Qt.NoFocus)
        self.notes.hide()
        v.addWidget(self.notes)
        self.btn_notes.clicked.connect(lambda: self.notes.setVisible(not self.notes.isVisible()))
        self.btn_install.clicked.connect(ctl.download_and_install)
        self.btn_skip.clicked.connect(ctl.skip)
        self.btn_later.clicked.connect(ctl.later)
        self.hide()

    def show_release(self, info):
        self.label.setText(f"<b>Lazarus {info['version']}</b> is available (this program is {__version__}).")
        self.notes.setMarkdown(info.get("notes") or "No release notes.")
        self.notes.hide()
        if U.can_install():
            self.btn_install.setEnabled(True)
            self.btn_install.setToolTip("Downloads the installer, checks its SHA-256, then updates this installation")
        else:
            self.btn_install.setEnabled(False)
            self.btn_install.setToolTip("This is a source (development) run: it never installs over itself. "
                                        "Use the installer from the Releases page.")
        self.show()


class UpdateController(QObject):
    def __init__(self, win, bar_host):
        super().__init__(win)
        self.win = win
        self.info = None
        self.thread = None
        self.dl = None
        self.dlg = None
        self.last = None                      # (tests) the last check result / outcome
        self.checks = 0
        self.bar = UpdateBar(self)
        bar_host.insertWidget(0, self.bar)
        self.act_check = QAction("Check for updates…", win, triggered=lambda _=False: self.check(manual=True))
        self.act_check.setToolTip("Ask GitHub whether a newer Lazarus is out")

    # ---------- checking ----------
    def startup(self):
        """After the window is up: a delayed background check, if the setting is on."""
        if S.get(self.win.settings, "update_check"):
            QTimer.singleShot(STARTUP_DELAY_MS, lambda: self.check(manual=False))

    def check(self, manual=False):
        if self.thread is not None:
            return False
        self.checks += 1
        t = self.thread = _Check()
        t.done.connect(lambda res, manual=manual: self._checked(res, manual))
        t.finished.connect(self._thread_done)
        t.start()
        if manual:
            self.win.statusBar().showMessage("Checking GitHub for a newer Lazarus…", 4000)
        return True

    def _thread_done(self):
        t, self.thread = self.thread, None
        if t is not None:
            t.deleteLater()

    def wait(self, ms=20000):
        """(tests) until the running check / download is done."""
        from PySide6.QtCore import QEventLoop, QElapsedTimer
        tm = QElapsedTimer()
        tm.start()
        while (self.thread is not None or self.dl is not None) and tm.elapsed() < ms:
            loop = QEventLoop()
            QTimer.singleShot(20, loop.quit)
            loop.exec()

    def _checked(self, res, manual):
        self.last = res
        st = res.get("status")
        skipped = str(self.win.settings.value(SKIP_KEY, "", type=str) or "")
        if st == "newer":
            self.info = res
            if not manual and res["version"] == skipped:
                return                                   # skipped: quiet (a newer one still shows)
            self.bar.show_release(res)
            if manual:
                self.win.statusBar().showMessage(f"Lazarus {res['version']} is available: see the bar at the top.", 6000)
            if os.environ.get("LAZARUS_UPDATE_TEST_ACTION") == "install":
                QTimer.singleShot(0, self.download_and_install)
            return
        if not manual:
            return                                       # startup: silent unless there's news
        if st == "current":
            QMessageBox.information(self.win, APP_NAME, f"Lazarus is up to date ({__version__}).")
        elif st == "none":
            QMessageBox.information(self.win, APP_NAME, f"No Lazarus release is published yet. This program is "
                                                        f"{__version__}.")
        else:
            QMessageBox.warning(self.win, APP_NAME, f"Couldn't check for updates: {res.get('reason', 'unknown error')}")

    def skip(self):
        if self.info:
            self.win.settings.setValue(SKIP_KEY, self.info["version"])
            self.win.statusBar().showMessage(f"Lazarus {self.info['version']} skipped. A newer version will still "
                                             "show.", 5000)
        self.bar.hide()

    def later(self):
        self.bar.hide()

    # ---------- download + install ----------
    def download_and_install(self):
        if not self.info or self.dl is not None:
            return False
        if not U.can_install():
            QMessageBox.information(self.win, APP_NAME, "This is a source (development) run of Lazarus: it doesn't "
                                                        "install updates over itself. Use the installer from the "
                                                        "Releases page.")
            self.last = {"status": "refused", "reason": "source run"}
            return False
        dlg = self.dlg = QProgressDialog(f"Downloading Lazarus {self.info['version']}…", "Cancel", 0, 100, self.win)
        dlg.setWindowTitle(APP_NAME)
        dlg.setWindowModality(Qt.NonModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        d = self.dl = _Download(self.info)
        dlg.canceled.connect(lambda: setattr(d, "cancelled", True))
        d.progress.connect(lambda done, total: (dlg.setMaximum(max(1, total)), dlg.setValue(min(done, max(1, total))))
                           if self.dlg is dlg else None)
        d.done.connect(self._downloaded)
        d.start()
        return True

    def cancel_download(self):
        if self.dl is not None:
            self.dl.cancelled = True

    def _downloaded(self, path, err):
        d, self.dl = self.dl, None
        dlg, self.dlg = self.dlg, None
        if dlg is not None:
            dlg.close()
            dlg.deleteLater()
        if d is not None:
            d.wait()
            d.deleteLater()
        if err == "cancelled":
            self.last = {"status": "cancelled"}
            self.win.statusBar().showMessage("Update download cancelled; nothing was kept.", 5000)
            return
        if err:
            self.last = {"status": "download-error", "reason": err}
            QMessageBox.warning(self.win, APP_NAME, err)
            return
        self.install(path)

    def install(self, setup):
        """Ask about unsaved work (Save / Discard / Cancel), start Setup, close Lazarus."""
        w = self.win
        if not w.text_mode.confirm_discard() or not w.editor_mode.confirm_all():
            self.last = {"status": "postponed"}
            w.statusBar().showMessage("Update postponed: your work is still open.", 5000)
            return False
        if not U.launch_setup(setup):
            self.last = {"status": "launch-failed"}
            QMessageBox.warning(w, APP_NAME, "Setup couldn't be started (was the Windows prompt declined?).")
            return False
        self.last = {"status": "installing", "setup": str(setup)}
        w._update_exit = True
        w.close()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()
        return True
