"""Stage 18b: File › Export from the PDF Editor: PDF, EPUB, Markdown, HTML and plain text, made
from the document as it is now (the working copy's bytes, unsaved edits included). The engine
(no Qt); it runs in a worker PROCESS (docexport_ui), like the Word export (stage 16).

Every output is written atomically: a hidden partial file next to the destination, renamed into
place when complete, so Cancel, a failure or a crash leaves nothing behind. The UI checks the
destination against protect.py (never an open original or protected file) before starting.

What each format is, and its limits (best effort, by design):
- Text: PyMuPDF's text per page in reading order (sort=True: top to bottom, left to right by
  block; multi-column pages may interleave), optional page markers.
- HTML: PyMuPDF's per-page HTML (positioned text and images, close to the page's look) in ONE
  self-contained file: images are inlined as data URIs (no sibling folder).
- Markdown: headings from the stage 7a heading detection (font size / weight ranks, running
  headers ignored), paragraphs from text blocks (hyphenated line ends joined), bullet and
  numbered lists from their markers. No tables, no emphasis inside paragraphs, images left out.
- PDF: the working copy through workdoc.write_pdf, the same writer as Save / Save As, with the
  same password choice.
- EPUB 3 (reflowable, EbookLib): chapters from the document's bookmarks (top level), else from
  the stage 7a heading detection, else every 10 pages; headings, paragraphs and lists as in
  Markdown; embedded images carried; a page with no text at all (a scan without OCR) is carried
  as a picture of the page so nothing is lost. Chapters start at the top of a page (a chapter
  that starts mid-page includes that whole page). Multi-column layouts and tables flow as text."""
import html
import os
import re
import uuid
from pathlib import Path

import pymupdf

FORMATS = {  # key: (label, extension, file-dialog filter)
    "pdf": ("PDF", ".pdf", "PDF files (*.pdf)"),
    "epub": ("EPUB", ".epub", "EPUB e-books (*.epub)"),
    "md": ("Markdown", ".md", "Markdown (*.md)"),
    "html": ("HTML", ".html", "HTML (*.html)"),
    "txt": ("Plain text", ".txt", "Text (*.txt)"),
}
TEXT_FORMATS = ("epub", "md", "html", "txt")         # carry the text: blocked when copying is forbidden
PARTIAL = ".lazaruspdf-partial"
MARGIN = 0.1                                         # running headers / footers live in the top / bottom 10%
CHAPTER_PAGES = 10                                   # EPUB chapters when there is no outline / heading
SCAN_DPI = 110                                       # a text-less page carried into EPUB as a picture

BULLET = re.compile(r"^\s*([•◦▪‣·●○■□–—\-*])\s+")
NUMBER = re.compile(r"^\s*(\d{1,3}|[a-zA-Z])[.)]\s+")
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_MD_ESC = re.compile(r"([\\`*_\[\]<>|])")
_MD_LEAD = re.compile(r"^(\s*)([#>+\-=]|\d+[.)])")


class Cancelled(Exception):
    pass


def _xml(s):
    return html.escape(_XML_BAD.sub("", s))


def _md(s):
    s = _MD_ESC.sub(r"\\\1", s)
    return _MD_LEAD.sub(lambda m: m.group(1) + "\\" + m.group(2), s)


def _title(doc, fallback):
    t = ((doc.metadata or {}).get("title") or "").strip()
    return t or fallback


# ---------------- structure: headings, paragraphs, lists, images ----------------
def headings(doc):
    """{page: {text: level}} from the stage 7a heading detection (empty for a doc without text)."""
    from . import autobookmarks as AB
    an = AB.analyze(doc)
    out = {}
    if not an.has_text:
        return out, an
    for lvl, text, page, *_ in AB.from_headings(an, levels=3):
        out.setdefault(page - 1, {})[" ".join(text.split())] = min(int(lvl), 3)
    return out, an


