"""Stage 13: compare two PDFs. The engine (no Qt). It runs in a worker PROCESS (compare_ui):
text extraction, page alignment and the word diff are CPU-bound Python and PyMuPDF holds the
GIL inside each call, so a thread would still stall the window; a process never does.

Text diff (default):
  - Words with their boxes come from PyMuPDF ("words", in the DISPLAYED page frame), so every
    change maps to exact boxes. Options: ignore case, punctuation-only changes, whitespace
    (a change whose words only differ in spacing is not a change), running headers / footers
    and page numbers (the same rule as auto-bookmarks: the same line, digits aside, on a third
    or more of the pages; plus lines that are only a page number at the top or bottom).
  - Pages are aligned by content, never page N to page N: each page gets word-shingle
    fingerprints (a small rendered thumbnail for pages without text); likely pairs come from
    an index of shared shingles; mutual best matches are paired; the longest in-order chain of
    pairs is "in place", pairs outside it are "moved", unpaired pages are "added" / "removed"
    (inside a gap between in-place pages, leftovers on both sides are paired as edited pages).
  - The words of in-place pages are diffed as one flow: whatever is unmatched at the end of a
    page pair is carried into the next pair, so a paragraph that reflowed onto the next page
    is matched there, not reported as a delete + insert.
Visual diff: matched pages are rendered at the same scale (grey), lightly blurred, and pixels
that differ by more than a threshold are grouped into regions; regions smaller than a minimum
size are dropped, so anti-aliasing noise doesn't count.

Nothing here writes either document. A report is a NEW file (report())."""
import difflib
import re
import unicodedata
from collections import defaultdict

import pymupdf

R = pymupdf.Rect
DEFAULT_OPTIONS = {"ignore_whitespace": True, "ignore_case": False, "ignore_punct": False, "ignore_running": True,
                   "left_range": "all", "right_range": "all", "threshold": 48, "min_region": 3}
SIM_PAIR = 0.35            # page similarity to count as the same page
SIM_GAP = 0.12             # two leftover pages in the same gap: the same page, heavily edited
CARRY_LIMIT = 4000         # words carried across pages before they are flushed as changes
VIS_DPI = 100


class Cancelled(Exception):
    pass


# ---------------- words ----------------
class Tok:
    __slots__ = ("raw", "norm", "page", "rect", "line")

    def __init__(self, raw, norm, page, rect, line):
        self.raw, self.norm, self.page, self.rect, self.line = raw, norm, page, rect, line


def _strip_punct(s):
    return "".join(c for c in s if not unicodedata.category(c).startswith("P"))


def normalize(word, opts):
    w = word
    if opts.get("ignore_punct"):
        w = _strip_punct(w)
    if opts.get("ignore_case"):
        w = w.casefold()
    return w


PAGE_NO = re.compile(r"^(page\s*)?\d+(\s*(of|/)\s*\d+)?$", re.I)


def read_doc(doc, pages):
    """{page: [(word, displayed rect, (block, line))]} and {page: page size} for the given pages."""
    words, sizes = {}, {}
    for pno in pages:
        pg = doc[pno]
        m = pg.rotation_matrix
        sizes[pno] = (pg.rect.width, pg.rect.height)
        words[pno] = [(w[4], (R(w[:4]) * m).normalize(), (w[5], w[6])) for w in pg.get_text("words")]
        del pg
    return words, sizes


