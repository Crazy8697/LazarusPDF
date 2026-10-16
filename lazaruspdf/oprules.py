"""Which undo step type each editing op uses: declared per op, never decided at run time.

JOURNAL ops run inside a PyMuPDF journal operation (cheap; WorkingDoc.edit).
SNAPSHOT ops run on a fresh copy (WorkingDoc.snapshot_edit) because PyMuPDF refuses them
inside an operation, or runs them but can't undo them correctly. Trying an op and falling back
on an exception is NOT safe: a refused op can leave half-made changes behind before it throws
(set_toc does).

Every op a tool uses is listed here after probing it with probe() on worst-case documents
(see PROBE_DOCS), and the self-test re-proves each classification. A tool that calls
PdfEditorMode.run_edit() with an op missing from this table fails loudly (KeyError).

Page-structure ops (blank page, pages from other PDFs, bookmarks) are journal ops built on
low-level MuPDF calls in pageops.py / outline.py; they are not listed here."""
import hashlib

import pymupdf

JOURNAL = "journal"
SNAPSHOT = "snapshot"

# op key -> (kind, why). Probed with PyMuPDF 1.28.2.
OPS = {
    "highlight": (JOURNAL, "add_highlight_annot: journals, undo/redo restore"),
    "freetext": (JOURNAL, "add_freetext_annot: journals, undo/redo restore"),
    "ink": (JOURNAL, "add_ink_annot: journals, undo/redo restore"),
    "delete_annot": (JOURNAL, "delete_annot: journals, undo/redo restore"),
    "insert_text": (SNAPSHOT, "insert_text: refused inside an op when the page lacks the font (a new font object)"),
    "insert_textbox": (SNAPSHOT, "insert_textbox: refused inside an op when the page lacks the font"),
    "insert_image": (JOURNAL, "insert_image: journals, undo/redo restore"),
    "redact_apply": (JOURNAL, "add_redact_annot + apply_redactions: journals, undo/redo restore"),
    "rotate": (JOURNAL, "set_rotation: journals, undo/redo restore"),
    "bake": (JOURNAL, "bake (flatten annots/widgets): journals, undo/redo restore"),
    # stage 9a annotation tools (markup.py)
    "underline": (JOURNAL, "add_underline_annot: journals, undo/redo restore"),
    "strike": (JOURNAL, "add_strikeout_annot: journals, undo/redo restore"),
    "rect_annot": (JOURNAL, "add_rect_annot: journals, undo/redo restore"),
    "circle_annot": (JOURNAL, "add_circle_annot: journals, undo/redo restore"),
    "line_annot": (JOURNAL, "add_line_annot + set_line_ends: journals, undo/redo restore"),
    "polygon_annot": (JOURNAL, "add_polygon_annot: journals, undo/redo restore"),
    "text_note": (JOURNAL, "add_text_annot: journals, undo/redo restore"),
    "stamp": (JOURNAL, "add_stamp_annot: journals, undo/redo restore"),
    "move_annot": (JOURNAL, "set_rect, or delete + re-create (ink/line/polygon): journals, undo/redo restore"),
    "annot_style": (JOURNAL, "set_colors/opacity/border/line_ends/info + update: journals, undo/redo restore"),
    # stage 9b page-content tools (contentedit.py); role data in a private /LazarusData key
    "whiteout": (JOURNAL, "add_rect_annot, opaque sampled fill + role key: journals, undo/redo restore"),
    "image_stamp": (JOURNAL, "add_stamp_annot with an image (insert image, signature): journals, undo/redo restore"),
    "distance": (JOURNAL, "add_line_annot + /Cap /IT /Measure keys: journals, undo/redo restore"),
    "area": (JOURNAL, "add_polygon_annot + reading label + /IT /Measure keys: journals, undo/redo restore"),
    "callout": (JOURNAL, "add_freetext_annot callout (rich text) + role key: journals, undo/redo restore"),
    "move_roles": (JOURNAL, "move / restyle 9b roles (callout and measurements re-created): journals, undo/redo restore"),
    # stage 10
    "redact_mark": (JOURNAL, "add_redact_annot (box or text quads): journals, undo/redo restore"),
    "apply_redactions": (JOURNAL, "apply_redactions (text, image pixels, touched line art): journals, undo/redo restore"),
    "form_add": (JOURNAL, "add_widget (text, checkbox, radio via flags, dropdown, list): journals, undo/redo restore"),
    "form_fill": (JOURNAL, "widget field_value + update: journals, undo/redo restore"),
    "form_edit": (JOURNAL, "widget rect / name / choices / delete_widget: journals, undo/redo restore"),
    "flatten": (JOURNAL, "bake (out-of-range / unchosen annots parked, AcroForm rebuilt): journals, undo/redo restore"),
    "stamp_text": (SNAPSHOT, "insert_text on pages (header/footer, page numbers, Bates): refused when the font is new to the page"),
    "watermark": (SNAPSHOT, "insert_text (new font) / insert_image watermark: text refused when the font is new to the page"),
    "set_metadata": (SNAPSHOT, "set_metadata: refused inside an op when the file has no Info dictionary yet"),
    "scrub": (SNAPSHOT, "scrub: runs inside an op, but undo does NOT restore the document"),
    # stage 7 / 7a bookmarks
    "outline_write": (JOURNAL, "outline.write (MuPDF primitives, as hand-made bookmarks): journals, undo/redo restore"),
    "set_toc": (SNAPSHOT, "PyMuPDF set_toc: refused inside an op (and leaves a half-made change first); not used "
                          "on the working copy, outline.write is"),
    "remove_metadata": (SNAPSHOT, "set_metadata({}) + del_xml_metadata: refused inside an op without an Info dictionary"),
    "ocr_overlay": (SNAPSHOT, "show_pdf_page (the OCR text layer as a form XObject): refused inside an op"),
    # stage 11 text editing (textedit.py)
    "text_edit": (SNAPSHOT, "glyph-band redactions + insert_font / insert_text: a font new to the page (the reused "
                            "embedded font, or a standard font) is refused inside an op"),
    "text_cover": (SNAPSHOT, "draw_rect cover + insert_text: refused inside an op when the font is new to the page"),
    "text_replace": (SNAPSHOT, "find / replace: the text_edit write path for every block with a hit"),
    # stage 14
    "insert_image_pages": (JOURNAL, "image pages built in a plain PDF, grafted in with MuPDF primitives (as Insert "
                                    "pages from a PDF): journals, undo/redo restore"),
    # stage 15
    "insert_scan_pages": (JOURNAL, "scanned pages built in a plain PDF (the stage 14 engine), grafted in with MuPDF "
                                   "primitives: journals, undo/redo restore"),
    # stage 17: pages between tabs
    "paste_pages": (JOURNAL, "pageclip.graft_full (pages + annotations + links + form fields, MuPDF primitives "
                             "and xref keys) + optional bookmarks: journals, undo/redo restore"),
    "cut_pages": (JOURNAL, "delete_pages + bookmarks remapped (the source side of Cut): journals, undo/redo restore"),
    # stage 18: Insert › Link (links.py)
    "link_add": (JOURNAL, "insert_link, then /Rect and /Dest or /A URI written as xref keys: journals, undo/redo restore"),
    "link_edit": (JOURNAL, "move (/Rect), re-target (/Dest, /A) and delete (dropped from /Annots; PyMuPDF's "
                           "delete_link can't be undone) as xref keys: journals, undo/redo restore"),
}


