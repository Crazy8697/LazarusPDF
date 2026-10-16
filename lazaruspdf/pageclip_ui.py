"""Stage 17: pages between tabs. Copy / Cut / Paste of selected pages (thumbnails or Overview),
Paste Pages as New PDF, page drags onto another tab (hovering a tab switches to it), onto the
thumbnails / Overview of another tab and between the two compare panes, and the page
right-click menu of the Pages panel and Overview.

The clipboard is internal (pageclip.Clip): page data, never a path. A paste is ONE undo step in
the tab it lands in (op "paste_pages"); a cut is ONE undo step in the tab it came from
("cut_pages"); the two histories are independent. Pages of a PDF whose permissions don't allow
copying (and whose owner password isn't known) can't be copied, cut or dragged out of it (the
stage 11 / 16 rule), with the reason shown."""
import pymupdf
from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QKeySequence
from PySide6.QtWidgets import QGraphicsRectItem, QMenu, QMessageBox

from . import APP_NAME, outline as O, pageclip
from .pdfeditor import DROP_BLUE, PAGE_GAP, page_mime, page_payload

HOVER_MS = 350           # hovering a tab with dragged pages switches to it after this long

BLOCKED = ("This PDF's security settings don't allow copying its content, and its owner password isn't known "
           "here, so its pages can't be copied, cut or dragged into another PDF. Open it again with the owner "
           "password to copy them.")


def copy_blocked(work):
    """Why pages of this document can't be copied out (or None): the stage 11 / 16 rule."""
    lock = getattr(work, "lock", None)
    if lock is None or lock.owner_known or lock.permissions & pymupdf.PDF_PERM_COPY:
        return None
    return BLOCKED


def _delete_fn(doc, pages):
    """Delete pages and keep the bookmarks pointing at the pages that stay (pageops.delete's way)."""
    n = doc.page_count
    toc0 = O.read(doc)
    gone = set(pages)
    mapping, k = {}, 0
    for p in range(n):
        mapping[p] = None if p in gone else k
        k += p not in gone

    def fn(d):
        d.delete_pages(sorted(gone))
        if toc0:
            O.write(d, O.remap(toc0, mapping))
    return fn


