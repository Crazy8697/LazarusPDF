"""Stage 11: edit existing text in place, and find / replace. The engine (no Qt).

The model is PyMuPDF's own structure (rawdict: block > line > span > char), read from a
one-page plain copy (reading text from a journalled document can change it). A block is the
unit of editing: its characters keep their style (font, flags, size, colour) and a new text
is laid out again inside the block's box ("reflow"), with each character keeping the style
of the character it replaces (typed or replaced text takes the style next to it).

Writing a change:
  1. Remove the block's old characters: a redaction per glyph, shrunk to a band around the
     middle of the letter, applied with images and line art left alone. (MuPDF removes every
     glyph whose box touches a redaction area; line boxes overlap the next line, glyph bands
     don't.) Redaction marks the user has pending on that page are set aside and put back, so
     they are never applied by an edit.
  2. Write the new text: the block's embedded font when it can render every character
     (it is embedded again from its own program), the exact standard font for a standard
     font, else the closest standard font (serif / sans / mono, bold, italic) with a notice.
  3. Verify by extracting the page again: every other character is still where it was, and
     the characters that are new read exactly the new text (the old ones are gone). Any
     difference raises EditFailed and the caller's operation leaves the document unchanged.
A block that can't be edited this way (a Type3 font, text with no usable encoding, text at
an angle) can be COVERED instead: a rectangle of the page's own colour (sampled) over it,
and the new text on top. The old text is still in the file; Redact is what removes text.

The invisible text layer OCR adds (or any invisible text) is not visible text: it is never
edited as if it were.

Find / replace runs the find bar's pattern over each block, builds the new text of every
block with a hit, and writes those blocks through the same path. A hit that runs across two
blocks, or sits in invisible text, or needs characters no available font has, is skipped
with its reason; nothing is ever half-applied."""
import difflib
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field

import pymupdf

R = pymupdf.Rect
P = pymupdf.Point

BASE14 = {"Helvetica": "helv", "Helvetica-Bold": "hebo", "Helvetica-Oblique": "heit", "Helvetica-BoldOblique": "hebi",
          "Times-Roman": "tiro", "Times-Bold": "tibo", "Times-Italic": "tiit", "Times-BoldItalic": "tibi",
          "Courier": "cour", "Courier-Bold": "cobo", "Courier-Oblique": "coit", "Courier-BoldOblique": "cobi"}
STD_LABEL = {"helv": "Helvetica", "hebo": "Helvetica Bold", "heit": "Helvetica Oblique", "hebi": "Helvetica Bold Oblique",
             "tiro": "Times", "tibo": "Times Bold", "tiit": "Times Italic", "tibi": "Times Bold Italic",
             "cour": "Courier", "cobo": "Courier Bold", "coit": "Courier Oblique", "cobi": "Courier Bold Oblique"}
INVISIBLE_FONTS = ("GlyphLessFont",)
MIN_SHRINK = 0.35                 # shrink-to-fit goes no smaller than this
FIT_SLACK = 1.0                   # points of tolerance when checking the box


class EditFailed(Exception):
    """The edit could not be made cleanly; nothing was changed."""


class NoFont(EditFailed):
    """No available font can show some of the characters."""


class Overflow(EditFailed):
    """The new text doesn't fit the box (the caller chooses shrink or grow)."""


# ---------------- the model ----------------
@dataclass
class Ch:
    c: str
    rect: R                     # unrotated page space
    origin: P
    style: tuple                # (font, flags, size, color)
    invisible: bool = False


@dataclass
class Line:
    chars: list
    x0: float = 0.0             # extent along the text direction, in the block's frame
    x1: float = 0.0
    base: float = 0.0           # baseline, across the text direction, in the block's frame


