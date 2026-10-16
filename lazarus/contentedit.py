"""Stage 9b: page-content tools: whiteout, insert image, signature, measurement (distance,
area, scale) and callouts.

Each placed item is an annotation tagged with its role in a private /LazarusData key (a hex
string holding JSON), so the 9a machinery selects, moves, resizes, restyles and deletes it,
and every change is ONE journal step (ops declared and probed in oprules.py).

- Whiteout: an opaque, borderless rectangle filled with the page's own background colour,
  sampled from the page right where it is dropped (the dominant colour; the Properties
  panel overrides it). It covers content visually; it does NOT remove what is underneath
  (that is redaction, stage 10).
- Image / signature: a stamp annotation carrying the picture. PNG and JPEG go in as they are;
  WebP and every other raster format are decoded by Pillow first (PyMuPDF can't read WebP, and
  a refused image can leave a half-made stamp behind). SVG is rasterised by MuPDF at its own
  size and 300 dpi, so it is crisp at its intended dimensions; once placed it is a bitmap.
- Signatures: a local library of PNGs (draw once or import, reuse), kept in the app-data
  folder (next to the test settings when LAZARUS_SETTINGS_INI is set). Never in the repo,
  never sent anywhere. Or place a one-off without saving it.
- Measurement: distance = a dimension line whose caption is the reading; area = a polygon
  plus its reading label. The scale is the PDF's own (/VP viewport /Measure) when the page
  declares one, else a manual calibration (a known distance + its real length), else the
  paper size in mm. Each measurement carries a standard /Measure dictionary too.
- Callout: a text box with a leader line and arrow pointing at a spot (a rich-text FreeText
  callout, so its border colour can be set). Moving the box keeps the tip on its spot."""
import io
import json
import math
import os
import re
import uuid
from pathlib import Path

import pymupdf

R = pymupdf.Rect
P = pymupdf.Point
ROLE_KEY = "LazarusData"
SVG_DPI = 300
UNITS = [("mm", 25.4 / 72), ("cm", 2.54 / 72), ("m", 0.0254 / 72), ("in", 1 / 72), ("ft", 1 / 864), ("pt", 1.0)]
UNIT_FACTOR = dict(UNITS)          # unit -> that unit per PDF point (paper size)


# ---------------- role data on an annotation ----------------
def set_data(doc, xref, data):
    doc.xref_set_key(xref, ROLE_KEY, "<" + json.dumps(data).encode().hex() + ">")


def get_data(doc, xref):
    try:
        kind, val = doc.xref_get_key(xref, ROLE_KEY)
        return json.loads(val) if kind == "string" else {}
    except Exception:
        return {}


def role_of(annot):
    return get_data(annot.parent.parent, annot.xref).get("role", "")


# ---------------- whiteout: the page's background colour ----------------
def sample_background(page, rect, margin=6):
    """Dominant colour of the page (content only, no annotations) in and right around rect
    (displayed coordinates). Returns (r, g, b) floats."""
    unrot = (R(rect) * page.derotation_matrix).normalize()
    area = R(unrot.x0 - margin, unrot.y0 - margin, unrot.x1 + margin, unrot.y1 + margin) & page.rect
    if area.is_empty:
        return (1.0, 1.0, 1.0)
    scale = min(1.0, 160 / max(area.width, area.height))     # a small render is plenty
    pix = page.get_pixmap(clip=area, matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=False)
    counts = {}
    s, n = pix.samples, pix.n
    for i in range(0, len(s) - n + 1, n):
        key = (s[i] >> 3, s[i + 1] >> 3, s[i + 2] >> 3)        # 32 levels per channel
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return (1.0, 1.0, 1.0)
    best = max(counts, key=counts.get)
    # average the real pixels of that bucket (not the bucket's corner)
    tot = [0, 0, 0]
    k = 0
    for i in range(0, len(s) - n + 1, n):
        if (s[i] >> 3, s[i + 1] >> 3, s[i + 2] >> 3) == best:
            tot[0] += s[i]
            tot[1] += s[i + 1]
            tot[2] += s[i + 2]
            k += 1
    return tuple(round(c / k / 255, 4) for c in tot)