def running_lines(words, sizes, n_pages):
    """Line keys (text with digits as #) that repeat on a third or more of the pages, and
    (page, line) ids that are only a page number at the top or bottom of their page."""
    per_key = defaultdict(set)
    numbers = set()
    for pno, ws in words.items():
        lines = defaultdict(list)
        for w, r, ln in ws:
            lines[ln].append((w, r))
        h = sizes[pno][1]
        for ln, items in lines.items():
            text = " ".join(w for w, _ in items)
            per_key[re.sub(r"\d+", "#", text)].add(pno)
            top = min(r.y0 for _, r in items)
            if PAGE_NO.match(text.strip()) and (top < h * 0.12 or top > h * 0.85):
                numbers.add((pno, ln))
    lim = max(3, n_pages // 3)
    return {k for k, ps in per_key.items() if len(ps) >= lim}, numbers


def tokens(words, sizes, opts, n_pages):
    """{page: [Tok]} with the chosen options applied."""
    run_keys, numbers = (running_lines(words, sizes, n_pages) if opts.get("ignore_running") else (set(), set()))
    out = {}
    for pno, ws in words.items():
        skip = set()
        if run_keys or numbers:
            lines = defaultdict(list)
            for w, r, ln in ws:
                lines[ln].append(w)
            for ln, items in lines.items():
                if re.sub(r"\d+", "#", " ".join(items)) in run_keys or (pno, ln) in numbers:
                    skip.add(ln)
        ts = []
        for w, r, ln in ws:
            if ln in skip:
                continue
            n = normalize(w, opts)
            if not n:
                continue                         # punctuation only, ignored
            ts.append(Tok(w, n, pno, r, (pno,) + tuple(ln)))
        out[pno] = ts
    return out


# ---------------- page fingerprints and alignment ----------------
def shingles(toks, k=3):
    ws = [t.norm.casefold() for t in toks]
    if len(ws) < k:
        return {hash(tuple(ws))} if ws else set()
    return {hash(tuple(ws[i:i + k])) for i in range(len(ws) - k + 1)}


def thumb(page):
    """A tiny grey rendering: the fingerprint of a page without text."""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(24 / page.rect.width, 32 / page.rect.height),
                          colorspace=pymupdf.csGRAY, alpha=False)
    return bytes(pix.samples)


