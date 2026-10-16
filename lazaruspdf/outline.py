"""Bookmarks (PDF outline) for the working copy.

The outline is a list of entries [level, title, page (1-based, -1 = none), dest dict], the
same shape as PyMuPDF's get_toc(simple=False).

write() replaces the outline inside a journal operation, so each bookmark edit is one
undoable step. PyMuPDF's set_toc() can't be used there: it calls get_new_xref() and
update_object(), which a journalled document refuses mid-operation. So the outline
dictionaries are built with MuPDF primitives (pdf_add_new_dict / pdf_dict_put...), the
same approach the page ops use. Destinations reference page OBJECTS, so moving, inserting
or duplicating pages keeps every bookmark on its page automatically. Only deletion needs
handling, done via remap().

Plain (non-journalled) documents, like Extract/Split output, use doc.set_toc()."""
import pymupdf
from pymupdf import mupdf

N = mupdf.pdf_new_name


def read(doc):
    return [list(e) for e in doc.get_toc(simple=False)]


def normalize(toc):
    """Make levels valid: the first entry is level 1, and each entry is at most one deeper
    than the one before. Children of a removed entry move up under the previous entry."""
    out, prev = [], 0
    for e in toc:
        lvl = max(1, min(int(e[0]), prev + 1))
        out.append([lvl] + list(e[1:]))
        prev = lvl
    return out


def remap(toc, mapping):
    """mapping: old 0-based page -> new 0-based page, or None if the page is gone.
    Entries whose page is gone are dropped (no dangling bookmarks); others are renumbered.
    Entries with no page (-1, e.g. a web link) are kept as they are."""
    out = []
    stack = []                    # ancestors: (original level, dropped?)
    for e in toc:
        lvl, title, page = e[0], e[1], e[2]
        dest = dict(e[3]) if len(e) > 3 and isinstance(e[3], dict) else {}
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        promoted = sum(1 for _, gone in stack if gone)     # children take a dropped parent's place
        if page is None or page < 1:
            keep, new = True, None
        else:
            new = mapping.get(page - 1)
            keep = new is not None
        stack.append((lvl, not keep))
        if not keep:
            continue
        if new is None:
            out.append([lvl - promoted, title, page, dest])
            continue
        if "page" in dest:
            dest["page"] = new
        out.append([lvl - promoted, title, new + 1, dest])
    return normalize(out)


def _dest_array(pdf, doc, page0, dest):
    """[page /XYZ left top null]: keep the original scroll position when we know it."""
    arr = mupdf.pdf_new_array(pdf, 5)
    mupdf.pdf_array_push(arr, mupdf.pdf_lookup_page_obj(pdf, page0))
    to = dest.get("to") if dest else None
    if to is not None:
        h = doc[page0].mediabox.height
        mupdf.pdf_array_push(arr, N("XYZ"))
        mupdf.pdf_array_push_real(arr, float(to[0]))
        mupdf.pdf_array_push_real(arr, float(h - to[1]))
        mupdf.pdf_array_push_real(arr, 0)                # zoom 0 = keep the reader's zoom (PDF spec)
    else:
        mupdf.pdf_array_push(arr, N("Fit"))
    return arr


def write(doc, toc):
    """Replace the outline. Call inside a journal operation (WorkingDoc.edit)."""
    toc = normalize(toc)
    pdf = pymupdf._as_pdf_document(doc)
    root = mupdf.pdf_dict_get(mupdf.pdf_trailer(pdf), N("Root"))
    mupdf.pdf_dict_del(root, N("Outlines"))         # old objects become unreferenced; dropped on save
    if not toc:
        return
    ol = mupdf.pdf_add_new_dict(pdf, 4)
    mupdf.pdf_dict_put(ol, N("Type"), N("Outlines"))
    mupdf.pdf_dict_put(root, N("Outlines"), ol)

    # build the tree: each node = (dict object, children list)
    nodes = []
    stack = [(0, ol, [])]                            # (level, obj, children)
    top_children = stack[0][2]
    for lvl, title, page, *rest in toc:
        dest = rest[0] if rest and isinstance(rest[0], dict) else {}
        item = mupdf.pdf_add_new_dict(pdf, 6)
        mupdf.pdf_dict_put_text_string(item, N("Title"), str(title))
        kind = dest.get("kind", pymupdf.LINK_GOTO if page and page > 0 else pymupdf.LINK_NONE)
        if page and page > 0 and 0 < page <= doc.page_count:
            mupdf.pdf_dict_put(item, N("Dest"), _dest_array(pdf, doc, page - 1, dest))
        elif kind == pymupdf.LINK_URI and dest.get("uri"):
            act = mupdf.pdf_new_dict(pdf, 2)
            mupdf.pdf_dict_put(act, N("S"), N("URI"))
            mupdf.pdf_dict_put_text_string(act, N("URI"), dest["uri"])
            mupdf.pdf_dict_put(item, N("A"), act)
        color = dest.get("color")
        if color and len(color) == 3:
            c = mupdf.pdf_new_array(pdf, 3)
            for v in color:
                mupdf.pdf_array_push_real(c, float(v))
            mupdf.pdf_dict_put(item, N("C"), c)
        flags = (1 if dest.get("italic") else 0) | (2 if dest.get("bold") else 0)
        if flags:
            mupdf.pdf_dict_put_int(item, N("F"), flags)
        while stack[-1][0] >= lvl:
            stack.pop()
        parent_level, parent_obj, siblings = stack[-1]
        mupdf.pdf_dict_put(item, N("Parent"), parent_obj)
        node = (lvl, item, [], bool(dest.get("collapse")))
        siblings.append(node)
        stack.append((lvl, item, node[2]))
        nodes.append(node)

    def link(parent_obj, children):
        """First/Last/Next/Prev links; returns the number of visible descendants."""
        if not children:
            return 0
        mupdf.pdf_dict_put(parent_obj, N("First"), children[0][1])
        mupdf.pdf_dict_put(parent_obj, N("Last"), children[-1][1])
        visible = 0
        for k, (lvl, obj, kids, collapsed) in enumerate(children):
            if k:
                mupdf.pdf_dict_put(obj, N("Prev"), children[k - 1][1])
            if k < len(children) - 1:
                mupdf.pdf_dict_put(obj, N("Next"), children[k + 1][1])
            sub = link(obj, kids)
            if kids:
                mupdf.pdf_dict_put_int(obj, N("Count"), -sub if collapsed else sub)
            visible += 1 + (0 if collapsed else sub)
        return visible

    total = link(ol, top_children)
    mupdf.pdf_dict_put_int(ol, N("Count"), total)


def for_pages(toc, pages):
    """Bookmarks for a new file made of `pages` (old 0-based, in order): retargeted, others dropped."""
    mapping = {p: k for k, p in enumerate(pages)}
    return remap(toc, mapping)


def to_set_toc(toc):
    """Entries in the shape doc.set_toc() accepts for plain documents."""
    out = []
    for e in toc:
        dest = dict(e[3]) if len(e) > 3 and isinstance(e[3], dict) else {}
        if e[2] and e[2] > 0:
            dest.setdefault("kind", pymupdf.LINK_GOTO)
            dest["page"] = e[2] - 1
        out.append([e[0], e[1], e[2], dest] if dest else [e[0], e[1], e[2]])
    return out