def create_whiteout(doc, pno, rect_disp, color):
    pg = doc[pno]
    a = pg.add_rect_annot((R(rect_disp) * pg.derotation_matrix).normalize())
    a.set_colors(stroke=color, fill=color)
    a.set_border(width=0)
    a.set_opacity(1.0)
    a.set_info(subject="Whiteout")
    a.update()
    set_data(doc, a.xref, {"role": "whiteout"})
    return a.xref


# ---------------- images (and SVG) ----------------
def load_image(path=None, data=None, name=""):
    """Image file -> (png_or_jpeg_bytes, width_pt, height_pt, kind). SVG is rasterised here."""
    if data is None:
        data = Path(path).read_bytes()
        name = name or Path(path).name
    low = name.lower()
    if low.endswith(".svg") or data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:300]:
        src = pymupdf.open(stream=data, filetype="svg")
        try:
            pg = src[0]
            w, h = pg.rect.width, pg.rect.height
            pix = pg.get_pixmap(dpi=SVG_DPI, alpha=True)
            return pix.tobytes("png"), w, h, "svg"
        finally:
            src.close()
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    im.load()
    dpi = im.info.get("dpi", (96, 96))
    try:
        dx, dy = float(dpi[0]) or 96.0, float(dpi[1]) or 96.0
    except Exception:
        dx = dy = 96.0
    w, h = im.width * 72 / dx, im.height * 72 / dy
    if im.format in ("PNG", "JPEG") and im.mode in ("RGB", "RGBA", "L", "LA", "P", "CMYK"):
        return data, w, h, im.format.lower()
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "A" in im.mode or im.mode == "P" else "RGB")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue(), w, h, (im.format or "image").lower()


def fit_rect(rect, w, h):
    """The largest rect of aspect w:h centred in rect."""
    rect = R(rect)
    if w <= 0 or h <= 0:
        return rect
    s = min(rect.width / w, rect.height / h)
    cx, cy = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
    return R(cx - w * s / 2, cy - h * s / 2, cx + w * s / 2, cy + h * s / 2)


def create_image(doc, pno, rect_disp, img_bytes, role="image", subject="Image"):
    pg = doc[pno]
    a = pg.add_stamp_annot((R(rect_disp) * pg.derotation_matrix).normalize(), stamp=img_bytes)
    a.set_info(subject=subject)
    a.update()
    set_data(doc, a.xref, {"role": role})
    return a.xref


# ---------------- signature library ----------------
class SignatureLibrary:
    """Saved signatures: PNG files in a local folder. Never in the repo, never sent anywhere."""

    def __init__(self, folder=None):
        self.folder = Path(folder) if folder else self.default_folder()

    @staticmethod
    def default_folder():
        ini = os.environ.get("LAZARUS_SETTINGS_INI")
        if ini:                                             # tests: next to the isolated settings
            return Path(ini).parent / "lazarus-signatures"
        from PySide6.QtCore import QStandardPaths
        base = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation) or str(Path.home() / ".lazarus")
        return Path(base) / "signatures"

    def list(self):
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.glob("*.png"))

    def add(self, png_bytes, name):
        self.folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w\- ]", "", name).strip() or "Signature"
        p = self.folder / f"{safe}.png"
        k = 2
        while p.exists():
            p = self.folder / f"{safe} {k}.png"
            k += 1
        p.write_bytes(png_bytes)
        return p

    def remove(self, path):
        p = Path(path)
        if p.parent == self.folder and p.exists():
            p.unlink()


def strokes_to_png(strokes, width=600, height=200, pen=4.0, color=(20, 30, 90)):
    """A drawn signature (strokes of (x, y) points in a width x height pad) -> transparent PNG,
    cropped to the ink."""
    from PIL import Image, ImageDraw
    sc = 2
    im = Image.new("RGBA", (width * sc, height * sc), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    for st in strokes:
        pts = [(x * sc, y * sc) for x, y in st]
        if len(pts) == 1:
            x, y = pts[0]
            dr.ellipse((x - pen, y - pen, x + pen, y + pen), fill=color + (255,))
        elif pts:
            dr.line(pts, fill=color + (255,), width=int(pen * sc), joint="curve")
    box = im.getbbox()
    if box:
        pad = 6 * sc
        im = im.crop((max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad)))
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