def kind(op):
    return OPS[op][0]


# ---------------- probing (used by the self-test; also how a new op is classified) ----------------
_PNG = None


def _png():
    global _PNG
    if _PNG is None:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        pix.clear_with(200)
        _PNG = pix.tobytes("png")
    return _PNG


# what each op does on a probe document (on the LAST page: text + one highlight, rotated in
# the rotated_page probe)
ACTIONS = {
    "highlight": lambda d: d[-1].add_highlight_annot(pymupdf.Rect(70, 100, 200, 120)),
    "freetext": lambda d: d[-1].add_freetext_annot(pymupdf.Rect(70, 140, 220, 180), "note"),
    "ink": lambda d: d[-1].add_ink_annot([[(80, 200), (140, 240)]]),
    "delete_annot": lambda d: (lambda pg: pg.delete_annot(pg.first_annot))(d[-1]),   # keep the page alive
    "insert_text": lambda d: d[-1].insert_text((72, 300), "watermark"),
    "insert_textbox": lambda d: d[-1].insert_textbox(pymupdf.Rect(72, 320, 300, 380), "header text"),
    "insert_image": lambda d: d[-1].insert_image(pymupdf.Rect(72, 400, 150, 480), stream=_png()),
    "redact_apply": lambda d: (d[-1].add_redact_annot(pymupdf.Rect(70, 60, 200, 80)), d[-1].apply_redactions()),
    "rotate": lambda d: d[-1].set_rotation(90),
    "bake": lambda d: d.bake(),
    "underline": lambda d: (lambda pg: pg.add_underline_annot(pg.search_for("probe", quads=True)))(d[-1]),
    "strike": lambda d: (lambda pg: pg.add_strikeout_annot(pg.search_for("probe", quads=True)))(d[-1]),
    "rect_annot": lambda d: d[-1].add_rect_annot(pymupdf.Rect(300, 300, 400, 360)),
    "circle_annot": lambda d: d[-1].add_circle_annot(pymupdf.Rect(300, 300, 400, 360)),
    "line_annot": lambda d: (lambda a: (a.set_line_ends(0, pymupdf.PDF_ANNOT_LE_OPEN_ARROW), a.update()))(
        d[-1].add_line_annot((300, 300), (400, 360))),
    "polygon_annot": lambda d: d[-1].add_polygon_annot([(300, 300), (400, 300), (350, 380)]),
    "text_note": lambda d: d[-1].add_text_annot((300, 300), "note"),
    "stamp": lambda d: d[-1].add_stamp_annot(pymupdf.Rect(300, 500, 500, 560), stamp=0),
    "move_annot": lambda d: _move_all(d),
    "whiteout": lambda d: _ce().create_whiteout(d, d.page_count - 1, pymupdf.Rect(100, 300, 220, 360), (1.0, 0.98, 0.9)),
    "image_stamp": lambda d: _ce().create_image(d, d.page_count - 1, pymupdf.Rect(300, 300, 380, 380), _png()),
    "distance": lambda d: _ce().create_distance(d, d.page_count - 1, (100, 400), (300, 400), _ce().Scale(0.3528, "mm", "paper"), {}),
    "area": lambda d: _ce().create_area(d, d.page_count - 1, [(100, 450), (250, 450), (180, 560)], _ce().Scale(0.3528, "mm", "paper"), {}),
    "callout": lambda d: _ce().create_callout(d, d.page_count - 1, (120, 200), (200, 150, 380, 200), {"text": "probe"}),
    "move_roles": lambda d: _move_roles(d),
    "redact_mark": lambda d: _s10().mark_redactions(d, [(d.page_count - 1, (60, 55, 250, 85))]),
    "form_add": lambda d: [_s10().add_field(d, d.page_count - 1, k, (100, 300 + 30 * n, 220, 320 + 30 * n))
                           for n, k in enumerate(("form_text", "form_check", "form_radio", "form_combo", "form_list"))],
    "form_fill": lambda d: _form_fill(d),
    "flatten": lambda d: _flatten(d),
    "watermark": lambda d: (_s10().watermark_text(d, _s10().targets(range(d.page_count), d.page_count), "PROBE"),
                            _s10().watermark_image(d, _s10().targets(range(d.page_count), d.page_count), _png(),
                                                   "br", 20, 0.4, 30, False)),
    "stamp_text": lambda d: _s10().header_footer(d, _s10().targets(range(d.page_count), d.page_count),
                                                 {"tl": "Left {page}", "bc": "Centre", "br": "{page} / {pages}"}),
    "form_edit": lambda d: _form_edit(d),
    "apply_redactions": lambda d: (_s10().mark_redactions(d, [(d.page_count - 1, (60, 55, 250, 85))]),
                                   _s10().apply_redactions(d)),
    "annot_style": lambda d: _style_all(d),
    "set_metadata": lambda d: d.set_metadata({"title": "probe"}),
    "remove_metadata": lambda d: _s10().remove_metadata(d),
    "outline_write": lambda d: _outline().write(d, [[1, "Chapter", 1, {"to": (72, 60)}], [2, "Section", d.page_count, {}],
                                                   [1, "Rotated end", d.page_count, {}]]),
    "set_toc": lambda d: d.set_toc([[1, "Chapter", 1], [2, "Section", d.page_count]]),
    "ocr_overlay": lambda d: _s10().add_ocr_layers(d, {d.page_count - 1: _text_layer()}),
    "scrub": lambda d: d.scrub(),
    "text_edit": lambda d: _text_edit(d),
    "text_cover": lambda d: _text_edit(d, cover=True),
    "text_replace": lambda d: _text_replace(d),
    "insert_image_pages": lambda d: _insert_image_pages(d),
    "insert_scan_pages": lambda d: _insert_scan_pages(d),
    "paste_pages": lambda d: _paste_pages(d),
    "cut_pages": lambda d: _cut_pages(d),
    "link_add": lambda d: _link_add(d),
    "link_edit": lambda d: _link_edit(d),
}

