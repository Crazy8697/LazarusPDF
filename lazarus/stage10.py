"""Stage 10 document tools: the operations (no UI). Each runs inside one undoable operation
through PdfEditorMode.run_edit, declared in oprules.py.

Redaction: marks are PyMuPDF Redact annotations (reviewable, movable, removable like any
annotation). Applying them TRULY removes what is underneath: text under a mark, image pixels
under it, and vector drawings it touches (PyMuPDF apply_redactions). Afterwards the text is
re-extracted from a plain copy to verify nothing is left under any mark."""
import re

import pymupdf

R = pymupdf.Rect


# ---------------- page ranges ----------------
def parse_range(text, n):
    """"" / "all" -> every page; "1-3, 5, 8-" -> 0-based sorted pages (1-based in the text).
    Raises ValueError on nonsense."""
    t = (text or "").strip().lower()
    if t in ("", "all"):
        return list(range(n))
    out = set()
    for part in re.split(r"[,\s]+", t):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m:
            raise ValueError(f"Not a page range: {part}")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else n
        if a < 1 or b < a:
            raise ValueError(f"Not a page range: {part}")
        out.update(range(a - 1, min(b, n)))
    if not out:
        raise ValueError("No pages in that range")
    return sorted(out)


# ---------------- redaction ----------------
def mark_redactions(doc, marks, fill=(0, 0, 0)):
    """marks: [(page, rect or quad in DISPLAYED coordinates | ("pdf", quad))]. Returns xrefs."""
    out = []
    for pno, geom in marks:
        pg = doc[pno]
        if isinstance(geom, tuple) and len(geom) == 2 and geom[0] == "pdf":
            area = geom[1]                                     # already in PDF space (text quads)
        else:
            area = (R(geom) * pg.derotation_matrix).normalize()
        a = pg.add_redact_annot(area, fill=tuple(fill))
        a.set_info(subject="Redaction mark")
        a.update()
        out.append(a.xref)
        del a, pg
    return out


def redaction_marks(doc):
    """{page: [rect (PDF space)]} of every Redact annotation."""
    out = {}
    for i in range(doc.page_count):
        pg = doc[i]
        rs = [R(a.rect) for a in pg.annots(types=[pymupdf.PDF_ANNOT_REDACT])]
        if rs:
            out[i] = rs
    return out


def apply_redactions(doc):
    """Apply every mark: text under it removed, image pixels blanked, touched vector art removed.
    Returns {page: [rects]} that were applied (for verification)."""
    marks = redaction_marks(doc)
    for i in marks:
        pg = doc[i]
        pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                            text=pymupdf.PDF_REDACT_TEXT_REMOVE)
        del pg
    return marks


def verify_redactions(doc, applied):
    """Re-extract text from a plain copy: characters still inside an applied mark. 0 = clean."""
    plain = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
    left = 0
    try:
        for i, rects in applied.items():
            pg = plain[i]
            raw = pg.get_text("rawdict")
            for b in raw.get("blocks", []):
                for ln in b.get("lines", []):
                    for sp in ln.get("spans", []):
                        for ch in sp.get("chars", []):
                            if not ch["c"].strip():
                                continue
                            box = R(ch["bbox"])
                            c = pymupdf.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
                            if any(r.contains(c) for r in rects):
                                left += 1
    finally:
        plain.close()
    return left


def remove_marks(doc):
    n = 0
    for i in range(doc.page_count):
        pg = doc[i]
        for a in list(pg.annots(types=[pymupdf.PDF_ANNOT_REDACT])):
            pg.delete_annot(a)
            n += 1
        del pg
    return n


# ---------------- forms ----------------
FIELD_KINDS = {
    "form_text": (pymupdf.PDF_WIDGET_TYPE_TEXT, "Text"),
    "form_check": (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "Check"),
    "form_radio": (pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "Radio"),
    "form_combo": (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "Dropdown"),
    "form_list": (pymupdf.PDF_WIDGET_TYPE_LISTBOX, "List"),
}
DEFAULT_CHOICES = ["Option 1", "Option 2", "Option 3"]


def field_names(doc):
    names = set()
    for i in range(doc.page_count):
        pg = doc[i]
        names.update(w.field_name for w in pg.widgets())
        del pg
    return names