# ---------------- measurement ----------------
class Scale:
    """unit per PDF point. source: "pdf" (declared by the page), "calibrated", "paper"."""

    def __init__(self, factor, unit, source):
        self.factor, self.unit, self.source = float(factor), unit, source

    def describe(self):
        src = {"pdf": "from the PDF", "calibrated": "calibrated", "paper": "paper size, not calibrated"}[self.source]
        return f"1 pt = {self.factor:.5g} {self.unit} ({src})"


def declared_scale(doc, pno, pt_unrot=None):
    """The page's own scale from /VP (a viewport /Measure, rectilinear), or None."""
    pg = doc[pno]
    try:
        kind, vp = doc.xref_get_key(pg.xref, "VP")
    except Exception:
        return None
    if kind in ("null", "none") or not vp:
        return None
    if kind == "xref":
        vp = doc.xref_object(int(vp.split()[0]))
    text = vp
    for ref in re.findall(r"(\d+) 0 R", vp):            # inline any referenced dicts (one level)
        try:
            text = text.replace(f"{ref} 0 R", doc.xref_object(int(ref)), 1)
        except Exception:
            pass
    m_u = re.search(r"/U\s*\(([^)]*)\)", text)
    m_c = re.search(r"/C\s+([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", text)
    if not (m_u and m_c):
        return None
    try:
        c = float(m_c.group(1))
    except ValueError:
        return None
    if c <= 0:
        return None
    return Scale(c, m_u.group(1) or "units", "pdf")


def scale_for(doc, pno, calibrated=None, unit="mm"):
    s = declared_scale(doc, pno)
    if s is not None:
        return s
    if calibrated is not None:
        return calibrated
    return Scale(UNIT_FACTOR[unit], unit, "paper")


def fmt(value, unit, area=False):
    return f"{value:,.2f} {unit}{'²' if area else ''}"


def measure_dict(scale):
    return (f"<</Type/Measure/Subtype/RL/R({1:.0f} pt = {scale.factor:.6g} {scale.unit})"
            f"/X[<</Type/NumberFormat/U({scale.unit})/C {scale.factor:.8g}/D 100>>]"
            f"/D[<</Type/NumberFormat/U({scale.unit})/C 1/D 100>>]"
            f"/A[<</Type/NumberFormat/U({scale.unit}2)/C 1/D 100>>]>>")


def polygon_area(pts):
    a = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        a += x0 * y1 - x1 * y0
    return abs(a) / 2


def create_distance(doc, pno, p0_disp, p1_disp, scale, style):
    pg = doc[pno]
    a0, a1 = P(p0_disp) * pg.derotation_matrix, P(p1_disp) * pg.derotation_matrix
    length = math.hypot(a1.x - a0.x, a1.y - a0.y) * scale.factor
    a = pg.add_line_annot(a0, a1)
    a.set_line_ends(pymupdf.PDF_ANNOT_LE_BUTT, pymupdf.PDF_ANNOT_LE_BUTT)
    a.set_colors(stroke=tuple(style.get("stroke", (0.1, 0.35, 0.9))))
    a.set_border(width=float(style.get("width", 1.0)))
    a.set_info(content=fmt(length, scale.unit), subject="Distance")
    doc.xref_set_key(a.xref, "Cap", "true")
    doc.xref_set_key(a.xref, "IT", "/LineDimension")
    doc.xref_set_key(a.xref, "Measure", measure_dict(scale))
    a.update()
    set_data(doc, a.xref, {"role": "distance", "factor": scale.factor, "unit": scale.unit, "source": scale.source,
                           "value": length})
    return a.xref


