"""One Lazarus PDF per user (stage 12). The first one listens on a local socket (a named pipe on
Windows, QtNetwork's QLocalServer); a later launch, e.g. a double-clicked PDF while Lazarus PDF is
running, hands its PDF paths to it and exits. Each path becomes a new tab in the running
window (or brings its tab to the front if it is open already), and the window is raised.

The message is one line of JSON: {"open": [absolute paths]}; the reply is "ok". Paths are
read where they arrive and never logged. If two launches start at once, the one that wins the
name becomes the instance and the other one hands off to it."""
import getpass
import hashlib
import json
import os
import sys
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

PROTOCOL = 1


def _headless():
    """Offscreen (the self-test): never touch the real desktop's foreground window."""
    return os.environ.get("QT_QPA_PLATFORM") == "offscreen"


def server_name():
    env = os.environ.get("LAZARUS_INSTANCE_NAME")
    if env:
        return env
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return "LazarusPDF-" + hashlib.sha1(user.lower().encode()).hexdigest()[:12] + f"-v{PROTOCOL}"


def pdf_args(argv):
    """The PDF paths in a command line (absolute)."""
    return [os.path.abspath(a) for a in argv if a.lower().endswith(".pdf") and os.path.isfile(a)]


def hand_off(paths, name=None, timeout_ms=8000):
    """Give paths to a running Lazarus PDF. True if one took them (this launch should exit). No
    instance at all fails at once; a busy one gets timeout_ms to accept and answer."""
    s = QLocalSocket()
    s.connectToServer(name or server_name())
    if not s.waitForConnected(timeout_ms):
        return False
    if sys.platform == "win32" and not _headless():
        try:                                   # let the running window come to the front
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
    s.write((json.dumps({"open": list(paths)}) + "\n").encode("utf-8"))
    s.flush()
    # (flush() may already have sent it all: then waitForBytesWritten() says False with nothing
    # left to write, which is not a failure)
    while s.bytesToWrite() > 0:
        if not s.waitForBytesWritten(timeout_ms):
            return False
    reply = b""
    t0 = time.monotonic()
    while b"\n" not in reply and time.monotonic() - t0 < timeout_ms / 1000:
        if s.bytesAvailable() or s.waitForReadyRead(200):
            reply += bytes(s.readAll())
        elif s.state() != QLocalSocket.ConnectedState:
            reply += bytes(s.readAll())
            break
    s.disconnectFromServer()
    return reply.startswith(b"ok")


_MUTEX = None


def claim(name=None):
    """True if this process is THE instance for this user (a named mutex decides, so two
    launches at the same moment can't both become one); the claim lasts as long as the process."""
    global _MUTEX
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    h = k32.CreateMutexW(None, False, "Local\\" + (name or server_name()))
    if not h:
        return True                            # can't tell: behave as before (the server decides)
    if k32.GetLastError() == 183:              # ERROR_ALREADY_EXISTS: another Lazarus PDF owns it
        k32.CloseHandle(h)
        return False
    _MUTEX = h
    return True


class InstanceServer(QObject):
    """Listens for later launches. openRequested(paths) for each message (an empty list:
    just bring the window up)."""
    openRequested = Signal(list)

    def __init__(self, name=None, parent=None):
        super().__init__(parent)
        self.name = name or server_name()
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._new)
        self.connections = 0                   # handed-off launches received (the self-test reads it)
        self._buf = {}

    def listen(self):
        """True if this process is now THE instance; False if another one holds the name."""
        if self.server.listen(self.name):
            return True
        if sys.platform != "win32":
            QLocalServer.removeServer(self.name)   # a stale socket file after a crash
            return self.server.listen(self.name)
        return False

    def close(self):
        self.server.close()

    def _new(self):
        while self.server.hasPendingConnections():
            c = self.server.nextPendingConnection()
            self._buf[c] = b""
            c.readyRead.connect(lambda c=c: self._read(c))
            c.disconnected.connect(lambda c=c: self._gone(c))
            if c.bytesAvailable():
                self._read(c)

    def _gone(self, c):
        """A launch disconnected: read what it sent first (a fast one may be gone already)."""
        if c.bytesAvailable():
            self._read(c)
        self._buf.pop(c, None)
        c.deleteLater()

    def _read(self, c):
        """Complete lines only; safe to re-enter (a reply can make the client disconnect at once)."""
        data = self._buf.pop(c, b"") + bytes(c.readAll())
        *lines, rest = data.split(b"\n")
        if c.state() == QLocalSocket.ConnectedState:
            self._buf[c] = rest
        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode("utf-8"))
                paths = [str(p) for p in msg.get("open", [])]
            except Exception:
                c.write(b"error\n")
                continue
            self.connections += 1
            QTimer.singleShot(0, lambda p=paths: self.openRequested.emit(p))
            c.write(b"ok\n")
            c.flush()


class OpenQueue(QObject):
    """Paths handed over by later launches, opened one by one as tabs. Waits while the window
    is locked (a find-index build) or a dialog is open, so nothing jumps the queue."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.pending = []
        self.opened = 0
        self._timer = QTimer(self, singleShot=True, interval=0)
        self._timer.timeout.connect(self._next)

    def add(self, paths):
        self.pending.extend(paths)
        self.raise_window()
        self._timer.start(0)

    def raise_window(self):
        if _headless():
            return
        w = self.win
        if w.isMinimized():
            w.showNormal()
        w.show()
        w.raise_()
        w.activateWindow()
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(w.winId()))
            except Exception:
                pass

    def _next(self):
        from PySide6.QtWidgets import QApplication
        if not self.pending:
            return
        busy = getattr(self.win, "find", None) is not None and self.win.find.locked
        if busy or QApplication.activeModalWidget() is not None:
            self._timer.start(200)                 # try again shortly
            return
        p = self.pending.pop(0)
        if os.path.isfile(p):
            self.win.editor_mode.open_pdf(os.path.normpath(p), wait=False)
            self.opened += 1
        else:
            self.win.statusBar().showMessage("A PDF handed over by another launch isn't there any more.", 5000)
        if self.pending:
            self._timer.start(0)


def wait_for_instance(paths, tries=120, delay=0.25):
    """Another launch won the name a moment ago: hand off to it once it is listening."""
    for _ in range(tries):
        if hand_off(paths):
            return True
        time.sleep(delay)
    return False