# made BEFORE the journal operation starts (an op that changes something already there)
SETUP = {"link_edit": lambda d: _link_add(d)}


def _link_add(d):
    from . import links
    n = d.page_count - 1
    links.create(d, n, pymupdf.Rect(80, 300, 220, 330), {"kind": "page", "page": 0, "fit": "width"})
    links.create(d, n, pymupdf.Rect(80, 340, 220, 370), {"kind": "uri", "uri": "https://example.com/probe"})


def _link_edit(d):
    from . import links
    n = d.page_count - 1
    a, b = links.link_xrefs(d, n)[-2:]
    links.move(d, n, a, pymupdf.Rect(300, 500, 420, 540))
    links.retarget(d, n, a, {"kind": "uri", "uri": "https://example.com/edited"})
    links.retarget(d, n, b, {"kind": "page", "page": n, "fit": "zoom", "zoom": 150})
    links.delete(d, n, [b])


def _clip_source():
    """Pages to paste: text, a rotated page, a note, a whiteout, a link between them, a form field
    named like one the probe files will have, and a bookmark."""
    from . import contentedit, pageclip, stage10
    s = pymupdf.open()
    for i in range(2):
        s.new_page().insert_text((72, 72), f"clip {i}")
    s[1].set_rotation(90)
    s[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 100, 200, 120), "page": 1})
    s[0].add_text_annot((300, 300), "note")
    contentedit.create_whiteout(s, 0, pymupdf.Rect(100, 400, 200, 450), (1, 1, 0.9))
    stage10.add_field(s, 0, "form_text", (100, 300, 220, 320), name="Text 1")
    s.set_toc([[1, "Clip", 1]])
    clip = pageclip.copy_pages(pymupdf.open("pdf", s.tobytes()), [0, 1])
    s.close()
    return clip