def _join_lines(lines):
    """Lines of one block as running text: a hyphen at a line end joins the word."""
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if out.endswith("-") and ln[:1].islower():
            out = out[:-1] + ln
        else:
            out = (out + " " + ln) if out else ln
    return out


def page_items(doc, pno, heads, running=()):
    """The page as ("h", level, text) / ("p", text) / ("ul" | "ol", [items]) / ("img", bytes, ext)
    in reading order."""
    pg = doc[pno]
    d = pg.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_PRESERVE_IMAGES, sort=True)
    page_heads = heads.get(pno, {})
    h = pg.rect.height or 1
    items = []
    for b in d.get("blocks", []):
        if b.get("type") == 1:
            img = b.get("image")
            if img and b.get("width", 0) >= 8 and b.get("height", 0) >= 8:
                items.append(("img", img, b.get("ext", "png")))
            continue
        lines = []
        for ln in b.get("lines", []):
            t = "".join(s.get("text", "") for s in ln.get("spans", []))
            if t.strip():
                lines.append(" ".join(t.split()))
        if not lines:
            continue
        whole = " ".join(lines)
        y0, y1 = b.get("bbox", (0, 0, 0, h))[1], b.get("bbox", (0, 0, 0, h))[3]
        in_margin = y1 <= h * MARGIN or y0 >= h * (1 - MARGIN)
        if in_margin and re.sub(r"\d+", "#", whole) in running:
            continue                     # a running header / footer (only in the top / bottom margin)
        if whole in page_heads:
            items.append(("h", page_heads[whole], whole))
            continue
        if len(lines) == 1 and lines[0] in page_heads:
            items.append(("h", page_heads[lines[0]], lines[0]))
            continue
        kind = "ul" if BULLET.match(lines[0]) else "ol" if NUMBER.match(lines[0]) else None
        if kind:
            pat = BULLET if kind == "ul" else NUMBER
            entries = []
            for ln in lines:
                if pat.match(ln):
                    entries.append([pat.sub("", ln, count=1)])
                elif entries:
                    entries[-1].append(ln)
            items.append((kind, [_join_lines(e) for e in entries]))
            continue
        items.append(("p", _join_lines(lines)))
    return items


# ---------------- the formats ----------------
def _text(doc, pages, opts, step):
    out = []
    marks = opts.get("page_marks", True)
    for k, p in enumerate(pages):
        step(k)
        if marks:
            out.append(f"[Page {p + 1}]\n")
        out.append(doc[p].get_text("text", sort=True).rstrip() + "\n")
        out.append("\f\n" if marks and k < len(pages) - 1 else "\n")
    return "".join(out).encode("utf-8")


_HTML_CSS = """body{background:#e9e9e9;margin:0;padding:16px;font-family:sans-serif}
.lz-page{background:#fff;margin:0 auto 16px;box-shadow:0 1px 4px rgba(0,0,0,.25);position:relative;overflow:hidden}
.lz-label{font:12px sans-serif;color:#666;text-align:center;margin:0 0 4px}"""


def _html(doc, pages, opts, step, title):
    parts = [f'<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">'
             f'<meta name="viewport" content="width=device-width,initial-scale=1">'
             f"<title>{_xml(title)}</title><style>{_HTML_CSS}</style></head><body>\n"]
    for k, p in enumerate(pages):
        step(k)
        pg = doc[p]
        parts.append(f'<p class="lz-label">Page {p + 1}</p>'
                     f'<div class="lz-page" id="page-{p + 1}" style="width:{pg.rect.width:.0f}pt;'
                     f'height:{pg.rect.height:.0f}pt">\n')
        parts.append(pg.get_text("html"))                     # images come inlined as data URIs
        parts.append("</div>\n")
    parts.append("</body></html>\n")
    return "".join(parts).encode("utf-8")