class Frame:
    """The block's own axes: x along the text, y down across it, origin at the box corner
    where its text starts. Maps between that frame and unrotated page space."""
    DIRS = {(1, 0): 0, (0, -1): 90, (-1, 0): 180, (0, 1): 270}     # text direction -> insert_text rotate

    def __init__(self, bbox, d):
        b = R(bbox)
        self.d = d
        self.rotate = self.DIRS[d]
        if d == (1, 0):
            self.o, self.u, self.v, self.w, self.h = P(b.x0, b.y0), (1, 0), (0, 1), b.width, b.height
        elif d == (0, -1):
            self.o, self.u, self.v, self.w, self.h = P(b.x0, b.y1), (0, -1), (1, 0), b.height, b.width
        elif d == (-1, 0):
            self.o, self.u, self.v, self.w, self.h = P(b.x1, b.y1), (-1, 0), (0, -1), b.width, b.height
        else:
            self.o, self.u, self.v, self.w, self.h = P(b.x1, b.y0), (0, 1), (-1, 0), b.height, b.width

    def local(self, p):
        dx, dy = p.x - self.o.x, p.y - self.o.y
        return dx * self.u[0] + dy * self.u[1], dx * self.v[0] + dy * self.v[1]

    def page(self, x, y):
        return P(self.o.x + x * self.u[0] + y * self.v[0], self.o.y + x * self.u[1] + y * self.v[1])

    def local_rect(self, r):
        pts = [self.local(P(r.x0, r.y0)), self.local(P(r.x1, r.y1))]
        return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)

    def page_rect(self, x0, y0, x1, y1):
        a, b = self.page(x0, y0), self.page(x1, y1)
        return R(min(a.x, b.x), min(a.y, b.y), max(a.x, b.x), max(a.y, b.y))


def _axis(d):
    dx, dy = d
    for k in Frame.DIRS:
        if abs(dx - k[0]) < 0.02 and abs(dy - k[1]) < 0.02:
            return k
    return None


