"""Stage 18c: the Annotations panel. A dock (tabbed with Pages and Bookmarks, View › Annotations
panel) listing every annotation of the active tab: page, type and its text where it has any
(the text under a highlight / underline / strikeout, a note's or text box's text, a callout's
text, a measurement's reading, a link's target). Click an entry: the view goes to that page,
scrolls the annotation into view and selects it (move / resize / Properties as usual).

Live by a short debounced full refresh: any change to the document (an edit, undo / redo, a
page op), another tab, or the panel being shown rebuilds the list; while the panel is hidden
nothing is read. View only: no export, no report.

Covered: every 9a annotation (highlight, underline, strikeout, ink, rectangle, ellipse, line,
arrow, polygon, note, text box, stamp), every 9b role (whiteout, image, signature, distance,
area, callout), stage 10 redaction marks, and stage 18 links. Not listed: form fields (they are
fields, worked with Edit fields / the Select tool, not annotations here), a note's popup window
(part of its note), and an area's reading label (its reading is shown on the area's entry)."""
import pymupdf
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtWidgets import QDockWidget, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from . import contentedit as CE
from . import links as LK

NAMES = {"Highlight": "Highlight", "Underline": "Underline", "StrikeOut": "Strikeout", "Squiggly": "Squiggly",
         "Ink": "Drawing", "Square": "Rectangle", "Circle": "Ellipse", "Line": "Line", "Polygon": "Polygon",
         "PolyLine": "Polyline", "Text": "Note", "FreeText": "Text box", "Stamp": "Stamp",
         "Redact": "Redaction mark", "FileAttachment": "Attachment", "Caret": "Caret", "Sound": "Sound"}
ROLES = {"whiteout": "Whiteout", "image": "Image", "signature": "Signature", "distance": "Distance",
         "area": "Area", "callout": "Callout"}
SKIP_TYPES = ("Popup", "Widget", "Link")        # (links are listed from the page's link list)
MARKUP = ("Highlight", "Underline", "StrikeOut", "Squiggly")
SNIP = 160


def _snip(t):
    t = " ".join((t or "").split())
    return t if len(t) <= SNIP else t[:SNIP - 1] + "…"


def _under(pg, a):
    """The text under a text-markup annotation (its quads)."""
    v = a.vertices or []
    parts = []
    for k in range(0, len(v) - 3, 4):
        r = pymupdf.Quad(v[k:k + 4]).rect
        parts.append(pg.get_textbox(r))
    return " ".join(p.strip() for p in parts if p.strip())


def rows(doc):
    """[(page, xref, type label, text, kind)] for the whole document, page by page, in each
    page's drawing order. kind: "annot" | "link"."""
    out = []
    plain = None
    for pno in range(doc.page_count):
        pg = doc[pno]
        for a in pg.annots():
            t = a.type[1]
            if t in SKIP_TYPES:
                continue
            role = CE.get_data(doc, a.xref).get("role", "")
            if role == "area_label":
                continue
            label = ROLES.get(role) or NAMES.get(t, t)
            if t == "Line" and not role and (a.line_ends or (0, 0))[1] not in (0, pymupdf.PDF_ANNOT_LE_NONE):
                label = "Arrow"
            if t in MARKUP:
                try:
                    text = _under(pg, a)
                except Exception:          # (a journalled rotated page: read a plain copy)
                    if plain is None:
                        plain = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
                    pp = plain[pno]
                    pa = next((b for b in pp.annots() if b.xref == a.xref), None)
                    text = _under(pp, pa) if pa is not None else ""
            elif role in ("image", "signature", "whiteout"):
                text = ""                          # (no text of their own; PyMuPDF's default content is noise)
            elif role == "callout":
                text = CE.get_data(doc, a.xref).get("text") or (a.info or {}).get("content", "")
            elif t == "Stamp" and not role:
                text = (a.info or {}).get("content", "") or (a.info or {}).get("subject", "")
            else:
                text = (a.info or {}).get("content", "")
            out.append((pno, a.xref, label, _snip(text), "annot"))
        del pg
        for x in LK.link_xrefs(doc, pno):
            out.append((pno, x, "Link", _snip(LK.describe(LK.target_of(doc, x))), "link"))
    if plain is not None:
        plain.close()
    return out


class AnnotationsPanel(QObject):
    def __init__(self, ed, win):
        super().__init__(win)
        self.ed, self.win = ed, win
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(4, 4, 4, 4)
        self.count = QLabel("")
        self.count.setObjectName("legend")
        v.addWidget(self.count)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Page", "Type", "Text"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setColumnWidth(0, 44)
        self.tree.setColumnWidth(1, 96)
        self.tree.itemActivated.connect(self._go)
        self.tree.itemClicked.connect(self._go)
        v.addWidget(self.tree, 1)
        self.dock = QDockWidget("Annotations", win)
        self.dock.setObjectName("annotsdock")
        self.dock.setWidget(body)
        self.dock.visibilityChanged.connect(lambda on: on and self._stale and self.refresh())
        self._stale = True
        self._timer = QTimer(self, singleShot=True, interval=150)
        self._timer.timeout.connect(self.refresh)
        ed.docChanged.connect(self.schedule)
        ed.sessionChanged.connect(self.schedule)
        ed.pagesChanged.connect(lambda _s: self.schedule())
        self.refresh_count = 0              # (the self-test reads it)

    def schedule(self):
        self._stale = True
        if self.dock.isVisible():
            self._timer.start()

    def refresh(self):
        self._timer.stop()
        self._stale = False
        self.refresh_count += 1
        self.tree.clear()
        doc = self.ed.doc
        if doc is None or doc.is_closed:
            self.count.setText("No PDF open.")
            return
        rs = rows(doc)
        items = []
        for pno, x, label, text, kind in rs:
            it = QTreeWidgetItem([str(pno + 1), label, text])
            it.setData(0, Qt.UserRole, (pno, x, kind))
            it.setToolTip(2, text)
            it.setTextAlignment(0, Qt.AlignRight | Qt.AlignVCenter)
            items.append(it)
        self.tree.addTopLevelItems(items)
        self.count.setText(f"{len(rs)} annotation(s). Click one to go to it." if rs else
                           "No annotations in this PDF.")

    def entries(self):
        """[(page, type, text)] as listed (tests)."""
        return [(int(self.tree.topLevelItem(i).text(0)) - 1, self.tree.topLevelItem(i).text(1),
                 self.tree.topLevelItem(i).text(2)) for i in range(self.tree.topLevelItemCount())]

    def go(self, row):
        it = self.tree.topLevelItem(row)
        if it is not None:
            self._go(it)

    def _go(self, it, _col=0):
        data = it.data(0, Qt.UserRole)
        if not data or self.ed.doc is None:
            return
        pno, x, kind = data
        mk = self.ed.markup
        want = "link" if kind == "link" else "select"
        if mk.tool != want:
            mk.set_tool(want)
            (mk.actions.get("link") if kind == "link" else self.ed.act_select).setChecked(True)
        c = self.ed.canvas
        c.goto(pno)
        mk.select(pno, [x])
        pg_, sel = mk.selected_annots()
        if sel:
            from .markup import to_disp_rect
            r = c.page_to_scene(pno, to_disp_rect(pg_, sel[0].rect))
            c.ensureVisible(r, 40, 40)
        del pg_, sel