def _markdown(doc, pages, opts, step, title):
    heads, an = headings(doc)
    out = []
    for k, p in enumerate(pages):
        step(k)
        for it in page_items(doc, p, heads, an.running if an.has_text else ()):
            if it[0] == "h":
                out += ["#" * it[1] + " " + _md(it[2]), ""]
            elif it[0] == "p":
                out += [_md(it[1]), ""]
            elif it[0] in ("ul", "ol"):
                out += [("- " if it[0] == "ul" else f"{n}. ") + _md(e) for n, e in enumerate(it[1], 1)]
                out.append("")
    return ("\n".join(out).rstrip() + "\n").encode("utf-8")


_EPUB_CSS = """body{font-family:serif;line-height:1.45;margin:0 4%}
h1,h2,h3{font-family:sans-serif;line-height:1.2}
p{margin:0 0 .8em;text-indent:0}
img{max-width:100%;height:auto}
figure{margin:1em 0;text-align:center}
.scan{max-width:100%}"""


def chapters(doc, pages, heads):
    """[(title, [pages])] for the EPUB: top-level bookmarks, else level-1 detected headings,
    else every CHAPTER_PAGES pages."""
    from . import outline as O
    sel = set(pages)
    starts = []
    for e in O.read(doc):
        if e[0] == 1 and e[2] and e[2] > 0 and (e[2] - 1) in sel:
            starts.append((e[2] - 1, e[1]))
    if not starts:
        for p in sorted(heads):
            if p in sel:
                for text, lvl in heads[p].items():
                    if lvl == 1:
                        starts.append((p, text))
                        break
    if not starts:
        return [((f"Pages {pages[i] + 1} to {pages[min(i + CHAPTER_PAGES, len(pages)) - 1] + 1}"
                  if min(i + CHAPTER_PAGES, len(pages)) - 1 > i else f"Page {pages[i] + 1}"),
                 pages[i:i + CHAPTER_PAGES]) for i in range(0, len(pages), CHAPTER_PAGES)]
    seen, merged = set(), []
    for p, t in sorted(starts, key=lambda x: x[0]):
        if p not in seen:
            seen.add(p)
            merged.append((p, t))
    out = []
    if pages[0] < merged[0][0]:
        out.append(("Opening pages", [p for p in pages if p < merged[0][0]]))
    for i, (p, t) in enumerate(merged):
        end = merged[i + 1][0] if i + 1 < len(merged) else None
        out.append((t, [q for q in pages if q >= p and (end is None or q < end)]))
    return [(t, ps) for t, ps in out if ps]


def _epub(doc, pages, opts, step, title, dest):
    from ebooklib import epub
    heads, an = headings(doc)
    running = an.running if an.has_text else ()
    book = epub.EpubBook()
    book.set_identifier(f"urn:uuid:{uuid.uuid4()}")
    book.set_title(title)
    book.set_language(opts.get("lang", "en"))
    author = ((doc.metadata or {}).get("author") or "").strip()
    if author:
        book.add_author(author)
    css = epub.EpubItem(uid="style", file_name="style/main.css", media_type="text/css", content=_EPUB_CSS.encode())
    book.add_item(css)
    secs, n_img, k = [], 0, 0
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif"}
    for ci, (ctitle, cpages) in enumerate(chapters(doc, pages, heads)):
        body = [f"<h1>{_xml(ctitle)}</h1>"]
        for p in cpages:
            step(k)
            k += 1
            items = page_items(doc, p, heads, running)
            if not any(it[0] != "img" for it in items) and not doc[p].get_text().strip():
                # no text at all (a scan without OCR): the page as a picture
                pix = doc[p].get_pixmap(dpi=SCAN_DPI)
                n_img += 1
                name = f"images/page{p + 1}_{n_img}.jpg"
                book.add_item(epub.EpubImage(uid=f"img{n_img}", file_name=name, media_type="image/jpeg",
                                             content=pix.tobytes("jpg", jpg_quality=80)))
                body.append(f'<figure><img class="scan" src="{name}" alt="Page {p + 1}"/></figure>')
                continue
            for it in items:
                if it[0] == "h":
                    if it[2] == ctitle:
                        continue                                  # already the chapter title
                    lvl = min(3, it[1] + 1)
                    body.append(f"<h{lvl}>{_xml(it[2])}</h{lvl}>")
                elif it[0] == "p":
                    body.append(f"<p>{_xml(it[1])}</p>")
                elif it[0] in ("ul", "ol"):
                    body.append(f"<{it[0]}>" + "".join(f"<li>{_xml(e)}</li>" for e in it[1]) + f"</{it[0]}>")
                elif it[0] == "img":
                    ext = (it[2] or "png").lower()
                    data = it[1]
                    if ext not in mime:                           # e.g. jpx, jbig2: re-encode as PNG
                        try:
                            data, ext = pymupdf.Pixmap(data).tobytes("png"), "png"
                        except Exception:
                            continue
                    n_img += 1
                    name = f"images/img{n_img}.{ext}"
                    book.add_item(epub.EpubImage(uid=f"img{n_img}", file_name=name, media_type=mime[ext], content=data))
                    body.append(f'<figure><img src="{name}" alt="Image, page {p + 1}"/></figure>')
        sec = epub.EpubHtml(title=ctitle, file_name=f"chapter{ci + 1:03d}.xhtml", lang=opts.get("lang", "en"))
        sec.content = "".join(body)
        sec.add_item(css)
        book.add_item(sec)
        secs.append(sec)
    book.toc = secs
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + secs
    epub.write_epub(str(dest), book)