def sim(a, b):
    if isinstance(a, bytes) or isinstance(b, bytes):
        if not (isinstance(a, bytes) and isinstance(b, bytes)) or len(a) != len(b) or not a:
            return 0.0
        return 1.0 - sum(abs(x - y) for x, y in zip(a, b)) / (255.0 * len(a))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def align(fl, fr, lpages, rpages, tick=None):
    """fl / fr: {page: fingerprint}. Returns (chain [(l, r)] in order, moved [(l, r)],
    removed [l], added [r])."""
    index = defaultdict(set)
    for l in lpages:
        if not isinstance(fl[l], bytes):
            for h in fl[l]:
                index[h].add(l)
    scores = {}
    lpos = {p: i for i, p in enumerate(lpages)}
    rpos = {p: i for i, p in enumerate(rpages)}
    notext_l = [l for l in lpages if isinstance(fl[l], bytes)]
    for k, r in enumerate(rpages):
        if tick and k % 10 == 0:
            tick(k, len(rpages))
        cands = defaultdict(int)
        if isinstance(fr[r], bytes):
            for l in notext_l[:400]:
                cands[l] = 1
        else:
            for h in fr[r]:
                for l in index.get(h, ()):
                    cands[l] += 1
        # neighbours by position too (short pages share few shingles)
        i = int(rpos[r] * len(lpages) / max(1, len(rpages)))
        for j in range(max(0, i - 2), min(len(lpages), i + 3)):
            cands[lpages[j]] += 0
        best = sorted(cands, key=lambda l: -cands[l])[:8]
        for l in best:
            s = sim(fl[l], fr[r])
            if s > 0:
                scores[(l, r)] = s
    # mutual best (ties go to the closest position)
    def key(p):
        l, r = p
        return scores[p] - 1e-4 * abs(lpos[l] / max(1, len(lpages)) - rpos[r] / max(1, len(rpages)))
    best_l, best_r = {}, {}
    for p in scores:
        l, r = p
        if scores[p] < SIM_PAIR:
            continue
        if r not in best_r or key(p) > key((best_r[r], r)):
            best_r[r] = l
        if l not in best_l or key(p) > key((l, best_l[l])):
            best_l[l] = r
    pairs = [(l, r) for r, l in best_r.items() if best_l.get(l) == r]
    used_l, used_r = {l for l, _ in pairs}, {r for _, r in pairs}
    for p in sorted((p for p in scores if scores[p] >= 0.5), key=lambda p: -scores[p]):
        if p[0] not in used_l and p[1] not in used_r:
            pairs.append(p)
            used_l.add(p[0])
            used_r.add(p[1])
    # the longest (heaviest) in-order chain = pages in place; the rest moved
    pairs.sort(key=lambda p: (lpos[p[0]], rpos[p[1]]))
    n = len(pairs)
    w = [1.0 + scores[p] for p in pairs]
    best = [w[i] for i in range(n)]
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if rpos[pairs[j][1]] < rpos[pairs[i][1]] and best[j] + w[i] > best[i]:
                best[i], prev[i] = best[j] + w[i], j
    chain = []
    if n:
        i = max(range(n), key=lambda k: best[k])
        while i >= 0:
            chain.append(pairs[i])
            i = prev[i]
        chain.reverse()
    in_chain = set(chain)
    moved = [p for p in pairs if p not in in_chain]
    # leftovers in the same gap between two in-place pages: the same page, heavily edited
    used_l = {l for l, _ in pairs}
    used_r = {r for _, r in pairs}
    full = []
    bounds = [(-1, -1)] + [(lpos[l], rpos[r]) for l, r in chain] + [(len(lpages), len(rpages))]
    for k in range(len(bounds) - 1):
        (a0, b0), (a1, b1) = bounds[k], bounds[k + 1]
        if k:
            full.append(chain[k - 1])
        gl = [lpages[i] for i in range(a0 + 1, a1) if lpages[i] not in used_l]
        gr = [rpages[i] for i in range(b0 + 1, b1) if rpages[i] not in used_r]
        for l, r in zip(gl, gr):
            if sim(fl[l], fr[r]) >= SIM_GAP:
                full.append((l, r))
                used_l.add(l)
                used_r.add(r)
    removed = [l for l in lpages if l not in used_l]
    added = [r for r in rpages if r not in used_r]
    return full, moved, removed, added


# ---------------- the text diff ----------------
def _rects(toks):
    """{page: [rects]}: one rect per line run."""
    out = defaultdict(list)
    cur, key = None, None
    for t in toks:
        if t.line != key:
            if cur is not None:
                out[key[0]].append(cur)
            cur, key = R(t.rect), t.line
        else:
            cur |= t.rect
    if cur is not None:
        out[key[0]].append(cur)
    return dict(out)


def _text(toks, limit=80):
    s = " ".join(t.raw for t in toks)
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _change(kind, a, b, anchor_l, anchor_r, how="text"):
    left, right = _rects(a), _rects(b)
    lp = a[0].page if a else (anchor_l.page if anchor_l else None)
    rp = b[0].page if b else (anchor_r.page if anchor_r else None)
    return {"kind": kind, "how": how, "left": left, "right": right, "ltext": _text(a), "rtext": _text(b),
            "lpage": lp, "rpage": rp,
            "lanchor": (anchor_l.page, tuple(anchor_l.rect)) if not a and anchor_l else None,
            "ranchor": (anchor_r.page, tuple(anchor_r.rect)) if not b and anchor_r else None}


