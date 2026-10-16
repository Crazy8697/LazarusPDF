"""Page operations on the PDF Editor's working copy.

Every op that changes the document is ONE journal operation, so Ctrl+Z / Ctrl+Y undo and
redo it whole. Extract and Split only write new files, from an in-memory snapshot, and
leave the working copy alone. Nothing here writes to an original: every new file goes
through protect.check(). PDFs used for Insert/Merge are opened read-only, registered as
protected, and never written.

PyMuPDF 1.28's new_page() / insert_pdf() refuse to run on a journalled document, so blank
pages and pages from other PDFs are added with the MuPDF calls those functions wrap
(pdf_add_page + pdf_insert_page, pdf_graft_mapped_page). These are journal-safe inside an
operation.

Bookmarks: destinations point at page objects, so move / insert / duplicate keep every
bookmark on its page by themselves (rotate and crop don't touch them). Delete drops
bookmarks to deleted pages (their children take their place) in the same undo step.
Merge brings each merged PDF's own bookmarks in, nested under a parent named after that
file. Extract and Split give the new files the bookmarks for the pages they contain,
renumbered."""
import os
import tempfile
from pathlib import Path

import pymupdf
from pymupdf import mupdf

from . import locked
from . import outline as O
from . import protect

STRUCTURAL = "structural"      # WorkingDoc op marker: page count / order / geometry changed


# ---------------- low-level helpers ----------------
def _insert_blank(doc, at, width, height):
    pdf = pymupdf._as_pdf_document(doc)
    res = mupdf.pdf_add_new_dict(pdf, 1)
    obj = mupdf.pdf_add_page(pdf, mupdf.FzRect(0, 0, width, height), 0, res, mupdf.FzBuffer())
    mupdf.pdf_insert_page(pdf, at, obj)


def _graft(doc, src, pages, at):
    """Deep-copy pages of src (an ordinary, non-journalled document) into doc before index at."""
    dst = pymupdf._as_pdf_document(doc)
    s = pymupdf._as_pdf_document(src)
    gmap = mupdf.pdf_new_graft_map(dst)
    for k, i in enumerate(pages):
        mupdf.pdf_graft_mapped_page(gmap, at + k, s, i)


def write_new_pdf(doc, dest, lock=None):
    """Atomic write of a (non-journalled) document to a NEW file; never an original.
    lock: the source's protection (locked.Lock), applied to the new file."""
    dest = Path(dest)
    protect.check(dest)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(dest.parent))
    os.close(fd)
    try:
        enc = lock.save_kwargs() if lock else {"encryption": pymupdf.PDF_ENCRYPT_NONE}
        doc.save(tmp, garbage=3, deflate=True, **enc)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def snapshot(work):
    """A plain in-memory copy of the working document (journalled docs can't be read
    page-by-page with insert_pdf)."""
    return pymupdf.open("pdf", work.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))


def open_reference(path, ask=None):
    """Open another PDF for Insert/Merge: read-only use, registered as protected.
    ask(attempt) -> password or None, for a password-protected one (None: PasswordCancelled)."""
    protect.register(f"ref:{Path(path).resolve()}", path)
    d, _ = locked.open_pdf(path, ask)
    if not d.is_pdf:
        d.close()
        raise RuntimeError("That file isn't a PDF.")
    return d


# ---------------- operations (return the pages to select afterwards) ----------------
def _structural(work, name, fn):
    out = work.edit(name, fn, pages=STRUCTURAL)
    work.doc._reset_page_refs()
    return out


def move(work, pages, before):
    """Move pages (any set) so they sit, in order, before original index `before` (n = end)."""
    n = work.doc.page_count
    sel = sorted(set(pages))
    rest_before = [p for p in range(n) if p not in sel and p < before]
    rest_after = [p for p in range(n) if p not in sel and p >= before]
    order = rest_before + sel + rest_after
    if order == list(range(n)):
        return sel
    _structural(work, "Reorder pages", lambda d: d.select(order))
    start = len(rest_before)
    return list(range(start, start + len(sel)))