class PageClipController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.ed = win.editor_mode
        self.ov = win.overview_mode
        self.ops = win.pageops
        self.clip = None
        self.last = {}                     # what the last copy / cut / paste did (the self-test reads it)
        self._hover = None                 # (tab index) a page drag is hovering
        self._hover_timer = QTimer(self, singleShot=True, interval=HOVER_MS)
        self._hover_timer.timeout.connect(self._hover_switch)
        self._pane_bar = {}

        mk = lambda text, fn, tip, key=None: self._act(text, fn, tip, key)    # noqa: E731
        self.act_copy = mk("Copy pages", self.copy, "Copy the selected pages (to paste into any open tab)",
                           QKeySequence.Copy)
        self.act_cut = mk("Cut pages", self.cut, "Cut the selected pages (one undo step here)", QKeySequence.Cut)
        self.act_paste = mk("Paste pages", lambda: self.paste(after=True),
                            "Paste the copied pages after the selected page (one undo step)", QKeySequence.Paste)
        self.act_paste_before = mk("Paste pages before", lambda: self.paste(after=False),
                                   "Paste the copied pages before the selected page")
        self.act_paste_after = mk("Paste pages after", lambda: self.paste(after=True),
                                  "Paste the copied pages after the selected page")
        self.act_new = mk("Paste Pages as New PDF", self.paste_new,
                          "Open the copied pages as a new, unsaved PDF tab")
        self.act_bookmarks = QAction("Bring bookmarks when pasting", win, checkable=True)
        self.act_bookmarks.setToolTip("Pasted pages bring the bookmarks that point at them (off: pages only)")
        self.act_bookmarks.setChecked(False)
        page_menu = next((m for m in self.ed.menus if m.title() == "Page"), None)
        if page_menu is not None:
            page_menu.addSeparator()
            for a in (self.act_copy, self.act_cut, self.act_paste_before, self.act_paste_after, self.act_bookmarks):
                page_menu.addAction(a)
        fm = getattr(win, "file_menu", None)
        if fm is not None:
            fm.insertAction(win.scanner.act_scan, self.act_new)

        # keys: Ctrl+C / X / V act on pages while the thumbnails or Overview have the focus
        self._key_widgets = [self.ed.thumbs, self.ov.grid, self.ov.grid.viewport()]
        for w in self._key_widgets:
            w.installEventFilter(self)
        # drags
        sid = lambda: self.ed.session or None     # noqa: E731
        self.ed.thumbs.origin = sid
        self.ov.grid.origin = sid
        self.ed.thumbs.pagesDropped.connect(lambda p, k: self.drop(p, self.ed.cur, k))
        self.ov.grid.pagesDropped.connect(lambda p, k: self.drop(p, self.ed.cur, k))
        tb = self.ed.tabbar
        tb.setAcceptDrops(True)
        tb.installEventFilter(self)
        self.panes = self.ed.compare.panes
        for p in self.panes:
            p.setAcceptDrops(True)
            p.viewport().setAcceptDrops(True)
            p.viewport().installEventFilter(self)
        # right-click menus
        for w in (self.ed.thumbs, self.ov.grid):
            w.setContextMenuPolicy(Qt.CustomContextMenu)
        self.ed.thumbs.customContextMenuRequested.connect(self._thumb_menu)
        self.ov.grid.customContextMenuRequested.connect(self._grid_menu)
        self.ed.pagesChanged.connect(lambda _s: self.update_enabled())
        self.ed.sessionChanged.connect(self.update_enabled)
        self.update_enabled()
        # event filters on widgets this object doesn't own must go before Python shuts down, or
        # the interpreter's teardown can call into a half-destroyed filter (an access violation)
        import atexit
        from PySide6.QtWidgets import QApplication
        self._filtered = self._key_widgets + [tb] + [p.viewport() for p in self.panes]
        QApplication.instance().aboutToQuit.connect(self.detach)
        atexit.register(self.detach)

    def detach(self):
        for o in getattr(self, "_filtered", []):
            try:
                o.removeEventFilter(self)
            except RuntimeError:               # already deleted
                pass
        self._filtered = []

    def _act(self, text, fn, tip, key=None):
        a = QAction(text, self.win, triggered=lambda _=False: fn())
        a.setToolTip(tip)
        a.setStatusTip(tip)
        if key is not None:
            a.setShortcut(key)                 # shown in the menus; the keys themselves: eventFilter
            a.setShortcutContext(Qt.WidgetShortcut)
        return a

    def update_enabled(self):
        doc = self.ed.doc
        has = doc is not None
        k = len(self.ops.selection()) if has else 0
        self.act_copy.setEnabled(k > 0)
        self.act_cut.setEnabled(0 < k < (doc.page_count if has else 0))
        for a in (self.act_paste, self.act_paste_before, self.act_paste_after):
            a.setEnabled(has and self.clip is not None)
        self.act_new.setEnabled(self.clip is not None)

    # ---------- the source: the active tab's selected pages ----------
    def _source(self):
        s = self.ed.cur
        if s is None or not s.ready or s.is_job:
            return None, []
        return s, self.ops.selection()

    def _session(self, sid):
        return next((x for x in self.ed.sessions if x.sid == sid and x.ready and not x.is_job), None)

    def _blocked(self, s):
        why = copy_blocked(s.work)
        if why:
            self.last = {"op": "blocked"}
            QMessageBox.warning(self.win, APP_NAME, why)
        return why

    def copy(self):
        s, pages = self._source()
        if not pages or self._blocked(s):
            return False
        self.clip = pageclip.copy_pages(s.work.doc, pages, origin=s.sid)
        self.last = {"op": "copy", "n": len(pages)}
        self.win.statusBar().showMessage(f"Copied {len(pages)} page(s). Ctrl+V pastes them into any open tab.", 5000)
        self.update_enabled()
        return True

    def cut(self):
        s, pages = self._source()
        if not pages or self._blocked(s):
            return False
        if len(pages) >= s.work.doc.page_count:
            QMessageBox.warning(self.win, APP_NAME, "Can't cut every page; a PDF needs at least one. "
                                                    "Copy them instead.")
            return False
        clip = pageclip.copy_pages(s.work.doc, pages, origin=s.sid)
        fn = _delete_fn(s.work.doc, pages)
        if not self._edit(s, "cut_pages", f"Cut {len(pages)} page(s)", fn,
                          [min(min(pages), s.work.doc.page_count - len(pages) - 1)]):
            return False
        self.clip = clip
        self.last = {"op": "cut", "n": len(pages)}
        self.win.statusBar().showMessage(f"Cut {len(pages)} page(s). Ctrl+V pastes them; Ctrl+Z here puts them back.",
                                         5000)
        self.update_enabled()
        return True

    # ---------- paste ----------
    def _anchor(self, s, after):
        """Where a paste lands in the active tab: after (or before) the selection; no selection:
        after the current page (or before it)."""
        sel = self.ops.selection() if s is self.ed.cur else []
        n = s.work.doc.page_count
        if not sel:
            cur = max(0, s.canvas.current_page())
            return min(n, cur + 1) if after else cur
        return max(sel) + 1 if after else min(sel)

    def paste(self, after=True, at=None, session=None):
        s = session or self.ed.cur
        if self.clip is None or s is None or not s.ready or s.is_job:
            return False
        if at is None:
            at = self._anchor(s, after)
        return self.paste_clip(self.clip, s, at)

    def paste_clip(self, clip, s, at):
        """Paste clip into session s before page index at: ONE undo step there."""
        at = max(0, min(at, s.work.doc.page_count))
        toc = clip.toc if self.act_bookmarks.isChecked() else None
        cd = clip.doc()
        out = {}
        try:
            ok = self._edit(s, "paste_pages", f"Paste {clip.n} page(s)",
                            lambda d: out.setdefault("renames", pageclip.paste(d, cd, at, toc)),
                            list(range(at, at + clip.n)))
        finally:
            cd.close()
        if not ok:
            return False
        renames = out.get("renames") or []
        self.last = {"op": "paste", "n": clip.n, "at": at, "renames": renames, "bookmarks": bool(toc)}
        self.win.statusBar().showMessage(f"Pasted {clip.n} page(s). Ctrl+Z undoes it.", 5000)
        if renames:                            # said once per paste
            lines = "\n".join(f"{a}  →  {b}" for a, b in renames[:12])
            more = f"\n(and {len(renames) - 12} more)" if len(renames) > 12 else ""
            QMessageBox.information(self.win, APP_NAME,
                                    f"{len(renames)} pasted form field(s) had a name already used in this PDF "
                                    f"and were renamed, so the fields stay separate:\n\n{lines}{more}")
        return True

    def paste_new(self):
        """The copied pages as a NEW unsaved tab (bookmarks if "Bring bookmarks" is on)."""
        if self.clip is None:
            return None
        d = self.clip.doc()
        try:
            toc = self.clip.toc if self.act_bookmarks.isChecked() else []
            d.set_toc(O.to_set_toc(toc) if toc else [])
            data = d.tobytes(garbage=1)
        finally:
            d.close()
        s = self.ed.open_built(data, "Pasted pages.pdf", "Pages pasted as a new PDF (not saved yet)")
        self.last = {"op": "paste_new", "n": self.clip.n}
        return s

    # ---------- one edit on any open tab, then its views ----------
    def _edit(self, s, op, label, fn, select):
        from . import oprules
        assert oprules.kind(op) == oprules.JOURNAL
        try:
            s.work.edit(label, fn, pages="structural")
        except Exception as e:                 # rolled back by WorkingDoc.edit: nothing changed
            QMessageBox.warning(self.win, APP_NAME, f"That couldn't be done:\n{e}")
            return False
        s.work.doc._reset_page_refs()
        if s is self.ed.cur:
            self.ed.rebuild(select)
        else:                                  # e.g. the right compare pane: its tab's views follow
            c = s.canvas
            zoom, cur = c.zoom, max(0, c.current_page())
            s.doc_version += 1
            c.set_document(s.work.doc)
            c.set_zoom(zoom)
            c.goto(min(cur, s.work.doc.page_count - 1))
            if self.ed.comparing():
                self.ed.compare.refresh(s)
            self.ed._status()
            self.win.update_title()
        return True

    # ---------- drops ----------
    def drop(self, payload, dest, at):
        """Pages dragged from a tab (payload: sid + pages) dropped into session dest before at:
        a copy (the source tab is untouched)."""
        src = self._session(payload.get("sid"))
        if src is None or dest is None or not dest.ready or dest.is_job or not payload.get("pages"):
            return False
        if self._blocked(src):
            return False
        n = src.work.doc.page_count
        pages = [p for p in payload["pages"] if 0 <= p < n]
        if not pages:
            return False
        clip = pageclip.copy_pages(src.work.doc, pages, origin=src.sid)
        return self.paste_clip(clip, dest, at)

    def _hover_switch(self):
        i = self._hover
        if i is not None and 0 <= i < len(self.ed.sessions):
            s = self.ed.sessions[i]
            if s is not self.ed.cur and s.ready and not s.is_job:
                self.ed.activate(s)

    def eventFilter(self, obj, e):
        t = e.type()
        if obj in self._key_widgets and t in (QEvent.ShortcutOverride, QEvent.KeyPress):
            for seq, fn in ((QKeySequence.Copy, self.copy), (QKeySequence.Cut, self.cut),
                            (QKeySequence.Paste, lambda: self.paste(after=True))):
                if e.matches(seq):
                    e.accept()
                    if t == QEvent.KeyPress:
                        fn()
                    return True
            return False
        if obj is self.ed.tabbar:
            return self._tab_drag(e)
        for k, p in enumerate(self.panes):
            if obj is p.viewport():
                return self._pane_event(k, p, e)
        return False

    def _tab_drag(self, e):
        t = e.type()
        if t not in (QEvent.DragEnter, QEvent.DragMove, QEvent.DragLeave, QEvent.Drop):
            return False
        if t == QEvent.DragLeave:
            self._hover = None
            self._hover_timer.stop()
            return False
        p = page_payload(e.mimeData())
        if p is None:
            return False                       # not ours: file drops go on to the main window
        i = self.ed.tabbar.tabAt(e.position().toPoint())
        e.setDropAction(Qt.CopyAction)
        e.accept()
        if t in (QEvent.DragEnter, QEvent.DragMove):
            if i != self._hover:
                self._hover = i
                self._hover_timer.start()
            return True
        self._hover = None
        self._hover_timer.stop()
        if 0 <= i < len(self.ed.sessions):     # dropped on a tab: the pages go at its end
            s = self.ed.sessions[i]
            if s.ready and not s.is_job and s.sid != p["sid"]:
                self.drop(p, s, s.work.doc.page_count)
        return True

    # ---------- compare panes ----------
    def _pane_gap(self, pane, pos):
        """Insert-before index and the scene y of the insertion line for viewport pos."""
        pt = pane.mapToScene(pos)
        pages = pane.pages
        for i, pg in enumerate(pages):
            r = pg["rect"]
            if pt.y() < r.center().y():
                return i, r.top() - PAGE_GAP / 2
        if not pages:
            return 0, 0
        return len(pages), pages[-1]["rect"].bottom() + PAGE_GAP / 2

    def _pane_line(self, k, pane, y):
        bar = self._pane_bar.get(k)
        if bar is None or bar.scene() is not pane.scene():
            bar = QGraphicsRectItem()
            bar.setBrush(QColor(DROP_BLUE))
            bar.setPen(Qt.NoPen)
            bar.setZValue(50)
            pane.scene().addItem(bar)
            self._pane_bar[k] = bar
        w = max((pg["rect"].width() for pg in pane.pages), default=400)
        x = min((pg["rect"].left() for pg in pane.pages), default=0)
        bar.setRect(QRectF(x, y - 3, w, 6))
        bar.show()

    def _pane_hide(self, k):
        bar = self._pane_bar.get(k)
        if bar is not None and bar.scene() is not None:
            bar.hide()

    def _pane_event(self, k, pane, e):
        t = e.type()
        if t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton and e.modifiers() & Qt.AltModifier:
            i = pane.page_at(pane.mapToScene(e.position().toPoint()))
            s = self.ed.compare.sessions[k]
            if i is not None and s is not None:
                self._pane_drag_start = (k, i, e.position().toPoint())
                return True
            return False
        if t == QEvent.MouseMove and getattr(self, "_pane_drag_start", None):
            k0, i, start = self._pane_drag_start
            if (e.position().toPoint() - start).manhattanLength() > 6:
                self._pane_drag_start = None
                self.drag_from_pane(k0, i)
            return True
        if t == QEvent.MouseButtonRelease and getattr(self, "_pane_drag_start", None):
            self._pane_drag_start = None
            return True
        if t == QEvent.DragLeave:
            self._pane_hide(k)
            return False
        if t not in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            return False
        p = page_payload(e.mimeData())
        s = self.ed.compare.sessions[k]
        if p is None or s is None or not s.ready:
            return False
        e.setDropAction(Qt.CopyAction)
        e.accept()
        at, y = self._pane_gap(pane, e.position().toPoint())
        if t == QEvent.Drop:
            self._pane_hide(k)
            self.drop(p, s, at)
        else:
            self._pane_line(k, pane, y)
        return True

    def pane_mime(self, k, page):
        s = self.ed.compare.sessions[k]
        return page_mime(s.sid if s is not None else None, [page])

    def drag_from_pane(self, k, page):
        """Alt+drag a page in a compare pane: drop it on the other pane (or a tab, the thumbnails)."""
        from PySide6.QtGui import QDrag
        drag = QDrag(self.panes[k].viewport())
        drag.setMimeData(self.pane_mime(k, page))
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)
        for j in range(2):
            self._pane_hide(j)

    # ---------- the page right-click menu ----------
    def page_menu(self):
        """The same menu for the Pages panel and Overview (the selection is already set)."""
        ops, win = self.ops, self.win
        self.update_enabled()
        m = QMenu(win)
        for a in (self.act_copy, self.act_cut, self.act_paste_before, self.act_paste_after):
            m.addAction(a)
        m.addSeparator()
        m.addAction("Duplicate", ops.duplicate)
        m.addAction("Rotate left", lambda: ops.rotate(-90))
        m.addAction("Rotate right", lambda: ops.rotate(90))
        m.addAction("Delete…", ops.delete_selected)
        m.addAction("Extract to new PDF…", ops.extract)
        m.addSeparator()
        m.addAction("Insert blank page after", lambda: ops.insert_blank(after=True))
        m.addAction("Insert from file…", lambda: ops.insert_pdf(after=True))
        m.addAction("Insert images…", win.images.act_insert.trigger)
        m.addAction("Insert scan…", win.scanner.act_insert.trigger)
        n = len(ops.selection())
        total = self.ed.doc.page_count if self.ed.doc is not None else 0
        for a in m.actions():
            if a.text() in ("Duplicate", "Rotate left", "Rotate right", "Extract to new PDF…"):
                a.setEnabled(n > 0)
            elif a.text() == "Delete…":
                a.setEnabled(0 < n < total)
        return m

    def _thumb_menu(self, pos):
        th = self.ed.thumbs
        if self.ed.doc is None:
            return
        it = th.itemAt(pos)
        if it is not None and not it.isSelected():
            th.clearSelection()
            it.setSelected(True)
            th.setCurrentItem(it)
        self.page_menu().popup(th.viewport().mapToGlobal(pos))

    def _grid_menu(self, pos):
        g = self.ov.grid
        if g.doc is None:
            return
        i = g.index_at(g.mapToScene(pos))
        if i is not None and i not in g.selected:
            g.set_selection([i])
        self.page_menu().popup(g.viewport().mapToGlobal(QPoint(pos)))