def diff_tokens(A, B, opts, out, final=True):
    """Diff two word lists, appending changes to out. Returns the carry (unmatched tails after
    the last equal run) unless final."""
    sm = difflib.SequenceMatcher(None, [t.norm for t in A], [t.norm for t in B], autojunk=False)
    ops = sm.get_opcodes()
    cut = len(ops)
    if not final:
        last_eq = max((k for k, o in enumerate(ops) if o[0] == "equal"), default=-1)
        cut = last_eq + 1
        if last_eq < 0 and len(A) + len(B) <= CARRY_LIMIT:
            return A, B                                  # nothing matched yet: keep it all for the next page
    for tag, i1, i2, j1, j2 in ops[:cut]:
        if tag == "equal":
            continue
        a, b = A[i1:i2], B[j1:j2]
        anchor_l = A[i1 - 1] if i1 else (A[i2] if i2 < len(A) else None)
        anchor_r = B[j1 - 1] if j1 else (B[j2] if j2 < len(B) else None)
        if tag == "replace":
            if opts.get("ignore_whitespace") and "".join(t.norm for t in a) == "".join(t.norm for t in b):
                continue
            out.append(_change("modify", a, b, None, None))
        elif tag == "delete":
            out.append(_change("delete", a, [], None, anchor_r))
        else:
            out.append(_change("insert", [], b, anchor_l, None))
    if final or cut == len(ops):
        return [], []
    _tag, i1, i2, j1, j2 = ops[cut - 1]
    return A[i2:], B[j2:]


# ---------------- the visual diff ----------------
def visual_regions(pl, pr, threshold=48, min_cells=3, cell=4):
    """Regions (displayed rects, left page space) where two pages differ visibly."""
    import numpy as np
    s = VIS_DPI / 72
    pa = pl.get_pixmap(matrix=pymupdf.Matrix(s, s), colorspace=pymupdf.csGRAY, alpha=False)
    W, H = pa.width, pa.height
    sx, sy = W / pr.rect.width, H / pr.rect.height
    pb = pr.get_pixmap(matrix=pymupdf.Matrix(sx, sy), colorspace=pymupdf.csGRAY, alpha=False)
    if pb.width != W or pb.height != H:
        return []
    if pa.samples == pb.samples:
        return []                                           # identical renderings: nothing to find
    a = np.frombuffer(pa.samples, dtype=np.uint8).reshape(pa.height, pa.stride)[:, :W].astype(np.int16)
    b = np.frombuffer(pb.samples, dtype=np.uint8).reshape(pb.height, pb.stride)[:, :W].astype(np.int16)

    def blur(x):
        p = np.pad(x, 1, mode="edge")
        acc = np.zeros((H, W), dtype=np.int16)
        for dy in range(3):
            for dx in range(3):
                acc += p[dy:dy + H, dx:dx + W]
        return acc // 9
    # a pixel differs only if NO pixel within one pixel of it in the other page matches it, both
    # ways: anti-aliasing and sub-pixel shifts move edges by less than that
    ba, bb = blur(a), blur(b)

    def nearest(x, y):
        p = np.pad(y, 1, mode="edge")
        best = np.abs(x - p[1:1 + H, 1:1 + W])
        for dy in range(3):
            for dx in range(3):
                if dy == 1 and dx == 1:
                    continue
                np.minimum(best, np.abs(x - p[dy:dy + H, dx:dx + W]), out=best)
        return best
    rough = np.abs(ba - bb) > threshold
    if not rough.any():
        return []
    mask = rough & (nearest(ba, bb) > threshold) & (nearest(bb, ba) > threshold)
    gh, gw = H // cell, W // cell
    if not gh or not gw:
        return []
    cells = mask[:gh * cell, :gw * cell].reshape(gh, cell, gw, cell).sum(axis=(1, 3)) >= 2
    seen = np.zeros_like(cells)
    regions = []
    ys, xs = np.nonzero(cells)
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if seen[y0, x0]:
            continue
        stack, comp = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            comp.append((y, x))
            for dy in (-2, -1, 0, 1, 2):                       # gaps of one cell still join
                for dx in (-2, -1, 0, 1, 2):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < gh and 0 <= xx < gw and cells[yy, xx] and not seen[yy, xx]:
                        seen[yy, xx] = True
                        stack.append((yy, xx))
        if len(comp) < min_cells:
            continue
        cy = [c[0] for c in comp]
        cx = [c[1] for c in comp]
        regions.append(R(min(cx) * cell / s, min(cy) * cell / s, (max(cx) + 1) * cell / s, (max(cy) + 1) * cell / s))
    return merge_rects(regions)


