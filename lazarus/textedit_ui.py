"""Stage 11 UI: the Edit text tool (in place, on the canvas) and Replace in the find bar.

Edit text: pointing at text outlines its block; a click opens a live editor over the block, at
its position, size and rotation, with the block's text. A line under it says what will
happen (a substitute font, covering instead of replacing, shrinking). Ctrl+Enter or a click
outside applies (one undo step), Esc cancels. The engine is textedit.py.

Replace (Ctrl+H): a Replace row under the find bar. Replace changes the current hit; Replace
All… lists every change first (and every hit that will be skipped, with why), then applies
them as ONE undo step. The find index follows the change on its own (it re-reads a changed
document)."""
import math

from PySide6.QtCore import QObject, QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QBrush, QColor, QFont, QKeySequence, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QGraphicsRectItem,
                               QGraphicsTextItem, QGraphicsView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from . import APP_NAME
from . import textedit as TE

HOVER_PEN = QColor("#2d6cdf")
HOVER_FILL = QColor(45, 108, 223, 28)
NOTE_BG = QColor(30, 33, 40, 235)


def edit_blocked(work):
    """A reason editing content is not allowed (the file restricts it and its owner password
    isn't known here), or None."""
    import pymupdf
    lock = getattr(work, "lock", None)
    if lock is None or lock.owner_known:
        return None
    if lock.permissions & pymupdf.PDF_PERM_MODIFY:
        return None
    return ("This PDF's security settings don't allow changing its content, and its owner password isn't known "
            "here. Open it again with the owner password (it is asked for when the file opens) to edit its text.")


def qt_family(code):
    return {"he": "Arial", "ti": "Times New Roman", "co": "Courier New"}[code[:2]]


class EditItem(QGraphicsTextItem):
    """The live editor over a block."""

    def __init__(self, ctl):
        super().__init__()
        self.ctl = ctl
        self.setTextInteractionFlags(Qt.TextEditorInteraction)
        self.document().setDocumentMargin(0)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.ctl.cancel()
            return
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and e.modifiers() & Qt.ControlModifier:
            self.ctl.commit()
            return
        super().keyPressEvent(e)