def _paste_pages(d):
    from . import pageclip, stage10
    stage10.add_field(d, 0, "form_text", (100, 200, 220, 220), name="Text 1")      # a name collision
    clip = _clip_source()
    cd = clip.doc()
    pageclip.paste(d, cd, 1, clip.toc)
    cd.close()


def _cut_pages(d):
    from . import outline
    toc = outline.read(d)
    d.delete_pages([0])
    if toc:
        outline.write(d, outline.remap(toc, {0: None, 1: 0}))


def _insert_scan_pages(d):
    """Two mock-scanned pages (built by the images engine) grafted in before the last page."""
    from . import images, pageops, wia
    items = [{"data": wia.mock_page({"text": f"SCAN {k}"}, 100), "frame": 0, "rotate": 90 * k} for k in range(2)]
    src = images.build(items, {})
    pageops._graft(d, src, [0, 1], d.page_count - 1)
    src.close()


def _insert_image_pages(d):
    """Two image pages (one turned) grafted in before the last page."""
    from . import pageops
    src = pymupdf.open()
    for rot in (0, 90):
        p = src.new_page(width=300, height=200)
        p.insert_image(p.rect, stream=_png(), rotate=rot)
    pageops._graft(d, src, [0, 1], d.page_count - 1)
    src.close()