def add_field(doc, pno, kind, rect_disp, name=None, choices=None):
    """A new form field (text, checkbox, radio, dropdown, list) in a displayed rect."""
    ftype, base = FIELD_KINDS[kind]
    pg = doc[pno]
    if name is None:
        taken = field_names(doc)
        k = 1
        while f"{base} {k}" in taken:
            k += 1
        name = f"{base} {k}"
    w = pymupdf.Widget()
    radio = ftype == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON
    # PyMuPDF's add_widget can't create a radio button directly (it validates the parent/kids
    # before the field has an xref): make a checkbox, then set the radio flags on it
    w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX if radio else ftype
    w.field_name = name
    w.rect = (R(rect_disp) * pg.derotation_matrix).normalize()
    w.text_fontsize = 0                        # auto size
    w.border_color = (0.45, 0.45, 0.5)
    w.fill_color = (1, 1, 1) if ftype in (pymupdf.PDF_WIDGET_TYPE_TEXT, pymupdf.PDF_WIDGET_TYPE_COMBOBOX,
                                          pymupdf.PDF_WIDGET_TYPE_LISTBOX) else None
    if ftype in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
        w.choice_values = list(choices or DEFAULT_CHOICES)
        w.field_value = w.choice_values[0]
    elif ftype == pymupdf.PDF_WIDGET_TYPE_TEXT:
        w.field_value = ""
    a = pg.add_widget(w)
    if radio:
        doc.xref_set_key(a.xref, "Ff", str((1 << 15) | (1 << 14)))    # Radio + NoToggleToOff
    return a.xref


def widget(pg, xref):
    for w in pg.widgets():
        if w.xref == xref:
            return w
    return None


def fill_field(doc, pno, xref, value):
    """Set a field's value. Checkbox / radio: True / False (a radio switched on turns the other
    radios of the same field name off)."""
    pg = doc[pno]
    w = widget(pg, xref)
    if w is None:
        raise ValueError("That field is gone")
    t = w.field_type
    if t in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
        on = bool(value)
        if t == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON and on:
            for i in range(doc.page_count):
                p2 = doc[i]
                for o in p2.widgets():
                    if (o.field_type == t and o.field_name == w.field_name and o.xref != xref
                            and o.field_value not in (False, "Off", None)):
                        o.field_value = False
                        o.update()
                del p2
        w.field_value = w.on_state() if on else False
    else:
        w.field_value = value
    w.update()
    return xref


def field_props(doc, pno, xref, name=None, choices=None, fontsize=None):
    pg = doc[pno]
    w = widget(pg, xref)
    if name is not None and name.strip():
        w.field_name = name.strip()
    if choices is not None and w.field_type in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
        w.choice_values = list(choices)
        if w.field_value not in choices:
            w.field_value = choices[0] if choices else ""
    if fontsize is not None:
        w.text_fontsize = float(fontsize)
    w.update()
    return xref


def move_field(doc, pno, xref, rect_disp):
    pg = doc[pno]
    w = widget(pg, xref)
    w.rect = (R(rect_disp) * pg.derotation_matrix).normalize()
    w.update()
    return xref


def delete_field(doc, pno, xref):
    pg = doc[pno]
    w = widget(pg, xref)
    if w is not None:
        pg.delete_widget(w)
    return None


def field_at(pg, pt_disp, slack=1.0):
    """The form field under a displayed point: its xref, or None."""
    found = None
    for w in pg.widgets():
        r = (R(w.rect) * pg.rotation_matrix).normalize()
        if R(r.x0 - slack, r.y0 - slack, r.x1 + slack, r.y1 + slack).contains(pt_disp):
            found = w.xref
    return found


# ---------------- flatten ----------------
def _annot_refs(doc, page_xref):
    """The xrefs in a page's /Annots (inline array or a referenced one)."""
    kind, val = doc.xref_get_key(page_xref, "Annots")
    if kind == "xref":
        val = doc.xref_object(int(val.split()[0]))
    elif kind != "array":
        return []
    return [int(x) for x in re.findall(r"(\d+)\s+0\s+R", val)]


