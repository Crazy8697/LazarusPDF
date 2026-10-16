"""Stage 9a: annotations on the working copy (MARKUP tools), selecting / moving / resizing /
deleting them, and the Properties panel.

Every change is ONE journal operation through PdfEditorMode.run_edit (the ops are declared
journal-safe in oprules.py and probed there), so Ctrl+Z / Ctrl+Y undo and redo it. Nothing
here writes a file.

Coordinates: the canvas shows each page as displayed (with its /Rotate). Annotations live in
unrotated page space, so points and rects go through page.derotation_matrix on the way in
and page.rotation_matrix on the way out.

PyMuPDF gotcha: an Annot is only valid while its Page object is alive. Every function here
holds the page (pg = doc[pno]) for as long as it touches the page's annotations.

Moving and resizing: squares, ellipses, text boxes, stamps and notes use Annot.set_rect.
Ink, lines and polygons have no settable rect in PyMuPDF; they are re-created with their
points transformed and their properties copied, inside the same operation. Highlight /
underline / strikeout stay on their text: they can be restyled and deleted, not moved.

Stage 9b tools (contentedit.py) plug into the same machinery: whiteout, image, signature,
distance, area and callout annotations carry a role, and transform / restyle / delete treat
them by role (a callout keeps its tip on its spot; a measurement re-computes its reading)."""
import pymupdf
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QPainterPath, QPen
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDockWidget,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGraphicsEllipseItem, QGraphicsLineItem, QGraphicsPathItem, QGraphicsRectItem,
                               QHBoxLayout, QLabel, QLineEdit, QMenu, QPlainTextEdit, QPushButton, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

from . import contentedit as CE
from . import links as LK

R = pymupdf.Rect
P = pymupdf.Point

# tool key -> (op key in oprules, undo label)
TOOL_OPS = {
    "highlight": ("highlight", "Highlight"), "underline": ("underline", "Underline"),
    "strike": ("strike", "Strikeout"), "ink": ("ink", "Draw"),
    "rect": ("rect_annot", "Rectangle"), "ellipse": ("circle_annot", "Ellipse"),
    "line": ("line_annot", "Line"), "arrow": ("line_annot", "Arrow"),
    "polygon": ("polygon_annot", "Polygon"), "note": ("text_note", "Note"),
    "freetext": ("freetext", "Text box"), "stamp": ("stamp", "Stamp"),
    # stage 9b
    "whiteout": ("whiteout", "Whiteout"), "image": ("image_stamp", "Insert image"),
    "signature": ("image_stamp", "Signature"), "distance": ("distance", "Distance"),
    "area": ("area", "Area"), "callout": ("callout", "Callout"),
    # stage 10
    "redact": ("redact_mark", "Redaction mark"), "redact_text": ("redact_mark", "Redaction mark"),
    "form_text": ("form_add", "Add text field"), "form_check": ("form_add", "Add checkbox"),
    "form_radio": ("form_add", "Add radio button"), "form_combo": ("form_add", "Add dropdown"),
    "form_list": ("form_add", "Add list box"),
    # stage 18
    "link": ("link_add", "Link"),
}
FORM_TOOLS = ("form_text", "form_check", "form_radio", "form_combo", "form_list")
DRAG_TOOLS = ("ink", "rect", "ellipse", "line", "arrow", "freetext", "whiteout", "image", "signature",
              "distance", "calibrate", "callout", "redact", "link") + FORM_TOOLS
POLY_TOOLS = ("polygon", "area")
TEXT_TOOLS = ("highlight", "underline", "strike", "redact_text")
SHAPES = [("rect", "Rectangle", "rectangle-outline"), ("ellipse", "Ellipse", "ellipse-outline"),
          ("line", "Line", "vector-line"), ("arrow", "Arrow", "arrow-top-right"),
          ("polygon", "Polygon (click points, double-click to finish)", "vector-polygon")]
STAMPS = ["Approved", "As Is", "Confidential", "Departmental", "Experimental", "Expired", "Final",
          "For Comment", "For Public Release", "Not Approved", "Not For Public Release", "Sold",
          "Top Secret", "Draft"]                       # PyMuPDF stamp ids 0..13
FONTS = [("helv", "Helvetica"), ("tiro", "Times"), ("cour", "Courier")]
LINE_ENDS = [(pymupdf.PDF_ANNOT_LE_NONE, "None"), (pymupdf.PDF_ANNOT_LE_OPEN_ARROW, "Open arrow"),
             (pymupdf.PDF_ANNOT_LE_CLOSED_ARROW, "Closed arrow"), (pymupdf.PDF_ANNOT_LE_CIRCLE, "Circle"),
             (pymupdf.PDF_ANNOT_LE_SQUARE, "Square"), (pymupdf.PDF_ANNOT_LE_DIAMOND, "Diamond"),
             (pymupdf.PDF_ANNOT_LE_BUTT, "Bar"), (pymupdf.PDF_ANNOT_LE_SLASH, "Slash")]

RED, BLUE, YELLOW, BLACK = (0.86, 0.1, 0.1), (0.1, 0.35, 0.9), (1.0, 0.85, 0.0), (0.0, 0.0, 0.0)
DEFAULTS = {
    "highlight": dict(stroke=YELLOW, opacity=1.0),
    "underline": dict(stroke=BLUE, opacity=1.0),
    "strike": dict(stroke=RED, opacity=1.0),
    "ink": dict(stroke=RED, width=2.0, opacity=1.0),
    "rect": dict(stroke=RED, fill=None, width=2.0, opacity=1.0),
    "ellipse": dict(stroke=RED, fill=None, width=2.0, opacity=1.0),
    "line": dict(stroke=RED, width=2.0, opacity=1.0, ends=(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_NONE)),
    "arrow": dict(stroke=RED, width=2.0, opacity=1.0, ends=(pymupdf.PDF_ANNOT_LE_NONE, pymupdf.PDF_ANNOT_LE_OPEN_ARROW)),
    "polygon": dict(stroke=RED, fill=None, width=2.0, opacity=1.0),
    "note": dict(stroke=YELLOW, opacity=1.0, text=""),
    "freetext": dict(text_color=BLACK, fill=(1.0, 1.0, 0.85), width=1.0,
                     fontname="helv", fontsize=12.0, opacity=1.0, text="Text"),
    "stamp": dict(stamp=0, opacity=1.0),
    "whiteout": dict(fill=None),                 # None: sample the page's background where it is dropped
    "image": dict(opacity=1.0),
    "signature": dict(opacity=1.0),
    "distance": dict(stroke=BLUE, width=1.0),
    "area": dict(stroke=BLUE, fill=None, width=1.0, opacity=1.0),
    "callout": dict(CE.CALLOUT_DEFAULTS),
    "redact": dict(fill=(0.0, 0.0, 0.0)),
    "redact_text": dict(fill=(0.0, 0.0, 0.0)),
    **{k: {} for k in FORM_TOOLS},
    "link": {},
}
RECT_TYPES = ("Square", "Circle", "FreeText", "Stamp", "Text", "Redact")    # Annot.set_rect works
VERTEX_TYPES = ("Line", "Polygon", "PolyLine", "Ink")                  # re-created when moved
TEXT_MARKUP = ("Highlight", "Underline", "StrikeOut", "Squiggly")      # stay on their text
RESIZABLE = ("Square", "Circle", "FreeText", "Stamp", "Redact", "Widget", "Link") + VERTEX_TYPES
HANDLE = 7                      # resize handle size in screen pixels


# ---------------- geometry ----------------
def to_pdf_point(pg, pt):
    return P(pt) * pg.derotation_matrix


def to_pdf_rect(pg, r):
    return (R(r) * pg.derotation_matrix).normalize()


def to_disp_rect(pg, r):
    return (R(r) * pg.rotation_matrix).normalize()


# ---------------- operations (run inside a journal operation; return xref(s)) ----------------
def _rgb(c):
    return tuple(float(x) for x in c) if c else None


def apply_style(a, st):
    """Set the style keys present in st on annotation a, then update its appearance."""
    t = a.type[1]
    kw = {}
    if t == "FreeText":
        if "text_color" in st:
            kw["text_color"] = _rgb(st["text_color"])
        if "fill" in st:
            kw["fill_color"] = _rgb(st["fill"])
        if "fontsize" in st:
            kw["fontsize"] = float(st["fontsize"])
        if "fontname" in st:
            kw["fontname"] = st["fontname"]
        if "width" in st:
            a.set_border(width=float(st["width"]))
    else:
        if "stroke" in st and st["stroke"] is not None:
            a.set_colors(stroke=_rgb(st["stroke"]))
        if "fill" in st and t in ("Square", "Circle", "Polygon", "Redact"):
            a.set_colors(fill=_rgb(st["fill"]) or [])
        if "width" in st and t not in TEXT_MARKUP + ("Text", "Stamp"):
            a.set_border(width=float(st["width"]))
        if "ends" in st and t in ("Line", "PolyLine"):
            a.set_line_ends(*st["ends"])
    if "opacity" in st:
        a.set_opacity(float(st["opacity"]))
    info = {}
    if "text" in st and t in ("FreeText", "Text"):
        info["content"] = st["text"]
    if "author" in st:
        info["title"] = st["author"]
    if info:
        a.set_info(**info)
    a.update(**kw)


def read_style(a):
    t = a.type[1]
    c = a.colors or {}
    st = {"type": t, "opacity": 1.0 if a.opacity is None or a.opacity < 0 else a.opacity,
          "author": (a.info or {}).get("title", "")}
    if t == "FreeText":
        st["text"] = (a.info or {}).get("content", "")
        st["width"] = (a.border or {}).get("width") or 0.0
        da = _freetext_da(a)
        st.update(da)
        st["fill"] = tuple(c.get("fill") or []) or None
    else:
        st["stroke"] = tuple(c.get("stroke") or []) or None
        st["fill"] = tuple(c.get("fill") or []) or None
        w = (a.border or {}).get("width")
        st["width"] = float(w) if w is not None and w >= 0 else 1.0
        if t in ("Line", "PolyLine"):
            st["ends"] = tuple(a.line_ends or (0, 0))
        if t == "Text":
            st["text"] = (a.info or {}).get("content", "")
    return st


