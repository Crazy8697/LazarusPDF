"""Toolbar layout for the main window.

The top toolbar area has two rows:
  row 1: the mode switcher and each mode's main bar, plus any tool group moved up
         there (compact form: group title + icon-only buttons);
  row 2: the active mode's tool groups in full labelled form. It is only built when
         something is in it, so an empty row 2 takes no space.
Tool groups can also sit in the left/right toolbar areas as vertical sidebars (native Qt).
Each mode has its own movable groups (editor: Markup / Edit / Insert; Overview: Grid / Pages).

Qt does the dragging (the dotted handle). On release we decide the row from where the
cursor is, re-pack every top bar in a clean order at full size (Qt on its own squeezes a
full row rather than swapping), and restyle the group for where it landed.

Hit-testing uses the event's *global* position. During a drag Qt may lift the toolbar
out of the window to follow the cursor, so positions relative to the toolbar are not
reliable. The row-1 zone is forgiving: it reaches into the top of row 2. While dragging,
a drop target shows which row the group will land on."""
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtWidgets import QDockWidget, QLabel, QStyle, QStyleOptionToolBar, QWidget

ROW1_GRACE = 18          # px below row 1 that still count as a row-1 drop
ROW2_BAND = 110          # px below row 2 (or row 1 when row 2 is closed) that count as row 2
DOCK_FEATURES = QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable


class DropHint(QLabel):
    def __init__(self, parent):
        super().__init__("", parent)
        self.setObjectName("drophint")
        self.setAlignment(Qt.AlignCenter)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()


