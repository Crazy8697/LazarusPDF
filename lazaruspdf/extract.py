"""Text-layer-first extraction with OCR fallback. Never modifies the source."""
import statistics
from dataclasses import dataclass, field

import pymupdf

from .layout import join_lines, looks_garbled, reading_order


class _Lazy:
    """A module imported on first use (start-up speed: OpenCV / NumPy / Tesseract are only
    needed once extraction runs). The import statements stay visible to PyInstaller."""

    def __init__(self, load):
        self._load, self._mod = load, None

    def __getattr__(self, name):
        if self._mod is None:
            self._mod = self._load()
        return getattr(self._mod, name)


def _cv2():
    import cv2
    return cv2


def _ocr():
    from . import ocr as m
    return m


cv2 = _Lazy(_cv2)
ocr = _Lazy(_ocr)
GAP = None       # an unreadable stretch of text (the same marker as ocr.GAP)
DET_DPI = 150    # figure / uncovered-ink detection on text-layer pages
OCR_DPI = 300
MIN_LAYER_CHARS = 30  # fewer usable chars than this -> treat the page as a scan


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Filled:
    """A former gap the user has filled in the editor."""
    text: str


@dataclass
class Para:
    page: int
    box: tuple          # PDF points (approx. on OCR pages)
    segs: list          # str or GAP
    source: str = "layer"   # layer | ocr


@dataclass
class Figure:
    page: int
    box: tuple
    png: bytes
    labels: list = field(default_factory=list)


@dataclass
class PageResult:
    number: int         # 1-based
    method: str         # "text layer" | "OCR"
    items: list


def _center_in(box, outer):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def _layer_paras(page):
    """Text-layer blocks split into paragraphs: [(box, text, line_boxes)]."""
    d = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
    out = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        cur_lines, cur_boxes, prev = [], [], None
        for ln in b["lines"]:
            t = "".join(s["text"] for s in ln["spans"])
            if not t.strip():
                continue
            box = tuple(ln["bbox"])
            if prev is not None and box[1] - prev[3] > 0.5 * (prev[3] - prev[1]):
                out.append((cur_lines, cur_boxes))
                cur_lines, cur_boxes = [], []
            cur_lines.append(t)
            cur_boxes.append(box)
            prev = box
        if cur_lines:
            out.append((cur_lines, cur_boxes))
    paras = []
    for lines, boxes in out:
        box = (min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes))
        paras.append((box, join_lines(lines), boxes))
    return paras


def _gray_png(gray, dpi_from, dpi_to=150):
    f = dpi_to / dpi_from
    if f < 1:
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", gray)
    return buf.tobytes()


def _is_label(w):
    return w.conf >= ocr.LABEL_CONF and sum(c.isalnum() for c in w.text) >= 2


def _ocr_clip(page, clip, cover_pts, pno):
    """OCR a page region with known text whited out. Returns Paras (points)."""
    g = ocr.render_gray(page, OCR_DPI, clip)
    s = OCR_DPI / 72.0
    for b in cover_pts:
        x0, y0 = (b[0] - clip.x0) * s, (b[1] - clip.y0) * s
        x1, y1 = (b[2] - clip.x0) * s, (b[3] - clip.y0) * s
        cv2.rectangle(g, (int(x0) - 3, int(y0) - 4), (int(x1) + 3, int(y1) + 4), 255, -1)
    if clip.height > 72:
        g, _ = ocr.deskew(g)
    words = ocr.ocr_words(g, psm=6)
    if not words:
        return [Para(pno, tuple(clip), [GAP], "ocr")]
    paras = []
    for (bx, segs, _) in ocr.paragraphs(words):
        box = (clip.x0 + bx[0] / s, clip.y0 + bx[1] / s, clip.x0 + bx[2] / s, clip.y0 + bx[3] / s)
        if segs:
            paras.append(Para(pno, box, segs, "ocr"))
    return paras or [Para(pno, tuple(clip), [GAP], "ocr")]


def _layer_page(page, pno, good):
    s = DET_DPI / 72.0
    gray = ocr.render_gray(page, DET_DPI)
    line_boxes = [lb for _, _, lbs in good for lb in lbs]
    covered = [tuple(v * s for v in lb) for lb in line_boxes]
    heights = [lb[3] - lb[1] for lb in line_boxes]
    th = statistics.median(heights) * s if heights else None
    regions = ocr.find_regions(gray, covered, DET_DPI, th)

    items, used = [], set()
    for r in (r for r in regions if r.figure):
        box = tuple(v / s for v in r.box)
        clip = pymupdf.Rect(box) + (-4, -4, 4, 4)
        clip &= page.rect
        labels = []
        for i, (pb, text, _) in enumerate(good):
            if i not in used and _center_in(pb, box):
                used.add(i)
                labels.append(text)
        items.append(Figure(pno, box, ocr.render_png(page, clip), labels))
    for i, (pb, text, _) in enumerate(good):
        if i not in used:
            items.append(Para(pno, pb, [text]))
    for r in (r for r in regions if not r.figure):
        clip = (pymupdf.Rect(tuple(v / s for v in r.box)) + (-2, -2, 2, 2)) & page.rect
        inside = [lb for lb in line_boxes if pymupdf.Rect(lb).intersects(clip)]
        items.extend(_ocr_clip(page, clip, inside, pno))
    return items


