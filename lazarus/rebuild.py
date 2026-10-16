"""Stage 21: Extraction's two PDF outputs from the CORRECTED text (every filled gap included).
No Qt. Both write a NEW file through workdoc.write_pdf (atomic; never an original, protect.py).

Searchable PDF: the original pages exactly as they look, plus an invisible, selectable,
searchable text layer holding the corrected text (ocrlayer.py, the same code as the editor's OCR
page tool). Pages that already have a real text layer keep it; only what Extraction had to OCR on them
(pictures of text: the words the text layer doesn't cover) gets the corrected text added. Pages without a text
layer (scans) get the corrected text for the whole page.

Rebuilt pages: a new PDF typeset from the corrected text, one rebuilt page (or more, if the text
runs over) per original page, the same page size, no scan images: paragraphs in order, the
figures Extraction kept (pictures and drawings, cropped) placed where they came, open gaps shown
as their visible marker."""
import pymupdf

from . import locked
from .extract import Figure, GAP, Filled, Para
from .output import FIG_DPI, spaced

MIN_LAYER_CHARS = 30          # (as extraction: fewer real characters = a scan)


class Cancelled(Exception):
    pass


def para_tokens(it):
    """The corrected words of a paragraph: text, filled gaps; open gaps carry no text."""
    parts = []
    for s in spaced(it.segs):
        if s is GAP:
            parts.append(" ")
        elif isinstance(s, Filled):
            parts.append(s.text)
        else:
            parts.append(s)
    return "".join(parts).split()


def _open(src, password):
    data = open(src, "rb").read()
    doc, _lock = locked.open_pdf(data, (lambda attempt: password if attempt == 0 else None) if password else None)
    return doc


def searchable(pages, src, dest, password=None, lang="eng", progress=None, stopped=None):
    """-> pages that got a corrected layer."""
    from . import stage10 as T
    from .ocrlayer import corrected_words, layer_pdf, page_words
    from .workdoc import write_pdf
    doc = _open(src, password)
    try:
        out = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
    finally:
        doc.close()
    try:
        layers = {}
        for k, pr in enumerate(pages):
            if stopped is not None and stopped():
                raise Cancelled()
            if progress is not None:
                progress(k, len(pages))
            pno = pr.number - 1
            if pno >= out.page_count:
                continue
            pg = out[pno]
            paras = [it for it in pr.items if isinstance(it, Para)]
            if not paras:
                continue
            has_layer = len(pg.get_text().strip()) >= MIN_LAYER_CHARS
            placed = []
            if has_layer:
                # real text on the page already: only the parts Extraction had to OCR (pictures of
                # text) get the corrected text, on the words the text layer doesn't cover
                tokens = [t for it in paras if it.source == "ocr" for t in para_tokens(it)]
                if tokens:
                    covered = [pymupdf.Rect(w[:4]) for w in pg.get_text("words")]
                    free = [w for w in page_words(pg, lang)
                            if not any(r.contains((w.rect.tl + w.rect.br) / 2) for r in covered)]
                    placed = corrected_words(tokens, free)
            else:
                tokens = [t for it in paras for t in para_tokens(it)]
                placed = corrected_words(tokens, page_words(pg, lang))
            if placed:
                layers[pno] = layer_pdf(pg.rect.width, pg.rect.height, placed)
            del pg
        T.add_ocr_layers(out, layers)
        if progress is not None:
            progress(len(pages), len(pages))
        if stopped is not None and stopped():
            raise Cancelled()
        write_pdf(out.tobytes(), dest, {"encryption": pymupdf.PDF_ENCRYPT_NONE})
        return sorted(layers)
    finally:
        out.close()


MARGIN = 54                   # 0.75 inch
BODY_PT = 11.0
LINE = 1.38                   # line height, x the font size
PARA_GAP = 7.0                # points between paragraphs
SYSTEM_SERIF = r"C:\Windows\Fonts\times.ttf"     # embedded (a subset) at run time, never shipped
_SERIF = None