class Block:
    def __init__(self, idx, bbox, lines, problem=None):
        self.idx = idx
        self.bbox = R(bbox)
        self.lines = lines
        self.problem = problem
        chars = [c for ln in lines for c in ln.chars]
        vis = [c for c in chars if not c.invisible]
        self.invisible = bool(chars) and not vis
        if vis and len(vis) != len(chars) and not self.problem:
            self.problem = "mixes visible and invisible text"
        self.frame = None
        self.align = "left"
        self.pitch = None
        self.indent = 0.0
        self.hard = []                       # per line: True if a hard line break follows it

    def setup(self):
        """Frame, line metrics, alignment, hard breaks (after the direction is known)."""
        f = self.frame
        for ln in self.lines:
            xs = [f.local_rect(c.rect) for c in ln.chars if c.c.strip()] or [f.local_rect(c.rect) for c in ln.chars]
            ln.x0 = min(x[0] for x in xs)
            ln.x1 = max(x[2] for x in xs)
            ln.base = f.local(ln.chars[0].origin)[1]
        bases = [ln.base for ln in self.lines]
        size = max((c.style[2] for ln in self.lines for c in ln.chars), default=10.0)
        if len(bases) > 1:
            gaps = sorted(b - a for a, b in zip(bases, bases[1:]) if b - a > 0.5)
            self.pitch = gaps[len(gaps) // 2] if gaps else size * 1.2
        else:
            self.pitch = size * 1.2
        W = f.w
        tol = max(2.0, W * 0.01)
        n = len(self.lines)
        lefts = [ln.x0 for ln in self.lines]
        rights = [ln.x1 for ln in self.lines]
        if n >= 2:
            if n >= 3 and all(abs(x) < tol for x in lefts[1:]) and all(abs(W - x) < tol for x in rights[:-1]):
                self.align = "justify"
            elif all(abs(x - lefts[1]) < tol for x in lefts[1:]) and abs(lefts[0] - lefts[1]) < W * 0.25:
                self.align = "left"
            elif all(abs(W - x) < tol for x in rights):
                self.align = "right"
            elif all(abs((a + b) / 2 - W / 2) < tol for a, b in zip(lefts, rights)):
                self.align = "center"
        if self.align in ("left", "justify") and n >= 2:
            self.indent = max(0.0, lefts[0] - min(lefts[1:]))
        else:
            self.indent = lefts[0] if self.align == "left" else 0.0
        self.hard = [i < n - 1 and self.lines[i].x1 < W * 0.8 for i in range(n)]

    # ---- the block's text, three ways ----
    def styled(self, kind="flow"):
        """[(char, style)]. "search": lines joined by a space (as the find index sees it);
        "flow": the same length, but a hard line break is a newline; "edit": like flow, and a
        line ending in a hyphen joins the next one directly."""
        out = []
        for i, ln in enumerate(self.lines):
            if i:
                prev = out[-1][1] if out else ln.chars[0].style
                hyph = kind == "edit" and self.lines[i - 1].chars[-1].c == "-" and not self.hard[i - 1]
                if not hyph:
                    out.append(("\n" if kind != "search" and self.hard[i - 1] else " ", prev))
            out.extend((c.c, c.style) for c in ln.chars)
        return out

    def text(self, kind="flow"):
        return "".join(c for c, _ in self.styled(kind))

    @property
    def search(self):
        return self.text("search")

    @property
    def fp(self):
        """Fingerprint: finds the same block in a fresh model of the same page."""
        return (tuple(round(v, 1) for v in self.bbox), self.search)

    def chars(self):
        return [c for ln in self.lines for c in ln.chars]


def font_key(name):
    """Loose font-name key: a span may name a font by its PostScript name ("ArialMT",
    "Arial-BoldMT") where the font list says "Arial Regular" / "Arial Bold"."""
    n = re.sub(r"^[A-Z]{6}\+", "", name or "").lower()
    n = re.sub(r"[\s,_-]+", "", n)
    n = re.sub(r"(mt|ps|regular|roman|normal|book)$", "", n)
    n = re.sub(r"(mt|ps)(?=(bold|italic|oblique)?$)", "", n)
    return n


class PageModel:
    """Every text block of one page, with fonts. Read from a one-page plain copy."""

    def __init__(self, doc, pno):
        self.pno = pno
        shadow = pymupdf.open()
        try:
            shadow.insert_pdf(doc, from_page=pno, to_page=pno, annots=False, links=False)
            page = shadow[0]
            self.rotation = page.rotation
            self.rot = page.rotation_matrix
            self.derot = page.derotation_matrix
            self.fonts = {}
            for x, ext, typ, base, ref, enc, *_ in page.get_fonts(full=True):
                name = base.split("+", 1)[1] if re.match(r"^[A-Z]{6}\+", base or "") else (base or "")
                info = {"xref": x, "ext": ext, "type": typ, "base": base, "name": name, "buffer": None,
                        "subset": bool(re.match(r"^[A-Z]{6}\+", base or ""))}
                if ext not in ("n/a", "", None) and typ != "Type3":
                    try:
                        info["buffer"] = shadow.extract_font(x)[3] or None
                    except Exception:
                        info["buffer"] = None
                self.fonts[name] = info
                self.fonts.setdefault("key:" + font_key(name), info)
                if typ == "Type3":
                    self.fonts[f"Type3 ({x} 0 R)"] = info
            raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
        finally:
            shadow.close()
        self.blocks = []
        for b in raw["blocks"]:
            if b.get("type") != 0:
                continue
            lines, problem, dirs = [], None, set()
            for ln in b["lines"]:
                chars = []
                for sp in ln["spans"]:
                    st = (sp["font"], sp["flags"], round(sp["size"], 2), sp["color"])
                    inv = sp.get("alpha", 255) == 0 or sp["font"] in INVISIBLE_FONTS
                    fi = self.fonts.get(sp["font"])
                    if fi is not None and fi["type"] == "Type3" or sp["font"].startswith("Type3"):
                        problem = problem or "uses a Type3 font (its letters are drawn as pictures)"
                    for ch in sp["chars"]:
                        c = "\u0020" if ch["c"] == "\u00a0" else ch["c"]     # a no-break space is a space here
                        o = ord(c) if c else 0
                        if c == "\ufffd" or 0xE000 <= o <= 0xF8FF or (o < 32 and c not in "\t"):
                            problem = problem or "has no usable character encoding"
                        chars.append(Ch(c, R(ch["bbox"]), P(ch["origin"]), st, inv))
                if not chars:
                    continue
                if ln.get("wmode"):
                    problem = problem or "is vertical writing"
                ax = _axis(ln["dir"])
                if ax is None:
                    problem = problem or "is set at an angle"
                    ax = (1, 0)
                dirs.add(ax)
                lines.append(Line(chars))
            if not lines:
                continue
            if len(dirs) > 1:
                problem = problem or "mixes text directions"
            blk = Block(len(self.blocks), b["bbox"], lines, problem)
            blk.frame = Frame(blk.bbox, next(iter(dirs)))
            blk.setup()
            self.blocks.append(blk)

    # ---- queries ----
    def visible_blocks(self):
        return [b for b in self.blocks if not b.invisible]

    @property
    def has_visible_text(self):
        return any(c.c.strip() for b in self.visible_blocks() for c in b.chars())

    @property
    def has_invisible_text(self):
        return any(b.invisible for b in self.blocks)

    def disp_rect(self, blk):
        return (R(blk.bbox) * self.rot).normalize()

    def block_at(self, pt_disp, slack=2.0):
        """The block under a displayed point (visible ones first), or None."""
        p = P(pt_disp) * self.derot
        hit = [b for b in self.blocks if R(b.bbox.x0 - slack, b.bbox.y0 - slack, b.bbox.x1 + slack,
                                           b.bbox.y1 + slack).contains(p)]
        hit.sort(key=lambda b: (b.invisible, b.bbox.get_area()))
        return hit[0] if hit else None

    def find(self, fp):
        bbox, search = fp
        for b in self.blocks:
            if b.search == search and all(abs(a - c) < 1.0 for a, c in zip(b.bbox, bbox)):
                return b
        raise EditFailed("the text changed since it was read (try again)")

    def page_text(self):
        """As the find index reads the page: blocks joined by newlines."""
        return "\n".join(b.search for b in self.blocks)


def model(doc, pno):
    return PageModel(doc, pno)


# ---------------- fonts ----------------
@dataclass
class FontChoice:
    code: str                       # the font name for insert_text
    font: object                    # pymupdf.Font, for measuring
    buffer: bytes = None            # an embedded font program to insert (None: a standard font)
    substitute: bool = False
    label: str = ""
    reason: str = ""


_font_cache = {}


def _font(code=None, buffer=None):
    key = code or hashlib.sha1(buffer).hexdigest()
    if key not in _font_cache:
        _font_cache[key] = pymupdf.Font(code) if code else pymupdf.Font(fontbuffer=buffer)
    return _font_cache[key]


def classify(name, flags):
    """The closest standard font: serif / sans / mono, bold, italic."""
    n = (name or "").lower()
    mono = bool(flags & 8) or any(k in n for k in ("courier", "mono", "consol", "menlo", "typewriter"))
    serif = not mono and "sans" not in n and (bool(flags & 4) or any(
        k in n for k in ("times", "serif", "roman", "georgia", "garamond", "cambria", "minion", "palatino", "caslon",
                         "book antiqua", "baskerville")))
    bold = bool(flags & 16) or any(k in n for k in ("bold", "black", "heavy", "semibold", "demi"))
    italic = bool(flags & 2) or any(k in n for k in ("italic", "oblique"))
    fam = ("cour", "cobo", "coit", "cobi") if mono else ("tiro", "tibo", "tiit", "tibi") if serif else \
        ("helv", "hebo", "heit", "hebi")
    return fam[int(bold) + 2 * int(italic)]


def _covers(f, chars):
    return all(f.has_glyph(ord(c)) for c in chars)


def resolve(pm, style, needed, force_sub=False):
    """The font to write `needed` characters in a style with. Raises NoFont."""
    name, flags = style[0], style[1]
    need = {c for c in needed if c not in "\n"}
    fi = (pm.fonts.get(name) or pm.fonts.get("key:" + font_key(name))) if pm is not None else None
    reason = ""
    if not force_sub:
        if name in BASE14 and _covers(_font(BASE14[name]), need):
            return FontChoice(BASE14[name], _font(BASE14[name]), label=STD_LABEL[BASE14[name]])
        if fi is not None and fi.get("buffer"):
            try:
                f = _font(buffer=fi["buffer"])
                ok = _covers(f, need)
            except Exception:
                f, ok = None, False
            if ok:
                code = "Lz" + hashlib.sha1(fi["buffer"]).hexdigest()[:10]
                return FontChoice(code, f, fi["buffer"], label=name)
            if fi["subset"]:
                reason = "the embedded font is a subset that lacks some of the characters"
            else:
                reason = "the embedded font lacks some of the characters"
        elif fi is not None and fi["type"] == "Type3":
            reason = "the original is a Type3 font (pictures, not letters)"
        elif name not in BASE14:
            reason = "the original font isn't embedded in the file"
        else:
            reason = "the standard font lacks some of the characters"
    else:
        reason = "the original text can't be reused"
    code = classify(name, flags)
    f = _font(code)
    if not _covers(f, need):
        missing = "".join(sorted(c for c in need if not f.has_glyph(ord(c))))[:12]
        raise NoFont(f"no available font has the characters {missing!r}")
    return FontChoice(code, f, substitute=True, label=STD_LABEL[code], reason=reason)


# ---------------- layout ----------------
@dataclass
class Layout:
    runs: list = field(default_factory=list)       # (text, x, y baseline, style, size)
    lines: int = 0
    width_ok: bool = True
    height: float = 0.0
    fits: bool = True
    scale: float = 1.0


def _rgb(c):
    return ((c >> 16) & 255) / 255, ((c >> 8) & 255) / 255, (c & 255) / 255


def layout(blk, styled, choices, scale=1.0, grow=False):
    """Lay styled text out in the block's box: wrap at spaces, keep hard breaks, align as
    the block was aligned, first baseline where the old one was."""
    f = blk.frame
    W, H = f.w, f.h
    # tokens: words (lists of (c, style)), spaces, breaks
    paras, cur, word = [], [], []
    for c, st in styled:
        if c == "\n":
            if word:
                cur.append(("w", word))
                word = []
            paras.append(cur)
            cur = []
        elif c == " " or c == "\t":
            if word:
                cur.append(("w", word))
                word = []
            cur.append(("s", st))
        else:
            word.append((c, st))
    if word:
        cur.append(("w", word))
    paras.append(cur)

    def size(st):
        return st[2] * scale

    def runs_of(word):
        out = []
        for c, st in word:
            if out and out[-1][1] == st:
                out[-1][0] += c
            else:
                out.append([c, st])
        return out

    def wlen(word):
        return sum(choices[st].font.text_length(t, fontsize=size(st)) for t, st in runs_of(word))

    def slen(st):
        return choices[st].font.text_length(" ", fontsize=size(st))

    ind0 = blk.indent * scale if blk.align in ("left", "justify") else 0.0
    lines = []                      # (items [(word, space_before)], width, last_of_para)
    for para in paras:
        line, lw, pend = [], 0.0, 0.0
        for kind, val in para:
            if kind == "s":
                pend += slen(val)
                continue
            ww = wlen(val)
            avail = W - (ind0 if not lines else 0.0)
            if line and lw + pend + ww > avail + FIT_SLACK:
                lines.append((line, lw, False))
                line, lw, pend = [], 0.0, 0.0
            line.append((val, pend if line else 0.0))
            lw += (pend if len(line) > 1 else 0.0) + ww
            pend = 0.0
        lines.append((line, lw, True))
    out = Layout(scale=scale)
    maxsize = max((st[2] for _c, st in styled), default=10.0) * scale
    base0 = blk.lines[0].base * scale if blk.lines else maxsize
    pitch = blk.pitch * scale
    y = base0
    for k, (items, lw, last) in enumerate(lines):
        if k:
            y += pitch
        ind = ind0 if k == 0 else 0.0
        if blk.align == "center":
            x = (W - lw) / 2
        elif blk.align == "right":
            x = W - lw
        else:
            x = ind
        extra = 0.0
        if blk.align == "justify" and not last and len(items) > 1:
            extra = max(0.0, (W - ind - lw) / (len(items) - 1))
        for j, (word, sp) in enumerate(items):
            x += sp + (extra if j else 0.0)
            for t, st in runs_of(word):
                out.runs.append((t, x, y, st, size(st)))
                x += choices[st].font.text_length(t, fontsize=size(st))
        if lw + ind > W + FIT_SLACK:
            out.width_ok = False
    out.lines = len(lines)
    out.height = y + 0.25 * maxsize
    out.fits = out.width_ok and (grow or out.height <= H + FIT_SLACK)
    return out


def fit(blk, styled, choices, overflow=None):
    """A layout that respects the box. overflow: None (raise Overflow if it doesn't fit),
    "shrink" (smaller text, down to MIN_SHRINK) or "grow" (the box grows downward)."""
    lay = layout(blk, styled, choices, 1.0, grow=(overflow == "grow"))
    if lay.fits:
        return lay
    if overflow == "grow" and not lay.width_ok:
        overflow = "shrink"                     # a word wider than the box: only shrinking helps
    if overflow != "shrink":
        raise Overflow(f"the new text needs {lay.lines} line(s) and doesn't fit the box")
    lo, hi = MIN_SHRINK, 1.0
    best = None
    for _ in range(12):
        mid = (lo + hi) / 2
        t = layout(blk, styled, choices, mid)
        if t.fits:
            best, lo = t, mid
        else:
            hi = mid
    if best is None:
        raise Overflow("the new text doesn't fit even at a much smaller size")
    return best


# ---------------- styles for a new text ----------------
def restyle(old, new_text):
    """Carry the old characters' styles over to new_text: unchanged characters keep theirs,
    typed or replaced ones take the style of the character they replace (or the one before)."""
    a = "".join(c for c, _ in old)
    out = []
    default = old[0][1] if old else ("Helvetica", 0, 11.0, 0)
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, new_text, autojunk=False).get_opcodes():
        if op == "equal":
            out.extend((new_text[j1 + k], old[i1 + k][1]) for k in range(i2 - i1))
        elif j2 > j1:
            st = old[i1][1] if i1 < len(old) else (out[-1][1] if out else default)
            out.extend((c, st) for c in new_text[j1:j2])
    return out


