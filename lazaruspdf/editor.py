"""Editable review pane. Pages <-> QTextDocument with tagged gaps and figures.

Everything is editable. Each gap is a run of text whose char format carries
P_KIND="gap" and its page number. If the run still reads exactly
"[[FILL IN - p.N]]", the gap is open. Any other text means it is filled.
The look comes from STYLES only, so changing a colour is a one-line edit."""
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextFormat, QTextImageFormat)
from PySide6.QtWidgets import QTextEdit

from .extract import GAP, Figure, Filled, PageResult, Para
from .output import fig_marker, gap_marker

P_KIND = QTextFormat.UserProperty + 1    # char: "gap" | "figmark"
P_PAGE = QTextFormat.UserProperty + 2    # char/block: page number
B_KIND = QTextFormat.UserProperty + 3    # block: "page" | "label" | "ocr"
B_METHOD = QTextFormat.UserProperty + 4  # block: page method text

# Change these to restyle open/filled gaps and figure markers.
STYLES = {
    "gap_open": {"bg": "#ffeb3b", "fg": "#b71c1c", "bold": True, "underline": False},
    "gap_filled": {"bg": "#24452c", "fg": "#a5d6a7", "bold": False, "underline": True},
    "figmark": {"bg": "#4fc3f7", "fg": "#000000", "bold": True, "underline": False},
    "heading": {"fg": "#8a94a6", "size": 9},
    "label": {"fg": "#9aa0a6"},
}

MAX_FIG_W = 640


def _readable_fg(bg, light, dark):
    return light if QColor(bg).lightnessF() < 0.5 else dark


def set_gap_style_colors(open_bg, filled_bg):
    """Highlight colours come from Settings; the text colour is picked to stay readable."""
    STYLES["gap_open"].update(bg=QColor(open_bg).name(), fg=_readable_fg(open_bg, "#ffd6d6", "#b71c1c"))
    STYLES["gap_filled"].update(bg=QColor(filled_bg).name(), fg=_readable_fg(filled_bg, "#a5d6a7", "#1b5e20"))


def _styled(kind, page=None):
    f = QTextCharFormat()
    st = STYLES[kind]
    f.setBackground(QBrush(QColor(st["bg"])))
    f.setForeground(QBrush(QColor(st["fg"])))
    f.setFontWeight(QFont.Bold if st["bold"] else QFont.Normal)
    f.setFontUnderline(st["underline"])
    if st["underline"]:
        f.setUnderlineStyle(QTextCharFormat.DotLine)
    f.setProperty(P_KIND, "figmark" if kind == "figmark" else "gap")
    if page is not None:
        f.setProperty(P_PAGE, page)
    return f


def _plain():
    f = QTextCharFormat()
    f.setProperty(P_KIND, "")
    f.clearBackground()
    f.clearForeground()
    f.setFontWeight(QFont.Normal)
    f.setFontUnderline(False)
    return f


