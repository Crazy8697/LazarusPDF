"""Stage 17: pages between tabs (copy / cut / paste / drag) and a FULL page graft. No Qt here.

The clipboard is internal to Lazarus PDF and holds page DATA (a small PDF made of the copied pages,
taken from a plain snapshot of the source), never a file path: the source tab can change or
close afterwards. What travels: content, page size and rotation, annotations (every 9a / 9b role,
with their private role data), form fields, and links whose target is also among the copied
pages (links to pages left behind are dropped). Bookmarks pointing at the copied pages travel
too and are added only if asked for ("Bring bookmarks").

graft_full() puts pages into a document inside ONE journal operation, with MuPDF primitives
(as pageops._graft does). MuPDF's page graft copies a page's content and resources but NOT its
annotations, so they are grafted here through the same graft map: each annotation's /P (its
page) is dropped first so the source page isn't dragged along, links are pointed at the new
pages afterwards, and form fields are registered in the destination's AcroForm (a field whose
name is already used there is renamed, "Name (2)")."""
import re

import pymupdf
from pymupdf import mupdf

from . import outline as O

PAGE_REF = re.compile(r"^\s*\[?\s*(\d+)\s+0\s+R(.*)$", re.S)


class Clip:
    """Copied pages: the PDF bytes, how many, the bookmarks for them (renumbered 1..n), and the
    session they came from."""

    def __init__(self, data, n, toc, origin=None):
        self.data, self.n, self.toc, self.origin = data, n, toc, origin

    def doc(self):
        return pymupdf.open(stream=self.data, filetype="pdf")


def copy_pages(doc, pages, origin=None):
    """A Clip of `pages` (in that order) from a document (journalled or not)."""
    sel = list(pages)
    snap = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
    try:
        toc = O.for_pages(O.read(snap), sorted(set(sel)))
        snap.select(sel)                      # keeps links between kept pages, drops the others
        data = snap.tobytes(garbage=1)
    finally:
        snap.close()
    return Clip(data, len(sel), toc, origin)


# ---------------- the full graft ----------------
def _annots(doc, page_xref):
    kind, val = doc.xref_get_key(page_xref, "Annots")
    if kind == "xref":
        val = doc.xref_object(int(val.split()[0]))
    elif kind != "array":
        return []
    return [int(x) for x in re.findall(r"(\d+)\s+0\s+R", val)]


def _pdf_str(s):
    return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def _field_root(doc, xref):
    seen = set()
    while xref not in seen:
        seen.add(xref)
        kind, val = doc.xref_get_key(xref, "Parent")
        if kind != "xref":
            return xref
        xref = int(val.split()[0])
    return xref


def _field_name(doc, xref):
    kind, val = doc.xref_get_key(xref, "T")
    return val if kind == "string" else None


def _all_field_names(doc):
    names = set()
    for i in range(doc.page_count):
        for x in _annots(doc, doc.page_xref(i)):
            if doc.xref_get_key(x, "Subtype")[1] == "/Widget":
                n = _field_name(doc, _field_root(doc, x))
                if n:
                    names.add(n)
    return names