def _set_annot_refs(doc, page_xref, refs):
    doc.xref_set_key(page_xref, "Annots", "[" + " ".join(f"{x} 0 R" for x in refs) + "]" if refs else "null")


def flatten(doc, pages, annots=True, widgets=True):
    """Bake annotations and/or form fields of `pages` into the page content (their appearance
    becomes part of the page; they stop being editable). Redaction marks are never baked: they
    stay marks, to be applied (which removes content) or removed. Other pages and the kinds not
    chosen are untouched; form fields left on other pages keep working."""
    pages = set(pages)
    parked = {}                                        # page xref -> refs kept out of the bake
    widget_refs = []                                   # widgets that stay (for the AcroForm)
    for i in range(doc.page_count):
        px = doc[i].xref
        refs = _annot_refs(doc, px)
        bake, keep = [], []
        for x in refs:
            sub = doc.xref_get_key(x, "Subtype")[1]
            is_widget = sub == "/Widget"
            chosen = i in pages and sub != "/Redact" and sub != "/Popup" and (widgets if is_widget else annots)
            (bake if chosen else keep).append(x)
            if is_widget and not chosen:
                widget_refs.append(x)
        parked[px] = keep
        _set_annot_refs(doc, px, bake)
    cat = doc.pdf_catalog()
    kind, acro = doc.xref_get_key(cat, "AcroForm")
    if kind == "xref":
        acro = doc.xref_object(int(acro.split()[0]))
        kind = "dict"
    doc.bake(annots=True, widgets=True)
    for px, keep in parked.items():
        _set_annot_refs(doc, px, _annot_refs(doc, px) + keep)
    if kind == "dict" and widget_refs:                 # the fields still on the pages keep their form
        fields = "[" + " ".join(f"{x} 0 R" for x in widget_refs) + "]"
        acro = re.sub(r"/Fields\s*\[[^\]]*\]", "/Fields" + fields, acro) if "/Fields" in acro else \
            acro.replace("<<", "<</Fields" + fields, 1)
        doc.xref_set_key(cat, "AcroForm", acro)
    return sorted(pages)


# ---------------- compress (a new file; the working copy is not changed) ----------------
def compress_to(work, dest, downsample=False, dpi_threshold=225, dpi_target=150, quality=75):
    """Write a compressed copy of the working document to a NEW file: garbage collection, every
    stream deflated, content cleaned, object streams; optionally images above dpi_threshold
    resampled to dpi_target (JPEG quality for lossy ones). The document's current password
    choice applies. Returns the new file's size."""
    import os
    import tempfile
    from pathlib import Path
    from . import protect
    dest = Path(dest)
    if work.is_source(dest):
        raise protect.ProtectedPathError(protect.MESSAGE)
    protect.check(dest)
    copy = pymupdf.open(stream=work.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE), filetype="pdf")
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(dest.parent))
    os.close(fd)
    try:
        if downsample:
            copy.rewrite_images(dpi_threshold=int(dpi_threshold), dpi_target=int(dpi_target), quality=int(quality))
        copy.save(tmp, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True,
                  use_objstms=1, **work.save_options())
        os.replace(tmp, dest)
    finally:
        copy.close()
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest.stat().st_size