def _freetext_da(a):
    """Font, size and text colour of a FreeText annotation, from its /DA string."""
    out = {"fontname": "helv", "fontsize": 12.0, "text_color": BLACK}
    try:
        kind, da = a.parent.parent.xref_get_key(a.xref, "DA")
        if kind != "string":
            return out
        toks = da.split()
        for i, tk in enumerate(toks):
            if tk == "Tf" and i >= 2:
                out["fontname"] = toks[i - 2].lstrip("/").lower()[:4] or "helv"
                out["fontsize"] = float(toks[i - 1])
            if tk == "rg" and i >= 3:
                out["text_color"] = tuple(float(x) for x in toks[i - 3:i])
            if tk == "g" and i >= 1:
                g = float(toks[i - 1])
                out["text_color"] = (g, g, g)
    except Exception:
        pass
    return out


def create(doc, pno, tool, geom, style):
    """New annotation of a tool on page pno. geom (displayed page coordinates):
    rect tools: R; line/arrow: (p1, p2); polygon / ink: [points]; note: point;
    text markup: quads (already in PDF space, from textsel)."""
    pg = doc[pno]
    if tool == "highlight":
        a = pg.add_highlight_annot(geom)
    elif tool == "underline":
        a = pg.add_underline_annot(geom)
    elif tool == "strike":
        a = pg.add_strikeout_annot(geom)
    elif tool == "ink":
        a = pg.add_ink_annot([[tuple(to_pdf_point(pg, p)) for p in geom]])
    elif tool == "rect":
        a = pg.add_rect_annot(to_pdf_rect(pg, geom))
    elif tool == "ellipse":
        a = pg.add_circle_annot(to_pdf_rect(pg, geom))
    elif tool in ("line", "arrow"):
        a = pg.add_line_annot(to_pdf_point(pg, geom[0]), to_pdf_point(pg, geom[1]))
    elif tool == "polygon":
        a = pg.add_polygon_annot([tuple(to_pdf_point(pg, p)) for p in geom])
    elif tool == "note":
        a = pg.add_text_annot(to_pdf_point(pg, geom), style.get("text", ""), icon="Note")
    elif tool == "freetext":
        a = pg.add_freetext_annot(to_pdf_rect(pg, geom), style.get("text", "Text"),
                                  fontsize=float(style.get("fontsize", 12)), fontname=style.get("fontname", "helv"),
                                  text_color=_rgb(style.get("text_color")), fill_color=_rgb(style.get("fill")),
                                  rotate=pg.rotation)      # (a border colour needs rich text in PyMuPDF)
    elif tool == "stamp":
        a = pg.add_stamp_annot(to_pdf_rect(pg, geom), stamp=int(style.get("stamp", 0)))
    elif tool == "whiteout":
        del pg
        return CE.create_whiteout(doc, pno, geom, tuple(style["fill"]))
    elif tool in ("image", "signature"):
        del pg
        xref = CE.create_image(doc, pno, geom, style["image"], role=tool, subject=tool.capitalize())
        if style.get("opacity", 1.0) < 1.0:
            pg = doc[pno]
            apply_style(pg.load_annot(xref), {"opacity": style["opacity"]})
        return xref
    elif tool == "distance":
        del pg
        return CE.create_distance(doc, pno, geom[0], geom[1], style["scale"], style)
    elif tool == "area":
        del pg
        return CE.create_area(doc, pno, geom, style["scale"], style)
    elif tool == "callout":
        del pg
        return CE.create_callout(doc, pno, geom[0], geom[1], callout_style(style))
    elif tool in FORM_TOOLS:
        del pg
        from . import stage10
        return stage10.add_field(doc, pno, tool, geom)
    elif tool == "link":
        del pg
        return LK.create(doc, pno, geom, style["target"])
    elif tool in ("redact", "redact_text"):
        del pg
        from . import stage10
        marks = [(pno, ("pdf", q)) for q in geom] if tool == "redact_text" else [(pno, geom)]
        return stage10.mark_redactions(doc, marks, tuple(style.get("fill") or (0, 0, 0)))[0]
    else:
        raise ValueError(tool)
    st = {k: v for k, v in style.items() if k not in ("stamp", "text", "fontname", "fontsize", "text_color")}
    if tool == "freetext":
        st = {k: style[k] for k in ("width", "opacity") if k in style}
    if tool == "stamp":
        st = {k: style[k] for k in ("opacity",) if k in style}
    apply_style(a, st)
    return a.xref


def callout_style(st):
    """Properties-panel keys -> callout keys (stroke is its border; the line end is the leader's)."""
    out = {k: v for k, v in st.items() if k in CE.CALLOUT_DEFAULTS}
    if "stroke" in st:
        out["border"] = st["stroke"]
    if "ends" in st:
        out["line_end"] = int(st["ends"][1])
    return out


def restyle(doc, pno, xrefs, st):
    """Returns the selection's xrefs afterwards (a restyled callout is re-created)."""
    out = []
    for x in xrefs:
        if is_field(doc, pno, x):
            from . import stage10
            stage10.field_props(doc, pno, x, name=st.get("field_name"), choices=st.get("choices"),
                                fontsize=st.get("fontsize"))
            out.append(x)
            continue
        role = CE.get_data(doc, x).get("role", "")
        if role == "callout":
            out.append(CE.recreate_callout(doc, pno, x, changes=callout_style(st)))
            continue
        s = dict(st)
        if role == "whiteout" and "fill" in s:
            s = {"stroke": s["fill"], "fill": s["fill"]}      # opaque, no visible border
        pg = doc[pno]
        apply_style(pg.load_annot(x), s)
        del pg
        out.append(x)
    return out


def delete(doc, pno, xrefs):
    lks = [x for x in xrefs if LK.is_link(doc, x)]
    if lks:
        LK.delete(doc, pno, lks)
        xrefs = [x for x in xrefs if x not in lks]
    pg = doc[pno]
    for x in xrefs:
        if is_field(doc, pno, x):
            from . import stage10
            stage10.delete_field(doc, pno, x)
            continue
        d = CE.get_data(doc, x)
        try:
            a = pg.load_annot(x)
        except Exception:
            continue                                  # already gone (an area's label, say)
        pg.delete_annot(a)
        if d.get("role") == "area":                   # its reading goes with it
            lab = CE.area_label(pg, d.get("nm"))
            if lab is not None:
                pg.delete_annot(lab)
    return []


def _map_rect(old, new):
    """Matrix taking rect old onto rect new (scale + translate)."""
    sx = new.width / old.width if old.width else 1.0
    sy = new.height / old.height if old.height else 1.0
    return pymupdf.Matrix(sx, 0, 0, sy, new.x0 - old.x0 * sx, new.y0 - old.y0 * sy)


def transform(doc, pno, xref, new_disp):
    """Move / resize annotation xref so its displayed rect becomes new_disp. Returns its xref
    (a re-created vertex annotation gets a new one)."""
    if is_field(doc, pno, xref):
        from . import stage10
        return stage10.move_field(doc, pno, xref, new_disp)
    if LK.is_link(doc, xref):
        return LK.move(doc, pno, xref, new_disp)
    pg = doc[pno]
    a = pg.load_annot(xref)
    t = a.type[1]
    new_pdf = to_pdf_rect(pg, new_disp)
    data = CE.get_data(doc, xref)
    role = data.get("role", "")
    if role == "callout":                         # the box moves; the tip stays on its spot
        # the annotation's rect spans box + leader: map the stored box through the same change
        m = _map_rect(to_disp_rect(pg, a.rect), R(new_disp))
        box = R(data["box"]) * m
        del a, pg
        return CE.recreate_callout(doc, pno, xref, box_disp=list(box))
    if role in ("distance", "area"):              # re-created with its reading re-computed
        m = _map_rect(R(a.rect), new_pdf)
        verts = a.vertices
        st = read_style(a)
        scale = CE.Scale(data["factor"], data["unit"], data["source"])
        rm = pg.rotation_matrix
        disp = lambda v: tuple(P(v) * m * rm)
        if role == "distance":
            pg.delete_annot(a)
            del pg
            return CE.create_distance(doc, pno, disp(verts[0]), disp(verts[1]), scale, st)
        lab = CE.area_label(pg, data.get("nm"))
        pg.delete_annot(a)
        if lab is not None:
            pg.delete_annot(lab)
        del pg
        return CE.create_area(doc, pno, [disp(v) for v in verts], scale, st, nm=data.get("nm"))
    if t in RECT_TYPES:
        if t == "Text":                           # a note icon keeps its size
            new_pdf = R(new_pdf.x0, new_pdf.y0, new_pdf.x0 + a.rect.width, new_pdf.y0 + a.rect.height)
        a.set_rect(new_pdf)
        a.update()
        return xref
    if t not in VERTEX_TYPES:
        return xref                               # text markup stays on its text
    m = _map_rect(R(a.rect), new_pdf)
    st = read_style(a)
    verts = a.vertices
    flags = a.flags
    pg.delete_annot(a)
    if t == "Ink":
        pts = [[tuple(P(p) * m) for p in stroke] for stroke in verts]
        b = pg.add_ink_annot(pts)
    elif t == "Line":
        b = pg.add_line_annot(P(verts[0]) * m, P(verts[1]) * m)
    elif t == "Polygon":
        b = pg.add_polygon_annot([tuple(P(p) * m) for p in verts])
    else:
        b = pg.add_polyline_annot([tuple(P(p) * m) for p in verts])
    st.pop("type", None)
    st.pop("text", None)
    if not st.get("author"):
        st.pop("author", None)
    apply_style(b, st)
    b.set_flags(flags)
    b.update()
    return b.xref


def is_field(doc, pno, xref):
    """True if xref is a form field (widget) on page pno (they are not in page.annots())."""
    try:
        return doc.xref_get_key(xref, "Subtype")[1] == "/Widget"
    except Exception:
        return False


class FieldRef:
    """A form field shaped like an Annot for the selection frame / handles / panel."""

    def __init__(self, w):
        self.w = w
        self.xref = w.xref
        self.rect = pymupdf.Rect(w.rect)
        self.type = (pymupdf.PDF_ANNOT_WIDGET, "Widget")