def _text_edit(d, cover=False):
    """Edit every visible text block of the last page (a scan has none: nothing to do)."""
    from . import textedit as TE
    n = d.page_count - 1
    for fp in [b.fp for b in TE.PageModel(d, n).visible_blocks()]:
        pm = TE.PageModel(d, n)
        b = pm.find(fp)
        TE.apply_block(d, n, fp, b.text("edit") + " edited", overflow="grow", cover=cover or None)


def _text_replace(d):
    from . import textedit as TE
    from .search import compile_query
    items = TE.plan_replace(d, range(d.page_count), compile_query("probe"), "PROBED")
    TE.apply_replace(d, items)


def add_type3_text(d, pno, text="ABCAB", at=(72, 200)):
    """A Type3 font (its glyphs are drawn squares) with some text in it: the worst case for
    text tools. Used by the probe and the self-test."""
    pg = d[pno]
    pg.insert_text((at[0], at[1] - 40), " ", fontsize=1)        # (makes sure the page has a /Font dict)
    sq = d.get_new_xref()
    d.update_object(sq, "<<>>")
    d.update_stream(sq, b"500 0 0 0 500 700 d1 0 0 500 700 re f")
    f = d.get_new_xref()
    d.update_object(f, f"<</Type/Font/Subtype/Type3/FontBBox[0 0 500 700]/FontMatrix[0.001 0 0 0.001 0 0]"
                       f"/CharProcs<</sq {sq} 0 R>>/Encoding<</Type/Encoding/Differences[65/sq 66/sq 67/sq]>>"
                       f"/FirstChar 65/LastChar 67/Widths[500 500 500]/Resources<<>>>>")
    kind, res = d.xref_get_key(pg.xref, "Resources")
    if kind == "xref":
        rx = int(res.split()[0])
        fk, fv = d.xref_get_key(rx, "Font")
        if fk == "xref":
            d.xref_set_key(int(fv.split()[0]), "T3", f"{f} 0 R")
        else:
            d.xref_set_key(rx, "Font/T3", f"{f} 0 R")
    else:
        d.xref_set_key(pg.xref, "Resources/Font/T3", f"{f} 0 R")
    h = pg.rect.height if not pg.rotation else pg.mediabox.height
    c = d.get_new_xref()
    d.update_object(c, "<<>>")
    d.update_stream(c, f"BT /T3 14 Tf {at[0]} {h - at[1]} Td ({text}) Tj ET".encode())
    kind, cont = d.xref_get_key(pg.xref, "Contents")
    d.xref_set_key(pg.xref, "Contents", f"[{cont} {c} 0 R]" if kind == "xref" else cont[:-1] + f" {c} 0 R]")


def system_font():
    """An embeddable TrueType font from Windows (None if there is none)."""
    import os
    for name in ("arial.ttf", "verdana.ttf", "times.ttf"):
        p = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name)
        if os.path.isfile(p):
            return p
    return None


def _move_all(d):
    """Move every movable probe annotation (square: set_rect; line and ink: re-created)."""
    from .markup import to_disp_rect, transform
    n = d.page_count - 1
    pg = d[n]
    items = [(a.xref, to_disp_rect(pg, a.rect)) for a in pg.annots() if a.type[1] in ("Square", "Line", "Ink")]
    del pg
    for x, r in items:
        transform(d, n, x, pymupdf.Rect(r.x0 + 20, r.y0 + 30, r.x1 + 25, r.y1 + 40))


def _flatten(d):
    """Flatten the last page only: a field on page 1 must stay a working field."""
    s = _s10()
    s.add_field(d, 0, "form_text", (100, 300, 220, 320))
    s.add_field(d, d.page_count - 1, "form_check", (100, 340, 115, 355))
    s.mark_redactions(d, [(d.page_count - 1, (60, 55, 250, 85))])
    s.flatten(d, [d.page_count - 1])


