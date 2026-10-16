"""Text search for the PDF Editor / Overview. Read-only.

Text comes from ONE plain in-memory snapshot of the working copy (doc.tobytes() reopened),
never from the journalled document itself. PyMuPDF extracts text from rotated or annotated
pages by briefly changing them, which a journalled document would count as an edit.
The snapshot is rebuilt only when the document changes (doc_version); pages are indexed
lazily and cached.

Matching is done here rather than with page.search_for(), which is always
case-insensitive and has no whole-word option. Geometry comes from PyMuPDF's
per-character boxes (rawdict), mapped into the displayed (rotated) page frame like text
selection. Spaces in a query match any whitespace, so phrases match across line breaks."""
import re

import pymupdf


class SearchPage:
    __slots__ = ("text", "pos", "boxes", "lines", "m")

    def __init__(self, page):
        self.m = page.rotation_matrix
        raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
        text, pos, boxes, lines = [], [], [], []
        line_id = 0
        first_block = True
        for b in raw["blocks"]:
            if b.get("type") != 0:
                continue
            if not first_block:
                text.append("\n")
                pos.append(-1)
            first_block = False
            for li, line in enumerate(b["lines"]):
                if li:
                    text.append(" ")              # line break inside a block reads as a space
                    pos.append(-1)
                line_id += 1
                for span in line["spans"]:
                    for ch in span["chars"]:
                        text.append(ch["c"])
                        pos.append(len(boxes))
                        boxes.append(ch["bbox"])
                        lines.append(line_id)
        self.text = "".join(text)
        self.pos = pos
        self.boxes = boxes
        self.lines = lines

    def char_range(self, start, end):
        """String span -> (first char, last char), skipping separators."""
        a = next((self.pos[i] for i in range(start, end) if self.pos[i] >= 0), None)
        b = next((self.pos[i] for i in range(end - 1, start - 1, -1) if self.pos[i] >= 0), None)
        return a, b

    def rects(self, a, b):
        """One rectangle per line for chars a..b, in the displayed page frame."""
        out, cur, cur_line = [], None, None
        for k in range(a, b + 1):
            r = pymupdf.Rect(self.boxes[k])
            if self.lines[k] != cur_line:
                if cur is not None:
                    out.append(cur * self.m)
                cur, cur_line = r, self.lines[k]
            else:
                cur |= r
        if cur is not None:
            out.append(cur * self.m)
        return out


def compile_query(query, match_case=False, whole_word=False, regex=False):
    """Returns a compiled pattern, or raises re.error for a bad regex."""
    if regex:
        pat = query
    else:
        tokens = query.split()
        if not tokens:
            return None
        pat = r"\s+".join(re.escape(t) for t in tokens)
    if whole_word:
        pat = r"(?<!\w)(?:" + pat + r")(?!\w)"
    return re.compile(pat, 0 if match_case else re.IGNORECASE)


class SearchIndex:
    def __init__(self, doc, data=None):
        """data: the document's bytes when they already exist (after undoing a snapshot step,
        WorkingDoc.known_bytes()), so nothing is serialized again."""
        self.reused = data is not None
        if data is None:
            data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
        self.snapshot = pymupdf.open("pdf", data)    # plain copy: reading it never edits anything
        self.pages = [None] * self.snapshot.page_count

    @property
    def page_count(self):
        return len(self.pages)

    def indexed(self):
        """Pages already indexed. Searches scan in page order, so these are pages 1..indexed()."""
        return sum(1 for p in self.pages if p is not None)

    def complete(self):
        return all(p is not None for p in self.pages)

    def page(self, i):
        if self.pages[i] is None:
            self.pages[i] = SearchPage(self.snapshot[i])
        return self.pages[i]

    def find(self, i, pattern):
        """Hits on page i: list of (first char, last char)."""
        sp = self.page(i)
        out = []
        for mt in pattern.finditer(sp.text):
            if mt.end() == mt.start():
                continue
            a, b = sp.char_range(mt.start(), mt.end())
            if a is not None and b is not None:
                out.append((a, b))
        return out

    def close(self):
        self.snapshot.close()
