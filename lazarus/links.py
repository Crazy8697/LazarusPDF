"""Stage 18: Insert › Link. Link annotations made, moved, re-targeted and deleted inside ONE
journal operation each (declared and probed in oprules.py: link_add, link_edit). No Qt here.

A link is a standard /Link annotation: a page in this document (a /Dest array pointing at the
page object, so it follows its page through page ops and the stage 17 page graft) or a URL
(/A /URI). The raw keys are written directly so the target, the arrival view and the box are
exactly what was asked for:
  - arrival view "keep" (/XYZ, the reader's zoom), "fit" (/Fit), "width" (/FitH) or "zoom"
    (/XYZ at a percentage), all at the top of the target page;
  - the box (/Rect) comes from displayed page coordinates (rotated pages included).
PyMuPDF's delete_link() deletes the object, which the journal can't undo; a link is removed by
dropping it from the page's /Annots instead (the object stays as unreferenced garbage)."""
import re

import pymupdf

R = pymupdf.Rect
FITS = ("keep", "fit", "width", "zoom")
FIT_LABELS = {"keep": "Keep the reader's zoom", "fit": "Fit the page", "width": "Fit the width",
              "zoom": "Zoom to a percentage"}


def _annot_refs(doc, page_xref):
    kind, val = doc.xref_get_key(page_xref, "Annots")
    if kind == "xref":
        val = doc.xref_object(int(val.split()[0]))
    elif kind != "array":
        return []
    return [int(x) for x in re.findall(r"(\d+)\s+0\s+R", val)]


def _set_annot_refs(doc, page_xref, refs):
    kind, val = doc.xref_get_key(page_xref, "Annots")
    text = "[" + " ".join(f"{r} 0 R" for r in refs) + "]"
    if kind == "xref":
        doc.update_object(int(val.split()[0]), text)
    else:
        doc.xref_set_key(page_xref, "Annots", text)


def is_link(doc, xref):
    try:
        return doc.xref_get_key(xref, "Subtype")[1] == "/Link"
    except Exception:
        return False


def link_xrefs(doc, pno):
    """The link annotations of page pno, in drawing order."""
    return [x for x in _annot_refs(doc, doc.page_xref(pno)) if is_link(doc, x)]


def _pdf_str(s):
    return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def _unescape(s):
    s = s[1:-1] if s.startswith("(") and s.endswith(")") else s
    return re.sub(r"\\(.)", r"\1", s)


# ---------------- geometry ----------------
def rect_of(doc, pno, xref):
    """The link's box in UNROTATED page space (like Annot.rect)."""
    pg = doc[pno]
    v = doc.xref_get_key(xref, "Rect")[1].strip("[]").split()
    return (R([float(t) for t in v]) * pg.transformation_matrix).normalize()


def _write_rect(doc, pno, xref, disp):
    pg = doc[pno]
    pdf = (R(disp) * pg.derotation_matrix * ~pg.transformation_matrix).normalize()
    doc.xref_set_key(xref, "Rect", f"[{pdf.x0:g} {pdf.y0:g} {pdf.x1:g} {pdf.y1:g}]")


# ---------------- targets ----------------
def _dest(doc, target):
    p = int(target["page"])
    pg = doc[p]
    top = (pymupdf.Point(0, 0) * ~pg.transformation_matrix).y            # the page's top edge, PDF space
    fit = target.get("fit", "keep")
    if fit == "fit":
        view = "/Fit"
    elif fit == "width":
        view = f"/FitH {top:g}"
    elif fit == "zoom":
        view = f"/XYZ 0 {top:g} {max(0.1, float(target.get('zoom', 100)) / 100):g}"
    else:
        view = f"/XYZ 0 {top:g} 0"
    return f"[{doc.page_xref(p)} 0 R{view}]"