def choose_fonts(pm, blk, styled, force_sub=False):
    """{style: FontChoice} for every style the new text uses, plus the notices."""
    need = defaultdict(set)
    for c, st in styled:
        need[st].add(c)
    choices, notes = {}, []
    for st, cs in need.items():
        cs.add(" ")
        ch = resolve(pm, st, cs, force_sub)
        choices[st] = ch
        if ch.substitute:
            n = f"Substitute font: {ch.label} ({ch.reason})"
            if n not in notes:
                notes.append(n)
    return choices, notes


# ---------------- plan (no writing) ----------------
CHOSEN = "chosen"
COVER_NOTICE = ("The old text is covered, not removed: it can still be extracted from the file. "
                "Redact is the tool that removes text.")


def plan(pm, blk, new_styled, overflow=None):
    """What an edit would do: {"method", "notices", "layout"}. Raises NoFont / Overflow /
    EditFailed (invisible text)."""
    if blk.invisible:
        raise EditFailed("that is the invisible text layer (OCR), not visible text")
    cover = blk.problem is not None
    choices, notes = choose_fonts(pm, blk, new_styled, force_sub=cover)
    lay = fit(blk, new_styled, choices, overflow)
    if lay.scale < 0.999:
        notes.append(f"Shrunk to {round(lay.scale * 100)}% to fit the box")
    if lay.height > blk.frame.h + FIT_SLACK:
        notes.append("The box grew downward to fit the text")
    if cover:
        notes.append(COVER_NOTICE if blk.problem == CHOSEN else
                     f"This text {blk.problem}, so it can't be replaced in place. " + COVER_NOTICE)
    return {"method": "cover" if cover else "replace", "notices": notes, "layout": lay, "choices": choices}


