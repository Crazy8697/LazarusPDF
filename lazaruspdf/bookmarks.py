"""Bookmarks panel (PDF outline) for the PDF Editor and Overview.

The tree mirrors the working copy's outline (nested levels kept). Every edit (add, rename,
delete, drag to reorder or nest / un-nest, change target page) rebuilds the outline from
the tree and writes it as ONE journal operation (outline.write), so Ctrl+Z / Ctrl+Y undo
it and the unsaved flag is set. The original file is never written; bookmarks go out
with the next save, to the new file."""
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QDockWidget,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPushButton, QSpinBox, QToolBar, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import outline as O
from .pdfeditor import _icon

PAGE = Qt.UserRole
DEST = Qt.UserRole + 1
OUTLINE = "outline"            # WorkingDoc op marker: only the outline changed


class BookmarkTree(QTreeWidget):
    structureChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setHeaderLabels(["Bookmark", "Page"])
        self.setColumnCount(2)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, self.header().ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, self.header().ResizeMode.ResizeToContents)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setEditTriggers(QAbstractItemView.EditKeyPressed | QAbstractItemView.SelectedClicked)
        self.setUniformRowHeights(True)

    def dropEvent(self, e):
        super().dropEvent(e)             # Qt moves the item (reorder, or nest onto an item)
        if e.isAccepted():
            self.structureChanged.emit()


