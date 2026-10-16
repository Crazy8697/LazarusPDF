"""Rendering, deskew, Tesseract, and figure/drawing detection."""
from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf
import pytesseract

from .layout import looks_garbled

CONF_LINE = 60   # mean word confidence below this -> the whole line is a gap
CONF_WORD = 30   # a single word below this -> inline gap
LABEL_CONF = 70  # OCR words inside a drawing kept as figure labels

GAP = None       # segment marker for "fill in" (extract.GAP is the same marker)


@dataclass
class Word:
    text: str
    conf: float
    box: tuple    # x0, y0, x1, y1 in image pixels
    key: tuple    # (block, par, line)


def render_gray(page, dpi, clip=None):
    pix = page.get_pixmap(dpi=dpi, clip=clip, colorspace=pymupdf.csGRAY, alpha=False)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.stride)
    return a[:, : pix.width].copy()


def render_png(page, clip, dpi=150):
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
    return pix.tobytes("png")


def binarize(gray):
    _, b = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return b


def estimate_skew(gray):
    """Angle (deg) that best straightens text rows, searched in +/-5 deg."""
    h, w = gray.shape
    scale = min(1.0, 1000.0 / max(w, 1))
    small = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    b = binarize(small)
    if b.mean() < 0.5:  # near-blank
        return 0.0
    c = (b.shape[1] / 2, b.shape[0] / 2)

    def score(a):
        m = cv2.getRotationMatrix2D(c, a, 1.0)
        r = cv2.warpAffine(b, m, (b.shape[1], b.shape[0]), flags=cv2.INTER_NEAREST)
        return float(np.var(r.sum(axis=1, dtype=np.float64)))

    best = max(np.arange(-5, 5.01, 0.5), key=score)
    best = max(np.arange(best - 0.5, best + 0.51, 0.1), key=score)
    return float(best)


def deskew(gray):
    a = estimate_skew(gray)
    if abs(a) < 0.15:
        return gray, 0.0
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w / 2, h / 2), a, 1.0)
    out = cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)
    return out, a


def ocr_words(gray, psm=3):
    d = pytesseract.image_to_data(gray, lang="eng", config=f"--psm {psm}",
                                  output_type=pytesseract.Output.DICT)
    words = []
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        try:
            conf = float(d["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not t or conf < 0:
            continue
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        words.append(Word(t, conf, (x, y, x + w, y + h),
                          (d["block_num"][i], d["par_num"][i], d["line_num"][i])))
    return words


def line_segments(words):
    """One OCR line -> segments (str or GAP). Never keeps text Tesseract wasn't sure of."""
    if not words:
        return []
    total = sum(len(w.text) for w in words)
    mean = sum(w.conf * len(w.text) for w in words) / max(total, 1)
    text = " ".join(w.text for w in words)
    if mean < CONF_LINE or looks_garbled(text, strict=True):
        return [GAP]
    segs = []
    for w in words:
        if w.conf < CONF_WORD and any(ch.isalnum() for ch in w.text):
            segs.append(GAP)
        else:
            segs.append(w.text)
    return segs


def merge_segments(segs):
    """Join adjacent text pieces, collapse runs of gaps."""
    out = []
    for s in segs:
        if s is GAP:
            if out and out[-1] is GAP:
                continue
            out.append(GAP)
        else:
            if out and out[-1] is not GAP:
                prev = out[-1]
                out[-1] = prev + s if prev.endswith("-") else prev + " " + s
            else:
                out.append(s)
    return out


def paragraphs(words):
    """Group words by Tesseract (block, par); returns [(box, segments, words)]."""
    paras = {}
    for w in words:
        paras.setdefault(w.key[:2], []).append(w)
    out = []
    for _, ws in sorted(paras.items()):
        lines = {}
        for w in ws:
            lines.setdefault(w.key, []).append(w)
        segs = []
        for _, lw in sorted(lines.items()):
            lw.sort(key=lambda w: w.box[0])
            segs.extend(line_segments(lw))
        box = (min(w.box[0] for w in ws), min(w.box[1] for w in ws),
               max(w.box[2] for w in ws), max(w.box[3] for w in ws))
        out.append((box, merge_segments(segs), ws))
    return out


def _remove_long_rules(b):
    """Drop long straight rules (page frames, table lines, underlines)."""
    h, w = b.shape
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(int(w * 0.25), 1), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(int(h * 0.25), 1)))
    rules = cv2.morphologyEx(b, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(b, cv2.MORPH_OPEN, vk)
    return cv2.bitwise_and(b, cv2.bitwise_not(rules))


def _merge_boxes(boxes, pad):
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out = []
        while boxes:
            a = boxes.pop()
            i = 0
            while i < len(boxes):
                b = boxes[i]
                if (a[0] - pad <= b[2] and b[0] - pad <= a[2] and
                        a[1] - pad <= b[3] and b[1] - pad <= a[3]):
                    a = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    boxes.pop(i)
                    changed = True
                else:
                    i += 1
            out.append(a)
        boxes = out
    return [tuple(b) for b in boxes]


@dataclass
class Region:
    box: tuple        # pixels
    figure: bool
    ink: int


def find_regions(gray, covered_boxes, dpi, text_h=None):
    """Ink not covered by known text, split into drawings (figure=True) and
    leftover text-like marks (figure=False). covered_boxes in pixels."""
    b = binarize(gray)
    full = b.copy()
    for (x0, y0, x1, y1) in covered_boxes:
        cv2.rectangle(b, (int(x0) - 2, int(y0) - 3), (int(x1) + 2, int(y1) + 3), 0, -1)
    b = _remove_long_rules(b)
    if cv2.countNonZero(b) == 0:
        return []
    k = max(3, int(dpi * 0.08))
    dil = cv2.dilate(b, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    cnts, _ = cv2.findContours(dil, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        boxes.append((x, y, x + w, y + h))
    boxes = _merge_boxes(boxes, int(dpi * 0.05))

    th = text_h or dpi * 0.15
    big = th * 2.5
    n, _, stats, _ = cv2.connectedComponentsWithStats(b, connectivity=8)
    regions = []
    for (x0, y0, x1, y1) in boxes:
        w_in, h_in = (x1 - x0) / dpi, (y1 - y0) / dpi
        ink = cv2.countNonZero(b[y0:y1, x0:x1])
        if ink < 30 or (w_in < 0.15 and h_in < 0.15):
            continue
        sel = ((stats[1:, 0] >= x0) & (stats[1:, 1] >= y0) &
               (stats[1:, 0] + stats[1:, 2] <= x1) & (stats[1:, 1] + stats[1:, 3] <= y1))
        s = stats[1:][sel]
        big_ink = int(s[(s[:, 2] > big) | (s[:, 3] > big), 4].sum()) if len(s) else 0
        all_ink = cv2.countNonZero(full[y0:y1, x0:x1])
        covered_share = 1 - ink / max(all_ink, 1)
        is_fig = (big_ink / ink >= 0.35 and w_in >= 0.5 and h_in >= 0.4 and covered_share < 0.6)
        if is_fig:
            regions.append(Region((x0, y0, x1, y1), True, ink))
            continue
        if len(s):
            size = np.maximum(s[:, 2], s[:, 3])
            glyphs = int(((size >= th * 0.25) & (size <= th * 2)).sum())
        else:
            glyphs = 0
        if h_in >= 0.08 and w_in >= 0.25 and glyphs >= 3:
            regions.append(Region((x0, y0, x1, y1), False, ink))
    return regions