def rotate(work, pages, degrees):
    def fn(d):
        for p in pages:
            pg = d[p]
            pg.set_rotation((pg.rotation + degrees) % 360)
    _structural(work, f"Rotate {degrees}°", fn)
    return sorted(pages)


def delete(work, pages):
    sel = sorted(set(pages))
    n = work.doc.page_count
    if len(sel) >= n:
        raise ValueError("Can't delete every page; a PDF needs at least one.")
    toc0 = O.read(work.doc)
    gone = set(sel)
    mapping, k = {}, 0
    for p in range(n):
        if p in gone:
            mapping[p] = None
        else:
            mapping[p] = k
            k += 1

    def fn(d):
        d.delete_pages(sel)
        if toc0:                                   # no dangling bookmarks to deleted pages
            O.write(d, O.remap(toc0, mapping))
    _structural(work, "Delete pages", fn)
    return [min(sel[0], work.doc.page_count - 1)]


def insert_blank(work, at, like_page):
    r = work.doc[like_page].rect          # displayed size of the neighbouring page
    _structural(work, "Insert blank page", lambda d: _insert_blank(d, at, r.width, r.height))
    return [at]


def _structural_steps(work, name, steps, between=None):
    """_structural for a big change made in parts (WorkingDoc.edit_steps): one undo step."""
    out = work.edit_steps(name, steps, pages=STRUCTURAL, between=between)
    work.doc._reset_page_refs()
    return out


def insert_from(work, src_doc, at, pages=None, between=None):
    """Annotations, links and form fields come along (17); a big source goes in 50-page journal
    operations (one undo step), with between(progress) after each part."""
    pages = list(range(src_doc.page_count)) if pages is None else list(pages)
    from .pageclip import graft_steps
    _structural_steps(work, "Insert pages", lambda d: graft_steps(d, src_doc, pages, at, private=True), between)
    return list(range(at, at + len(pages)))


def merge(work, src_docs, at, names=None, between=None):
    """Graft every page of each PDF in at `at`. Each PDF's own bookmarks come along,
    nested under a new top-level bookmark named after that file (names[i])."""
    total = sum(s.page_count for s in src_docs)
    names = list(names or [f"Merged PDF {i + 1}" for i in range(len(src_docs))])
    src_tocs = [O.read(s) for s in src_docs]

    def fn(d):
        pos = at
        from .pageclip import graft_steps
        for s in src_docs:
            yield from graft_steps(d, s, range(s.page_count), pos, private=True)
            pos += s.page_count
        toc = O.read(d)                            # existing bookmarks already follow their pages
        block, pos = [], at
        for s, name, stoc in zip(src_docs, names, src_tocs):
            block.append([1, name, pos + 1, {}])
            for e in stoc:
                dest = {k: v for k, v in (e[3] if len(e) > 3 and isinstance(e[3], dict) else {}).items()
                        if k in ("to", "color", "bold", "italic", "collapse")}
                block.append([e[0] + 1, e[1], (pos + e[2]) if e[2] and e[2] > 0 else e[2], dest])
            pos += s.page_count
        # place the block before the first top-level bookmark that points after the merge point
        idx = next((i for i, e in enumerate(toc) if e[0] == 1 and e[2] and e[2] > at + total), len(toc))
        O.write(d, toc[:idx] + block + toc[idx:])
    _structural_steps(work, "Merge PDFs", fn, between)
    return list(range(at, at + total))


def duplicate(work, pages):
    sel = sorted(set(pages))

    def fn(d):
        for p in reversed(sel):                 # back to front keeps earlier indices valid
            # "insert before p + 1"; for the last page that index doesn't exist, so append (-1)
            d.fullcopy_page(p, p + 1 if p + 1 < d.page_count else -1)
    _structural(work, "Duplicate pages", fn)
    # copy of sel[k] lands at sel[k] + k + 1
    return [p + k + 1 for k, p in enumerate(sel)]