class TextEditController(QObject):
    def __init__(self, ed, win):
        super().__init__(win)
        self.ed, self.win = ed, win
        self._models = {}
        self._hover = None             # (canvas, item)
        self.editing = None            # {canvas, page, block, model, items, original}
        self.last_result = None        # what the last applied edit reported (tests)
        from .stage10_ui import realize
        a = realize(ed, "edittext", "square-edit-outline",
                    "<b>Edit text</b><br>Point at text to see its block; click to edit it in place. The text keeps "
                    "its font where the file allows (else the closest standard font, with a notice), reflows in its "
                    "box, and the old text is removed. Ctrl+Enter or a click outside applies, Esc cancels.",
                    handler=self.activate)
        a.setCheckable(True)
        ed.markup.group.addAction(a)
        ed.markup.actions["edittext"] = a
        self.action = a
        self._note_timer = QTimer(self, singleShot=True, interval=200)
        self._note_timer.timeout.connect(self._update_note)
        ed.docChanged.connect(self._doc_changed)
        ed.sessionChanged.connect(self.cancel)

    def activate(self):
        self.ed.markup.set_tool("edittext")
        for s in self.ed.sessions:
            if s.canvas is not None:
                s.canvas.viewport().setMouseTracking(True)       # hover outlines the block

    # ---------- the page model (cached per document state) ----------
    def model(self, pno):
        key = (self.ed.session, id(self.ed.doc), self.ed.doc_version, pno)
        m = self._models.get(key)
        if m is None:
            if len(self._models) > 40:
                self._models.clear()
            m = self._models[key] = TE.PageModel(self.ed.doc, pno)
        return m

    def _doc_changed(self):
        self._models.clear()
        self._clear_hover()
        if self.editing is not None and not self.editing.get("committing"):
            self.cancel()

    def leave(self):
        """Another tool was picked."""
        self.cancel()
        self._clear_hover()

    # ---------- canvas events ----------
    def _where(self, c, e):
        sp = c.mapToScene(e.position().toPoint())
        i = c.page_at(sp)
        return sp, i, (c.scene_to_page(i, sp) if i is not None else None)

    def _in_editor(self, sp):
        it = self.editing["items"]["text"] if self.editing else None
        return it is not None and it.sceneBoundingRect().adjusted(-3, -3, 3, 3).contains(sp)

    def press(self, c, e):
        if self.ed.work is None:
            return False
        sp, i, pt = self._where(c, e)
        if self.editing is not None:
            if self._in_editor(sp):
                QGraphicsView.mousePressEvent(c, e)          # the editor gets the click (caret, selection)
                self.editing["mouse"] = True
                return True
            if e.button() == Qt.LeftButton:
                self.commit()                               # a click outside applies
            return True
        if e.button() != Qt.LeftButton or i is None:
            return True
        self.open_at(c, i, pt)
        return True

    def move(self, c, e):
        if self.editing is not None:
            if self.editing.get("mouse"):
                QGraphicsView.mouseMoveEvent(c, e)
            return True
        sp, i, pt = self._where(c, e)
        self._show_hover(c, i, pt)
        return True

    def release(self, c, e):
        if self.editing is not None and self.editing.get("mouse"):
            QGraphicsView.mouseReleaseEvent(c, e)
            self.editing["mouse"] = False
        return True

    def double(self, c, e):
        return False                                        # the view passes it to the editor

    def key(self, c, e):
        if self.editing is None:
            return False
        if e.key() == Qt.Key_Escape:
            self.cancel()
            return True
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and e.modifiers() & Qt.ControlModifier:
            self.commit()
            return True
        return False                                        # typing goes to the editor

    # ---------- hover ----------
    def _clear_hover(self):
        if self._hover is not None:
            c, it = self._hover
            try:
                c.scene().removeItem(it)
            except RuntimeError:
                pass
            self._hover = None

    def _show_hover(self, c, i, pt):
        b = None
        if i is not None:
            try:
                m = self.model(i)
                b = m.block_at(pt)
            except Exception:
                b = None
        if b is None or b.invisible:
            self._clear_hover()
            return
        r = c.page_to_scene(i, m.disp_rect(b)).adjusted(-2, -2, 2, 2)
        if self._hover is not None and self._hover[0] is c:
            self._hover[1].setRect(r)
        else:
            self._clear_hover()
            it = QGraphicsRectItem(r)
            it.setPen(QPen(HOVER_PEN, 0, Qt.DashLine))
            it.setBrush(HOVER_FILL)
            it.setZValue(6)
            c.scene().addItem(it)
            self._hover = (c, it)
        self._hover[1].setToolTip(f"This text {b.problem}: it can be covered and rewritten, not replaced"
                                  if b.problem else "Click to edit this text")

    # ---------- open an editor ----------
    def open_at(self, c, i, pt):
        """Click on page i at displayed point pt: edit the block there (or say why not)."""
        why = edit_blocked(self.ed.work)
        if why:
            QMessageBox.warning(self.win, APP_NAME, why)
            return False
        m = self.model(i)
        b = m.block_at(pt)
        if b is None or (b.invisible and not m.has_visible_text):
            if not m.has_visible_text:
                QMessageBox.information(
                    self.win, APP_NAME,
                    "This page has no text to edit: it looks like a scan (a picture of a page)."
                    + (" It has an invisible OCR text layer, which isn't what the page shows." if m.has_invisible_text
                       else "") + "\n\nDocument › OCR to searchable makes it searchable. To change what the page "
                                  "shows, use Whiteout and the Text tool.")
            self.win.statusBar().showMessage("No text there. Point at text: its block is outlined.", 4000)
            return False
        if b.invisible:
            QMessageBox.warning(self.win, APP_NAME,
                                "That is the page's invisible text layer (added by OCR), not text you can see. "
                                "Editing it wouldn't change what the page shows, so it isn't edited here.\n\n"
                                "To change what the page shows, use Whiteout and the Text tool.")
            return False
        return self.start(c, i, m, b)

    def start(self, c, i, m, b):
        self.cancel()
        self._clear_hover()
        f = b.frame
        disp = m.disp_rect(b)
        page = self.ed.doc[i]
        from .contentedit import sample_background
        try:
            bg = sample_background(page, disp)
        except Exception:
            bg = (1.0, 1.0, 1.0)
        del page
        scene = c.scene()
        cover = QGraphicsRectItem(c.page_to_scene(i, disp).adjusted(-1, -1, 1, 1))
        cover.setBrush(QBrush(QColor.fromRgbF(*bg)))
        cover.setPen(QPen(HOVER_PEN, 0))
        cover.setZValue(19)
        scene.addItem(cover)
        st = b.lines[0].chars[0].style
        text = EditItem(self)
        font = QFont(qt_family(TE.classify(st[0], st[1])))
        font.setPixelSize(max(1, round(st[2])))
        font.setBold(bool(st[1] & 16) or "bold" in st[0].lower())
        font.setItalic(bool(st[1] & 2) or "italic" in st[0].lower())
        text.setFont(font)
        r, g, bl = TE._rgb(st[3])
        text.setDefaultTextColor(QColor.fromRgbF(r, g, bl))
        text.setPlainText(b.text("edit"))
        text.setTextWidth(max(20.0, f.w))
        o = f.page(0, 0) * m.rot                              # the text frame's corner, displayed
        u = f.page(1, 0) * m.rot
        angle = math.degrees(math.atan2(u.y - o.y, u.x - o.x))
        origin = c.page_to_scene(i, (TE.R(o.x, o.y, o.x, o.y))).topLeft()
        text.setPos(origin)
        text.setRotation(angle)
        text.setZValue(20)
        scene.addItem(text)
        note = QGraphicsTextItem()
        note.setDefaultTextColor(QColor("#ffd27f"))
        nf = QFont()
        nf.setPixelSize(9)
        note.setFont(nf)
        note.setZValue(21)
        scene.addItem(note)
        self.editing = {"canvas": c, "page": i, "block": b, "model": m, "original": b.text("edit"),
                        "items": {"cover": cover, "text": text, "note": note}, "disp": disp}
        text.document().contentsChanged.connect(self._note_timer.start)
        self._update_note()
        c.setFocus()
        text.setFocus()
        cur = text.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        text.setTextCursor(cur)
        self.win.statusBar().showMessage("Editing text: Ctrl+Enter or a click outside applies, Esc cancels.", 6000)
        return True

    def _update_note(self):
        e = self.editing
        if e is None:
            return
        txt = e["items"]["text"].toPlainText()
        b, m = e["block"], e["model"]
        notes = []
        try:
            p = TE.plan(m, b, TE.restyle(b.styled("edit"), txt), overflow="grow")
            notes = p["notices"]
        except TE.NoFont as ex:
            notes = [f"Can't be written: {ex}"]
        except TE.EditFailed as ex:
            notes = [str(ex)]
        e["notices"] = notes
        note = e["items"]["note"]
        hint = "Ctrl+Enter or click outside: apply  ·  Esc: cancel"
        note.setPlainText("\n".join(notes + [hint]))
        c, i = e["canvas"], e["page"]
        top = c.page_to_scene(i, e["disp"]).topLeft()
        note.setPos(QPointF(top.x(), top.y() - note.boundingRect().height() - 3))
        bgc = note.boundingRect()
        if "notebg" not in e["items"]:
            bgi = QGraphicsRectItem()
            bgi.setBrush(NOTE_BG)
            bgi.setPen(Qt.NoPen)
            bgi.setZValue(20.5)
            c.scene().addItem(bgi)
            e["items"]["notebg"] = bgi
        e["items"]["notebg"].setRect(note.mapRectToScene(bgc).adjusted(-3, -1, 3, 1))

    def notices(self):
        return list(self.editing.get("notices", [])) if self.editing else []

    def set_text(self, text):
        """Type into the open editor (tests)."""
        if self.editing:
            self.editing["items"]["text"].setPlainText(text)
            self._update_note()

    def cancel(self):
        e = self.editing
        if e is None:
            return
        self.editing = None
        self._note_timer.stop()
        for it in e["items"].values():
            try:
                if it.scene() is not None:
                    it.scene().removeItem(it)
            except RuntimeError:
                pass

    # ---------- apply ----------
    def commit(self, overflow=None, cover=None, ask=None):
        """Apply the edit (one undo step). ask(kind, detail) answers the questions a real user
        gets (tests): kind "overflow" -> "shrink" | "grow" | None, kind "cover" -> bool."""
        e = self.editing
        if e is None:
            return False
        txt = e["items"]["text"].toPlainText()
        if txt == e["original"]:
            self.cancel()
            return False
        b, m, i = e["block"], e["model"], e["page"]
        styled = TE.restyle(b.styled("edit"), txt)
        ask = ask or self._ask
        while True:
            try:
                p = TE.plan(m, b, styled, overflow)
                break
            except TE.Overflow as ex:
                overflow = ask("overflow", str(ex))
                if overflow is None:
                    return False                          # keep editing
            except TE.EditFailed as ex:
                QMessageBox.warning(self.win, APP_NAME, f"That text can't be written: {ex}.")
                return False
        op = "text_cover" if (p["method"] == "cover" or cover) else "text_edit"
        result = {}

        def fn(d):
            result.update(TE.apply_block(d, i, b.fp, new_styled=styled, overflow=overflow, cover=cover))
            return result
        e["committing"] = True
        try:
            ok = self.ed.run_edit(op, "Edit text", fn, [i], raise_errors=True)
        except TE.EditFailed as ex:
            e["committing"] = False
            if cover or not ask("cover", str(ex)):
                QMessageBox.warning(self.win, APP_NAME, f"The text couldn't be changed cleanly ({ex}). Nothing "
                                                        "was changed.")
                return False
            return self.commit(overflow, cover=True, ask=ask)
        except Exception as ex:
            e["committing"] = False
            QMessageBox.warning(self.win, APP_NAME, f"That edit couldn't be made:\n{ex}")
            return False
        self.cancel()
        if ok:
            self.last_result = result
            msg = "Text edited." + (" " + " · ".join(result.get("notices", [])) if result.get("notices") else "")
            self.win.statusBar().showMessage(msg + " Ctrl+Z undoes it.", 8000)
        return ok

    def _ask(self, kind, detail):
        if kind == "overflow":
            box = QMessageBox(self.win)
            box.setWindowTitle(APP_NAME)
            box.setText(f"The new text doesn't fit its box ({detail}).\n\nNothing is ever cut off: make it smaller, "
                        "or let the box grow downward?")
            shrink = box.addButton("Shrink to fit", QMessageBox.AcceptRole)
            grow = box.addButton("Let the box grow", QMessageBox.AcceptRole)
            box.addButton("Keep editing", QMessageBox.RejectRole)
            box.exec()
            return "shrink" if box.clickedButton() is shrink else "grow" if box.clickedButton() is grow else None
        r = QMessageBox.question(self.win, APP_NAME,
                                 f"The text couldn't be replaced cleanly ({detail}).\n\nCover it instead? The new "
                                 "text is written over a patch of the page's own colour; the old text stays in the "
                                 "file underneath (Redact is what removes text).",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return r == QMessageBox.Yes


# ---------------- Replace (find bar) ----------------
class ReplaceController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.fc = win.find
        self.ed = win.editor_mode
        self.last = None                  # (replaced, items) of the last Replace / Replace All (tests)
        from PySide6.QtWidgets import QToolBar
        bar = QToolBar("Replace", win)
        bar.setObjectName("replacebar")
        bar.setMovable(False)
        lab = QLabel(" REPLACE ")
        lab.setObjectName("grouplabel")
        bar.addWidget(lab)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Replace with…  (regex: $1 or \\1 = a group)")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(280)
        self.edit.returnPressed.connect(self.replace_one)
        bar.addWidget(self.edit)
        self.btn_one = QPushButton("Replace")
        self.btn_one.setToolTip("Replace the current hit (one undo step), then go to the next")
        self.btn_one.clicked.connect(self.replace_one)
        bar.addWidget(self.btn_one)
        self.btn_all = QPushButton("Replace All…")
        self.btn_all.setToolTip("List every change first, then replace them all as ONE undo step")
        self.btn_all.clicked.connect(lambda: self.replace_all())
        bar.addWidget(self.btn_all)
        bar.addWidget(QLabel("  Pages:"))
        self.range = QLineEdit("all")
        self.range.setPlaceholderText("all, or e.g. 1-3, 5")
        self.range.setMaximumWidth(110)
        bar.addWidget(self.range)
        win.removeToolBar(self.fc.bar)                  # rows: Find on top, Replace under it
        win.addToolBar(Qt.BottomToolBarArea, bar)
        win.addToolBarBreak(Qt.BottomToolBarArea)
        win.addToolBar(Qt.BottomToolBarArea, self.fc.bar)
        self.fc.bar.hide()
        bar.hide()
        self.bar = bar
        self.act_replace = QAction("Replace…", win, shortcut=QKeySequence("Ctrl+H"))
        self.act_replace.setToolTip("Find and replace text (Ctrl+H)")
        self.act_replace.triggered.connect(lambda _=False: self.open())
        win.addAction(self.act_replace)
        self.fc.on_close.append(self.bar.hide)

    def open(self):
        if self.ed.doc is None or self.win.mode not in ("editor", "overview"):
            return
        if self.win.mode != "editor":
            self.win.set_mode("editor")
        self.fc.open()
        self.bar.show()
        if self.fc.edit.text():
            self.edit.setFocus()
            self.edit.selectAll()

    def _pattern(self):
        from .search import compile_query
        import re
        q = self.fc.edit.text()
        try:
            return compile_query(q, self.fc.case.isChecked(), self.fc.word.isChecked(), self.fc.regex.isChecked())
        except re.error as e:
            QMessageBox.warning(self.win, APP_NAME, f"That regular expression isn't valid:\n{e}")
            return None

    def _blocked(self):
        why = edit_blocked(self.ed.work)
        if why:
            QMessageBox.warning(self.win, APP_NAME, why)
        return why

    def replace_one(self):
        """Replace the current hit (only it), as one undo step; then the next hit is current."""
        fc = self.fc
        if self.ed.doc is None or not fc.hits or fc.cur < 0 or fc.index is None or self._blocked():
            if self.ed.doc is not None and not fc.hits:
                self.win.statusBar().showMessage("Nothing to replace: no current hit.", 4000)
            return 0
        pat = self._pattern()
        if pat is None:
            return 0
        p, a, b = fc.hits[fc.cur]
        rects = fc.index.page(p).rects(a, b)
        items = TE.plan_replace(self.ed.doc, [p], pat, self.edit.text(), fc.regex.isChecked(), "shrink",
                                only=(p, rects[0]))
        ok = [it for it in items if it.status == "ok"]
        if not ok:
            reason = items[0].reason if items else "that hit isn't inside one text block"
            QMessageBox.information(self.win, APP_NAME, f"This hit can't be replaced: {reason}.")
            self.last = (0, items)
            return 0
        try:
            done = self.ed.run_edit("text_replace", "Replace", lambda d: TE.apply_replace(d, ok), [p],
                                    raise_errors=True)
        except TE.EditFailed as e:
            QMessageBox.information(self.win, APP_NAME, f"This hit can't be replaced cleanly: {e}. Nothing was "
                                                        "changed.")
            self.last = (0, items)
            return 0
        n = self.ed.last_edit_result if done else 0
        self.last = (n, items)
        if done:
            notes = sorted({x for it in ok for x in it.notices})
            self.win.statusBar().showMessage("Replaced 1 hit." + (" " + " · ".join(notes) if notes else "")
                                             + " Ctrl+Z undoes it.", 8000)
        return n

    def replace_all(self, confirm=None, overflow="shrink"):
        """Preview every change, then apply them as ONE undo step. confirm(items) -> bool
        answers the preview (tests); None shows it."""
        from . import stage10 as T
        if self.ed.doc is None or self._blocked():
            return 0
        if not self.fc.edit.text().strip():
            self.win.statusBar().showMessage("Type what to find first.", 4000)
            return 0
        pat = self._pattern()
        if pat is None:
            return 0
        try:
            pages = T.parse_range(self.range.text(), self.ed.doc.page_count)
        except ValueError as e:
            QMessageBox.warning(self.win, APP_NAME, str(e))
            return 0
        from PySide6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            items = TE.plan_replace(self.ed.doc, pages, pat, self.edit.text(), self.fc.regex.isChecked(), overflow)
        finally:
            QApplication.restoreOverrideCursor()
        if not items:
            QMessageBox.information(self.win, APP_NAME, "No hits to replace" + ("" if len(pages) ==
                                    self.ed.doc.page_count else " on those pages") + ".")
            self.last = (0, items)
            return 0
        if confirm is None:
            dlg = ReplacePreview(self.win, items, overflow)
            ok = dlg.exec() == QDialog.Accepted
            if ok and dlg.overflow != overflow:
                return self.replace_all(confirm=lambda _i: True, overflow=dlg.overflow)
        else:
            ok = confirm(items)
        if not ok or not any(it.status == "ok" for it in items):
            self.last = (0, items)
            return 0
        late = []
        done = False
        for _ in range(len(items) + 1):
            todo = [it for it in items if it.status == "ok"]
            if not todo:
                break
            try:
                done = self.ed.run_edit("text_replace", f"Replace all ({sum(it.hits for it in todo)})",
                                        lambda d, todo=todo: TE.apply_replace(d, todo),
                                        sorted({it.page for it in todo}), raise_errors=True)
                break
            except TE.ItemFailed as e:              # nothing was changed; skip that block, try again
                e.item.status, e.item.reason = "skip", e.reason
                late.append(e.item)
            except Exception as e:
                QMessageBox.warning(self.win, APP_NAME, f"Replace All couldn't be made:\n{e}")
                break
        n = self.ed.last_edit_result if done else 0
        self.last = (n, items)
        skipped = [it for it in items if it.status != "ok"]
        if done:
            self.win.statusBar().showMessage(f"Replaced {n} hit(s)" + (f"; {sum(it.hits for it in skipped)} "
                                             "skipped" if skipped else "") + ". Ctrl+Z undoes it all.", 8000)
        if late:
            QMessageBox.information(self.win, APP_NAME, f"{len(late)} block(s) couldn't be changed cleanly when it "
                                                        "came to it and were skipped:\n\n"
                                    + "\n".join(f"Page {it.page + 1}: {it.reason}" for it in late[:10]))
        return n


class ReplacePreview(QDialog):
    """Every change Replace All will make, and every hit it will skip (with the reason)."""

    def __init__(self, parent, items, overflow):
        super().__init__(parent)
        self.setWindowTitle("Replace All: review the changes")
        self.resize(900, 520)
        self.overflow = overflow
        v = QVBoxLayout(self)
        ok = [it for it in items if it.status == "ok"]
        sk = [it for it in items if it.status != "ok"]
        head = QLabel(f"{sum(it.hits for it in ok)} hit(s) in {len(ok)} text block(s) will be replaced"
                      + (f"; {sum(it.hits for it in sk)} hit(s) will be skipped." if sk else ".")
                      + " All of it is ONE undo step.")
        head.setWordWrap(True)
        v.addWidget(head)
        t = QTreeWidget()
        t.setHeaderLabels(["Page", "Hits", "Before", "After", "Notes"])
        t.setRootIsDecorated(False)
        t.setSelectionMode(QAbstractItemView.NoSelection)
        h = t.header()
        for k in (0, 1):
            h.setSectionResizeMode(k, QHeaderView.ResizeToContents)
        t.setColumnWidth(2, 250)
        t.setColumnWidth(3, 250)
        h.setStretchLastSection(True)
        for it in ok + sk:
            notes = "Skipped: " + it.reason if it.status != "ok" else " · ".join(it.notices)
            row = QTreeWidgetItem([str(it.page + 1), str(it.hits), it.before, it.after if it.status == "ok" else "",
                                   notes])
            for k in range(5):
                row.setToolTip(k, row.text(k))
            if it.status != "ok":
                for k in range(5):
                    row.setForeground(k, QBrush(QColor("#ff8a80")))
            t.addTopLevelItem(row)
        self.tree = t
        v.addWidget(t, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("If a block's new text doesn't fit its box:"))
        self.fit = QComboBox()
        self.fit.addItem("Shrink it to fit", "shrink")
        self.fit.addItem("Let the box grow downward", "grow")
        self.fit.setCurrentIndex(max(0, self.fit.findData(overflow)))
        self.fit.currentIndexChanged.connect(lambda _i: setattr(self, "overflow", self.fit.currentData()))
        row.addWidget(self.fit)
        row.addStretch(1)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(f"Replace {sum(it.hits for it in ok)}")
        bb.button(QDialogButtonBox.Ok).setEnabled(bool(ok))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