# ---------------- run (in the worker) ----------------
def run(spec, progress=None, stopped=None):
    """spec: {"fmt", "data", "dest", "pages" (0-based), "options", "title", "save_kwargs" (pdf)}.
    Writes dest atomically; returns the number of pages exported."""
    fmt, dest = spec["fmt"], Path(spec["dest"])
    pages = list(spec["pages"])
    opts = spec.get("options") or {}
    total = len(pages)

    def step(k):
        if stopped is not None and stopped():
            raise Cancelled()
        if progress is not None:
            progress(k, total)

    if fmt == "pdf":
        from .workdoc import write_pdf
        step(0)
        try:
            write_pdf(spec["data"], dest, spec.get("save_kwargs") or {"encryption": pymupdf.PDF_ENCRYPT_NONE},
                      pages, stopped)
        except InterruptedError:
            raise Cancelled()
        if progress is not None:
            progress(total, total)
        return total
    tmp = dest.with_name(f".{dest.stem}{PARTIAL}{dest.suffix}")
    doc = pymupdf.open(stream=spec["data"], filetype="pdf")
    try:
        title = _title(doc, spec.get("title") or "Document")
        if fmt == "txt":
            tmp.write_bytes(_text(doc, pages, opts, step))
        elif fmt == "html":
            tmp.write_bytes(_html(doc, pages, opts, step, title))
        elif fmt == "md":
            tmp.write_bytes(_markdown(doc, pages, opts, step, title))
        elif fmt == "epub":
            _epub(doc, pages, opts, step, title, tmp)
        else:
            raise ValueError(fmt)
        step(total)                                   # a last Cancel check before anything appears
        os.replace(tmp, dest)
        return total
    finally:
        doc.close()
        if tmp.exists():
            tmp.unlink()


def worker_main(spec, conn, cancel):
    """Events: ("progress", done, total) ... then ("done", pages) / ("cancelled",) / ("failed", text)."""
    import time as _t
    delay = float(spec.get("test_delay", 0) or 0)

    def progress(done, total):
        if delay:
            _t.sleep(delay)
        conn.send(("progress", done, total))
    try:
        conn.send(("done", run(spec, progress, cancel.is_set)))
    except Cancelled:
        conn.send(("cancelled",))
    except Exception as e:                            # shown on screen only, never with the file's name
        conn.send(("failed", f"{type(e).__name__}: {e}"))


def data_uri_count(text):
    """How many inlined images an exported HTML file has (the self-test)."""
    return len(re.findall(r"src=\"data:image/", text))