class LinkRef:
    """A link shaped like an Annot for the selection frame / handles / panel (stage 18)."""

    def __init__(self, doc, pno, xref):
        self.xref = xref
        self.rect = LK.rect_of(doc, pno, xref)
        self.type = (pymupdf.PDF_ANNOT_LINK, "Link")
        self.target = LK.target_of(doc, xref)


def link_ref(doc, pno, xref):
    """A LinkRef if xref is a link of page pno (still there), else None."""
    return LinkRef(doc, pno, xref) if xref in LK.link_xrefs(doc, pno) else None


def hit(pg, pt_disp, slack=3.0):
    """Topmost annotation whose displayed rect contains pt_disp: its xref, or None."""
    found = None
    for a in pg.annots():
        if a.type[1] in ("Popup", "Link", "Widget"):
            continue
        r = to_disp_rect(pg, a.rect)
        r = R(r.x0 - slack, r.y0 - slack, r.x1 + slack, r.y1 + slack)
        if r.contains(pt_disp):
            found = a.xref                    # later annotations are drawn on top
    return found


# ---------------- the controller: tools, canvas interaction, selection ----------------
class MarkupController(QObject):
    selectionChanged = Signal()

    def __init__(self, ed, win):
        super().__init__(win)
        self.ed, self.win = ed, win
        self.tool = "select"
        self.shape = "rect"
        self.stamp = 0
        self.measure_tool = "distance"
        self.image = None                # (png bytes, width pt, height pt, kind) for the Image tool
        self.signature = None            # png bytes of the signature being placed
        self.signatures = CE.SignatureLibrary()
        self.defaults = {k: dict(v) for k, v in DEFAULTS.items()}
        self.sel_page = None             # page index of the selection
        self.sel = []                    # selected annotation xrefs (one page)
        self._overlay = []               # scene items of the selection frame / handles
        self._ghost = None
        self._drag = None                # active gesture
        self._poly = []                  # polygon points so far (displayed coords)
        self._wire_tools()
        self.panel = PropertiesPanel(self, win)
        self.dock = self.panel.dock
        ed.docChanged.connect(self._doc_changed)
        ed.sessionChanged.connect(self.clear_selection)
        ed.sessionChanged.connect(self._show_links)
        ed.pageChanged.connect(lambda _p: self._show_links() if self.tool == "link" else None)

    # ---------- tools ----------
    def _wire_tools(self):
        from .pdfeditor import _icon
        ed, win = self.ed, self.win
        self.group = QActionGroup(win)
        self.group.setExclusive(True)
        for a in (ed.act_select, ed.act_hand):
            self.group.addAction(a)
        ed.act_select.triggered.connect(lambda _=False: self.set_tool("select"))
        ed.act_hand.triggered.connect(lambda _=False: self.set_tool("hand"))
        self.actions = {}
        spec = {"highlight": ("marker", "Highlight", "Highlight text: drag across it"),
                "underline": ("format-underline", "Underline", "Underline text: drag across it"),
                "strike": ("format-strikethrough", "Strike", "Strike out text: drag across it"),
                "draw": ("draw", "Draw", "Freehand ink: drag to draw"),
                "shapes": ("shape-outline", "Shapes", "Rectangle, ellipse, line, arrow, polygon "
                                                       "(the arrow beside it picks the shape)"),
                "textbox": ("text-box-outline", "Text box", "Drag a box, then type its text in Properties"),
                "note": ("note-text-outline", "Note", "Click to place a sticky note; its text is in Properties"),
                "stamp": ("stamper", "Stamp", "Click to place a stamp (the arrow beside it picks which)"),
                "whiteout": ("eraser-variant", "Whiteout", "Drag a box: covers content with the page's own background "
                             "colour, sampled where you drop it. It hides; it does not remove (that is Redact)"),
                "image": ("image-plus", "Image", "Pick a picture (PNG, JPG, WebP, GIF, BMP, TIFF, SVG…), then drag a "
                          "box or click to place it. SVG becomes a crisp bitmap at its own size when inserted"),
                "signature": ("draw-pen", "Signature", "Draw or import a signature (saved for reuse, or one-off), "
                              "then drag a box or click to place it"),
                "callout": ("comment-arrow-right-outline", "Callout", "Press on the spot to point at, drag to where the "
                            "text box goes; type its text in Properties"),
                "measure": ("ruler", "Measure", "Distance (drag), area (click corners, double-click to finish), "
                            "or calibrate the scale (the arrow beside it picks which)"),
                "link": ("link-variant", "Link", "Drag a box over the spot, then pick a page of this PDF or a web "
                         "address. With this tool, click a link to select it: drag to move, handles resize, "
                         "Del deletes, the target is in Properties")}
        tool_of = {"draw": "ink", "textbox": "freetext"}
        for key, (icon, label, tip) in spec.items():
            a = ed.soon[key]
            try:
                a.triggered.disconnect()                    # drop the "not implemented yet" handler
            except (RuntimeError, TypeError):
                pass
            a.setProperty("soon", False)
            a.setIcon(_icon(icon))
            a.setCheckable(True)
            a.setToolTip(f"<b>{label}</b><br>{tip}<br>Esc returns to Select")
            a.setStatusTip(f"{label}: {tip}")
            t = tool_of.get(key, key)
            if key == "shapes":
                a.triggered.connect(lambda _=False: self.set_tool(self.shape))
            elif key == "measure":
                a.triggered.connect(lambda _=False: self.set_tool(self.measure_tool))
            elif key == "image":
                a.triggered.connect(lambda _=False: self.pick_image())
            elif key == "signature":
                a.triggered.connect(lambda _=False: self.use_signature())
            else:
                a.triggered.connect(lambda _=False, t=t: self.set_tool(t))
            self.group.addAction(a)
            self.actions[key] = a
        self.shape_menu = QMenu("Shapes", win)
        for k, label, icon in SHAPES:
            sa = self.shape_menu.addAction(_icon(icon), label)
            sa.triggered.connect(lambda _=False, k=k: self._pick_shape(k))
        self.actions["shapes"].setMenu(self.shape_menu)
        self.stamp_menu = QMenu("Stamps", win)
        for i, label in enumerate(STAMPS):
            sa = self.stamp_menu.addAction(label)
            sa.triggered.connect(lambda _=False, i=i: self._pick_stamp(i))
        self.actions["stamp"].setMenu(self.stamp_menu)
        self.measure_menu = QMenu("Measure", win)
        for k, label in (("distance", "Distance (drag)"), ("area", "Area (click corners, double-click to finish)"),
                         ("calibrate", "Calibrate scale… (drag along a known distance)")):
            ma = self.measure_menu.addAction(label)
            ma.triggered.connect(lambda _=False, k=k: self._pick_measure(k))
        self.actions["measure"].setMenu(self.measure_menu)
        self.sig_menu = QMenu("Signatures", win)
        self.sig_menu.aboutToShow.connect(self._fill_sig_menu)
        self.actions["signature"].setMenu(self.sig_menu)
        for bar in ed.groups:                               # restyle the toolbar buttons
            for key, a in self.actions.items():
                w = bar.widgetForAction(a)
                if w is not None:
                    w.setProperty("soon", False)
                    if key in ("shapes", "stamp", "measure", "signature"):
                        w.setPopupMode(QToolButton.MenuButtonPopup)
                    w.style().unpolish(w)
                    w.style().polish(w)
        self.act_delete = QAction("Delete annotation", win, triggered=lambda _=False: self.delete_selected())
        self.act_delete.setToolTip("Delete the selected annotation(s) (Del on the page)")

    def _pick_shape(self, k):
        self.shape = k
        self.set_tool(k)

    # ---------- 9b: image, signature, measurement ----------
    def _pick_measure(self, k):
        if k != "calibrate":
            self.measure_tool = k
        self.set_tool(k)

    def pick_image(self, path=None):
        """Image tool: choose the picture first, then drag / click to place it."""
        path = path or ask_image_file(self.win)
        if not path:
            self.actions["image"].setChecked(False)
            self.ed.act_select.setChecked(True)
            self.set_tool("select")
            return False
        try:
            png, w, h, kind = CE.load_image(path)
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            from . import APP_NAME
            QMessageBox.warning(self.win, APP_NAME, f"Couldn't read that image:\n{e}")
            self.set_tool("select")
            return False
        self.image = (png, w, h, kind)
        self.set_tool("image")
        self.win.statusBar().showMessage("Drag a box (or click) to place the image"
                                         + (" (SVG: placed as a crisp bitmap)" if kind == "svg" else "") + ".", 6000)
        return True

    def _fill_sig_menu(self):
        m = self.sig_menu
        m.clear()
        saved = self.signatures.list()
        for p in saved:
            a = m.addAction(p.stem)
            a.triggered.connect(lambda _=False, p=p: self.use_signature(png=p.read_bytes()))
        if saved:
            m.addSeparator()
        m.addAction("Draw a signature…").triggered.connect(lambda: self.new_signature("draw"))
        m.addAction("Import a signature image…").triggered.connect(lambda: self.new_signature("import"))
        if saved:
            rm = m.addMenu("Remove a saved signature")
            for p in saved:
                rm.addAction(p.stem).triggered.connect(lambda _=False, p=p: self.signatures.remove(p))

    def new_signature(self, how):
        res = ask_signature(self.win, how)
        if not res:
            return False
        png, save, name = res
        if save:
            self.signatures.add(png, name)
        return self.use_signature(png=png)

    def use_signature(self, png=None):
        """Place a signature: png given (saved or one-off), else the last used, else draw one."""
        if png is None:
            png = self.signature
        if png is None:
            saved = self.signatures.list()
            if saved:
                png = saved[0].read_bytes()
            else:
                return self.new_signature("draw")
        self.signature = png
        self.set_tool("signature")
        self.win.statusBar().showMessage("Drag a box (or click) to place the signature.", 5000)
        return True

    def scale(self, page):
        return CE.scale_for(self.ed.doc, page, getattr(self.ed.work, "calibration", None))

    def calibrate(self, page, p0, p1, value=None, unit=None):
        """A known distance (displayed points p0..p1) is `value` `unit` long. Asks if not given."""
        pg = self.ed.doc[page]
        a0, a1 = P(p0) * pg.derotation_matrix, P(p1) * pg.derotation_matrix
        del pg
        length = ((a1.x - a0.x) ** 2 + (a1.y - a0.y) ** 2) ** 0.5
        if length < 1:
            return None
        if value is None:
            res = ask_calibration(self.win, length)
            if not res:
                return None
            value, unit = res
        self.ed.work.calibration = CE.Scale(float(value) / length, unit, "calibrated")
        self.win.statusBar().showMessage(f"Scale set: {self.ed.work.calibration.describe()}. "
                                         "Pages that declare their own scale keep it.", 6000)
        self.set_tool(self.measure_tool)
        return self.ed.work.calibration

    def _pick_stamp(self, i):
        self.stamp = i
        self.defaults["stamp"]["stamp"] = i
        self.set_tool("stamp")

    def action_for(self, tool):
        if tool in ("rect", "ellipse", "line", "arrow", "polygon"):
            return self.actions["shapes"]
        if tool in ("redact", "redact_text"):
            return self.actions.get("redact")
        if tool in FORM_TOOLS + ("form_edit",):
            return self.actions.get("formfield")
        if tool in ("distance", "area", "calibrate"):
            return self.actions["measure"]
        return self.actions.get({"ink": "draw", "freetext": "textbox"}.get(tool, tool))

    def set_tool(self, tool):
        """select | hand | highlight | underline | strike | ink | rect | ellipse | line | arrow |
        polygon | note | freetext | stamp | edittext"""
        self._cancel_gesture()
        if tool != "edittext" and hasattr(self.ed, "texted"):
            self.ed.texted.leave()
        self.tool = tool
        if tool in ("select", "hand"):
            self.ed._set_pan(tool == "hand")
        else:
            self.ed._set_pan(False)
            a = self.action_for(tool)
            if a is not None:
                a.setChecked(True)
            self.ed.canvas.clear_selection()
        cur = Qt.ArrowCursor if tool in ("select", "hand") + TEXT_TOOLS else Qt.CrossCursor
        if tool in TEXT_TOOLS or tool == "edittext":
            cur = Qt.IBeamCursor
        for s in self.ed.sessions:
            if tool != "hand" and s.canvas is not None:          # (a batch job's tab has no canvas)
                s.canvas.viewport().setCursor(cur)
        self.panel.show_for()
        self._show_links()
        self.win.statusBar().showMessage({
            "link": "Link: drag a box, then pick a page or a web address. Click a link to select it; "
                    "drag to move, handles resize, Del deletes.",
            "select": "Select: click an annotation to select it (Ctrl+click adds), drag to move, handles resize, Del deletes.",
            "polygon": "Polygon: click each corner, double-click (or Enter) to finish, Esc cancels.",
            "area": "Area: click each corner, double-click (or Enter) to finish, Esc cancels.",
            "distance": "Distance: drag from one point to the other.",
            "calibrate": "Calibrate: drag along a distance you know; then type its real length.",
            "whiteout": "Whiteout: drag a box. Its colour is sampled from the page where you drop it.",
            "callout": "Callout: press on the spot to point at, drag to where the text box goes.",
            "redact": "Redact: drag a box over what must go. Review the marks, then Edit › Apply redactions.",
            "form_edit": "Edit fields: click a field to select it, drag to move, handles resize, Del deletes; "
                         "name and choices in Properties. (With the Select tool, clicking a field fills it.)",
            "redact_text": "Redact: drag across text. Review the marks, then Edit › Apply redactions.",
            "edittext": "Edit text: point at text to see its block, click to edit it in place. Ctrl+Enter or a "
                        "click outside applies, Esc cancels.",
        }.get(tool, ""), 5000)

    # ---------- canvas events (PageCanvas.interact) ----------
    def _page_pt(self, c, e):
        sp = c.mapToScene(e.position().toPoint())
        i = c.page_at(sp)
        return sp, i, (c.scene_to_page(i, sp) if i is not None else None)

    def press(self, c, e):
        if self.tool == "edittext":
            return self.ed.texted.press(c, e)
        if e.button() != Qt.LeftButton or self.ed.work is None or self.tool == "hand":
            return False
        sp, i, pt = self._page_pt(c, e)
        if self.tool == "select" and i is not None:
            from . import stage10
            pg = self.ed.doc[i]
            fx = stage10.field_at(pg, pt)
            del pg
            if fx is not None and self._handle_at(c, sp) is None:
                self.clear_selection()
                self.fill_click(i, fx)                    # clicking a form field fills it
                return True
        if self.tool == "form_edit":
            h = self._handle_at(c, sp)
            if h is not None:
                self._drag = {"kind": "resize", "handle": h, "start": sp, "rect": self._sel_rect_scene()}
                return True
            if i is None:
                self.clear_selection()
                return True
            from . import stage10
            pg = self.ed.doc[i]
            fx = stage10.field_at(pg, pt)
            del pg
            if fx is None:
                self.clear_selection()
                return True
            self.select(i, [fx])
            self._drag = {"kind": "move", "start": sp, "rect": self._sel_rect_scene()}
            return True
        if self.tool == "select":
            h = self._handle_at(c, sp)
            if h is not None:
                self._drag = {"kind": "resize", "handle": h, "start": sp, "rect": self._sel_rect_scene()}
                return True
            if i is None:
                self.clear_selection()
                return False
            pg = self.ed.doc[i]
            x = hit(pg, pt)
            if x is None:
                self.clear_selection()
                return False                          # the canvas selects text as before
            if e.modifiers() & (Qt.ControlModifier | Qt.ShiftModifier) and self.sel_page == i:
                if x in self.sel:
                    self.sel.remove(x)
                else:
                    self.sel.append(x)
            elif x not in self.sel or self.sel_page != i:
                self.sel_page, self.sel = i, [x]
            self._draw_overlay()
            self.selectionChanged.emit()
            self.panel.show_for()
            if x in self.sel and self._movable():
                self._drag = {"kind": "move", "start": sp, "rect": self._sel_rect_scene()}
            return True
        if self.tool in TEXT_TOOLS:
            return False                              # canvas text selection; applied on release
        if self.tool == "link":
            h = self._handle_at(c, sp)
            if h is not None:
                self._drag = {"kind": "resize", "handle": h, "start": sp, "rect": self._sel_rect_scene()}
                return True
            x = LK.hit(self.ed.doc, i, pt) if i is not None else None
            if x is not None:
                if x not in self.sel or self.sel_page != i:
                    self.select(i, [x])
                self._drag = {"kind": "move", "start": sp, "rect": self._sel_rect_scene()}
                return True
            if self.sel:
                self.clear_selection()
        if i is None:
            return True
        if self.tool in POLY_TOOLS:
            if not self._poly or self._drag is None:
                self._poly = [pt]
                self._drag = {"kind": "poly", "page": i}
            elif self._drag.get("page") == i:
                self._poly.append(pt)
            self._show_ghost(c)
            return True
        if self.tool in ("note", "stamp"):
            self._place_click(c, i, pt)
            return True
        self._drag = {"kind": "draw", "page": i, "start": pt, "pts": [pt], "cur": pt}
        self._show_ghost(c)
        return True

    def move(self, c, e):
        if self.tool == "edittext":
            return self.ed.texted.move(c, e)
        d = self._drag
        if d is None:
            return False
        sp, i, pt = self._page_pt(c, e)
        if d["kind"] in ("move", "resize"):
            d["cur"] = sp
            self._show_ghost(c)
            return True
        if d["kind"] == "draw" and i == d["page"]:
            d["cur"] = pt
            if self.tool == "ink":
                d["pts"].append(pt)
            self._show_ghost(c)
            return True
        if d["kind"] == "poly":
            d["cur"] = pt if i == d["page"] else d.get("cur")
            self._show_ghost(c)
            return True
        return True

    def release(self, c, e):
        if self.tool == "edittext":
            return self.ed.texted.release(c, e)
        d = self._drag
        if d is None or e.button() != Qt.LeftButton:
            return False
        if d["kind"] == "poly":
            return True                               # more clicks to come
        self._drag = None
        self._clear_ghost(c)
        if d["kind"] in ("move", "resize"):
            cur = d.get("cur")
            if cur is None or (cur - d["start"]).manhattanLength() < 2:
                return True
            self._commit_transform(c, d)
            return True
        if d["kind"] == "draw":
            self._commit_draw(c, d)
        return True

    def double(self, c, e):
        if self.tool == "edittext":
            return self.ed.texted.double(c, e)
        if self.tool in POLY_TOOLS and self._drag and self._drag.get("kind") == "poly":
            self._finish_polygon(c)
            return True
        return False

    def key(self, c, e):
        if self.tool == "edittext" and self.ed.texted.key(c, e):
            return True
        if e.key() == Qt.Key_Escape:
            if self._drag or self._poly:
                self._cancel_gesture()
                return True
            if self.sel:
                self.clear_selection()
                return True
            if self.tool != "select":
                self.set_tool("select")
                self.ed.act_select.setChecked(True)
                return True
            return False
        if e.key() in (Qt.Key_Delete, Qt.Key_Backspace) and self.sel:
            self.delete_selected()
            return True
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.tool in POLY_TOOLS and self._poly:
            self._finish_polygon(c)
            return True
        return False

    def text_released(self, c):
        """The canvas finished a text drag: with a text-markup tool, mark the selection."""
        if self.tool not in TEXT_TOOLS or c.selection is None or self.ed.work is None:
            return
        i, a, b = c.selection
        quads = c.page_text(i).quads(a, b)
        if not quads:
            return
        op, label = TOOL_OPS[self.tool]
        st = dict(self.defaults[self.tool])
        tool = self.tool
        if self.ed.run_edit(op, label, lambda doc: create(doc, i, tool, quads, st), [i]):
            self.sel_page, self.sel = i, [self.ed.last_edit_result]
            self._after_edit(f"{label} added. Ctrl+Z undoes it.")

    # ---------- committing ----------
    def _commit_draw(self, c, d):
        i, tool = d["page"], self.tool
        p0, p1 = d["start"], d.get("cur", d["start"])
        if tool == "ink":
            geom = d["pts"]
            if len(geom) < 2:
                return
        elif tool in ("line", "arrow", "distance"):
            if abs(p1.x - p0.x) + abs(p1.y - p0.y) < 3:
                return
            geom = (p0, p1)
        elif tool == "calibrate":
            if abs(p1.x - p0.x) + abs(p1.y - p0.y) >= 3:
                self.calibrate(i, p0, p1)
            return
        elif tool == "callout":
            tip, at = p0, p1
            if abs(at.x - tip.x) + abs(at.y - tip.y) < 8:
                at = P(tip.x + 50, tip.y - 70)
            w_, h_ = 180, 50
            x0 = at.x if at.x >= tip.x else at.x - w_
            geom = (tip, R(x0, at.y - h_ / 2, x0 + w_, at.y + h_ / 2))
        elif tool == "link":
            r = R(p0, p1).normalize()
            if r.width < 6 or r.height < 6:
                return
            target = ask_link_target(self.win, self.ed.doc.page_count, None, self.ed.doc)
            if target is None:
                return
            self._create(c, i, "link", r, {"target": target})
            return
        elif tool in ("image", "signature"):
            r = R(p0, p1).normalize()
            if tool == "image":
                png, w_, h_, _ = self.image
            else:
                png, (w_, h_) = self.signature, CE.load_image(data=self.signature, name="sig.png")[1:3]
                if w_ > 220:                              # a signature lands at a sensible width
                    w_, h_ = 160.0, h_ * 160.0 / w_
            if r.width < 6 or r.height < 6:               # a click: its own size, top-left at the click
                geom = R(p0.x, p0.y, p0.x + w_, p0.y + h_)
            else:
                geom = CE.fit_rect(r, w_, h_)
        else:
            r = R(p0, p1).normalize()
            if tool == "freetext" and (r.width < 8 or r.height < 8):
                r = R(p0.x, p0.y, p0.x + 180, p0.y + 40)
            elif r.width < 3 or r.height < 3:
                return
            geom = r
        self._create(c, i, tool, geom)

    def _place_click(self, c, i, pt):
        if self.tool == "note":
            self._create(c, i, "note", pt)
        else:
            self._create(c, i, "stamp", R(pt.x, pt.y, pt.x + 170, pt.y + 50))

    def _finish_polygon(self, c):
        d, pts = self._drag, self._poly
        self._drag, self._poly = None, []
        self._clear_ghost(c)
        if d is None or len(pts) < 3:
            return
        self._create(c, d["page"], self.tool if self.tool in POLY_TOOLS else "polygon", pts)

    def create_annot(self, tool, page, geom, style=None):
        """Programmatic creation (tests and later tools): same path as the mouse."""
        return self._create(self.ed.canvas, page, tool, geom, style)

    def _create(self, c, i, tool, geom, style=None):
        op, label = TOOL_OPS[tool]
        st = dict(self.defaults["arrow" if tool == "arrow" else tool])
        if style:
            st.update(style)
        if tool == "whiteout" and not st.get("fill"):
            pg = self.ed.doc[i]
            st["fill"] = CE.sample_background(pg, geom)          # the page's own colour, right there
            del pg
        elif tool == "image":
            if self.image is None:
                return None
            st.setdefault("image", self.image[0])
        elif tool == "signature":
            if self.signature is None:
                return None
            st.setdefault("image", self.signature)
        elif tool in ("distance", "area"):
            st.setdefault("scale", self.scale(i))
        if self.ed.run_edit(op, label, lambda doc: create(doc, i, tool, geom, st), [i]):
            self.sel_page, self.sel = i, [self.ed.last_edit_result]
            if tool == "link":
                self._after_edit("Link added. Ctrl+Z undoes it.")
                return self.sel[0]
            if tool in FORM_TOOLS:
                self.set_tool("form_edit")          # a new field: move / resize / name it right away
            elif tool in ("note", "freetext", "stamp", "image", "signature", "callout"):   # (redaction stays on)
                self.set_tool("select")             # placed: edit it in Properties
                self.ed.act_select.setChecked(True)
            self._after_edit(f"{label} added. Ctrl+Z undoes it.")
            return self.sel[0]
        return None

    def _commit_transform(self, c, d):
        r0, r1 = d["rect"], self._target_rect(d)
        if r0 is None or r1 is None:
            return
        i = self.sel_page
        o = c.pages[i]["rect"].topLeft()
        disp = lambda q: R(q.left() - o.x(), q.top() - o.y(), q.right() - o.x(), q.bottom() - o.y())
        pg = self.ed.doc[i]
        news = {}
        for x in self.sel:
            a = self._annot(pg, i, x)
            t = a.type[1]
            if t in TEXT_MARKUP:
                continue
            cur = to_disp_rect(pg, a.rect)
            if d["kind"] == "move":
                dx, dy = r1.left() - r0.left(), r1.top() - r0.top()
                news[x] = R(cur.x0 + dx, cur.y0 + dy, cur.x1 + dx, cur.y1 + dy)
            else:
                news[x] = disp(r1)
        del pg
        if not news:
            return

        def fn(doc):
            return [transform(doc, i, x, nr) for x, nr in news.items()]
        label = "Move annotation" if d["kind"] == "move" else "Resize annotation"
        if self._links_only():
            label = label.replace("annotation", "link")
        if self.ed.run_edit(self._op("move_annot"), label, fn, [i]):
            self.sel_page, self.sel = i, list(self.ed.last_edit_result)   # (re-created ones have new xrefs)
            self._after_edit(("Moved." if d["kind"] == "move" else "Resized.") + " Ctrl+Z undoes it.")

    def move_selected(self, dx, dy):
        """Move the selection by (dx, dy) displayed points (tests, keyboard nudges later)."""
        if not self.sel:
            return False
        i = self.sel_page
        pg = self.ed.doc[i]
        news = {}
        for x in self.sel:
            a = self._annot(pg, i, x)
            if a.type[1] in TEXT_MARKUP:
                continue
            r = to_disp_rect(pg, a.rect)
            news[x] = R(r.x0 + dx, r.y0 + dy, r.x1 + dx, r.y1 + dy)
        del pg
        if not news:
            return False
        ok = self.ed.run_edit(self._op("move_annot"), "Move annotation",
                              lambda doc: [transform(doc, i, x, nr) for x, nr in news.items()], [i])
        if ok:
            self.sel_page, self.sel = i, list(self.ed.last_edit_result)   # (re-created ones have new xrefs)
            self._after_edit("Moved. Ctrl+Z undoes it.")
        return ok

    def resize_selected(self, new_disp):
        if len(self.sel) != 1:
            return False
        i, x = self.sel_page, self.sel[0]
        ok = self.ed.run_edit(self._op("move_annot"), "Resize annotation",
                              lambda doc: [transform(doc, i, x, R(new_disp))], [i])
        if ok:
            self.sel_page, self.sel = i, list(self.ed.last_edit_result)   # (re-created ones have new xrefs)
            self._after_edit("Resized. Ctrl+Z undoes it.")
        return ok

    def restyle_selected(self, st):
        if not self.sel:
            return False
        i, xs = self.sel_page, list(self.sel)
        ok = self.ed.run_edit("annot_style", "Annotation properties", lambda doc: restyle(doc, i, xs, st), [i])
        if ok:
            self.sel_page, self.sel = i, list(self.ed.last_edit_result)   # a restyled callout is a new object
            self._after_edit("Properties changed. Ctrl+Z undoes it.")
        return ok

    def delete_selected(self):
        if not self.sel:
            return False
        i, xs = self.sel_page, list(self.sel)
        links_only = self._links_only()
        what = "link" if links_only else "annotation"
        ok = self.ed.run_edit(self._op("delete_annot"), f"Delete {what}", lambda doc: delete(doc, i, xs), [i])
        if ok:
            self.sel_page, self.sel = None, []
            self._after_edit(f"Deleted {len(xs)} {what}(s). Ctrl+Z undoes it.")
        return ok

    # ---------- links (stage 18) ----------
    def _links_only(self):
        doc = self.ed.doc
        return bool(self.sel) and doc is not None and all(LK.is_link(doc, x) for x in self.sel)

    def _op(self, op):
        """A selection of links moves / deletes with the link op (declared in oprules)."""
        return "link_edit" if self._links_only() else op

    def create_link(self, page, disp_rect, target):
        """Programmatic Insert › Link (tests): same path as drawing one."""
        return self._create(self.ed.canvas, page, "link", R(disp_rect), {"target": target})

    def retarget_selected(self, target):
        if not self._links_only() or len(self.sel) != 1:
            return False
        i, x = self.sel_page, self.sel[0]
        ok = self.ed.run_edit("link_edit", "Link target", lambda doc: LK.retarget(doc, i, x, target), [i])
        if ok:
            self._after_edit(f"Link now goes to {LK.describe(target)}. Ctrl+Z undoes it.")
        return ok

    def _show_links(self):
        """With the Link tool: a thin outline on every link of the pages around the current one
        (links are invisible on the page itself)."""
        self._remove_items(getattr(self, "_link_items", []))
        self._link_items = []
        c, doc = self.ed.canvas, self.ed.doc
        if self.tool != "link" or doc is None or c is None or not getattr(c, "pages", None):
            return
        cur = max(0, c.current_page())
        pen = QPen(QColor("#2d6cdf"), 0, Qt.DotLine)
        for i in range(max(0, cur - 2), min(doc.page_count, len(c.pages), cur + 3)):
            pg = doc[i]
            for x in LK.link_xrefs(doc, i):
                r = (LK.rect_of(doc, i, x) * pg.rotation_matrix).normalize()
                it = QGraphicsRectItem(c.page_to_scene(i, r))
                it.setPen(pen)
                it.setBrush(QBrush(QColor(45, 108, 223, 28)))
                it.setZValue(5)
                c.scene().addItem(it)
                self._link_items.append(it)
            del pg

    def select(self, page, xrefs):
        self.sel_page, self.sel = page, list(xrefs)
        self._draw_overlay()
        self.selectionChanged.emit()
        self.panel.show_for()

    def _after_edit(self, msg):
        self._show_links()
        self._draw_overlay()
        self.selectionChanged.emit()
        self.panel.show_for()
        self.win.statusBar().showMessage(msg, 4000)

    # ---------- selection state ----------
    def selected_annots(self):
        """[(page, Annot)] for the current selection: the page is returned so it stays alive."""
        doc = self.ed.doc
        if doc is None or self.sel_page is None or self.sel_page >= doc.page_count:
            return None, []
        pg = doc[self.sel_page]
        out = []
        for x in self.sel:
            a = None
            if is_field(doc, self.sel_page, x):
                from . import stage10
                w = stage10.widget(pg, x)
                a = FieldRef(w) if w is not None else None
            elif LK.is_link(doc, x):
                a = link_ref(doc, self.sel_page, x)
            else:
                try:
                    a = pg.load_annot(x)
                except Exception:
                    a = None
            if a is not None:
                out.append(a)
        return pg, out

    def clear_selection(self):
        self._cancel_gesture()
        if self.sel or self._overlay:
            self.sel_page, self.sel = None, []
            self._draw_overlay()
            self.selectionChanged.emit()
            self.panel.show_for()

    def _doc_changed(self):
        """After any change (an edit, undo/redo): keep only selected annotations that still exist."""
        pg, annots = self.selected_annots()
        alive = [a.xref for a in annots]
        if alive != self.sel:
            self.sel = alive
            if not alive:
                self.sel_page = None
        self._show_links()
        self._draw_overlay()
        self.panel.show_for()

    def _movable(self):
        pg, annots = self.selected_annots()
        return any(a.type[1] not in TEXT_MARKUP for a in annots)

    def _annot(self, pg, i, x):
        """The annotation xref on page i, or a FieldRef for a form field."""
        if is_field(self.ed.doc, i, x):
            from . import stage10
            return FieldRef(stage10.widget(pg, x))
        if LK.is_link(self.ed.doc, x):
            return LinkRef(self.ed.doc, i, x)
        return pg.load_annot(x)

    # ---------- forms ----------
    def fill_click(self, i, x):
        """Select tool on a form field: fill it (type text, toggle, pick a choice)."""
        from . import stage10
        pg = self.ed.doc[i]
        w = stage10.widget(pg, x)
        if w is None:
            return False
        t, name, cur = w.field_type, w.field_name, w.field_value
        choices = list(w.choice_values or [])
        del w, pg
        if t == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
            value = cur in (False, "Off", None, "")
        elif t == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
            value = True
        elif t in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
            value = ask_field_choice(self.win, name, choices, cur)
        elif t == pymupdf.PDF_WIDGET_TYPE_TEXT:
            value = ask_field_text(self.win, name, cur or "")
        else:
            return False
        if value is None:
            return True
        return self.fill_field(i, x, value)

    def fill_field(self, i, x, value):
        from . import stage10
        ok = self.ed.run_edit("form_fill", "Fill form field", lambda doc: stage10.fill_field(doc, i, x, value), [i])
        if ok:
            self.win.statusBar().showMessage("Field filled. Ctrl+Z undoes it.", 3000)
        return ok

    # ---------- overlay (selection frame + handles) ----------
    def _sel_rect_scene(self):
        pg, annots = self.selected_annots()
        if not annots:
            return None
        c = self.ed.canvas
        r = None
        for a in annots:
            q = c.page_to_scene(self.sel_page, to_disp_rect(pg, a.rect))
            r = q if r is None else r.united(q)
        return r

    def _remove_items(self, items):
        for it in items:
            try:
                sc = it.scene()
                if sc is not None:
                    sc.removeItem(it)
            except RuntimeError:
                pass

    def _draw_overlay(self):
        self._remove_items(self._overlay)
        self._overlay = []
        c = self.ed.canvas
        if self.ed.doc is None or not self.sel:
            return
        pg, annots = self.selected_annots()
        pen = QPen(QColor("#2d6cdf"), 0, Qt.DashLine)
        for a in annots:
            it = QGraphicsRectItem(c.page_to_scene(self.sel_page, to_disp_rect(pg, a.rect)).adjusted(-2, -2, 2, 2))
            it.setPen(pen)
            it.setZValue(6)
            c.scene().addItem(it)
            self._overlay.append(it)
        if len(annots) == 1 and annots[0].type[1] in RESIZABLE:
            hs = HANDLE / max(c.zoom, 0.05)
            for k, pt in self._handles(self._sel_rect_scene()).items():
                h = QGraphicsRectItem(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))
                h.setBrush(QBrush(QColor("#ffffff")))
                h.setPen(QPen(QColor("#2d6cdf"), 0))
                h.setZValue(7)
                h.setData(0, k)
                c.scene().addItem(h)
                self._overlay.append(h)

    @staticmethod
    def _handles(r):
        cx, cy = r.center().x(), r.center().y()
        return {"tl": r.topLeft(), "t": QPointF(cx, r.top()), "tr": r.topRight(), "r": QPointF(r.right(), cy),
                "br": r.bottomRight(), "b": QPointF(cx, r.bottom()), "bl": r.bottomLeft(), "l": QPointF(r.left(), cy)}

    def _handle_at(self, c, sp):
        for it in self._overlay:
            try:
                k = it.data(0)
                if k and it.rect().adjusted(-2, -2, 2, 2).contains(sp):
                    return k
            except RuntimeError:
                pass
        return None

    def _target_rect(self, d):
        r0, cur = d["rect"], d.get("cur")
        if r0 is None or cur is None:
            return r0
        dx, dy = cur.x() - d["start"].x(), cur.y() - d["start"].y()
        if d["kind"] == "move":
            return r0.translated(dx, dy)
        h, r = d["handle"], QRectF(r0)
        if "l" in h:
            r.setLeft(r.left() + dx)
        if "r" in h:
            r.setRight(r.right() + dx)
        if h.startswith("t"):
            r.setTop(r.top() + dy)
        if h.startswith("b"):
            r.setBottom(r.bottom() + dy)
        r = r.normalized()
        if r.width() < 4:
            r.setWidth(4)
        if r.height() < 4:
            r.setHeight(4)
        return r

    # ---------- rubber-band ghost while drawing / moving ----------
    def _clear_ghost(self, c=None):
        if self._ghost is not None:
            self._remove_items([self._ghost])
            self._ghost = None

    def _show_ghost(self, c):
        self._clear_ghost()
        d = self._drag
        if d is None:
            return
        pen = QPen(QColor("#ff9800"), 0, Qt.DashLine)
        if d["kind"] in ("move", "resize"):
            item = QGraphicsRectItem(self._target_rect(d))
        else:
            i = d["page"]
            o = c.pages[i]["rect"].topLeft()
            sc = lambda p: QPointF(o.x() + p.x, o.y() + p.y)
            if d["kind"] == "poly":
                path = QPainterPath(sc(self._poly[0]))
                for p in self._poly[1:] + ([d["cur"]] if d.get("cur") else []):
                    path.lineTo(sc(p))
                item = QGraphicsPathItem(path)
            elif self.tool == "ink":
                path = QPainterPath(sc(d["pts"][0]))
                for p in d["pts"][1:]:
                    path.lineTo(sc(p))
                item = QGraphicsPathItem(path)
                pen = QPen(QColor("#dc1a1a"), 0)
            elif self.tool in ("line", "arrow", "distance", "calibrate", "callout"):
                a, b = sc(d["start"]), sc(d.get("cur", d["start"]))
                item = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
            elif self.tool == "ellipse":
                item = QGraphicsEllipseItem(QRectF(sc(d["start"]), sc(d.get("cur", d["start"]))).normalized())
            else:
                item = QGraphicsRectItem(QRectF(sc(d["start"]), sc(d.get("cur", d["start"]))).normalized())
        item.setPen(pen)
        item.setZValue(8)
        c.scene().addItem(item)
        self._ghost = item

    def _cancel_gesture(self):
        self._drag = None
        self._poly = []
        self._clear_ghost()