def _form_fill(d):
    n = d.page_count - 1
    s = _s10()
    xs = [s.add_field(d, n, k, (100, 300 + 30 * j, 220, 320 + 30 * j))
          for j, k in enumerate(("form_text", "form_check", "form_radio", "form_combo"))]
    s.fill_field(d, n, xs[0], "filled")
    s.fill_field(d, n, xs[1], True)
    s.fill_field(d, n, xs[2], True)
    s.fill_field(d, n, xs[3], "Option 2")


def _form_edit(d):
    n = d.page_count - 1
    s = _s10()
    a = s.add_field(d, n, "form_text", (100, 300, 220, 320))
    b = s.add_field(d, n, "form_combo", (100, 340, 220, 360))
    s.move_field(d, n, a, (150, 400, 300, 430))
    s.field_props(d, n, b, name="renamed", choices=["x", "y"], fontsize=9)
    s.delete_field(d, n, a)


def _text_layer():
    """A stand-in for Tesseract's output: a one-page PDF of invisible text."""
    t = pymupdf.open()
    t.new_page().insert_text((72, 72), "ocr words", render_mode=3)
    b = t.tobytes()
    t.close()
    return b


def _outline():
    from . import outline
    return outline


def _s10():
    from . import stage10
    return stage10


def _ce():
    from . import contentedit
    return contentedit


def _move_roles(d):
    """Make one of each 9b role, then move each (callout, distance and area are re-created) and
    restyle the callout and the whiteout."""
    from .markup import restyle, transform
    ce = _ce()
    n = d.page_count - 1
    sc = ce.Scale(0.3528, "mm", "paper")
    xs = [ce.create_whiteout(d, n, pymupdf.Rect(100, 300, 220, 360), (1, 1, 1)),
          ce.create_distance(d, n, (100, 400), (300, 400), sc, {}),
          ce.create_area(d, n, [(100, 450), (250, 450), (180, 560)], sc, {}),
          ce.create_callout(d, n, (120, 200), (200, 150, 380, 200), {"text": "probe"})]
    moved = []
    for x in xs:
        pg = d[n]
        r = (pymupdf.Rect(pg.load_annot(x).rect) * pg.rotation_matrix).normalize()
        del pg
        moved.append(transform(d, n, x, pymupdf.Rect(r.x0 + 15, r.y0 + 20, r.x1 + 15, r.y1 + 20)))
    restyle(d, n, [moved[0], moved[3]], {"fill": (0.9, 0.9, 0.8), "fontsize": 14, "ends": (0, 5)})


def _style_all(d):
    from .markup import apply_style
    pg = d[-1]
    for a in pg.annots():
        apply_style(a, {"stroke": (0, 0, 1), "fill": (1, 1, 0), "width": 3, "opacity": 0.5,
                        "author": "probe", "ends": (0, pymupdf.PDF_ANNOT_LE_CLOSED_ARROW)})


def _make(info, rotated, font="helv"):
    d = pymupdf.open()
    for i in range(2):
        p = d.new_page()
        if font == "subset" and system_font():             # an embedded TrueType, subset below
            p.insert_font(fontname="sub", fontfile=system_font())
            p.insert_text((72, 72), f"page {i + 1} probe text", fontname="sub")
        elif font in ("subset", "type3"):
            p.insert_text((72, 72), f"page {i + 1} probe text", fontname="helv")
        elif font:
            p.insert_text((72, 72), f"page {i + 1} probe text", fontname=font)
        else:                                        # a scan: an image, no text
            p.insert_image(pymupdf.Rect(36, 36, 300, 300), stream=_png())
        p.add_highlight_annot(pymupdf.Rect(70, 60, 200, 80))
        if i == 1:                                   # one of each movable kind on the last page
            p.add_rect_annot(pymupdf.Rect(100, 600, 200, 660))
            p.add_line_annot((100, 700), (200, 740))
            p.add_ink_annot([[(300, 600), (340, 650), (380, 610)]])
        if rotated and i == 1:
            p.set_rotation(90)
    if font == "type3":
        add_type3_text(d, d.page_count - 1)
    if font == "subset":
        d.subset_fonts()
    if info:
        d.set_metadata({"title": "orig", "author": "probe"})
    data = d.tobytes(no_new_id=True)
    d.close()
    if not info:                       # no /Info dictionary at all (as in many real files)
        x = pymupdf.open("pdf", data)
        x.xref_set_key(-1, "Info", "null")
        data = x.tobytes(garbage=1)
        x.close()
    return data