def merge_rects(rects, gap=6.0):
    """Regions that touch or nearly touch become one."""
    rects = [R(r) for r in rects]
    merged = True
    while merged:
        merged = False
        out = []
        for r in rects:
            for k, o in enumerate(out):
                if R(o.x0 - gap, o.y0 - gap, o.x1 + gap, o.y1 + gap).intersects(r):
                    out[k] = o | r
                    merged = True
                    break
            else:
                out.append(r)
        rects = out
    return rects


# ---------------- the whole comparison ----------------
def compare(dl, dr, mode="text", opts=None, progress=None, stopped=None):
    """Compare two open documents. Returns a plain dict (picklable): pages, alignment, changes,
    page events, pages without text, summary."""
    from .stage10 import parse_range
    o = dict(DEFAULT_OPTIONS)
    o.update(opts or {})
    lpages = parse_range(o.get("left_range", "all"), dl.page_count)
    rpages = parse_range(o.get("right_range", "all"), dr.page_count)

    def tick(phase, done, total):
        if stopped and stopped():
            raise Cancelled()
        if progress:
            progress(phase, done, total)
    tick("Reading the left file", 0, 1)
    wl, sl = read_doc(dl, lpages)
    tick("Reading the right file", 0, 1)
    wr, sr = read_doc(dr, rpages)
    tl = tokens(wl, sl, o, len(lpages))
    tr = tokens(wr, sr, o, len(rpages))
    notext = {"left": [p for p in lpages if not wl[p]], "right": [p for p in rpages if not wr[p]]}
    fl, fr = {}, {}
    for side, doc, pages, toks, fp in (("left", dl, lpages, tl, fl), ("right", dr, rpages, tr, fr)):
        for k, p in enumerate(pages):
            if k % 20 == 0:
                tick(f"Fingerprinting the {side} pages", k, len(pages))
            if toks[p]:
                fp[p] = shingles(toks[p])
            else:                                      # no (usable) text: what the page looks like
                pg = doc[p]
                fp[p] = thumb(pg)
                del pg
    chain, moved, removed, added = align(fl, fr, lpages, rpages, lambda d, t: tick("Aligning pages", d, t))
    changes = []
    events = ([{"kind": "removed", "lpage": l, "rpage": None} for l in removed]
              + [{"kind": "added", "lpage": None, "rpage": r} for r in added]
              + [{"kind": "moved", "lpage": l, "rpage": r} for l, r in moved])
    if mode == "visual":
        for k, (l, r) in enumerate(chain + moved):
            tick("Comparing pages", k, len(chain) + len(moved))
            pa, pb = dl[l], dr[r]
            for rect in visual_regions(pa, pb, int(o.get("threshold", 48)), int(o.get("min_region", 3))):
                sx = pb.rect.width / pa.rect.width
                sy = pb.rect.height / pa.rect.height
                rr = R(rect.x0 * sx, rect.y0 * sy, rect.x1 * sx, rect.y1 * sy)
                changes.append({"kind": "modify", "how": "visual", "left": {l: [rect]}, "right": {r: [rr]},
                                "ltext": "", "rtext": "", "lpage": l, "rpage": r, "lanchor": None, "ranchor": None})
            del pa, pb
    else:
        ca, cb = [], []
        nt_l, nt_r = set(notext["left"]), set(notext["right"])
        for k, (l, r) in enumerate(chain):
            tick("Comparing text", k, len(chain))
            if l in nt_l and r in nt_r:
                continue
            ca, cb = diff_tokens(ca + tl[l], cb + tr[r], o, changes, final=(k == len(chain) - 1))
        if ca or cb:
            diff_tokens(ca, cb, o, changes, final=True)
        for l, r in moved:
            diff_tokens(tl[l], tr[r], o, changes, final=True)
    tick("Done", 1, 1)
    count = lambda k: sum(1 for c in changes if c["kind"] == k)
    summary = {"insert": count("insert"), "delete": count("delete"), "modify": count("modify"),
               "added": len(added), "removed": len(removed), "moved": len(moved)}
    return {"mode": mode, "options": o, "pairs": chain, "moved": moved, "removed": removed, "added": added,
            "changes": changes, "events": events, "notext": notext if mode == "text" else {"left": [], "right": []},
            "summary": summary, "sizes": {"left": sl, "right": sr}}