def _ocr_page(page, pno):
    gray = ocr.render_gray(page, OCR_DPI)
    gray, angle = ocr.deskew(gray)
    s = OCR_DPI / 72.0
    words = ocr.ocr_words(gray, psm=3)
    confident = [w.box for w in words if w.conf >= ocr.CONF_LINE]
    hs = [w.box[3] - w.box[1] for w in words if w.conf >= ocr.CONF_LINE]
    th = statistics.median(hs) * 1.3 if hs else None
    regions = ocr.find_regions(gray, confident, OCR_DPI, th)

    items, in_fig = [], set()
    for r in (r for r in regions if r.figure):
        x0, y0, x1, y1 = r.box
        labels = []
        for i, w in enumerate(words):
            if _center_in(w.box, r.box):
                in_fig.add(i)
                if _is_label(w):
                    labels.append(w.text)
        box = tuple(v / s for v in r.box)
        if abs(angle) < 0.15:
            png = ocr.render_png(page, (pymupdf.Rect(box) + (-4, -4, 4, 4)) & page.rect)
        else:
            pad = int(4 * s)
            png = _gray_png(gray[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad], OCR_DPI)
        items.append(Figure(pno, box, png, [" ".join(labels)] if labels else []))

    rest = [w for i, w in enumerate(words) if i not in in_fig]
    for (bx, segs, _) in ocr.paragraphs(rest):
        if segs:
            items.append(Para(pno, tuple(v / s for v in bx), segs, "ocr"))

    # Ink Tesseract skipped entirely and that isn't a drawing: try again as a block.
    for r in (r for r in regions if not r.figure):
        if any(_center_in(w.box, r.box) for w in words):
            continue
        x0, y0, x1, y1 = r.box
        crop = gray[y0:y1, x0:x1]
        sub = ocr.ocr_words(crop, psm=6)
        if not sub:
            items.append(Para(pno, tuple(v / s for v in r.box), [GAP], "ocr"))
            continue
        for (bx, segs, _) in ocr.paragraphs(sub):
            if segs:
                box = ((x0 + bx[0]) / s, (y0 + bx[1]) / s, (x0 + bx[2]) / s, (y0 + bx[3]) / s)
                items.append(Para(pno, box, segs, "ocr"))
    return items


def extract_page(page, pno):
    paras = _layer_paras(page)
    good = [p for p in paras if not looks_garbled(p[1])]
    chars = sum(len(t.replace(" ", "")) for _, t, _ in good)
    if chars >= MIN_LAYER_CHARS:
        items, method = _layer_page(page, pno, good), "text layer"
    else:
        items, method = _ocr_page(page, pno), "OCR"
    order = reading_order([it.box for it in items])
    merged = []
    for it in (items[i] for i in order):
        # back-to-back unreadable paragraphs are one gap, not several
        if (merged and isinstance(it, Para) and isinstance(merged[-1], Para)
                and it.segs == [GAP] and merged[-1].segs[-1:] == [GAP]):
            continue
        merged.append(it)
    return PageResult(pno, method, merged)


def extract(pdf_path, progress=None, should_stop=None, password=None):
    """pdf_path: a path, or the PDF's bytes. progress(done, total, status, gaps, figures).
    password: for a protected PDF (asked for by the UI before the run starts)."""
    if isinstance(pdf_path, (bytes, bytearray)):     # batch: the file as it is mid-pipeline
        doc = pymupdf.open(stream=pdf_path, filetype="pdf")
    else:
        doc = pymupdf.open(pdf_path)       # read-only; never saved
    try:
        if doc.needs_pass and not (password and doc.authenticate(password)):
            raise RuntimeError("The file is password-protected.")
        total = doc.page_count
        pages, gaps, figs = [], 0, 0
        for i in range(total):
            if should_stop and should_stop():
                raise Cancelled()
            if progress:
                progress(i, total, f"Page {i + 1} of {total}", gaps, figs)
            pr = extract_page(doc[i], i + 1)
            for it in pr.items:
                if isinstance(it, Figure):
                    figs += 1
                else:
                    gaps += sum(1 for sg in it.segs if sg is GAP)
            pages.append(pr)
            if progress:
                progress(i + 1, total, f"Page {i + 1} of {total} ({pr.method})", gaps, figs)
        return pages
    finally:
        doc.close()