def human(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024


# ---------------- OCR to searchable ----------------
OCR_DPI = 300
REAL_TEXT_CHARS = 20          # a page with at least this much text already has a real text layer


def has_real_text(page):
    return len(page.get_text().strip()) >= REAL_TEXT_CHARS


def ocr_page_pdf(page, lang="eng", dpi=OCR_DPI):
    """Tesseract on the page AS DISPLAYED -> a one-page PDF holding only invisible text. The shared
    layer code (ocrlayer.py) that Extraction's Searchable PDF uses too (stage 21)."""
    from .ocrlayer import ocr_layer
    return ocr_layer(page, lang, dpi)


def add_ocr_layers(doc, layers):
    """layers: {page: text-only PDF bytes}. Each is laid over its page (invisible text, so the
    page looks the same), aligned with what is displayed on rotated pages too."""
    for pno, pdf_bytes in sorted(layers.items()):
        src = pymupdf.open("pdf", pdf_bytes)
        pg = doc[pno]
        pg.show_pdf_page((pg.rect * pg.derotation_matrix).normalize(), src, 0, rotate=pg.rotation,
                         keep_proportion=False)
        del pg
        src.close()
    return sorted(layers)


# ---------------- metadata ----------------
META_FIELDS = [("title", "Title"), ("author", "Author"), ("subject", "Subject"), ("keywords", "Keywords"),
               ("creator", "Creator"), ("producer", "Producer")]


def pdf_date(s):
    """D:20240131120000+01'00' -> 2024-01-31 12:00:00 (+01:00); '' if absent."""
    import re as _re
    m = _re.match(r"D:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Zz+\-])?(\d{2})?'?(\d{2})?", s or "")
    if not m:
        return s or ""
    y, mo, d, h, mi, se, z, zh, zm = m.groups()
    out = f"{y}-{mo or '01'}-{d or '01'} {h or '00'}:{mi or '00'}:{se or '00'}"
    if z in ("Z", "z"):
        out += " UTC"
    elif z:
        out += f" ({z}{zh or '00'}:{zm or '00'})"
    return out


def set_metadata(doc, values):
    """Edit the document information (title, author, ...); the dates are kept."""
    meta = dict(doc.metadata or {})
    meta.update({k: v for k, v in values.items() if k in dict(META_FIELDS)})
    doc.set_metadata({k: meta.get(k, "") or "" for k in ("title", "author", "subject", "keywords", "creator",
                                                           "producer", "creationDate", "modDate")})
    return True


def remove_metadata(doc):
    """Remove all document metadata: the Info dictionary and the XMP stream."""
    doc.set_metadata({})
    doc.del_xml_metadata()
    return True


# ---------------- placing text on pages (headers, footers, numbers, Bates, watermarks) ----------------
FONTS = [("helv", "Helvetica"), ("hebo", "Helvetica Bold"), ("tiro", "Times"), ("tibo", "Times Bold"),
         ("cour", "Courier"), ("cobo", "Courier Bold")]
SLOTS = {"tl": "Top left", "tc": "Top centre", "tr": "Top right", "bl": "Bottom left", "bc": "Bottom centre",
         "br": "Bottom right"}


def expand(template, page_no, total):
    """{page} {pages} {date} in a header / footer template."""
    import datetime
    return (template.replace("{page}", str(page_no)).replace("{pages}", str(total))
            .replace("{date}", datetime.date.today().isoformat()))


def place_text(pg, slot, text, font="helv", size=10.0, color=(0, 0, 0), mx=36.0, my=28.0, opacity=1.0):
    """Text in a slot of the DISPLAYED page (reads upright on rotated pages too)."""
    if not text:
        return
    W, H = pg.rect.width, pg.rect.height             # displayed size
    tw = pymupdf.get_text_length(text, fontname=font, fontsize=size)
    x = {"l": mx, "c": (W - tw) / 2, "r": W - mx - tw}[slot[1]]
    y = my + size * 0.8 if slot[0] == "t" else H - my
    pt = pymupdf.Point(x, y) * pg.derotation_matrix
    pg.insert_text(pt, text, fontsize=size, fontname=font, color=tuple(color), rotate=pg.rotation,
                   fill_opacity=float(opacity))


def targets(pages, total):
    """[(page in this doc, its page number, document total, position in the range)]. A preview
    stamps a one-page copy with the real numbers: [(0, real number, total, position)]."""
    return [(p, p + 1, total, k) for k, p in enumerate(pages)]


WM_POSITIONS = [("diagonal", "Diagonal (corner to corner)"), ("center", "Centre"), ("tl", "Top left"),
                ("tr", "Top right"), ("bl", "Bottom left"), ("br", "Bottom right")]


def _wm_anchor(pg, position, w, h, margin):
    """Centre of the watermark in DISPLAYED coordinates, and the angle a diagonal needs."""
    W, H = pg.rect.width, pg.rect.height
    import math
    diag = math.degrees(math.atan2(H, W))
    if position in ("center", "diagonal"):
        return pymupdf.Point(W / 2, H / 2), diag
    x = margin + w / 2 if position[1] == "l" else W - margin - w / 2
    y = margin + h / 2 if position[0] == "t" else H - margin - h / 2
    return pymupdf.Point(x, y), diag


def watermark_text(doc, tg, text, position="diagonal", font="hebo", size=60.0, color=(0.75, 0.1, 0.1),
                   opacity=0.25, angle=None, behind=True, margin=36.0):
    """Text watermark. angle: degrees counter-clockwise as seen (None: the diagonal for
    "diagonal", else 0). behind: under the page content."""
    import math
    tw = pymupdf.get_text_length(text, fontname=font, fontsize=size)
    for pno, *_ in tg:
        pg = doc[pno]
        c_disp, diag = _wm_anchor(pg, position, tw, size, margin)
        a = angle if angle is not None else (diag if position == "diagonal" else 0.0)
        c = c_disp * pg.derotation_matrix
        start = pymupdf.Point(c.x - tw / 2, c.y + size * 0.35)       # centred on c before turning
        pg.insert_text(start, text, fontsize=size, fontname=font, color=tuple(color), fill_opacity=float(opacity),
                       morph=(c, pymupdf.Matrix(a + pg.rotation)), overlay=not behind)
        del pg
    return [t_[0] for t_ in tg]


def watermark_image_bytes(img_bytes, opacity, angle):
    """The picture with the opacity in its alpha channel, turned by angle (counter-clockwise)."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    if angle:
        im = im.rotate(angle, expand=True, resample=Image.BICUBIC)
    a = im.getchannel("A").point(lambda v: int(v * float(opacity)))
    im.putalpha(a)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue(), im.width, im.height


def watermark_image(doc, tg, png, position="center", width_pct=40.0, opacity=0.25, angle=0.0, behind=True,
                    margin=36.0):
    """Image watermark: width_pct of the page width, upright on rotated pages."""
    data, iw, ih = watermark_image_bytes(png, opacity, angle)
    for pno, *_ in tg:
        pg = doc[pno]
        w = pg.rect.width * float(width_pct) / 100
        h = w * ih / iw
        c, _diag = _wm_anchor(pg, "center" if position == "diagonal" else position, w, h, margin)
        rect_disp = pymupdf.Rect(c.x - w / 2, c.y - h / 2, c.x + w / 2, c.y + h / 2)
        pg.insert_image((rect_disp * pg.derotation_matrix).normalize(), stream=data, rotate=pg.rotation,
                        overlay=not behind)
        del pg
    return [t_[0] for t_ in tg]


def bates_label(prefix, n, digits, suffix):
    return f"{prefix}{n:0{int(digits)}d}{suffix}"


def bates(doc, tg, prefix="", start=1, digits=6, suffix="", slot="br", font="helv", size=9.0, color=(0, 0, 0),
          mx=36.0, my=20.0):
    """Sequential Bates numbers over the range: prefix + zero-padded number + suffix."""
    for pno, _num, _total, k in tg:
        pg = doc[pno]
        place_text(pg, slot, bates_label(prefix, start + k, digits, suffix), font, size, color, mx, my)
        del pg
    return [t_[0] for t_ in tg]


NUMBER_FORMATS = [("n", "1"), ("page_n", "Page 1"), ("page_n_of", "Page 1 of N")]


def page_numbers(doc, tg, slot="bc", fmt="n", start=1, font="helv", size=10.0, color=(0, 0, 0), mx=36.0, my=24.0):
    """Number the pages of the range start, start+1, ... ("of N": N is the last number used)."""
    last = start + len(tg) - 1
    for pno, _num, _total, k in tg:
        n = start + k
        text = {"n": str(n), "page_n": f"Page {n}", "page_n_of": f"Page {n} of {last}"}[fmt]
        pg = doc[pno]
        place_text(pg, slot, text, font, size, color, mx, my)
        del pg
    return [t_[0] for t_ in tg]


def header_footer(doc, tg, slots, font="helv", size=9.0, color=(0.2, 0.2, 0.2), mx=36.0, my=24.0):
    """slots: {"tl": template, ...} (empty = nothing there). Templates may use {page} {pages} {date}."""
    for pno, num, total, _k in tg:
        pg = doc[pno]
        for slot, tpl in slots.items():
            place_text(pg, slot, expand(tpl, num, total), font, size, color, mx, my)
        del pg
    return [t_[0] for t_ in tg]