CROP_MODES = ("box", "margins", "proportion")


def crop(work, pages, rect_on, from_page, mode="box"):
    """Set the crop box of `pages` from `rect_on` (displayed coordinates of page `from_page`).
    mode "box": each page gets the same displayed rectangle, clipped to that page;
    "margins": each page loses the same margins (points from each edge) as the drawn page;
    "proportion": each page keeps the same fraction of its width and height (pages of mixed
    sizes, stage 17). Crop hides content; nothing is deleted."""
    rect_on = pymupdf.Rect(rect_on).normalize()
    if mode not in CROP_MODES:
        raise ValueError(mode)

    def fn(d):
        ref = d[from_page].rect
        ml, mt, mr, mb = rect_on.x0 - ref.x0, rect_on.y0 - ref.y0, ref.x1 - rect_on.x1, ref.y1 - rect_on.y1
        fx0, fy0 = (rect_on.x0 - ref.x0) / ref.width, (rect_on.y0 - ref.y0) / ref.height
        fx1, fy1 = (rect_on.x1 - ref.x0) / ref.width, (rect_on.y1 - ref.y0) / ref.height
        for p in pages:
            pg = d[p]
            pr = pg.rect
            if mode == "margins":
                want = pymupdf.Rect(pr.x0 + ml, pr.y0 + mt, pr.x1 - mr, pr.y1 - mb)
            elif mode == "proportion":
                want = pymupdf.Rect(pr.x0 + fx0 * pr.width, pr.y0 + fy0 * pr.height,
                                    pr.x0 + fx1 * pr.width, pr.y0 + fy1 * pr.height)
            else:
                want = rect_on
            if want.is_empty or want.width < 1 or want.height < 1:
                continue
            r = want & pr
            if r.is_empty:
                continue
            u = (r * pg.derotation_matrix).normalize()      # unrotated, relative to the crop box
            cb = pg.cropbox
            absr = pymupdf.Rect(u.x0 + cb.x0, u.y0 + cb.y0, u.x1 + cb.x0, u.y1 + cb.y0) & pg.mediabox
            if not absr.is_empty:
                pg.set_cropbox(absr)
    _structural(work, "Crop pages", fn)
    return sorted(pages)


def extract(work, pages, dest):
    """The selected pages as a NEW PDF. select() on a plain snapshot keeps links between the
    extracted pages (copying page by page dropped every internal link: stage 18 fix)."""
    snap = snapshot(work)
    try:
        sel = sorted(set(pages))
        toc = O.for_pages(O.read(snap), sel)       # bookmarks for these pages, renumbered
        snap.select(sel)
        snap.set_toc(O.to_set_toc(toc) if toc else [])
        write_new_pdf(snap, dest, work.protection)  # same protection as the document's saves
    finally:
        snap.close()


def split_ranges(n, every=None, after=None):
    """Page ranges for Split: every N pages, or after each listed page (0-based)."""
    if every:
        return [(s, min(s + every, n) - 1) for s in range(0, n, every)]
    cuts = sorted({p for p in (after or []) if 0 <= p < n - 1})
    out, s = [], 0
    for c in cuts:
        out.append((s, c))
        s = c + 1
    out.append((s, n - 1))
    return out


def split(work, ranges, dests):
    snap = snapshot(work)
    try:
        for (a, b), dest in zip(ranges, dests):
            protect.check(dest)
        for (a, b), dest in zip(ranges, dests):
            out = pymupdf.open()
            try:
                out.insert_pdf(snap, from_page=a, to_page=b)
                toc = O.for_pages(O.read(snap), list(range(a, b + 1)))
                if toc:
                    out.set_toc(O.to_set_toc(toc))
                write_new_pdf(out, dest, work.protection)
            finally:
                out.close()
    finally:
        snap.close()