def read_content(a):
    return (a.info or {}).get("content", "")


# ---------------- dialogs (module-level so the self-test can answer them) ----------------
IMAGE_FILTER = ("Images (*.png *.jpg *.jpeg *.webp *.gif *.bmp *.tif *.tiff *.svg);;"
                "SVG (rasterised on insert) (*.svg);;All files (*)")


class LinkTargetForm(QWidget):
    """Where a link goes: a page of this PDF (with the view on arrival) or a web address. Used by
    the new-link dialog and the Properties panel (stage 18)."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.kind = QComboBox()
        self.kind.addItem("A page in this PDF", "page")
        self.kind.addItem("A web address", "uri")
        self.page = QSpinBox()
        self.page.setMinimum(1)
        self.fit = QComboBox()
        for k in LK.FITS:
            self.fit.addItem(LK.FIT_LABELS[k], k)
        self.zoom = QSpinBox()
        self.zoom.setRange(10, 800)
        self.zoom.setSuffix(" %")
        self.zoom.setValue(100)
        self.uri = QLineEdit()
        self.uri.setPlaceholderText("https://…")
        self.rows = {}
        for key, label, w in (("kind", "Goes to", self.kind), ("page", "Page", self.page),
                              ("fit", "On arrival", self.fit), ("zoom", "Zoom", self.zoom),
                              ("uri", "Address", self.uri)):
            lab = QLabel(label)
            f.addRow(lab, w)
            self.rows[key] = (lab, w)
        self.kind.currentIndexChanged.connect(lambda _: self._sync())
        self.fit.currentIndexChanged.connect(lambda _: self._sync())
        self._sync()

    def _sync(self):
        page = self.kind.currentData() == "page"
        zoom = page and self.fit.currentData() == "zoom"
        for key, on in (("page", page), ("fit", page), ("zoom", zoom), ("uri", not page)):
            for w in self.rows[key]:
                w.setVisible(on)

    def set_pages(self, n):
        self.page.setMaximum(max(1, n))
        self.page.setSuffix(f" of {max(1, n)}")

    def set_target(self, t):
        t = t or {"kind": "page", "page": 0}
        if t.get("kind") not in ("page", "uri"):
            t = {"kind": "page", "page": 0}
        self.kind.setCurrentIndex(self.kind.findData(t["kind"]))
        self.page.setValue(int(t.get("page", 0)) + 1)
        self.fit.setCurrentIndex(max(0, self.fit.findData(t.get("fit", "keep"))))
        self.zoom.setValue(int(t.get("zoom", 100)))
        self.uri.setText(t.get("uri", ""))
        self._sync()

    def target(self):
        if self.kind.currentData() == "uri":
            uri = self.uri.text().strip()
            if uri and "://" not in uri and not uri.startswith("mailto:"):
                uri = "https://" + uri                      # "example.com" means the web page
            return {"kind": "uri", "uri": uri} if uri else None
        return {"kind": "page", "page": self.page.value() - 1, "fit": self.fit.currentData(),
                "zoom": self.zoom.value()}


def ask_link_target(parent, n_pages, current=None, doc=None):
    """The new link's target: a page (and the view on arrival) or a web address; None if cancelled."""
    dlg = QDialog(parent)
    dlg.setWindowTitle("Link")
    v = QVBoxLayout(dlg)
    form = LinkTargetForm(dlg)
    form.set_pages(n_pages)
    form.set_target(current)
    v.addWidget(form)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(dlg.accept)
    bb.rejected.connect(dlg.reject)
    v.addWidget(bb)
    form.uri.setFocus() if form.kind.currentData() == "uri" else form.page.setFocus()
    while dlg.exec() == QDialog.Accepted:
        t = form.target()
        if t is not None:
            return t
        from PySide6.QtWidgets import QMessageBox
        from . import APP_NAME
        QMessageBox.warning(parent, APP_NAME, "Type the web address the link should open.")
    return None