def page_map(result):
    """{left page: right page} and back, for synced scrolling: the in-place pages only (a moved
    page is reached from the change list; scrolling past it shouldn't jump the other pane)."""
    lr = {l: r for l, r in result["pairs"]}
    return lr, {r: l for l, r in lr.items()}


# ---------------- the worker process ----------------
def worker_main(spec, conn, cancel):
    """spec: {"left": bytes, "right": bytes, "mode", "options"} or {"report": ...}."""
    if spec.get("test_delay"):                               # self-test hook: a slow comparison
        import time as _time
        _send = conn.send

        class _Slow:
            def send(self, m):
                _time.sleep(spec["test_delay"])
                _send(m)
        conn = _Slow()
    try:
        dl = pymupdf.open(stream=spec["left"], filetype="pdf")
        dr = pymupdf.open(stream=spec["right"], filetype="pdf")
        try:
            if spec.get("report"):
                report(dl, dr, spec["result"], spec["report"], spec["colors"],
                       progress=lambda *p: conn.send(("progress",) + p), stopped=cancel.is_set)
                conn.send(("done", {"report": spec["report"]}))
            else:
                res = compare(dl, dr, spec.get("mode", "text"), spec.get("options"),
                              progress=lambda *p: conn.send(("progress",) + p), stopped=cancel.is_set)
                conn.send(("done", res))
        finally:
            dl.close()
            dr.close()
    except Cancelled:
        conn.send(("cancelled",))
    except Exception as e:                                  # shown on screen only
        conn.send(("failed", f"{type(e).__name__}: {e}"))