def create_area(doc, pno, pts_disp, scale, style, nm=None):
    """A polygon plus its reading (a small borderless label at the centroid), linked by /NM."""
    pg = doc[pno]
    pts = [tuple(P(p) * pg.derotation_matrix) for p in pts_disp]
    value = polygon_area(pts) * scale.factor ** 2
    a = pg.add_polygon_annot(pts)
    color = tuple(style.get("stroke", (0.1, 0.35, 0.9)))
    a.set_colors(stroke=color, fill=tuple(style["fill"]) if style.get("fill") else [])
    a.set_border(width=float(style.get("width", 1.0)))
    a.set_opacity(float(style.get("opacity", 1.0)))
    nm = nm or f"lz-area-{uuid.uuid4().hex[:12]}"
    a.set_info(content=fmt(value, scale.unit, area=True), subject="Area")
    doc.xref_set_key(a.xref, "IT", "/PolygonDimension")
    doc.xref_set_key(a.xref, "Measure", measure_dict(scale))
    a.update()
    set_data(doc, a.xref, {"role": "area", "factor": scale.factor, "unit": scale.unit, "source": scale.source,
                           "value": value, "nm": nm})
    cx = sum(p[0] for p in pts) / len(pts)
    cy = sum(p[1] for p in pts) / len(pts)
    text = fmt(value, scale.unit, area=True)
    w = 7 * len(text) + 12
    lab = pg.add_freetext_annot(R(cx - w / 2, cy - 9, cx + w / 2, cy + 9), text, fontsize=10, fontname="helv",
                                text_color=color, fill_color=(1, 1, 1), rotate=pg.rotation, align=1)
    lab.set_opacity(0.9)
    lab.set_info(subject="Area reading")
    lab.update()
    set_data(doc, lab.xref, {"role": "area_label", "nm": nm})
    return a.xref


def area_label(pg, nm):
    """The reading label of the area measurement nm (linked through the role data)."""
    for a in pg.annots():
        d = get_data(pg.parent, a.xref)
        if d.get("role") == "area_label" and d.get("nm") == nm:
            return a
    return None


# ---------------- callouts ----------------
CALLOUT_DEFAULTS = dict(text="Callout", fontname="helv", fontsize=11.0, text_color=(0, 0, 0), fill=(1.0, 1.0, 0.85),
                        border=(0.2, 0.2, 0.2), width=1.0, line_end=pymupdf.PDF_ANNOT_LE_OPEN_ARROW)
CSS_FONT = {"helv": "sans-serif", "tiro": "serif", "cour": "monospace"}


def _hex(c):
    return "#%02x%02x%02x" % tuple(int(round(255 * float(x))) for x in c[:3])


def create_callout(doc, pno, tip_disp, box_disp, st):
    """A callout: box_disp (displayed rect) with a leader from its nearest edge to tip_disp."""
    pg = doc[pno]
    st = {**CALLOUT_DEFAULTS, **st}
    box = (R(box_disp) * pg.derotation_matrix).normalize()
    tip = P(tip_disp) * pg.derotation_matrix
    # the knee: the middle of the box edge facing the tip
    cx, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
    if abs(tip.x - cx) / max(box.width, 1) > abs(tip.y - cy) / max(box.height, 1):
        knee = P(box.x0 if tip.x < cx else box.x1, cy)
    else:
        knee = P(cx, box.y0 if tip.y < cy else box.y1)
    style = (f"font-family:{CSS_FONT.get(st['fontname'], 'sans-serif')};font-size:{float(st['fontsize']):g}pt;"
             f"color:{_hex(st['text_color'])}")
    a = pg.add_freetext_annot(box, st["text"], fontsize=float(st["fontsize"]), fontname=st["fontname"],
                              text_color=tuple(st["text_color"]), fill_color=tuple(st["fill"]) if st.get("fill") else None,
                              border_color=tuple(st["border"]) if st.get("border") else None,
                              border_width=float(st["width"]), callout=(tip, knee), line_end=int(st["line_end"]),
                              rotate=pg.rotation, richtext=True, style=style)
    a.set_info(subject="Callout")
    a.update()
    data = {"role": "callout", "tip": list(tip_disp), "box": list(R(box_disp)), **{k: (list(v) if isinstance(v, tuple) else v)
                                                                                   for k, v in st.items()}}
    set_data(doc, a.xref, data)
    return a.xref


def recreate_callout(doc, pno, xref, changes=None, box_disp=None, tip_disp=None):
    """Restyle / move / resize a callout: re-created with its data updated (one operation)."""
    d = get_data(doc, xref)
    pg = doc[pno]
    pg.delete_annot(pg.load_annot(xref))
    st = {k: (tuple(v) if isinstance(v, list) else v) for k, v in d.items() if k in CALLOUT_DEFAULTS}
    st.update(changes or {})
    tip = tip_disp if tip_disp is not None else d["tip"]
    box = box_disp if box_disp is not None else d["box"]
    return create_callout(doc, pno, tip, box, st)