def ask_field_text(parent, name, current):
    from PySide6.QtWidgets import QInputDialog
    v, ok = QInputDialog.getText(parent, "Fill field", f"{name}:", QLineEdit.Normal, current)
    return v if ok else None


def ask_field_choice(parent, name, choices, current):
    from PySide6.QtWidgets import QInputDialog
    if not choices:
        return None
    i = choices.index(current) if current in choices else 0
    v, ok = QInputDialog.getItem(parent, "Fill field", f"{name}:", choices, i, False)
    return v if ok else None


def ask_image_file(parent):
    p, _ = QFileDialog.getOpenFileName(parent, "Insert image", "", IMAGE_FILTER)
    return p or None


def ask_calibration(parent, length_pt):
    """-> (real length, unit) for a drawn line of length_pt points, or None."""
    d = QDialog(parent)
    d.setWindowTitle("Calibrate scale")
    f = QFormLayout(d)
    f.addRow(QLabel(f"The line you drew is {length_pt:.1f} pt on the page.\nWhat is its real length?"))
    val = QDoubleSpinBox()
    val.setRange(0.0001, 1e9)
    val.setDecimals(4)
    val.setValue(1.0)
    unit = QComboBox()
    for u, _ in CE.UNITS:
        unit.addItem(u)
    unit.setEditable(True)
    f.addRow("Length", val)
    f.addRow("Unit", unit)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(d.accept)
    bb.rejected.connect(d.reject)
    f.addRow(bb)
    if d.exec() != QDialog.Accepted:
        return None
    return val.value(), unit.currentText().strip() or "units"