class LayoutManager(QObject):
    def __init__(self, win, row1_fixed, groups_by_mode, docks):
        super().__init__(win)
        self.win = win
        self.row1_fixed = row1_fixed          # always on row 1, in this order
        self.groups_by_mode = groups_by_mode  # mode key -> its movable tool groups
        self.groups = [g for gs in groups_by_mode.values() for g in gs]
        self.mode_of = {g: m for m, gs in groups_by_mode.items() for g in gs}
        self.docks = docks
        self.place = {g: "row2" for g in self.groups}
        self.rows = {"row1": [], "row2": list(self.groups)}
        self.mode = None
        self.locked = False
        self._drag = None
        self.hint = DropHint(win)
        for g in self.groups:
            g.installEventFilter(self)
        for d in docks:
            d.setFeatures(DOCK_FEATURES)

    def _mine(self, row):
        return [g for g in self.rows[row] if self.mode_of[g] == self.mode]

    # ---------- building the top rows ----------
    def _top_bars(self):
        return list(self.row1_fixed) + [g for g in self.groups if self.place[g] in ("row1", "row2")]

    def repack(self, mode=None):
        if mode is not None:
            self.mode = mode
        win = self.win
        vis = {b: not b.isHidden() for b in self._top_bars()}
        for b in self._top_bars():
            if win.toolBarArea(b) == Qt.TopToolBarArea:
                win.removeToolBar(b)
        for b in self.row1_fixed + self._mine("row1"):
            win.addToolBar(Qt.TopToolBarArea, b)
        row2 = self._mine("row2")
        if row2:
            win.addToolBarBreak(Qt.TopToolBarArea)
            for b in row2:
                win.addToolBar(Qt.TopToolBarArea, b)
        vis[self.row1_fixed[0]] = True        # the mode switcher is always shown
        for b, v in vis.items():
            if win.toolBarArea(b) == Qt.TopToolBarArea:
                b.setVisible(v)
        for g in self.groups:
            self.style(g)

    def style(self, g):
        compact = self.place[g] == "row1"
        g.setToolButtonStyle(Qt.ToolButtonIconOnly if compact else Qt.ToolButtonTextUnderIcon)
        g.setIconSize(QSize(18, 18) if compact else QSize(20, 20))
        if g.property("compact") != compact:
            g.setProperty("compact", compact)
            g.style().unpolish(g)
            g.style().polish(g)
            for w in g.findChildren(QWidget):
                w.style().unpolish(w)
                w.style().polish(w)

    # ---------- geometry (window coordinates) ----------
    # Row positions come from the menu bar and the bars' size hints, not from the bars'
    # current geometry: after a re-pack QMainWindow animates toolbars into place, and a
    # drag started mid-animation would otherwise read positions far off-screen.
    def _row1_span(self):
        mb = self.win.menuBar()
        top = mb.mapTo(self.win, QPoint(0, mb.height())).y() if mb.isVisible() else 0
        bars = [b for b in self.row1_fixed + self._mine("row1") if not b.isHidden()]
        return top, top + max(b.sizeHint().height() for b in bars)

    def _row2_bottom(self, exclude=None):
        bars = [b for b in self._mine("row2") if not b.isHidden() and b is not exclude]
        if not bars:
            return None
        return self._row1_span()[1] + max(b.sizeHint().height() for b in bars)

    def _target_row(self, d, pos):
        """'row1', 'row2' or None (not a row drop) for a window-coordinate point."""
        if pos.y() < d["row1_bottom"] + ROW1_GRACE:
            return "row1"
        if pos.y() <= (d["row2_bottom"] or d["row1_bottom"]) + ROW2_BAND:
            return "row2"
        return None

    def _win_pos(self, ev):
        return self.win.mapFromGlobal(ev.globalPosition().toPoint())

    # ---------- dragging ----------
    def eventFilter(self, obj, ev):
        if obj not in self.place or self.locked:
            return False
        t = ev.type()
        if t == QEvent.MouseButtonPress and ev.button() == Qt.LeftButton:
            opt = QStyleOptionToolBar()
            obj.initStyleOption(opt)
            handle = obj.style().subElementRect(QStyle.SE_ToolBarHandle, opt, obj)
            if handle.contains(ev.position().toPoint()):
                centers = {g: g.mapTo(self.win, g.rect().center()).x()
                           for g in self.groups if self.place[g] in ("row1", "row2") and g.isVisible()}
                top, bottom = self._row1_span()
                self._drag = {"bar": obj, "origin": self.place[obj], "centers": centers,
                              "row1_top": top, "row1_bottom": bottom,
                              "row2_bottom": self._row2_bottom(exclude=obj)}
        elif t == QEvent.MouseMove and self._drag and self._drag["bar"] is obj:
            self._show_hint(self._win_pos(ev))
        elif t == QEvent.MouseButtonRelease and self._drag and self._drag["bar"] is obj:
            drag, self._drag = self._drag, None
            self.hint.hide()
            pos = self._win_pos(ev)
            QTimer.singleShot(0, lambda: self._dropped(drag, pos))
        return False

    def _show_hint(self, pos):
        d = self._drag
        side = self.win.width() * 0.10
        if not (side < pos.x() < self.win.width() - side):
            self.hint.hide()            # near an edge: Qt shows its own side-area placement
            return
        row = self._target_row(d, pos)
        if row == "row1":
            top, bottom = d["row1_top"], d["row1_bottom"]
            self.hint.setText("Row 1 (compact)")
            self.hint.setGeometry(QRect(0, top, self.win.width(), bottom - top))
        elif row == "row2" and d["row2_bottom"] is None:
            self.hint.setText("Drop here to open row 2")
            self.hint.setGeometry(QRect(0, d["row1_bottom"], self.win.width(), 58))
        elif row == "row2":
            self.hint.setText("Row 2")
            self.hint.setGeometry(QRect(0, d["row1_bottom"], self.win.width(),
                                        d["row2_bottom"] - d["row1_bottom"]))
        else:
            self.hint.hide()
            return
        self.hint.show()
        self.hint.raise_()

    def _dropped(self, d, pos):
        bar = d["bar"]
        area = self.win.toolBarArea(bar)
        for r in self.rows.values():
            if bar in r:
                r.remove(bar)
        target = self._target_row(d, pos)
        edge = self.win.width() * 0.10
        near_edge = pos.x() < edge or pos.x() > self.win.width() - edge
        # The cursor wins: a drop clearly in a row (away from the window edges) goes to that
        # row even if Qt snapped the bar back to a side area it was lifted from.
        if area in (Qt.LeftToolBarArea, Qt.RightToolBarArea) and (target is None or near_edge):
            self.place[bar] = "left" if area == Qt.LeftToolBarArea else "right"
            self.repack()
            return
        if area in (Qt.LeftToolBarArea, Qt.RightToolBarArea):
            self.win.removeToolBar(bar)        # leave the side area; repack re-adds it on top
            bar.show()
        if target is None:                     # dropped somewhere unrelated: back where it was
            target = d["origin"] if d["origin"] in self.rows else "row2"
        others = self.rows[target]
        centers = d["centers"]
        mine = [g for g in others if self.mode_of[g] == self.mode_of[bar]]
        idx_mine = sum(1 for g in mine if centers.get(g, -1) < pos.x())
        # insert after the idx_mine-th group of this mode (other modes' groups keep their slots)
        if idx_mine == 0:
            idx = others.index(mine[0]) if mine else len(others)
        else:
            idx = others.index(mine[idx_mine - 1]) + 1
        others.insert(idx, bar)
        self.place[bar] = target
        self.repack()

    # ---------- lock ----------
    def set_locked(self, on):
        self.locked = on
        for g in self.groups:
            g.setMovable(not on)
        for d in self.docks:
            d.setFeatures(QDockWidget.NoDockWidgetFeatures if on else DOCK_FEATURES)