class Editor(QTextEdit):
    gapsChanged = Signal(int, int)   # open, filled
    dirtyChanged = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setAcceptRichText(False)     # pasting brings plain text, never foreign formats
        self.setAcceptDrops(False)        # file drops go to the main window
        self.images = {}                  # resource name -> original PNG bytes
        self.loaded = False
        self._busy = False
        self._typed = False
        self._count_timer = QTimer(self, singleShot=True, interval=250)
        self._count_timer.timeout.connect(self._emit_counts)
        self._pending = None
        self._restyle_timer = QTimer(self, singleShot=True, interval=0)
        self._restyle_timer.timeout.connect(self._restyle)
        self.document().contentsChange.connect(self._on_change)
        self.cursorPositionChanged.connect(self._on_cursor)
        self.setPlaceholderText("Nothing loaded. File › Import PDF… to start.")
        font = QFont("Georgia", 12)
        self.document().setDefaultFont(font)
        self.document().setDocumentMargin(28)

    # ---------- load ----------
    def load(self, pages):
        self._busy = True
        self.images = {}
        doc = QTextDocument(self)
        doc.setDefaultFont(self.document().defaultFont())
        doc.setDocumentMargin(28)
        cur = QTextCursor(doc)
        first = True
        n_img = 0

        def new_block(kind=None, page=None, method=None):
            nonlocal first
            bf = QTextBlockFormat()
            bf.setBottomMargin(8)
            if kind:
                bf.setProperty(B_KIND, kind)
            if page is not None:
                bf.setProperty(P_PAGE, page)
            if method:
                bf.setProperty(B_METHOD, method)
            if kind == "page":
                bf.setTopMargin(18)
            if first:
                cur.setBlockFormat(bf)
                cur.setBlockCharFormat(_plain())
                first = False
            else:
                cur.insertBlock(bf, _plain())

        hfmt = _plain()
        hfmt.setForeground(QBrush(QColor(STYLES["heading"]["fg"])))
        hfmt.setFontPointSize(STYLES["heading"]["size"])
        hfmt.setFontWeight(QFont.DemiBold)
        lfmt = _plain()
        lfmt.setForeground(QBrush(QColor(STYLES["label"]["fg"])))
        lfmt.setFontItalic(True)

        for pr in pages:
            new_block("page", pr.number, pr.method)
            cur.insertText(f"PAGE {pr.number} · {pr.method}" + ("  (blank)" if not pr.items else ""), hfmt)
            for it in pr.items:
                if isinstance(it, Figure):
                    n_img += 1
                    name = f"lazaruspdf-fig-{n_img}"
                    self.images[name] = it.png
                    img = QImage.fromData(it.png)
                    doc.addResource(QTextDocument.ImageResource, QUrl(name), img)
                    new_block()
                    cur.insertText(fig_marker(pr.number), _styled("figmark", pr.number))
                    new_block()
                    imf = QTextImageFormat()
                    imf.setName(name)
                    w = min(MAX_FIG_W, img.width())
                    imf.setWidth(w)
                    imf.setHeight(img.height() * w / max(img.width(), 1))
                    cur.insertImage(imf)
                    for lab in it.labels:
                        new_block("label", pr.number)
                        cur.insertText(f"Text in figure: {lab}", lfmt)
                    continue
                new_block("ocr" if it.source == "ocr" else None, pr.number)
                first_seg = True
                for s in it.segs:
                    if not first_seg:
                        cur.insertText(" ", _plain())
                    first_seg = False
                    if s is GAP:
                        cur.insertText(gap_marker(it.page), _styled("gap_open", it.page))
                    elif isinstance(s, Filled):
                        cur.insertText(s.text, _styled("gap_filled", it.page))
                    else:
                        cur.insertText(s, _plain())
        doc.clearUndoRedoStacks()
        doc.setModified(False)
        self.setDocument(doc)    # Qt disposes of the previous document it owned
        doc.contentsChange.connect(self._on_change)
        doc.modificationChanged.connect(self.dirtyChanged)
        self.loaded = True
        self._busy = False
        self.moveCursor(QTextCursor.Start)
        self._emit_counts()

    # ---------- gap state tracking ----------
    @staticmethod
    def _frags(block):
        it = block.begin()
        while not it.atEnd():
            yield it.fragment()
            it += 1

    def _gap_frag_at(self, pos):
        """Gap fragment containing pos (start < pos < end), else None."""
        block = self.document().findBlock(pos)
        for f in self._frags(block):
            if f.charFormat().property(P_KIND) == "gap" and f.position() < pos < f.position() + f.length():
                return f
        return None

    def _on_change(self, pos, removed, added):
        if self._busy:
            return
        self._typed = True
        # restyle after Qt has finished applying the edit, never inside the signal
        lo, hi = self._pending if self._pending else (pos, pos + added)
        self._pending = (min(lo, pos), max(hi, pos + added))
        self._restyle_timer.start()

    def _restyle(self):
        if not self._pending:
            return
        pos, stop = self._pending
        self._pending = None
        doc = self.document()
        b = doc.findBlock(pos)
        end = doc.findBlock(min(stop, max(doc.characterCount() - 1, 0)))
        self._busy = True
        try:
            while b.isValid():
                for f in list(self._frags(b)):
                    fmt = f.charFormat()
                    if fmt.property(P_KIND) != "gap":
                        continue
                    page = fmt.property(P_PAGE)
                    want = "gap_open" if f.text() == gap_marker(page) else "gap_filled"
                    if fmt.background().color().name() != STYLES[want]["bg"]:
                        c = QTextCursor(doc)
                        c.setPosition(f.position())
                        c.setPosition(f.position() + f.length(), QTextCursor.KeepAnchor)
                        c.joinPreviousEditBlock()
                        c.setCharFormat(_styled(want, page))
                        c.endEditBlock()
                if not end.isValid() or b == end:
                    break
                b = b.next()
        finally:
            self._busy = False
        self._count_timer.start()

    def flush(self):
        """Apply any pending restyle now (before save/export)."""
        self._restyle_timer.stop()
        self._restyle()

    def _on_cursor(self):
        if self._busy:
            return
        typed, self._typed = self._typed, False
        c = self.textCursor()
        if c.hasSelection() or typed:
            return
        pos = c.position()
        f = self._gap_frag_at(pos)
        if f is not None and f.text() == gap_marker(f.charFormat().property(P_PAGE)):
            # clicking into an open gap selects it whole, so typing replaces the marker
            self.select_fragment(f)
            return
        self._plain_after_marker()

    _NAV_KEYS = {Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End,
                 Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Escape}

    def keyPressEvent(self, e):
        super().keyPressEvent(e)
        if e.key() in self._NAV_KEYS and not self.textCursor().hasSelection():
            self._typed = False
            self._plain_after_marker()   # navigating always ends a fill, even if the caret didn't move

    def _plain_after_marker(self):
        c = self.textCursor()
        before = c.charFormat()
        if before.property(P_KIND) in ("gap", "figmark"):
            nxt = QTextCursor(c)
            moved = nxt.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor)
            inside = moved and nxt.charFormat().property(P_KIND) == before.property(P_KIND) \
                and not nxt.selectedText().startswith(" ")
            if not inside:
                # caret just after a marker: new typing should be plain text
                self.setCurrentCharFormat(_plain())

    def select_fragment(self, f):
        c = QTextCursor(self.document())
        c.setPosition(f.position())
        c.setPosition(f.position() + f.length(), QTextCursor.KeepAnchor)
        self._busy = True
        self.setTextCursor(c)
        self._busy = False
        self.ensureCursorVisible()

    def set_gap_colors(self, open_bg, filled_bg):
        """Re-colour every gap now (Settings › gap colours). Leaves the dirty flag alone."""
        set_gap_style_colors(open_bg, filled_bg)
        if not self.loaded:
            return
        doc = self.document()
        was_modified = doc.isModified()
        self._busy = True
        try:
            c = QTextCursor(doc)
            c.beginEditBlock()
            for f in list(self.gap_fragments()):
                page = f.charFormat().property(P_PAGE)
                kind = "gap_open" if f.text() == gap_marker(page) else "gap_filled"
                c.setPosition(f.position())
                c.setPosition(f.position() + f.length(), QTextCursor.KeepAnchor)
                c.setCharFormat(_styled(kind, page))
            c.endEditBlock()
        finally:
            self._busy = False
        doc.setModified(was_modified)

    def gap_fragments(self):
        b = self.document().begin()
        while b.isValid():
            for f in self._frags(b):
                if f.charFormat().property(P_KIND) == "gap":
                    yield f
            b = b.next()

    def gap_counts(self):
        open_n = filled = 0
        for f in self.gap_fragments():
            if f.text() == gap_marker(f.charFormat().property(P_PAGE)):
                open_n += 1
            else:
                filled += 1
        return open_n, filled

    def _emit_counts(self):
        self.gapsChanged.emit(*self.gap_counts())

    def goto_gap(self, forward=True):
        here = self.textCursor().selectionEnd() if forward else self.textCursor().selectionStart()
        opens = [f for f in self.gap_fragments()
                 if f.text() == gap_marker(f.charFormat().property(P_PAGE))]
        if not opens:
            return False
        if forward:
            nxt = next((f for f in opens if f.position() >= here), opens[0])
        else:
            nxt = next((f for f in reversed(opens) if f.position() + f.length() <= here), opens[-1])
        self.select_fragment(nxt)
        return True

    # ---------- back to the page model ----------
    def pages(self):
        pages, pr = [], None
        doc = self.document()
        b = doc.begin()
        while b.isValid():
            bf = b.blockFormat()
            kind = bf.property(B_KIND)
            page = bf.property(P_PAGE)
            if kind == "page" and (pr is None or page != pr.number):
                pr = PageResult(int(page), bf.property(B_METHOD) or "", [])
                pages.append(pr)
                b = b.next()
                continue
            if pr is None:  # text typed above the first page heading
                pr = PageResult(1, "", [])
                pages.append(pr)
            if kind == "label" and pr.items and isinstance(pr.items[-1], Figure):
                pr.items[-1].labels.append(b.text().removeprefix("Text in figure: ").strip())
                b = b.next()
                continue
            segs = []
            src = "ocr" if kind == "ocr" else "layer"

            def flush():
                nonlocal segs
                cleaned = _tidy(segs)
                if cleaned:
                    pr.items.append(Para(pr.number, (0, 0, 0, 0), cleaned, src))
                segs = []

            for f in self._frags(b):
                fmt = f.charFormat()
                if fmt.isImageFormat():
                    flush()
                    name = fmt.toImageFormat().name()
                    png = self.images.get(name)
                    if png:
                        pr.items.append(Figure(pr.number, (0, 0, 0, 0), png, []))
                    continue
                k = fmt.property(P_KIND)
                text = f.text().replace(" ", " ").replace("￼", "")
                if k == "figmark":
                    continue
                if k == "gap":
                    gp = fmt.property(P_PAGE)
                    segs.append(GAP if text == gap_marker(gp) else Filled(text.strip()))
                else:
                    segs.append(text)
            flush()
            b = b.next()
        return pages

    def is_dirty(self):
        return self.document().isModified()

    def mark_saved(self):
        self.document().setModified(False)


def _tidy(segs):
    """Merge neighbouring text, trim the spaces that only separated markers, drop empties."""
    out = []
    for s in segs:
        if isinstance(s, Filled) and not s.text:
            continue
        if isinstance(s, str) and out and isinstance(out[-1], str):
            out[-1] += s
        else:
            out.append(s)
    res = []
    for s in out:
        if isinstance(s, str):
            s = s.strip()
            if not s:
                continue
        res.append(s)
    return res