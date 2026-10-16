"""Stage 16: PDF to Word (.docx) with pdf2docx (MIT). The engine (no Qt); it runs in a worker
PROCESS (todocx_ui, batch): pdf2docx is CPU-bound Python on PyMuPDF, which holds the GIL.

The input is the document as it is now (the working copy's bytes, unsaved edits included); it
is converted on a TEMPORARY in-memory copy, so the working copy is never touched. "OCR first"
gives pages without real text an invisible text layer on that temporary copy only. "Without
images" removes the images from the temporary copy before converting.

The .docx is written atomically: a hidden partial file next to the destination, renamed into
place when complete, so a Cancel or a crash leaves nothing behind. A .docx has no PDF password:
the UI warns about that before exporting from a protected PDF.

Good enough by design: paragraphs, fonts where available, tables, images and basic layout come
through; complex layouts may not convert perfectly."""
import logging
import os
import re
from pathlib import Path

import pymupdf

DEFAULTS = {"range": "all", "images": True, "tables": True, "columns": True, "after": "folder"}
PARTIAL = ".lazarus-partial"


class Cancelled(BaseException):
    """Raised from inside pdf2docx's own page loop (a BaseException, so its per-page error
    handling doesn't swallow it)."""


def no_text_pages(doc, pages=None):
    """Pages without real text (scans without OCR)."""
    from .stage10 import has_real_text
    out = []
    for i in (range(doc.page_count) if pages is None else pages):
        pg = doc[i]
        if not has_real_text(pg):
            out.append(i)
        del pg
    return out


def _strip_images(doc, pages):
    """Remove the images from these pages (text and drawings stay): a temporary copy only."""
    for i in pages:
        pg = doc[i]
        rects = [r for img in pg.get_images(full=True) for r in pg.get_image_rects(img[0])]
        if rects:
            for r in rects:
                pg.add_redact_annot(r, fill=False, cross_out=False)
            pg.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_REMOVE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                text=pymupdf.PDF_REDACT_TEXT_NONE)
        del pg


def _readable(layer_pdf):
    """Tesseract's text-only page draws its text with render mode 3 (hidden), which pdf2docx
    skips. Render mode 0 here: the glyph-less font still draws nothing, but the text counts."""
    d = pymupdf.open("pdf", layer_pdf)
    try:
        for x in d[0].get_contents():
            s = d.xref_stream(x)
            d.update_stream(x, re.sub(rb"(^|\s)3 Tr", rb"\g<1>0 Tr", s))
        return d.tobytes()
    finally:
        d.close()


class _Progress(logging.Handler):
    """pdf2docx logs "(i/n) Page p" twice per page (parse, then write): that is the progress."""
    RX = re.compile(r"\((\d+)/(\d+)\) Page")

    def __init__(self, report, stopped):
        super().__init__(logging.INFO)
        self.report, self.stopped, self.phase = report, stopped, 0
        self.last = None

    def emit(self, record):
        if self.stopped and self.stopped():
            raise Cancelled()
        msg = record.getMessage()
        if "[4/4]" in msg:
            self.phase = 1
        m = self.RX.search(msg)
        if m and self.report:
            i, n = int(m.group(1)), int(m.group(2))
            self.report(self.phase * n + i, 2 * n)


def convert(data, dest, pages, opts=None, ocr_pages=(), lang="eng", progress=None, stopped=None):
    """Convert PDF bytes to dest (.docx). pages: 0-based pages to convert; ocr_pages: pages to
    OCR first (on the temporary copy). Returns the number of pages converted."""
    from pdf2docx import Converter
    from . import protect
    o = dict(DEFAULTS)
    o.update(opts or {})
    dest = Path(dest)
    protect.check(dest)
    tmp_doc = pymupdf.open(stream=data, filetype="pdf")           # the temporary copy
    try:
        if ocr_pages:
            from . import stage10 as T
            from . import tess
            tess.configure()
            layers = {}
            for k, p in enumerate(ocr_pages):
                if stopped and stopped():
                    raise Cancelled()
                if progress:
                    progress(-1 - k, -len(ocr_pages))              # negative: "OCR page k of n"
                layers[p] = _readable(T.ocr_page_pdf(tmp_doc[p], lang))
            # on the temporary copy only: an OCR'd page becomes its text (the scan picture would
            # come through again beside it), and pdf2docx reads text that isn't hidden
            _strip_images(tmp_doc, list(layers))
            T.add_ocr_layers(tmp_doc, layers)
        if not o.get("images", True):
            _strip_images(tmp_doc, pages)
        work = tmp_doc.tobytes(garbage=1)
    finally:
        tmp_doc.close()
    settings = {"parse_lattice_table": bool(o.get("tables", True)), "parse_stream_table": bool(o.get("tables", True)),
                "multi_processing": False, "ignore_page_error": True}
    if not o.get("columns", True):
        settings["min_section_height"] = 1e9            # no sections: one column
    tmp = dest.with_name(f".{dest.stem}{PARTIAL}{dest.suffix}")
    log = logging.getLogger()
    handler = _Progress(progress, stopped)
    old_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    cv = Converter(stream=work)
    try:
        cv.convert(str(tmp), pages=list(pages), **settings)
        cv.close()
        protect.check(dest)
        os.replace(tmp, dest)
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)
        try:
            cv.close()
        except Exception:
            pass
        if tmp.exists():
            tmp.unlink()
    return len(pages)


def worker_main(spec, conn, cancel):
    """{"data", "dest", "pages", "options", "ocr_pages", "lang", "test_delay"} -> events
    ("progress", done, total) ... then ("done", pages) / ("cancelled",) / ("failed", text)."""
    import time as _t
    delay = float(spec.get("test_delay", 0) or 0)

    def progress(done, total):
        if delay:
            _t.sleep(delay)
        conn.send(("progress", done, total))
    try:
        n = convert(spec["data"], spec["dest"], spec["pages"], spec.get("options"), spec.get("ocr_pages", ()),
                    spec.get("lang", "eng"), progress, cancel.is_set)
        conn.send(("done", n))
    except Cancelled:
        conn.send(("cancelled",))
    except Exception as e:                    # shown on screen only, never with the file's name
        conn.send(("failed", f"{type(e).__name__}: {e}"))