def _write_target(doc, xref, target):
    if target["kind"] == "uri":
        uri = target.get("uri", "").strip()
        if not uri:
            raise ValueError("A link needs a web address.")
        doc.xref_set_key(xref, "Dest", "null")
        doc.xref_set_key(xref, "A", f"<</S/URI/URI{_pdf_str(uri)}>>")
    else:
        if not 0 <= int(target["page"]) < doc.page_count:
            raise ValueError("That page isn't in this document.")
        doc.xref_set_key(xref, "A", "null")
        doc.xref_set_key(xref, "Dest", _dest(doc, target))


def target_of(doc, xref):
    """{"kind": "page", "page", "fit", "zoom"} or {"kind": "uri", "uri"}; {"kind": "other"} for
    anything else (a named destination, a launch action…)."""
    page_of = {doc.page_xref(i): i for i in range(doc.page_count)}
    kind, a = doc.xref_get_key(xref, "A/S")
    if a == "/URI":
        return {"kind": "uri", "uri": _unescape(doc.xref_get_key(xref, "A/URI")[1])}
    kind, dest = doc.xref_get_key(xref, "Dest")
    if kind != "array" and a == "/GoTo":
        kind, dest = doc.xref_get_key(xref, "A/D")
    if kind != "array":
        return {"kind": "other"}
    m = re.match(r"\s*\[\s*(\d+)\s+0\s+R\s*/(\w+)\s*([^\]]*)\]", dest)
    if not m or int(m.group(1)) not in page_of:
        return {"kind": "other"}
    view, args = m.group(2), m.group(3).split()
    t = {"kind": "page", "page": page_of[int(m.group(1))], "fit": "keep", "zoom": 100}
    if view in ("Fit", "FitB"):
        t["fit"] = "fit"
    elif view in ("FitH", "FitBH"):
        t["fit"] = "width"
    elif view == "XYZ" and len(args) >= 3:
        try:
            z = float(args[2])
        except ValueError:
            z = 0.0
        if z > 0:
            t["fit"], t["zoom"] = "zoom", round(z * 100)
    return t


def describe(target):
    if target["kind"] == "uri":
        return target.get("uri", "")
    if target["kind"] == "page":
        arrive = {"keep": "", "fit": ", fit page", "width": ", fit width",
                  "zoom": f", {target.get('zoom', 100)}%"}[target.get("fit", "keep")]
        return f"Page {target['page'] + 1}{arrive}"
    return "Another kind of link (kept as it is)"


# ---------------- operations (inside one journal operation) ----------------
def create(doc, pno, disp_rect, target):
    """A new link on page pno over displayed rect disp_rect. Returns its xref."""
    pg = doc[pno]
    before = set(_annot_refs(doc, pg.xref))
    pg.insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(0, 0, 10, 10), "uri": "about:blank"})
    new = [x for x in _annot_refs(doc, pg.xref) if x not in before]
    if len(new) != 1:
        raise RuntimeError("The link couldn't be made.")
    x = new[0]
    _write_rect(doc, pno, x, disp_rect)
    _write_target(doc, x, target)
    return x


def move(doc, pno, xref, disp_rect):
    _write_rect(doc, pno, xref, disp_rect)
    return xref


def retarget(doc, pno, xref, target):
    _write_target(doc, xref, target)
    return xref


def delete(doc, pno, xrefs):
    px = doc.page_xref(pno)
    gone = set(xrefs)
    _set_annot_refs(doc, px, [r for r in _annot_refs(doc, px) if r not in gone])
    return []


def hit(doc, pno, pt_disp, slack=2.0):
    """Topmost link whose displayed box contains pt_disp: its xref, or None."""
    pg = doc[pno]
    found = None
    for x in link_xrefs(doc, pno):
        r = (rect_of(doc, pno, x) * pg.rotation_matrix).normalize()
        if R(r.x0 - slack, r.y0 - slack, r.x1 + slack, r.y1 + slack).contains(pt_disp):
            found = x
    return found