def serif():
    """A serif text font: the system's Times New Roman (wide Unicode coverage), else the built-in
    Times. Placed glyph by glyph (TextWriter): no ligatures, so the text searches as typed."""
    global _SERIF
    if _SERIF is None:
        import os
        _SERIF = pymupdf.Font(fontfile=SYSTEM_SERIF) if os.path.isfile(SYSTEM_SERIF) else pymupdf.Font("tiro")
    return _SERIF


def para_text_marked(it):
    """A paragraph's corrected text; an open gap shows its visible marker."""
    from .output import gap_marker
    parts = []
    for s in spaced(it.segs):
        if s is GAP:
            parts.append(gap_marker(it.page))
        elif isinstance(s, Filled):
            parts.append(s.text)
        else:
            parts.append(s)
    return " ".join("".join(parts).split())


def _wrap(font, text, size, width):
    lines, cur = [], ""
    for word in text.split(" "):
        cand = (cur + " " + word) if cur else word
        if cur and font.text_length(cand, fontsize=size) > width:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        lines.append(cur)
    return lines


def rebuilt(pages, src, dest, password=None, progress=None, stopped=None, title=None):
    """-> number of pages written."""
    from .workdoc import write_pdf
    doc = _open(src, password)
    sizes = [(doc[i].rect.width, doc[i].rect.height) for i in range(doc.page_count)]
    doc.close()
    out = pymupdf.open()
    font = serif()
    lh = BODY_PT * LINE
    try:
        for k, pr in enumerate(pages):
            if stopped is not None and stopped():
                raise Cancelled()
            if progress is not None:
                progress(k, len(pages))
            w, h = sizes[pr.number - 1] if pr.number - 1 < len(sizes) else pymupdf.paper_size("letter")
            width = w - 2 * MARGIN
            pg = out.new_page(width=w, height=h)
            tw = pymupdf.TextWriter(pg.rect)
            y = MARGIN

            def new_page():
                nonlocal pg, tw, y
                tw.write_text(pg)
                pg = out.new_page(width=w, height=h)
                tw = pymupdf.TextWriter(pg.rect)
                y = MARGIN
            for it in pr.items:
                if isinstance(it, Figure):
                    pix = pymupdf.Pixmap(it.png)
                    # (the review's figures are rendered at FIG_DPI: that gives their size on paper)
                    fw = min(width, max(36.0, pix.width * 72.0 / FIG_DPI))
                    fh = fw * pix.height / max(1, pix.width)
                    if fh > h - 2 * MARGIN:
                        fw, fh = fw * (h - 2 * MARGIN) / fh, h - 2 * MARGIN
                    if y + fh > h - MARGIN:
                        new_page()
                    x0 = MARGIN + (width - fw) / 2
                    pg.insert_image(pymupdf.Rect(x0, y, x0 + fw, y + fh), stream=it.png)
                    y += fh + PARA_GAP
                    continue
                if not isinstance(it, Para):
                    continue
                for line in _wrap(font, para_text_marked(it), BODY_PT, width):
                    if y + lh > h - MARGIN:
                        new_page()
                    # word by word: no space glyph in the text (a font's space can extract as a
                    # no-break space); readers put the spaces back from the gaps
                    x, sp = MARGIN, font.text_length(" ", fontsize=BODY_PT)
                    for word in line.split(" "):
                        tw.append((x, y + BODY_PT), word, font=font, fontsize=BODY_PT)
                        x += font.text_length(word, fontsize=BODY_PT) + sp
                    y += lh
                y += PARA_GAP
            tw.write_text(pg)
        out.set_metadata({"title": title or "", "producer": "Lazarus", "creator": "Lazarus"})
        out.subset_fonts()
        if progress is not None:
            progress(len(pages), len(pages))
        if stopped is not None and stopped():
            raise Cancelled()
        n = out.page_count
        write_pdf(out.tobytes(garbage=3, deflate=True), dest, {"encryption": pymupdf.PDF_ENCRYPT_NONE})
        return n
    finally:
        out.close()