# ---------------- the report (a NEW file) ----------------
KIND_LABEL = {"insert": "Inserted", "delete": "Deleted", "modify": "Changed", "added": "Page added",
              "removed": "Page removed", "moved": "Page moved"}


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def report(dl, dr, result, dest, colors, progress=None, stopped=None, limit=300):
    """Summary, change list, and side-by-side snippets of each change with its marks, as a new
    PDF written atomically (never onto either compared file: the caller checked protect)."""
    import os
    from pathlib import Path
    from . import protect
    dest = Path(dest)
    protect.check(dest)
    col = {k: _hex_rgb(v) for k, v in colors.items()}
    out = pymupdf.open()
    W, H = 595, 842
    s = result["summary"]
    page = out.new_page(width=W, height=H)
    y = [60]

    def line(text, size=10, color=(0, 0, 0), font="helv"):
        nonlocal page
        if y[0] > H - 50:
            page = out.new_page(width=W, height=H)
            y[0] = 50
        page.insert_text((50, y[0]), text, fontsize=size, color=color, fontname=font)
        y[0] += size * 1.45
    line("Comparison report", 18, font="hebo")
    line(f"Mode: {'text' if result['mode'] == 'text' else 'visual'}   ·   the left file vs the right file", 10,
         (0.35, 0.35, 0.4))
    y[0] += 6
    line(f"Insertions {s['insert']}   Deletions {s['delete']}   Changes {s['modify']}   Pages added {s['added']}   "
         f"removed {s['removed']}   moved {s['moved']}", 11, font="hebo")
    y[0] += 8
    for e in result["events"]:
        if e["kind"] == "moved":
            line(f"Page moved: left page {e['lpage'] + 1} is right page {e['rpage'] + 1}", 10)
        elif e["kind"] == "removed":
            line(f"Page removed: left page {e['lpage'] + 1}", 10, col["delete"])
        else:
            line(f"Page added: right page {e['rpage'] + 1}", 10, col["insert"])
    for side in ("left", "right"):
        for p in result["notext"].get(side, []):
            line(f"No text on {side} page {p + 1} (a scan? OCR to searchable, or use Visual mode)", 9, (0.5, 0.4, 0.1))
    y[0] += 6
    for k, c in enumerate(result["changes"][:limit]):
        lp = "" if c["lpage"] is None else f"L{c['lpage'] + 1}"
        rp = "" if c["rpage"] is None else f"R{c['rpage'] + 1}"
        txt = (f"“{c['ltext']}” → “{c['rtext']}”" if c["kind"] == "modify" and c["how"] == "text" else
               c["ltext"] or c["rtext"] or "visual difference")
        line(f"{k + 1}. {KIND_LABEL[c['kind']]} {lp} {rp}: {txt}"[:110], 9, col[c["kind"]])
    if len(result["changes"]) > limit:
        line(f"… and {len(result['changes']) - limit} more", 9)
    # snippets: the area around each change on both sides, marks drawn in
    marked = {}
    for side, doc in (("left", dl), ("right", dr)):
        d = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        for c in result["changes"][:limit]:
            for p, rects in c[side].items():
                pg = d[p]
                for r in rects:
                    pg.draw_rect((R(r) * pg.derotation_matrix).normalize(), color=col[c["kind"]], width=1.2)
                del pg
        marked[side] = d
    try:
        cw, ch = (W - 120) / 2, 150
        for k, c in enumerate(result["changes"][:limit]):
            if stopped and stopped():
                raise Cancelled()
            if progress and k % 10 == 0:
                progress("Writing the report", k, min(limit, len(result["changes"])))
            if k % 4 == 0:
                page = out.new_page(width=W, height=H)
                y[0] = 50
            page.insert_text((50, y[0]), f"{k + 1}. {KIND_LABEL[c['kind']]}", fontsize=10, fontname="hebo",
                             color=col[c["kind"]])
            y[0] += 8
            for j, side in enumerate(("left", "right")):
                box = R(50 + j * (cw + 20), y[0], 50 + j * (cw + 20) + cw, y[0] + ch)
                page.draw_rect(box, color=(0.8, 0.8, 0.85), width=0.5)
                pno, rects = None, None
                if c[side]:
                    pno, rects = next(iter(c[side].items()))
                elif c.get("lanchor" if side == "left" else "ranchor"):
                    pno, r0 = c["lanchor" if side == "left" else "ranchor"]
                    rects = [R(r0)]
                if pno is None:
                    page.insert_text((box.x0 + 6, box.y0 + 14), "(nothing on this side)", fontsize=8,
                                     color=(0.5, 0.5, 0.55))
                    continue
                src = marked[side][pno]
                area = R(rects[0])
                for r in rects[1:]:
                    area |= R(r)
                area = R(area.x0 - 60, area.y0 - 40, area.x1 + 60, area.y1 + 40) & src.rect
                clip = (area * src.derotation_matrix).normalize()
                page.show_pdf_page(box, marked[side], pno, clip=clip, rotate=-src.rotation)
                page.insert_text((box.x0, box.y1 + 9), f"{side} page {pno + 1}", fontsize=7, color=(0.4, 0.4, 0.45))
            y[0] += ch + 30
    finally:
        for d in marked.values():
            d.close()
    tmp = dest.with_name(f".{dest.stem}.lazaruspdf-partial{dest.suffix}")
    try:
        out.save(str(tmp), garbage=3, deflate=True)       # no encryption: nothing of either file's password
        out.close()
        protect.check(dest)
        os.replace(tmp, dest)
    finally:
        if not out.is_closed:
            out.close()
        if tmp.exists():
            tmp.unlink()
    return str(dest)
