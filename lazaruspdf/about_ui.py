"""Stage 20: the About box (on the DarkRelay banner), Help › Licenses and Help › Support DarkRelay."""
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QLinearGradient, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from . import APP_NAME, __version__

SOURCE_URL = "https://github.com/Crazy8697/LazarusPDF"
SUPPORT_URL = "https://buymeacoffee.com/crazy8697"
HERE = Path(__file__).resolve().parent
BANNER = HERE / "assets" / "darkrelay-banner.jpg"
ICON = HERE / "assets" / "lazaruspdf.ico"


def resource(name):
    """LICENSE / THIRD-PARTY-NOTICES.txt: next to the frozen program's files, or the source root."""
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else HERE.parent
    return base / name


ABOUT_HTML = f"""
<p style="font-size:15px;margin:0 0 10px 0"><b>A free, local PDF editor and reader for Windows.</b></p>
<p>Read, annotate and mark up PDFs; edit their text; fill in and create forms; redact; rearrange, rotate,
crop, merge and split pages (also between open PDFs); add bookmarks, links, headers, page numbers,
watermarks and Bates numbers; protect with passwords; compare two PDFs; turn scans and images into
searchable PDFs; and export to Word, EPUB, Markdown, HTML or text. Extraction mode pulls the text out of
a PDF and highlights anything it can't read for you to fill in.</p>
<p>Everything runs on this computer. Nothing is uploaded, and an original PDF is never written: saves and
exports always go to a new file.</p>
<p><b>Updates</b>: the only network requests are GitHub's latest-release lookup for Lazarus PDF (at startup if
Settings › "Check for updates at startup" is on, or Help › Check for updates…) and, if you choose to
update, the installer download. No telemetry, no IDs, nothing about your files or settings is sent.</p>
<p style="margin-top:12px">By <b>DarkRelay</b> &nbsp;·&nbsp; Version {__version__}<br>
Free software under the GNU Affero General Public License v3.0 (AGPL-3.0); it comes with NO WARRANTY.<br>
Source code: <a href="{SOURCE_URL}"><span style="color:#7fd3ff">{SOURCE_URL}</span></a></p>
"""


class _Backdrop(QWidget):
    """The DarkRelay banner behind the About content: cropped so the relay node sits to the right,
    and darkened (more on the left, where the text is) so everything stays easy to read."""

    def __init__(self):
        super().__init__()
        self._src = QPixmap(str(BANNER)) if BANNER.exists() else QPixmap()
        self._cache = None

    def paintEvent(self, e):
        p = QPainter(self)
        r = self.rect()
        p.fillRect(r, QColor("#0a0e14"))
        if not self._src.isNull():
            if self._cache is None or self._cache.size() != r.size():
                sw, sh = self._src.width(), self._src.height()
                # crop the banner's height, with the width the dialog's shape needs, the node at ~78%
                cw = min(sw, int(sh * r.width() / max(1, r.height())))
                x0 = max(0, min(sw - cw, int(sw / 2 - cw * 0.78)))
                self._cache = self._src.copy(x0, 0, cw, sh).scaled(r.size(), Qt.IgnoreAspectRatio,
                                                                   Qt.SmoothTransformation)
            p.drawPixmap(0, 0, self._cache)
        g = QLinearGradient(0, 0, r.width(), 0)
        g.setColorAt(0.0, QColor(5, 7, 10, 235))
        g.setColorAt(0.62, QColor(5, 7, 10, 205))
        g.setColorAt(1.0, QColor(5, 7, 10, 120))
        p.fillRect(QRectF(r), g)
        p.end()


class AboutDialog(QDialog):
    def __init__(self, parent=None, licenses=None, support=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumSize(680, 600)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.backdrop = _Backdrop()
        outer.addWidget(self.backdrop)
        v = QVBoxLayout(self.backdrop)
        v.setContentsMargins(26, 22, 26, 16)
        head = QHBoxLayout()
        icon = QLabel()
        if ICON.exists():
            icon.setPixmap(QIcon(str(ICON)).pixmap(64, 64))
        head.addWidget(icon, 0, Qt.AlignTop)
        title = QLabel(f"<span style='font-size:22px;font-weight:700;color:#ffffff'>{APP_NAME}</span><br>"
                       f"<span style='color:#9fb4c8'>Version {__version__}</span>")
        head.addSpacing(10)
        head.addWidget(title, 1, Qt.AlignVCenter)
        v.addLayout(head)
        v.addSpacing(8)
        self.body = QLabel(ABOUT_HTML)
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.RichText)
        self.body.setOpenExternalLinks(True)
        self.body.setStyleSheet("color:#e6eef6;font-size:13px;background:transparent")
        self.body.setMaximumWidth(560)
        pal = self.body.palette()
        pal.setColor(QPalette.Link, QColor("#7fd3ff"))
        self.body.setPalette(pal)
        v.addWidget(self.body)
        v.addStretch(1)
        row = QHBoxLayout()
        self.btn_licenses = QPushButton("Licenses…")
        self.btn_support = QPushButton("Support DarkRelay")
        if licenses:
            self.btn_licenses.clicked.connect(licenses)
        if support:
            self.btn_support.clicked.connect(support)
        row.addWidget(self.btn_licenses)
        row.addWidget(self.btn_support)
        row.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.accepted.connect(self.accept)
        row.addWidget(bb)
        v.addLayout(row)


class LicensesDialog(QDialog):
    """Help › Licenses: Lazarus PDF's own license and the third-party notices."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME}: licenses")
        self.resize(820, 640)
        v = QVBoxLayout(self)
        intro = QLabel(f"Lazarus PDF is free software under the GNU AGPL-3.0. Source code: "
                       f"<a href='{SOURCE_URL}'>{SOURCE_URL}</a>. It includes the third-party components listed "
                       f"in the notices, each under its own license.")
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        v.addWidget(intro)
        self.tabs = QTabWidget()
        self.texts = {}
        for title, name in (("Third-party notices", "THIRD-PARTY-NOTICES.txt"), ("Lazarus PDF (AGPL-3.0)", "LICENSE")):
            tb = QTextBrowser()
            tb.setLineWrapMode(QTextBrowser.NoWrap)
            f = tb.font()
            f.setFamily("Consolas")
            tb.setFont(f)
            p = resource(name)
            text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else f"({name} is missing.)"
            tb.setPlainText(text)
            self.texts[name] = text
            self.tabs.addTab(tb, title)
        v.addWidget(self.tabs, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)


def open_support():
    """Help › Support DarkRelay: the Buy Me a Coffee page in the default browser."""
    return QDesktopServices.openUrl(QUrl(SUPPORT_URL))