def _prepare(src, pages, private=False):
    """On a private plain copy of the source (or on src itself when it is private: opened just
    for this and closed afterwards): every annotation of `pages` without /P, and the internal
    link targets noted (annot xref -> (target page position, rest of the destination))."""
    tmp = src if private else pymupdf.open("pdf", src.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
    pos = {tmp.page_xref(p): k for k, p in enumerate(pages)}
    links = {}
    for p in pages:
        for x in _annots(tmp, tmp.page_xref(p)):
            tmp.xref_set_key(x, "P", "null")
            if tmp.xref_get_key(x, "Subtype")[1] != "/Link":
                continue
            kind, dest = tmp.xref_get_key(x, "Dest")
            if kind != "array":
                kind, dest = tmp.xref_get_key(x, "A/D")
                if kind == "array":
                    tmp.xref_set_key(x, "A", "null")
            if kind != "array":
                continue
            m = PAGE_REF.match(dest)
            if m and int(m.group(1)) in pos:
                links[x] = (pos[int(m.group(1))], m.group(2).rstrip().rstrip("]"))
                tmp.xref_set_key(x, "Dest", "null")
            elif m:                                   # a page that isn't coming along: drop the link
                tmp.xref_set_key(x, "Dest", "null")
                links[x] = None
    return tmp, links


GRAFT_STEP = 50          # pages per journal operation in graft_steps()


def graft_full(doc, src, pages, at, private=False):
    """Graft `pages` of src (a plain document) into doc before index `at`, annotations, links and
    form fields included, in one go (inside the caller's ONE journal operation). Returns the
    form field renames [(old, new)]. private: src was opened only for this and is closed
    afterwards, so it is prepared in place (no copy of a big file)."""
    gen = graft_steps(doc, src, pages, at, private, step=None)
    while True:
        try:
            next(gen)
        except StopIteration as e:
            return e.value


def graft_steps(doc, src, pages, at, private=False, step=GRAFT_STEP):
    """graft_full as a generator that yields (pages done, total) every `step` pages, so a big
    graft can be split into several journal operations (WorkingDoc.edit_steps). One graft map
    for all of it (shared fonts and images are copied once); links and form fields are fixed
    up at the end, when every page is in place. Returns the renames."""
    pages = list(pages)
    tmp, links = _prepare(src, pages, private)
    try:
        taken = _all_field_names(doc)
        dst = pymupdf._as_pdf_document(doc)
        s = pymupdf._as_pdf_document(tmp)
        gmap = mupdf.pdf_new_graft_map(dst)
        roots, fix = [], []
        for k, i in enumerate(pages):
            if step and k and k % step == 0:
                yield k, len(pages)
            mupdf.pdf_graft_mapped_page(gmap, at + k, s, i)
            new_page = doc.page_xref(at + k)
            refs = []
            for x in _annots(tmp, tmp.page_xref(i)):
                if links.get(x, 0) is None:
                    continue                              # a link to a page left behind
                obj = mupdf.pdf_graft_mapped_object(gmap, mupdf.pdf_new_indirect(s, x, 0))
                nx = mupdf.pdf_to_num(obj)
                refs.append(nx)
                doc.xref_set_key(nx, "P", f"{new_page} 0 R")
                if x in links:
                    fix.append((nx, links[x]))
                if doc.xref_get_key(nx, "Subtype")[1] == "/Widget":
                    root = _field_root(doc, nx)
                    if root not in roots:
                        roots.append(root)
            if refs:
                doc.xref_set_key(new_page, "Annots", "[" + " ".join(f"{r} 0 R" for r in refs) + "]")
        if step and len(pages) > step:
            yield len(pages), len(pages)
        for nx, (t, rest) in fix:                         # internal links: to the new pages
            doc.xref_set_key(nx, "Dest", f"[{doc.page_xref(at + t)} 0 R{rest}]")
        renames = []
        for root in roots:
            name = _field_name(doc, root)
            if name and name in taken:
                k, new = 2, f"{name} ({2})"
                while new in taken:
                    k += 1
                    new = f"{name} ({k})"
                doc.xref_set_key(root, "T", _pdf_str(new))
                renames.append((name, new))
                name = new
            if name:
                taken.add(name)
        if roots:
            cat = doc.pdf_catalog()
            kind, acro = doc.xref_get_key(cat, "AcroForm")
            add = " ".join(f"{r} 0 R" for r in roots)
            if kind == "xref":
                ax = int(acro.split()[0])
                fk, fv = doc.xref_get_key(ax, "Fields")
                if fk == "xref":
                    fx = int(fv.split()[0])
                    doc.update_object(fx, doc.xref_object(fx).rstrip().rstrip("]") + " " + add + "]")
                else:
                    doc.xref_set_key(ax, "Fields", (fv.rstrip().rstrip("]") if fk == "array" else "[") + " " + add + "]")
            elif kind == "dict":
                if "/Fields" in acro:
                    acro = re.sub(r"/Fields\s*\[([^\]]*)\]", lambda m_: f"/Fields[{m_.group(1)} {add}]", acro, count=1)
                else:
                    acro = acro.replace("<<", f"<</Fields[{add}]", 1)
                doc.xref_set_key(cat, "AcroForm", acro)
            else:
                doc.xref_set_key(cat, "AcroForm", f"<</Fields[{add}]/DA(/Helv 0 Tf 0 g)>>")
        return renames
    finally:
        if tmp is not src:
            tmp.close()


def paste(doc, clip_doc, at, toc=None):
    """The paste operation (one journal op in the caller): every page of clip_doc before `at`;
    toc: the clip's bookmarks to add (renumbered for where they land). Returns field renames."""
    n = clip_doc.page_count
    renames = graft_full(doc, clip_doc, range(n), at, private=True)
    if toc:
        cur = O.read(doc)
        moved = [[e[0], e[1], (e[2] + at) if e[2] and e[2] > 0 else e[2]] + list(e[3:]) for e in toc]
        O.write(doc, cur + moved)
    return renames
