"""Text geometry for selection on the PDF Editor canvas.

Characters come from PyMuPDF rawdict (block > line > span > char), in reading order.
Extraction runs on a one-page copy (insert_pdf only reads the working document).
PyMuPDF extracts text from a rotated page by briefly setting /Rotate to 0, which a
journalled document refuses outside an undo operation.

Extracted boxes are in unrotated page space. Multiplying by the page's rotation_matrix
gives the displayed (rotated) frame, which matches the canvas: scene units are PDF points
of page.rect. derotation_matrix maps back, for annotations."""
import pymupdf


class PageText:
    def __init__(self, doc, index):
        shadow = pymupdf.open()
        try:
            # text only: copying annotations/links touches the source, which a journalled
            # document refuses outside an operation
            shadow.insert_pdf(doc, from_page=index, to_page=index, annots=False, links=False)
            page = shadow[0]
            m = page.rotation_matrix
            self.derotation = page.derotation_matrix
            raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
        finally:
            shadow.close()
        self.chars = []         # (rect in displayed page space, char, line key)
        for bi, b in enumerate(raw["blocks"]):
            if b.get("type") != 0:
                continue
            for li, line in enumerate(b["lines"]):
                for span in line["spans"]:
                    for ch in span["chars"]:
                        r = pymupdf.Rect(ch["bbox"]) * m
                        if r.is_empty and not ch["c"].strip():
                            continue
                        self.chars.append((r, ch["c"], (bi, li)))

    def nearest(self, pt):
        """Index of the character nearest to pt (displayed page space), or None."""
        best, best_d = None, None
        for i, (r, _, _) in enumerate(self.chars):
            dx = max(r.x0 - pt.x, 0, pt.x - r.x1)
            dy = max(r.y0 - pt.y, 0, pt.y - r.y1)
            d = dx * dx + dy * dy * 4          # favour the line the pointer is on
            if best_d is None or d < best_d:
                best, best_d = i, d
                if d == 0:
                    break
        return best

    def span(self, a, b):
        return (a, b) if a <= b else (b, a)

    def segments(self, a, b):
        """One rectangle per line covered by chars a..b (displayed page space)."""
        a, b = self.span(a, b)
        out, cur, key = [], None, None
        for r, _, k in self.chars[a:b + 1]:
            if k != key:
                if cur is not None:
                    out.append(cur)
                cur, key = pymupdf.Rect(r), k
            else:
                cur |= r
        if cur is not None:
            out.append(cur)
        return out

    def text(self, a, b):
        a, b = self.span(a, b)
        parts, key = [], None
        for _, c, k in self.chars[a:b + 1]:
            if key is not None and k != key:
                parts.append("\n" if k[0] != key[0] else " ")
                if parts[-1] == " " and parts[-2:-1] == [" "]:
                    parts.pop()
            parts.append(c)
            key = k
        return "".join(parts).replace("  \n", "\n")

    def quads(self, a, b):
        """Selection as quads in unrotated page space, ready for annotations."""
        return [(r * self.derotation).quad for r in self.segments(a, b)]