# other_fonts: its text uses a font none of the tools add, so a tool that has to add a font to
# the page meets the worst case (PyMuPDF refuses a NEW font inside a journal operation)
PROBE_DOCS = {"with_info": (True, False, "helv"), "no_info": (False, False, "helv"), "rotated_page": (True, True, "helv"),
              "other_fonts": (True, False, "coit")}
# ops that must also hold on a scan with no text at all (an image page, rotated too)
SCAN_PROBED = ("outline_write", "set_toc", "insert_image_pages", "insert_scan_pages", "paste_pages", "cut_pages",
               "link_add", "link_edit")
# stage 11 text ops: also a subset font missing glyphs, a Type3 font and a scan with no text
TEXT_PROBED = ("text_edit", "text_cover", "text_replace")


def state(doc):
    """A fingerprint of everything an op could change, read from a plain copy."""
    s = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
    try:
        out = [sorted((s.metadata or {}).items()), s.page_count, len(s.get_xml_metadata()),
               [tuple(e[:3]) for e in s.get_toc()]]
        for p in s:
            out.append((p.rotation, b"".join(s.xref_stream(x) or b"" for x in p.get_contents()),
                        [(a.type[1], tuple(round(v, 1) for v in a.rect), a.colors, round(a.opacity, 2),
                          (a.border or {}).get("width"), (a.info or {}).get("title")) for a in p.annots()],
                        len(p.get_images()), p.get_text(),
                        [(w.field_type, w.field_name, str(w.field_value), tuple(round(v, 1) for v in w.rect),
                          tuple(w.choice_values or ())) for w in p.widgets()],
                        [(k.get("kind"), tuple(round(v, 1) for v in k["from"]), k.get("page"), k.get("uri"),
                          s.xref_get_key(k["xref"], "Dest")[1] if k.get("xref") else None) for k in p.get_links()]))
        return hashlib.sha256(repr(out).encode()).hexdigest()
    finally:
        s.close()


def probe(op):
    """Run op inside a journal operation on each probe document. Returns
    {doc: "ok" | "refused" | "undo-wrong" | "redo-wrong" | "not-recorded"}. An op is
    journal-safe only if every probe document says "ok"."""
    out = {}
    docs = dict(PROBE_DOCS)
    if op in SCAN_PROBED or op in TEXT_PROBED:
        docs["scan_no_text"] = (True, True, None)
    if op in TEXT_PROBED:
        docs["subset_font"] = (True, True, "subset")
        docs["type3_font"] = (False, True, "type3")
    for name, (info, rotated, font) in docs.items():
        d = pymupdf.open("pdf", _make(info, rotated, font))
        try:
            if op in SETUP:
                SETUP[op](d)
                d0, d = d, pymupdf.open("pdf", d.tobytes())
                d0.close()
            d.journal_enable()
            s0 = state(d)
            d.journal_start_op(op)
            try:
                ACTIONS[op](d)
                refused = False
            except Exception:
                refused = True
            try:
                d.journal_stop_op()
            except Exception:
                refused = True
            if refused:
                out[name] = "refused"
                continue
            s1 = state(d)
            try:
                d.journal_undo()
            except Exception:
                out[name] = "not-recorded"
                continue
            d._reset_page_refs()
            if state(d) != s0:
                out[name] = "undo-wrong"
                continue
            d.journal_redo()
            d._reset_page_refs()
            out[name] = "ok" if state(d) == s1 else "redo-wrong"
        finally:
            d.close()
    return out


def classify(op):
    return JOURNAL if all(v == "ok" for v in probe(op).values()) else SNAPSHOT


def check_all():
    """{op: (declared, probed)} for every declared op; they must match."""
    return {op: (kind(op), classify(op)) for op in OPS}
