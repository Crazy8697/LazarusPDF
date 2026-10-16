"""Invisible, searchable text layers (stage 10 OCR page, stage 21 Searchable PDF). No Qt.

ONE code path for both:
  1. page_words(): Tesseract (the same engine and configuration as everywhere in Lazarus PDF,
     tess.py) reads the page AS DISPLAYED, without deskewing, so every word box is in the page's
     displayed coordinates;
  2. layer_pdf(): a one-page PDF holding only invisible text (render mode 3), each word placed in
     its box and stretched to its width, so selecting and searching line up with the image;
  3. stage10.add_ocr_layers() lays that page over the original one (rotated pages included).
The editor's OCR page tool places Tesseract's own words. Searchable PDF (Extraction) places the
user's CORRECTED text: corrected_words() aligns it with Tesseract's words, so an unchanged word
keeps its exact box, a corrected word takes the box of the word(s) it replaces, and text typed
into a gap goes where the gap's words were (or right after the word before it)."""
import difflib
import re

import pymupdf

OCR_DPI = 300
FONT = "helv"
_FONT = None


class PlacedWord:
    __slots__ = ("text", "rect", "conf", "key")

    def __init__(self, text, rect, conf=100.0, key=(0, 0, 0)):
        self.text, self.rect, self.conf, self.key = text, pymupdf.Rect(rect), conf, key


def _font():
    global _FONT
    if _FONT is None:
        _FONT = pymupdf.Font(FONT)
    return _FONT


def page_words(page, lang="eng", dpi=OCR_DPI, clip=None, psm=3):
    """Tesseract's words on the page as displayed: [PlacedWord] in page points, reading order."""
    import pytesseract
    from . import tess
    from .ocr import render_gray
    tess.configure()
    gray = render_gray(page, dpi, clip)
    d = pytesseract.image_to_data(gray, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    s = 72.0 / dpi
    ox, oy = (clip.x0, clip.y0) if clip is not None else (0.0, 0.0)
    out = []
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        try:
            conf = float(d["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not t or conf < 0:
            continue
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        out.append(PlacedWord(t, (ox + x * s, oy + y * s, ox + (x + w) * s, oy + (y + h) * s), conf,
                              (d["block_num"][i], d["par_num"][i], d["line_num"][i])))
    return out


def layer_pdf(width, height, words):
    """A one-page PDF (width x height points, the page as displayed) with each word as invisible
    text in its rect, stretched to the rect's width."""
    doc = pymupdf.open()
    pg = doc.new_page(width=width, height=height)
    f = _font()
    for w in words:
        r = w.rect
        if r.is_empty or r.height < 1 or not w.text:
            continue
        fs = max(2.0, r.height * 0.86)
        tl = f.text_length(w.text, fontsize=fs)
        base = pymupdf.Point(r.x0, r.y1 - r.height * 0.18)
        sx = (r.width / tl) if tl > 0 else 1.0
        try:
            pg.insert_text(base, w.text, fontsize=fs, fontname=FONT, render_mode=3,
                           morph=(base, pymupdf.Matrix(sx, 1.0)))
        except Exception:
            continue                                  # (a character the base font can't encode)
    del pg
    data = doc.tobytes(garbage=1, deflate=True)
    doc.close()
    return data


def ocr_layer(page, lang="eng", dpi=OCR_DPI):
    """The stage 10 OCR page layer: Tesseract's own words."""
    return layer_pdf(page.rect.width, page.rect.height, page_words(page, lang, dpi))


# ---------------- the corrected text, on Tesseract's word positions ----------------
_NORM = re.compile(r"[\W_]+", re.U)


def _norm(t):
    return _NORM.sub("", t.lower())


def _split(rect, tokens):
    """Split a rect horizontally among tokens, by length."""
    total = sum(max(1, len(t)) for t in tokens) or 1
    out, x = [], rect.x0
    for t in tokens:
        w = rect.width * max(1, len(t)) / total
        out.append(pymupdf.Rect(x, rect.y0, x + w, rect.y1))
        x += w
    return out


def corrected_words(tokens, ocr):
    """tokens: the corrected text's words, in order. ocr: Tesseract's PlacedWords for the same
    region, in reading order. -> [PlacedWord] with the corrected text on matching positions."""
    a, b = [_norm(t) for t in tokens], [_norm(w.text) for w in ocr]
    out = []
    last = None
    sm = difflib.SequenceMatcher(None, b, a, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(j2 - j1):
                last = ocr[i1 + k].rect
                out.append(PlacedWord(tokens[j1 + k], last))
        elif op == "replace":
            # the corrected words take the place of the words they replace, line by line
            span = ocr[i1:i2]
            lines = {}
            for w in span:
                lines.setdefault(w.key, []).append(w.rect)
            boxes = [pymupdf.Rect(min(r.x0 for r in rs), min(r.y0 for r in rs), max(r.x1 for r in rs),
                                  max(r.y1 for r in rs)) for rs in lines.values()]
            toks = tokens[j1:j2]
            per = max(1, -(-len(toks) // len(boxes)))
            for n, box in enumerate(boxes):
                chunk = toks[n * per:(n + 1) * per]
                if chunk:
                    out += [PlacedWord(t, r) for t, r in zip(chunk, _split(box, chunk))]
            rest = toks[len(boxes) * per:]
            if rest:
                out += [PlacedWord(t, r) for t, r in zip(rest, _split(boxes[-1], rest))]
            last = boxes[-1]
        elif op == "insert":
            # words with no counterpart on the image (typed into a gap with nothing readable): after
            # the previous word, on its line
            toks = tokens[j1:j2]
            ref = last or (ocr[i1].rect if i1 < len(ocr) else None)
            if ref is None:
                continue
            h = ref.height
            x0 = ref.x1 + h * 0.3 if last is not None else ref.x0
            box = pymupdf.Rect(x0, ref.y0, x0 + h * 0.55 * sum(len(t) + 1 for t in toks), ref.y1)
            out += [PlacedWord(t, r) for t, r in zip(toks, _split(box, toks))]
            last = box
        # "delete": words Tesseract saw that the corrected text doesn't have: no text for them
    return out