class BookmarksPanel(QObject):
    def __init__(self, editor, win):
        super().__init__(editor)
        self.ed, self.win = editor, win
        self._loading = False
        self.tree = BookmarkTree()
        self.tree.itemClicked.connect(self._clicked)
        self.tree.itemChanged.connect(self._renamed)
        self.tree.structureChanged.connect(lambda: self.commit("Move bookmark"))
        self.tree.currentItemChanged.connect(lambda *_: self.update_enabled())

        self.act_add = QAction(_icon("bookmark-plus-outline"), "Bookmark this page", win,
                               shortcut="Ctrl+B")
        # triggered(bool checked) would land in add(page=...); call it with no arguments
        self.act_add.triggered.connect(lambda _=False: self.add())
        self.act_add.setToolTip("Add a bookmark for the current page (Overview: the selected page)  Ctrl+B")
        self.act_rename = QAction(_icon("pencil-outline"), "Rename bookmark", win, triggered=self.rename)
        self.act_rename.setToolTip("Rename (F2)")
        self.act_delete = QAction(_icon("bookmark-remove-outline"), "Delete bookmark", win, triggered=self.delete)
        self.act_delete.setToolTip("Delete the bookmark and the bookmarks nested under it")
        self.act_here = QAction(_icon("crosshairs-gps"), "Point at current page", win, triggered=self.retarget_here)
        self.act_here.setToolTip("Change the bookmark's target to the current page (Overview: the selected page)")
        self.act_goto = QAction(_icon("file-document-arrow-right-outline"), "Set target page…", win,
                                triggered=self.retarget_dialog)
        self.act_goto.setToolTip("Change the bookmark's target page by number")
        self.act_auto = QAction(_icon("auto-fix"), "Auto-generate bookmarks…", win,
                                triggered=lambda _=False: self.auto_generate())
        self.act_auto.setToolTip("Propose bookmarks from the headings (font size / style) or a contents page; "
                                 "review and edit them before applying")

        bar = QToolBar()
        bar.setIconSize(bar.iconSize() * 0.9)
        for a in (self.act_add, self.act_rename, self.act_delete, self.act_here, self.act_goto):
            bar.addAction(a)
        bar.addSeparator()
        bar.addAction(self.act_auto)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)
        v.addWidget(bar)
        v.addWidget(self.tree, 1)
        self.dock = QDockWidget("Bookmarks", win)
        self.dock.setObjectName("bookmarksdock")
        self.dock.setWidget(body)
        self.update_enabled()

    # ---------- tree <-> outline ----------
    def reload(self):
        """Rebuild the tree from the working copy's outline (after any change / undo)."""
        path = self._path(self.tree.currentItem())
        self._loading = True
        self.tree.clear()
        doc = self.ed.doc
        parents = [self.tree.invisibleRootItem()]
        if doc is not None:
            for e in O.read(doc):
                lvl, title, page = e[0], e[1], e[2]
                dest = e[3] if len(e) > 3 and isinstance(e[3], dict) else {}
                del parents[lvl:]
                parent = parents[-1] if parents else self.tree.invisibleRootItem()
                it = QTreeWidgetItem(parent, [str(title), str(page) if page and page > 0 else "—"])
                it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
                it.setData(0, PAGE, page)
                it.setData(0, DEST, {k: v for k, v in dest.items() if k in ("to", "color", "bold", "italic",
                                                                         "collapse", "kind", "uri")})
                it.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                parents.append(it)
        self.tree.expandAll()
        self._loading = False
        self._select_path(path)
        self.update_enabled()

    def state(self):
        """Expand/collapse + current item, saved per tab."""
        collapsed = []

        def walk(item):
            for k in range(item.childCount()):
                c = item.child(k)
                if c.childCount() and not c.isExpanded():
                    collapsed.append(self._path(c))
                walk(c)
        walk(self.tree.invisibleRootItem())
        return {"current": self._path(self.tree.currentItem()), "collapsed": collapsed}

    def restore_state(self, st):
        if not st:
            return
        for p in st["collapsed"]:
            it = self._item(p)
            if it is not None:
                it.setExpanded(False)
        self._select_path(st["current"])

    def to_toc(self):
        toc = []

        def walk(item, depth):
            for k in range(item.childCount()):
                c = item.child(k)
                toc.append([depth, c.text(0), c.data(0, PAGE), dict(c.data(0, DEST) or {})])
                walk(c, depth + 1)
        walk(self.tree.invisibleRootItem(), 1)
        return toc

    def commit(self, name, select=None):
        """Write the tree as the document's outline: one undoable operation."""
        work = self.ed.work
        if work is None or self._loading:
            return
        toc = self.to_toc()
        work.edit(name, lambda d: O.write(d, toc), pages=OUTLINE)
        self.ed._changed(False)            # unsaved flag, undo/redo enable, title
        self.reload()
        if select is not None:
            self._select_path(select)
        self.win.statusBar().showMessage(f"{name}. Ctrl+Z undoes it.", 4000)

    # ---------- paths (to keep the selection across reloads) ----------
    def _path(self, item):
        p = []
        while item is not None:
            parent = item.parent() or self.tree.invisibleRootItem()
            p.insert(0, parent.indexOfChild(item))
            item = item.parent()
        return p or None

    def _item(self, path):
        it = self.tree.invisibleRootItem()
        for k in path or []:
            if k >= it.childCount():
                return None
            it = it.child(k)
        return None if it is self.tree.invisibleRootItem() else it

    def _select_path(self, path):
        it = self._item(path)
        if it is not None:
            self.tree.setCurrentItem(it)

    # ---------- actions ----------
    def target_page(self):
        """0-based page a new/retargeted bookmark points at."""
        if self.ed.doc is None:
            return None
        if self.win.mode == "overview" and self.win.overview_mode.grid.selected:
            return min(self.win.overview_mode.grid.selected)
        return max(0, self.ed.canvas.current_page())

    def update_enabled(self):
        has_doc = self.ed.doc is not None
        cur = self.tree.currentItem()
        mode_ok = getattr(self.win, "mode", None) in ("editor", "overview")
        self.act_add.setEnabled(has_doc and mode_ok)
        self.act_auto.setEnabled(has_doc and mode_ok)
        for a in (self.act_rename, self.act_delete, self.act_here, self.act_goto):
            a.setEnabled(has_doc and cur is not None)

    def auto_generate(self, answer=None):
        """Stage 7a: propose an outline, preview + edit it, then apply it (one undo step). The
        result is ordinary bookmarks: every stage-7 action works on them."""
        from PySide6.QtWidgets import QApplication
        from . import APP_NAME
        from . import autobookmarks as A
        doc = self.ed.doc
        if doc is None:
            return False
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            an = A.analyze(doc)
        finally:
            QApplication.restoreOverrideCursor()
        self.last_analysis = an
        if not an.has_text:
            QMessageBox.information(self.win, APP_NAME,
                                    "This PDF has no text that can be read (a scan?), so there are no headings to "
                                    "find. Run Document › OCR to searchable first, then try again.")
            return False
        existing = O.read(doc)
        if answer is None:
            answer = ask_autobookmarks(self.win, an, existing)
            if answer is None:
                return False
        toc, mode = answer
        final = A.merge(existing, toc, mode)
        if not self.ed.run_edit("outline_write", "Auto-generate bookmarks", lambda d: O.write(d, final), OUTLINE):
            return False
        self.reload()
        self.win.statusBar().showMessage(f"{len(toc)} bookmark(s) generated"
                                         + ({"replace": "", "append": " (added after the existing ones)",
                                             "under": " (nested under \"Generated bookmarks\")"}[mode] if existing else "")
                                         + ". Ctrl+Z undoes it.", 6000)
        return True

    def add(self, page=None, title=None):
        page = self.target_page() if page is None else page
        if page is None:
            return
        cur = self.tree.currentItem()
        self._loading = True
        parent = (cur.parent() if cur is not None else None) or self.tree.invisibleRootItem()
        index = parent.indexOfChild(cur) + 1 if cur is not None else parent.childCount()
        it = QTreeWidgetItem([title or f"Page {page + 1}", str(page + 1)])
        it.setData(0, PAGE, page + 1)
        it.setData(0, DEST, {})
        parent.insertChild(index, it)
        self._loading = False
        path = self._path(it)
        self.commit("Add bookmark", select=path)
        new = self._item(path)
        if new is not None and title is None:
            self.tree.setFocus()
            self.tree.editItem(new, 0)       # type the name right away (Enter commits the rename)

    def rename(self):
        cur = self.tree.currentItem()
        if cur is not None:
            self.tree.editItem(cur, 0)

    def _renamed(self, item, col):
        if self._loading or col != 0:
            return
        self.commit("Rename bookmark", select=self._path(item))

    def delete(self):
        cur = self.tree.currentItem()
        if cur is None:
            return
        parent = cur.parent() or self.tree.invisibleRootItem()
        idx = parent.indexOfChild(cur)
        self._loading = True
        parent.removeChild(cur)
        self._loading = False
        sel = self._path(parent.child(min(idx, parent.childCount() - 1))) if parent.childCount() else self._path(cur.parent())
        self.commit("Delete bookmark", select=sel)

    def retarget(self, page):
        cur = self.tree.currentItem()
        if cur is None or page is None:
            return
        self._loading = True
        cur.setData(0, PAGE, page + 1)
        d = dict(cur.data(0, DEST) or {})
        d.pop("to", None)                    # new page: no old scroll position
        cur.setData(0, DEST, d)
        self._loading = False
        self.commit("Change bookmark target", select=self._path(cur))

    def retarget_here(self):
        self.retarget(self.target_page())

    def retarget_dialog(self):
        cur = self.tree.currentItem()
        if cur is None or self.ed.doc is None:
            return
        n = self.ed.doc.page_count
        start = cur.data(0, PAGE) if (cur.data(0, PAGE) or 0) > 0 else 1
        v, ok = QInputDialog.getInt(self.win, "Bookmark target", f"Point this bookmark at page (1–{n}):",
                                    start, 1, n)
        if ok:
            self.retarget(v - 1)

    def _clicked(self, item, col):
        page = item.data(0, PAGE)
        if not page or page < 1 or self.ed.doc is None:
            return
        self.ed.canvas.goto(page - 1)                   # jump in the editor
        grid = self.win.overview_mode.grid               # and highlight the page in Overview
        if grid.doc is not None and page - 1 < len(grid.items):
            grid.set_selection([page - 1])
            grid.ensureVisible(grid.items[page - 1]["frame"])


