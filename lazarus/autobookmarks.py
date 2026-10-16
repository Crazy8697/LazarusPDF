"""Stage 7a: generate bookmarks (an outline) for a document automatically.

No UI here, so Batch (10b) can call it too: analyze(doc) reads the text once; propose(...)
turns it into an outline in the stage-7 shape [level, title, page (1-based), {"to": (x, y)}].

Two methods, both best-effort by design:
- Headings: the body text size is the size most characters use. Lines set larger than that,
  or bold at body size, are heading candidates. Their distinct styles (size + bold) are ranked,
  largest first, and the top N become levels 1..N. Running headers / footers (the same text on
  many pages), page numbers and over-long lines are ignored; a heading set over two lines
  becomes one title.
- Table-of-contents page: a page (early in the document) with many lines like
  "Chapter 2 Wiring ........ 14" or "2.1 Fuses 17". Entries are parsed, their level comes from
  the numbering depth (2 / 2.1 / 2.1.3) or the indent, and printed page numbers are mapped to
  real pages by finding the entry titles in the document (the most common offset wins).

Text is read from a plain in-memory copy (reading a journalled document can change it).
A document with no extractable text (a scan without OCR) gives nothing: run OCR first."""
import re
from collections import Counter, defaultdict

import pymupdf

MAX_TITLE = 120               # longer lines are body text, not headings
TOC_LINE = re.compile(r"^(?P<title>.*?\S)\s*(?:\.{2,}|·{2,}|…+|\s{2,}|\s)\s*(?P<page>\d{1,4})\s*$")
NUMBERED = re.compile(r"^(?P<num>\d+(?:\.\d+)*)\.?\s+\S")