# ---------------- writing ----------------
def _band(blk, ch):
    """A redaction area for one glyph: a band around the middle of the letter (in the block's
    own frame), so it touches no glyph of a neighbouring line."""
    f = blk.frame
    x0, _y0, x1, _y1 = f.local_rect(ch.rect)
    base = f.local(ch.origin)[1]
    s = ch.style[2]
    pad = min(0.2, (x1 - x0) / 4)
    return f.page_rect(x0 + pad, base - 0.45 * s, max(x0 + pad + 0.1, x1 - pad), base - 0.15 * s)


def _set_aside_marks(page):
    """Pending redaction marks on the page: removed now, put back by _restore_marks."""
    kept = []
    for a in list(page.annots(types=[pymupdf.PDF_ANNOT_REDACT])):
        kept.append((R(a.rect), (a.colors or {}).get("fill"), dict(a.info or {})))
        page.delete_annot(a)
    return kept


def _restore_marks(page, kept):
    for rect, fill, info in kept:
        a = page.add_redact_annot(rect, fill=tuple(fill) if fill else (0, 0, 0))
        if info:
            a.set_info(info)
        a.update()


def _remove(page, blk):
    kept = _set_aside_marks(page)
    for ch in blk.chars():
        page.add_redact_annot(_band(blk, ch), fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    _restore_marks(page, kept)


def _write(page, blk, lay, choices):
    f = blk.frame
    inserted = set()
    for text, x, y, st, size in lay.runs:
        ch = choices[st]
        if ch.buffer is not None and ch.code not in inserted:
            page.insert_font(fontname=ch.code, fontbuffer=ch.buffer)
            inserted.add(ch.code)
        page.insert_text(f.page(x, y), text, fontname=ch.code, fontsize=size, color=_rgb(st[3]), rotate=f.rotate)


def _norm(s):
    return "".join(s.split())


def _verify(doc, pno, before, blk, new_text, cover):
    """Re-extract: everything else is where it was, and what is new reads exactly new_text
    (so the old characters are gone; when covering, they are expected to remain)."""
    after = PageModel(doc, pno)
    keep = [c for b in before.blocks for c in b.chars() if c.c.strip() and (cover or b is not blk)]
    pool = defaultdict(list)
    for b in after.blocks:
        for c in b.chars():
            if c.c.strip():
                pool[c.c].append(c)
    used = set()
    for c in keep:
        m = next((x for x in pool[c.c] if id(x) not in used and abs(x.origin.x - c.origin.x) < 0.6
                  and abs(x.origin.y - c.origin.y) < 0.6), None)
        if m is None:
            raise EditFailed("other text on the page would have changed")
        used.add(id(m))
    new = "".join(c.c for b in after.blocks for c in b.chars() if c.c.strip() and id(c) not in used)
    if new != _norm(new_text) and sorted(new) != sorted(_norm(new_text)):
        raise EditFailed("the new text doesn't read back as typed" if new else "the new text didn't appear")
    return after


def apply_block(doc, pno, fp, new_text=None, new_styled=None, overflow=None, cover=None):
    """Rewrite one block (found by its fingerprint) as new_text (or new_styled). cover:
    None = cover only if the text can't be replaced cleanly; True = cover. Returns
    {"method", "notices"}. Raises EditFailed (nothing changed when the caller's operation
    rolls back or runs on a copy)."""
    pm = PageModel(doc, pno)
    blk = pm.find(fp)
    if new_styled is None:
        new_styled = restyle(blk.styled("edit"), new_text)
    text = "".join(c for c, _ in new_styled)
    if cover:
        blk.problem = blk.problem or CHOSEN
    p = plan(pm, blk, new_styled, overflow)
    page = doc[pno]
    if p["method"] == "cover":
        from .contentedit import sample_background
        color = sample_background(page, (R(blk.bbox) * page.rotation_matrix).normalize())
        page.draw_rect(R(blk.bbox.x0 - 1, blk.bbox.y0 - 1, blk.bbox.x1 + 1, blk.bbox.y1 + 1), color=None,
                       fill=color, width=0, overlay=True)
    else:
        _remove(page, blk)
    _write(page, blk, p["layout"], p["choices"])
    del page
    _verify(doc, pno, pm, blk, text, p["method"] == "cover")
    return {"method": p["method"], "notices": p["notices"]}


# ---------------- find / replace ----------------
def expand_template(repl, regex):
    """$1 / ${name} / \\1 in a regex replacement -> Python's \\g<1>; plain text otherwise."""
    if not regex:
        return None
    t = re.sub(r"\$\{(\w+)\}", r"\\g<\1>", repl)
    return re.sub(r"\$(\d+)", r"\\g<\1>", t)


def _replacement(m, repl, template):
    return m.expand(template) if template is not None else repl


@dataclass
class Item:
    """One block with hits (or one skipped hit)."""
    page: int
    fp: tuple = None
    hits: int = 0
    before: str = ""
    after: str = ""
    status: str = "ok"                  # ok | skip
    reason: str = ""
    method: str = ""
    notices: list = field(default_factory=list)
    styled: list = None


def _snippet(s, a, b, width=40):
    lo, hi = max(0, a - width // 2), min(len(s), b + width // 2)
    return ("…" if lo else "") + s[lo:hi].replace("\n", " ") + ("…" if hi < len(s) else "")


def plan_replace(doc, pages, pattern, repl, regex=False, overflow="shrink", only=None):
    """Every change Replace All would make, as Items (ok or skipped with the reason).
    only: (page, displayed rect) to restrict to the one hit there (single Replace)."""
    template = expand_template(repl, regex)
    items = []
    for pno in pages:
        pm = PageModel(doc, pno)
        text = pm.page_text()
        starts, pos = [], 0
        for b in pm.blocks:
            starts.append(pos)
            pos += len(b.search) + 1
        for m in pattern.finditer(text):                 # hits across two blocks
            if m.end() <= m.start():
                continue
            k = max(i for i, s in enumerate(starts) if s <= m.start())
            if m.end() > starts[k] + len(pm.blocks[k].search):
                if only is None:
                    items.append(Item(pno, hits=1, before=_snippet(text, m.start(), m.end()), status="skip",
                                      reason="the match runs across two separate text blocks"))
        for blk in pm.blocks:
            ms = [m for m in pattern.finditer(blk.search) if m.end() > m.start()]
            if only is not None and only[0] == pno:
                ms = [m for m in ms if _match_rect(pm, blk, m).intersects(only[1])][:1]
            elif only is not None:
                ms = []
            if not ms:
                continue
            flow = blk.styled("flow")
            new, last = [], 0
            for m in ms:
                new.extend(flow[last:m.start()])
                rep = _replacement(m, repl, template)
                st = flow[m.start()][1]
                new.extend((c, st) for c in rep)
                last = m.end()
            new.extend(flow[last:])
            ntext = "".join(c for c, _ in new)
            it = Item(pno, blk.fp, len(ms), _snippet(blk.search, ms[0].start(), ms[0].end()),
                      _snippet(ntext, ms[0].start(), ms[0].start() + len(_replacement(ms[0], repl, template))),
                      styled=new)
            try:
                p = plan(pm, blk, new, overflow)
                it.method, it.notices = p["method"], p["notices"]
            except EditFailed as e:
                it.status, it.reason = "skip", str(e)
            items.append(it)
    return items


def _match_rect(pm, blk, m):
    """Displayed rect of a match inside a block (search-text offsets -> characters)."""
    idx, k = [], 0
    for i, ln in enumerate(blk.lines):
        if i:
            idx.append(None)
        for c in ln.chars:
            idx.append(c)
    r = R()
    for c in idx[m.start():m.end()]:
        if c is not None:
            r |= c.rect
    return (r * pm.rot).normalize()


def apply_replace(doc, items):
    """Write every ok item. Raises ItemFailed (with the item) on the first that fails."""
    done = 0
    for it in items:
        if it.status != "ok":
            continue
        try:
            apply_block(doc, it.page, it.fp, new_styled=it.styled, overflow="shrink")
        except EditFailed as e:
            raise ItemFailed(it, str(e))
        done += it.hits
    return done


class ItemFailed(EditFailed):
    def __init__(self, item, reason):
        super().__init__(reason)
        self.item, self.reason = item, reason


def replace_all(doc, pages, pattern, repl, regex=False, apply_fn=None):
    """Plan + apply on a document where a failure can't be rolled back (batch): each attempt
    runs on a copy; failing items are skipped and the rest re-tried; the final set is then
    applied to doc. Returns (replaced count, items)."""
    items = plan_replace(doc, pages, pattern, repl, regex)
    for _ in range(len(items) + 1):
        trial = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        try:
            apply_replace(trial, items)
        except ItemFailed as e:
            e.item.status, e.item.reason = "skip", e.reason
            continue
        finally:
            trial.close()
        break
    return apply_replace(doc, items), items