# ---------------- stage 7a: auto-generate (preview dialog) ----------------
class AutoBookmarksDialog(QDialog):
    """Proposed bookmarks in an editable tree before anything is applied: rename (double-click /
    F2), drag to reorder or re-nest, Remove / Indent / Outdent. The controls choose the method
    and which heading styles count (changing them regenerates the tree)."""
    LEVEL_ROLE = Qt.UserRole + 1

    def __init__(self, parent, analysis, existing):
        super().__init__(parent)
        from . import autobookmarks as A
        self.A, self.an, self.existing = A, analysis, existing
        self.setWindowTitle("Auto-generate bookmarks")
        self.resize(760, 620)
        v = QVBoxLayout(self)
        top = QFormLayout()
        self.method = QComboBox()
        self.method.addItem("Headings (font size and style)", "headings")
        if analysis.toc_page is not None:
            self.method.addItem(f"Table of contents on page {analysis.toc_page + 1}", "toc")
        top.addRow("Find them from", self.method)
        self.levels = QSpinBox()
        self.levels.setRange(1, 6)
        self.levels.setValue(3)
        self.delta = QDoubleSpinBox()
        self.delta.setRange(0.0, 20.0)
        self.delta.setSingleStep(0.5)
        self.delta.setValue(0.5)
        self.delta.setSuffix(" pt above body text")
        top.addRow("Heading levels", self.levels)
        top.addRow("Larger by at least", self.delta)
        v.addLayout(top)
        self.styles = QListWidget()
        self.styles.setMaximumHeight(110)
        v.addWidget(QLabel(f"Heading styles found (body text is {analysis.body:g} pt); ticked ones are used, "
                           "largest first = level 1:"))
        v.addWidget(self.styles)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Bookmark", "Page"])
        self.tree.setDragDropMode(QAbstractItemView.InternalMove)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setColumnWidth(0, 520)
        v.addWidget(self.tree, 1)
        row = QHBoxLayout()
        for label, fn in (("Remove", self.remove), ("Indent", lambda: self.shift(1)), ("Outdent", lambda: self.shift(-1))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        self.count = QLabel("")
        row.addWidget(self.count)
        v.addLayout(row)
        self.mode = QComboBox()
        if existing:
            self.mode.addItem(f"Replace the {len(existing)} existing bookmark(s)", "replace")
            self.mode.addItem("Merge: add after the existing bookmarks", "append")
            self.mode.addItem("Merge: nest under a new \"Generated bookmarks\" entry", "under")
            mrow = QFormLayout()
            mrow.addRow("This PDF already has bookmarks:", self.mode)
            v.addLayout(mrow)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self.method.currentIndexChanged.connect(self.regenerate)
        self.levels.valueChanged.connect(self._styles_changed)
        self.delta.valueChanged.connect(self._styles_changed)
        self.styles.itemChanged.connect(lambda _it: self.regenerate())
        self._styles_changed()

    # ---------- controls ----------
    def _styles_changed(self, *_):
        self.styles.blockSignals(True)
        self.styles.clear()
        for k, (size, bold, n) in enumerate(self.an.styles(self.delta.value())):
            it = QListWidgetItem(f"{size:g} pt{' bold' if bold else ''}  ({n} line{'s' if n != 1 else ''})")
            it.setData(Qt.UserRole, (size, bold))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if k < self.levels.value() else Qt.Unchecked)
            self.styles.addItem(it)
        self.styles.blockSignals(False)
        self.regenerate()

    def chosen_styles(self):
        return [self.styles.item(i).data(Qt.UserRole) for i in range(self.styles.count())
                if self.styles.item(i).checkState() == Qt.Checked]

    def regenerate(self, *_):
        heading = self.method.currentData() == "headings"
        for w in (self.levels, self.delta, self.styles):
            w.setEnabled(heading)
        if heading:
            toc = self.A.from_headings(self.an, min_delta=self.delta.value(), styles=self.chosen_styles())
        else:
            toc = self.A.from_toc_page(self.an)
        self.load(toc)

    # ---------- the tree ----------
    def load(self, toc):
        self.tree.clear()
        parents = [self.tree.invisibleRootItem()]
        for lvl, title, page, *rest in toc:
            del parents[lvl:]
            parent = parents[-1] if parents else self.tree.invisibleRootItem()
            it = QTreeWidgetItem(parent, [title, str(page)])
            it.setFlags(it.flags() | Qt.ItemIsEditable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
            it.setData(0, Qt.UserRole, page)
            it.setData(1, Qt.UserRole, rest[0] if rest else {})
            parents.append(it)
        self.tree.expandAll()
        self.count.setText(f"{len(toc)} bookmark(s)")

    def toc(self):
        out = []

        def walk(item, depth):
            for k in range(item.childCount()):
                c = item.child(k)
                out.append([depth, c.text(0), c.data(0, Qt.UserRole), dict(c.data(1, Qt.UserRole) or {})])
                walk(c, depth + 1)
        walk(self.tree.invisibleRootItem(), 1)
        return out

    def remove(self):
        for it in self.tree.selectedItems():
            (it.parent() or self.tree.invisibleRootItem()).removeChild(it)
        self.count.setText(f"{len(self.toc())} bookmark(s)")

    def shift(self, d):
        """Indent: become the last child of the previous sibling. Outdent: move up a level."""
        for it in self.tree.selectedItems():
            parent = it.parent() or self.tree.invisibleRootItem()
            i = parent.indexOfChild(it)
            if d > 0 and i > 0:
                prev = parent.child(i - 1)
                parent.takeChild(i)
                prev.addChild(it)
                prev.setExpanded(True)
            elif d < 0 and it.parent() is not None:
                gp = it.parent().parent() or self.tree.invisibleRootItem()
                pi = gp.indexOfChild(it.parent())
                it.parent().takeChild(i)
                gp.insertChild(pi + 1, it)
            self.tree.setCurrentItem(it)

    def result_value(self):
        return self.toc(), (self.mode.currentData() if self.existing else "replace")


def ask_autobookmarks(parent, analysis, existing):
    """-> (outline, "replace" | "append" | "under") or None."""
    d = AutoBookmarksDialog(parent, analysis, existing)
    return d.result_value() if d.exec() == QDialog.Accepted else None