class SignaturePad(QWidget):
    """Draw with the mouse (or a pen); strokes in pad coordinates."""

    def __init__(self, w=600, h=200):
        super().__init__()
        self.setFixedSize(w, h)
        self.strokes = []
        self.setAttribute(Qt.WA_StaticContents)

    def mousePressEvent(self, e):
        self.strokes.append([(e.position().x(), e.position().y())])
        self.update()

    def mouseMoveEvent(self, e):
        if self.strokes:
            self.strokes[-1].append((e.position().x(), e.position().y()))
            self.update()

    def clear(self):
        self.strokes = []
        self.update()

    def paintEvent(self, e):
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#ffffff"))
        p.setPen(QPen(QColor("#c8c8c8"), 1, Qt.DashLine))
        p.drawLine(20, self.height() - 40, self.width() - 20, self.height() - 40)
        p.setPen(QPen(QColor(20, 30, 90), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for st in self.strokes:
            for a, b in zip(st, st[1:]):
                p.drawLine(QPointF(*a), QPointF(*b))
        p.end()


class SignatureDialog(QDialog):
    def __init__(self, parent, how):
        super().__init__(parent)
        self.setWindowTitle("Signature")
        v = QVBoxLayout(self)
        self.png = None
        self.pad = None
        if how == "draw":
            v.addWidget(QLabel("Sign in the box:"))
            self.pad = SignaturePad()
            v.addWidget(self.pad)
            clear = QPushButton("Clear")
            clear.clicked.connect(self.pad.clear)
            v.addWidget(clear)
        else:
            p, _ = QFileDialog.getOpenFileName(parent, "Signature image", "", IMAGE_FILTER)
            if p:
                self.png = CE.load_image(p)[0]
            v.addWidget(QLabel("Signature image loaded." if self.png else "No image chosen."))
        self.save = QCheckBox("Save to my signatures (kept on this computer only)")
        self.save.setChecked(True)
        self.name = QLineEdit("My signature")
        row = QHBoxLayout()
        row.addWidget(self.save)
        row.addWidget(self.name)
        v.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Place it")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def result_png(self):
        if self.pad is not None:
            return CE.strokes_to_png(self.pad.strokes, self.pad.width(), self.pad.height()) if self.pad.strokes else None
        return self.png


def ask_signature(parent, how):
    """-> (png bytes, save?, name) or None."""
    d = SignatureDialog(parent, how)
    if d.exec() != QDialog.Accepted:
        return None
    png = d.result_png()
    return (png, d.save.isChecked(), d.name.text()) if png else None


# ---------------- Properties panel ----------------
class ColorButton(QPushButton):
    changed = Signal(object)

    def __init__(self, allow_none=False):
        super().__init__()
        self.color = None
        self.allow_none = allow_none
        self.setFixedWidth(64)
        self.clicked.connect(self._pick)

    def set(self, rgb):
        self.color = tuple(rgb) if rgb else None
        if self.color:
            q = QColor.fromRgbF(*self.color[:3])
            self.setStyleSheet(f"background:{q.name()};border:1px solid #3a4150")
            self.setText("")
        else:
            self.setStyleSheet("")
            self.setText("none")

    def _pick(self):
        start = QColor.fromRgbF(*self.color[:3]) if self.color else QColor("#ffffff")
        q = QColorDialog.getColor(start, self, "Colour")
        if q.isValid():
            self.set((q.redF(), q.greenF(), q.blueF()))
            self.changed.emit(self.color)


class PropertiesPanel(QObject):
    """The selected annotation's properties (every change one undo step); with nothing
    selected, the defaults for the active tool's next annotation."""

    def __init__(self, ctl, win):
        super().__init__(win)
        self.ctl = ctl
        self._loading = False
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(8, 8, 8, 8)
        self.title = QLabel("No annotation selected")
        self.title.setWordWrap(True)
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        v.addWidget(self.title)
        form = QFormLayout()
        self.stroke = ColorButton()
        self.fill = ColorButton(allow_none=True)
        self.no_fill = QCheckBox("No fill")
        fill_row = QWidget()
        fh = QHBoxLayout(fill_row)
        fh.setContentsMargins(0, 0, 0, 0)
        fh.addWidget(self.fill)
        fh.addWidget(self.no_fill)
        fh.addStretch(1)
        self.opacity = QSpinBox()
        self.opacity.setRange(5, 100)
        self.opacity.setSuffix(" %")
        self.width = QDoubleSpinBox()
        self.width.setRange(0, 20)
        self.width.setSingleStep(0.5)
        self.width.setSuffix(" pt")
        self.start_end = QComboBox()
        self.end_end = QComboBox()
        for code, label in LINE_ENDS:
            self.start_end.addItem(label, code)
            self.end_end.addItem(label, code)
        self.font = QComboBox()
        for code, label in FONTS:
            self.font.addItem(label, code)
        self.size = QDoubleSpinBox()
        self.size.setRange(4, 96)
        self.size.setSuffix(" pt")
        self.text_color = ColorButton()
        self.author = QLineEdit()
        self.author.setPlaceholderText("(none)")
        self.text = QPlainTextEdit()
        self.text.setFixedHeight(90)
        self.apply_text = QPushButton("Apply text")
        self.fname = QLineEdit()
        self.choices = QLineEdit()
        self.choices.setPlaceholderText("Option 1, Option 2, …")
        self.reading = QLabel("")
        self.reading.setWordWrap(True)
        self.link_form = LinkTargetForm()
        self.link_apply = QPushButton("Apply target")
        self.link_apply.setToolTip("Point the selected link at this target (one undo step)")
        self.sample = QPushButton("Sample the page colour again")
        self.sample.setToolTip("Re-read the page's background around this whiteout")
        self.rows = {}
        for key, label, w in (("stroke", "Colour", self.stroke), ("fill", "Fill", fill_row),
                              ("opacity", "Opacity", self.opacity), ("width", "Border width", self.width),
                              ("start", "Line start", self.start_end), ("end", "Line end", self.end_end),
                              ("font", "Font", self.font), ("size", "Font size", self.size),
                              ("text_color", "Text colour", self.text_color), ("author", "Author", self.author),
                              ("reading", "Reading", self.reading), ("sample", "", self.sample),
                              ("fname", "Field name", self.fname), ("choices", "Choices", self.choices)):
            lab = QLabel(label)
            form.addRow(lab, w)
            self.rows[key] = (lab, w)
        v.addLayout(form)
        v.addWidget(self.link_form)
        v.addWidget(self.link_apply)
        self.link_apply.clicked.connect(self._apply_link)
        self.text_label = QLabel("Text")
        v.addWidget(self.text_label)
        v.addWidget(self.text)
        v.addWidget(self.apply_text)
        self.delete_btn = QPushButton("Delete annotation")
        self.delete_btn.clicked.connect(lambda: ctl.delete_selected())
        v.addWidget(self.delete_btn)
        hint = QLabel("Every change here is one undo step (Ctrl+Z). With nothing selected, changes "
                      "apply to the next annotation the active tool makes.")
        hint.setObjectName("legend")
        hint.setWordWrap(True)
        v.addWidget(hint)
        v.addStretch(1)
        self.dock = QDockWidget("Properties", win)
        self.dock.setObjectName("propsdock")
        self.dock.setWidget(body)
        # edits
        self.stroke.changed.connect(lambda c: self._set({"stroke": c}))
        self.fill.changed.connect(lambda c: self._set({"fill": c}))
        self.no_fill.toggled.connect(lambda on: self._set({"fill": None if on else (self.fill.color or (1, 1, 1))}))
        self.opacity.editingFinished.connect(lambda: self._set({"opacity": self.opacity.value() / 100}))
        self.width.editingFinished.connect(lambda: self._set({"width": self.width.value()}))
        self.start_end.activated.connect(lambda _: self._set({"ends": (self.start_end.currentData(), self.end_end.currentData())}))
        self.end_end.activated.connect(lambda _: self._set({"ends": (self.start_end.currentData(), self.end_end.currentData())}))
        self.font.activated.connect(lambda _: self._set({"fontname": self.font.currentData()}))
        self.size.editingFinished.connect(lambda: self._set({"fontsize": self.size.value()}))
        self.text_color.changed.connect(lambda c: self._set({"text_color": c}))
        self.author.editingFinished.connect(lambda: self._set({"author": self.author.text()}))
        self.apply_text.clicked.connect(lambda: self._set({"text": self.text.toPlainText()}))
        self.sample.clicked.connect(self._resample)
        self.fname.editingFinished.connect(lambda: self._set({"field_name": self.fname.text()}))
        self.choices.editingFinished.connect(
            lambda: self._set({"choices": [c.strip() for c in self.choices.text().split(",") if c.strip()]}))
        self.show_for()

    def _resample(self):
        ctl = self.ctl
        pg, annots = ctl.selected_annots()
        if not annots:
            return
        color = CE.sample_background(pg, to_disp_rect(pg, annots[0].rect))
        del annots, pg
        ctl.restyle_selected({"fill": color})

    def _set(self, st):
        if self._loading:
            return
        ctl = self.ctl
        if ctl.sel:
            ctl.restyle_selected(st)
        elif ctl.tool in ctl.defaults:
            ctl.defaults[ctl.tool].update(st)
            ctl.win.statusBar().showMessage("Default for the next annotation updated.", 3000)

    def _apply_link(self):
        t = self.link_form.target()
        if t is None:
            from PySide6.QtWidgets import QMessageBox
            from . import APP_NAME
            QMessageBox.warning(self.ctl.win, APP_NAME, "Type the web address the link should open.")
            return
        self.ctl.retarget_selected(t)

    def _show(self, keys):
        for key, (lab, w) in self.rows.items():
            on = key in keys
            lab.setVisible(on)
            w.setVisible(on)
        for w in (self.link_form, self.link_apply):
            w.setVisible("link" in keys)
        textual = "text" in keys
        for w in (self.text_label, self.text, self.apply_text):
            w.setVisible(textual)

    def show_for(self):
        """Fill the panel from the selection (or the active tool's defaults)."""
        ctl = self.ctl
        self._loading = True
        try:
            pg, annots = ctl.selected_annots()
            role, data = "", {}
            if annots and isinstance(annots[0], LinkRef):
                n = len(annots)
                t = annots[0].target
                self.title.setText(("Link" if n == 1 else f"{n} links") + f" on page {ctl.sel_page + 1}"
                                   + (f"\nGoes to: {LK.describe(t)}" if n == 1 else ""))
                self.delete_btn.setVisible(True)
                self.delete_btn.setText("Delete link")
                self._show({"link"} if n == 1 and t.get("kind") in ("page", "uri") else set())
                self.link_form.set_pages(pg.parent.page_count)
                self.link_form.set_target(t)
                return
            self.delete_btn.setText("Delete annotation")
            if not annots and ctl.tool == "link":
                self.title.setText("Link: drag a box over the spot, then pick where it goes.\n"
                                   "Click an existing link to select it.")
                self._show(())
                self.delete_btn.setVisible(False)
                return
            if annots and isinstance(annots[0], FieldRef):
                w = annots[0].w
                self.title.setText(f"{w.field_type_string} field on page {ctl.sel_page + 1}")
                self.delete_btn.setVisible(True)
                keys = {"fname", "size"}
                if w.field_type in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
                    keys.add("choices")
                self._show(keys)
                self.fname.setText(w.field_name or "")
                self.choices.setText(", ".join(w.choice_values or []))
                self.size.setValue(float(w.text_fontsize or 0) or 11.0)
                return
            if annots:
                st = read_style(annots[0])
                t = st["type"]
                data = CE.get_data(pg.parent, annots[0].xref)
                role = data.get("role", "")
                if role == "callout":
                    st.update({k: (tuple(v) if isinstance(v, list) else v) for k, v in data.items()
                               if k in CE.CALLOUT_DEFAULTS})
                    st["stroke"] = st.get("border")
                    st["ends"] = (0, st.get("line_end", 0))
                n = len(annots)
                name = {"whiteout": "Whiteout", "image": "Image", "signature": "Signature", "distance": "Distance",
                        "area": "Area", "area_label": "Area reading", "callout": "Callout"}.get(role, f"{t} annotation")
                self.title.setText(name + (f" (+{n - 1} more)" if n > 1 else "") + f" on page {ctl.sel_page + 1}")
                self.delete_btn.setVisible(True)
            elif ctl.tool in ctl.defaults:
                st = dict(ctl.defaults[ctl.tool])
                role = ctl.tool if ctl.tool in ("whiteout", "image", "signature", "distance", "area", "callout") else ""
                if role == "callout":
                    st["stroke"] = st.get("border")
                    st["ends"] = (0, st.get("line_end", 0))
                t = {"highlight": "Highlight", "underline": "Underline", "strike": "StrikeOut", "ink": "Ink",
                     "rect": "Square", "ellipse": "Circle", "line": "Line", "arrow": "Line", "polygon": "Polygon",
                     "note": "Text", "freetext": "FreeText", "stamp": "Stamp", "whiteout": "Square", "image": "Stamp",
                     "signature": "Stamp", "distance": "Line", "area": "Polygon", "callout": "FreeText",
                     "redact": "Redact", "redact_text": "Redact",
                     **{k: "Widget" for k in FORM_TOOLS}}[ctl.tool]
                if ctl.tool in FORM_TOOLS:
                    self.title.setText("Drag a box to add the field.")
                    self._show(())
                    self.delete_btn.setVisible(False)
                    return
                self.title.setText(f"Next {role or t} (defaults)".replace("Next whiteout (defaults)",
                                   "Next whiteout (colour sampled from the page unless set here)"))
                self.delete_btn.setVisible(False)
            else:
                self.title.setText("No annotation selected.\nPick a MARKUP tool, or click an annotation.")
                self._show(())
                self.delete_btn.setVisible(False)
                return
            keys = {"opacity", "author"}
            if t in TEXT_MARKUP + ("Ink", "Square", "Circle", "Line", "Polygon", "PolyLine", "Text"):
                keys.add("stroke")
            if t == "Redact":
                keys = {"fill"}
            if t in ("Square", "Circle", "Polygon", "FreeText"):
                keys.add("fill")
            if t in ("Ink", "Square", "Circle", "Line", "Polygon", "PolyLine", "FreeText"):
                keys.add("width")
            if t in ("Line", "PolyLine"):
                keys |= {"start", "end"}
            if t == "FreeText":
                keys |= {"font", "size", "text_color", "text"}
            if t == "Text":
                keys.add("text")
            if not annots:
                keys.discard("author")
            # 9b roles override the generic rows
            if role == "whiteout":
                keys = {"fill"} | ({"sample"} if annots else set())
            elif role in ("image", "signature"):
                keys = {"opacity", "author"} if annots else {"opacity"}
            elif role == "distance":
                keys = {"stroke", "width"} | ({"reading", "author"} if annots else set())
            elif role == "area":
                keys = {"stroke", "fill", "width", "opacity"} | ({"reading", "author"} if annots else set())
            elif role == "callout":
                keys = {"stroke", "fill", "width", "end", "font", "size", "text_color", "text"}
            self.rows["stroke"][0].setText("Border colour" if role == "callout" else "Colour")
            self.rows["end"][0].setText("Leader end" if role == "callout" else "Line end")
            self.rows["width"][0].setText("Border width" if role in ("callout", "") else "Line width")
            if role in ("distance", "area") and annots:
                sc = CE.Scale(data["factor"], data["unit"], data["source"])
                self.reading.setText(f"{read_content(annots[0])}\nScale: {sc.describe()}")
            elif role in ("distance", "area"):
                self.reading.setText("")
            if role in ("distance", "area") and not annots and ctl.ed.doc is not None:
                p_ = ctl.ed.canvas.current_page()
                self.title.setText(self.title.text() + f"\nScale here: {ctl.scale(max(0, p_)).describe()}")
            self._show(keys)
            self.stroke.set(st.get("stroke"))
            fill = st.get("fill")
            self.fill.set(fill)
            self.no_fill.setChecked(fill is None)
            self.opacity.setValue(int(round(100 * float(st.get("opacity", 1.0)))))
            self.width.setValue(float(st.get("width", 1.0) or 0))
            ends = st.get("ends", (0, 0))
            self.start_end.setCurrentIndex(max(0, self.start_end.findData(ends[0])))
            self.end_end.setCurrentIndex(max(0, self.end_end.findData(ends[1])))
            self.font.setCurrentIndex(max(0, self.font.findData(st.get("fontname", "helv"))))
            self.size.setValue(float(st.get("fontsize", 12)))
            self.text_color.set(st.get("text_color", BLACK))
            self.author.setText(st.get("author", "") or "")
            self.text.setPlainText(st.get("text", "") or "")
            self.text_label.setText("Note text" if t == "Text" else "Text")
        finally:
            self._loading = False