class Analysis:
    """Every text line of the document, with its style. Built once; propose() is cheap."""

    def __init__(self, doc):
        self.page_count = doc.page_count
        self.lines = []           # dicts: page, text, size, bold, font, x0, y0, chars
        plain = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        try:
            for pno in range(plain.page_count):
                pg = plain[pno]
                d = pg.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
                for b in d.get("blocks", []):
                    for ln in b.get("lines", []):
                        spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
                        if not spans:
                            continue
                        text = " ".join(s["text"].strip() for s in spans)
                        main = max(spans, key=lambda s: len(s["text"]))
                        bold = bool(main["flags"] & 16) or "bold" in main["font"].lower()
                        self.lines.append({"page": pno, "text": " ".join(text.split()), "size": round(main["size"] * 2) / 2,
                                           "bold": bold, "font": main["font"], "x0": ln["bbox"][0], "y0": ln["bbox"][1],
                                           "y1": ln["bbox"][3], "chars": len(text)})
        finally:
            plain.close()
        weights = Counter()
        for ln in self.lines:
            weights[ln["size"]] += ln["chars"]
        self.body = weights.most_common(1)[0][0] if weights else 0.0
        # running headers / footers: the same text on a third or more of the pages
        per_text = defaultdict(set)
        for ln in self.lines:
            per_text[re.sub(r"\d+", "#", ln["text"])].add(ln["page"])
        lim = max(3, self.page_count // 3)
        self.running = {t for t, pages in per_text.items() if len(pages) >= lim}
        self.toc_page = find_toc_page(self)

    @property
    def has_text(self):
        return bool(self.lines)

    def styles(self, min_delta=0.5):
        """Heading candidate styles, ranked (largest size first, bold before regular):
        [(size, bold, how many lines)]."""
        cnt = Counter()
        for ln in self._candidates(min_delta):
            cnt[(ln["size"], ln["bold"])] += 1
        return sorted(((s, b, n) for (s, b), n in cnt.items()), key=lambda x: (-x[0], not x[1]))

    def _candidates(self, min_delta):
        for ln in self.lines:
            t = ln["text"]
            if len(t) > MAX_TITLE or len(t) < 2 or re.fullmatch(r"[\d\W]+", t):
                continue                                   # body text, page numbers, rules
            if re.sub(r"\d+", "#", t) in self.running:
                continue                                   # running header / footer
            if ln["size"] >= self.body + min_delta or (ln["bold"] and ln["size"] >= self.body - 0.25):
                yield ln


def analyze(doc):
    return Analysis(doc)


def from_headings(an, levels=3, min_delta=0.5, styles=None):
    """Outline from heading styles. styles: the (size, bold) pairs to use, in level order
    (default: the top `levels` of an.styles())."""
    if styles is None:
        styles = [(s, b) for s, b, _n in an.styles(min_delta)][:max(1, int(levels))]
    rank = {st: k + 1 for k, st in enumerate(styles)}
    toc, prev = [], None
    for ln in an._candidates(min_delta):
        lvl = rank.get((ln["size"], ln["bold"]))
        if lvl is None:
            continue
        # a heading set over two lines: same style, same page, right below the previous one
        if (prev is not None and prev["page"] == ln["page"] and (prev["size"], prev["bold"]) == (ln["size"], ln["bold"])
                and 0 <= ln["y0"] - prev["y1"] < ln["size"] * 0.8 and toc):
            toc[-1][1] = f"{toc[-1][1]} {ln['text']}"
            prev = ln
            continue
        toc.append([lvl, ln["text"], ln["page"] + 1, {"to": (ln["x0"], max(0.0, ln["y0"] - 4))}])
        prev = ln
    return fix_levels(toc)


def fix_levels(toc):
    """The first entry is level 1; no entry is more than one level deeper than the one before."""
    out, prev = [], 0
    for e in toc:
        lvl = max(1, min(int(e[0]), prev + 1))
        out.append([lvl] + list(e[1:]))
        prev = lvl
    return out


# ---------------- table-of-contents page ----------------
def _toc_entries(an, pno):
    out = []
    for ln in an.lines:
        if ln["page"] != pno:
            continue
        m = TOC_LINE.match(ln["text"])
        if not m:
            continue
        title = m.group("title").rstrip(" .·…")
        if len(title) < 2 or title.lower() in ("contents", "table of contents", "page"):
            continue
        out.append({"title": title, "printed": int(m.group("page")), "x0": ln["x0"]})
    return out


def find_toc_page(an):
    """The first page in the first third (or first 15 pages) with at least 5 contents-style
    lines making up most of the page; None if there is none."""
    limit = min(an.page_count, max(15, an.page_count // 3))
    per_page = Counter(ln["page"] for ln in an.lines)
    for pno in range(limit):
        ents = _toc_entries(an, pno)
        if len(ents) >= 5 and len(ents) >= 0.5 * per_page[pno]:
            return pno
    return None


def from_toc_page(an, pno=None):
    """Outline from a contents page: titles, levels (numbering depth, else indent), and real
    pages (printed numbers + the offset found by locating the titles in the document)."""
    pno = an.toc_page if pno is None else pno
    if pno is None:
        return []
    ents = _toc_entries(an, pno)
    # levels: numbering depth where present, else indentation clusters
    xs = sorted({round(e["x0"] / 6) for e in ents})
    for e in ents:
        m = NUMBERED.match(e["title"])
        e["level"] = len(m.group("num").split(".")) if m else xs.index(round(e["x0"] / 6)) + 1
    # printed page -> real page: where does each title actually appear (after the contents page)?
    by_page = defaultdict(list)
    for ln in an.lines:
        if ln["page"] > pno:
            by_page[ln["page"]].append(ln["text"].lower())
    offsets = Counter()
    for e in ents:
        key = e["title"].lower()
        for p, texts in by_page.items():
            if any(key == t or (len(key) > 6 and key in t) for t in texts):
                offsets[p - (e["printed"] - 1)] += 1
                break
    off = offsets.most_common(1)[0][0] if offsets else 0
    toc = []
    for e in ents:
        real = e["printed"] - 1 + off
        if 0 <= real < an.page_count:
            toc.append([e["level"], e["title"], real + 1, {}])
    return fix_levels(toc)


def propose(doc_or_analysis, method="headings", **kw):
    """The callable core (Batch can use it): an outline for the document, or [] if none."""
    an = doc_or_analysis if isinstance(doc_or_analysis, Analysis) else analyze(doc_or_analysis)
    if not an.has_text:
        return []
    if method == "toc":
        return from_toc_page(an)
    return from_headings(an, **kw)


def merge(existing, generated, mode):
    """mode: "replace" | "append" (alongside, after the existing ones) | "under" (nested under a
    new top-level "Generated bookmarks" entry)."""
    if mode == "replace" or not existing:
        return [list(e) for e in generated]
    if mode == "append":
        return [list(e) for e in existing] + [list(e) for e in generated]
    first = generated[0][2] if generated else 1
    return [list(e) for e in existing] + [[1, "Generated bookmarks", first, {}]] + \
        [[e[0] + 1] + list(e[1:]) for e in generated]
