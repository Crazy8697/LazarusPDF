"""Stage 10b: batch processing, the core (no Qt). The UI (batch_ui.py) runs it in a worker
PROCESS, so PyMuPDF (which holds the GIL inside each call) never stalls the window, and a
file that crashes MuPDF takes down only the worker: the UI marks that file failed and starts
a new worker for the rest.

A job: input files, an ordered pipeline of operations with settings captured up front (no
prompts mid-run), an output folder and a file-name pattern. Per file: read it into memory,
open it (the password given for protected inputs is tried once), run the pipeline on that
in-memory copy, write the result to a NEW file in the output folder, close. Nothing is ever
written to an input: every input is registered with protect.py, and output names never
resolve to an input or to a file that already exists ("name (2).pdf" instead). Writes are
atomic (a hidden partial file, then a rename), so a cancel or a crash leaves no half file.

Errors on one file are reported and the batch goes on. Events carry the file's INDEX only,
never its name, path or contents; error texts are scrubbed of the file's name and path."""
import datetime
import os
import re
import time
from pathlib import Path

import pymupdf

from . import protect

PARTIAL = ".lazarus-partial"           # marker in the names of not-yet-finished outputs

OPS = [  # key, label (in the order the "Add operation" menu shows them: a sensible pipeline order)
    ("ocr", "OCR to searchable"),
    ("redact_terms", "Redact search terms"),
    ("find_replace", "Find and replace"),
    ("bookmarks", "Auto-generate bookmarks"),
    ("metadata", "Metadata (set / remove all)"),
    ("flatten", "Flatten"),
    ("header_footer", "Header / footer"),
    ("page_numbers", "Page numbers"),
    ("bates", "Bates numbering"),
    ("watermark", "Watermark"),
    ("extract", "Extract text / export (Word, EPUB, Markdown, HTML, text, PDF)"),
    ("to_word", "Convert to Word (.docx, keeps layout)"),
    ("compress", "Compress"),
    ("password", "Password / permissions"),
]
LABELS = dict(OPS)

PATTERN_DEFAULT = "{name}-processed"
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class Cancelled(Exception):
    pass


class FileFailed(Exception):
    """A per-file failure with a reason meant for the status list."""


# ---------------- inputs and output names ----------------
def collect_inputs(paths, recurse=False):
    """Files and folders -> the PDFs to process (folders: their PDFs, sorted; optionally the
    subfolders too). Duplicates are dropped; order is kept."""
    out, seen = [], set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            it = p.rglob("*") if recurse else p.glob("*")
            found = sorted((x for x in it if x.is_file() and x.suffix.lower() == ".pdf"), key=lambda x: str(x).lower())
        else:
            found = [p]
        for f in found:
            key = os.path.normcase(str(f.resolve()))
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def expand_pattern(pattern, stem, n, total):
    """{name} = the input's name (no extension), {n} = its position in the batch (zero
    padded), {date} = today. Characters a file name can't hold are replaced."""
    width = len(str(max(1, total)))
    s = (pattern or PATTERN_DEFAULT).replace("{name}", stem).replace("{n}", f"{n:0{width}d}")
    s = s.replace("{date}", datetime.date.today().isoformat())
    s = _BAD_NAME.sub("_", s).strip().rstrip(".") or f"file-{n:0{width}d}"
    return s


def _norm(p):
    return os.path.normcase(str(Path(p).resolve()))


def free_name(folder, stem, ext, taken=(), inputs=()):
    """folder/stem+ext, or "stem (2)" ... when that exists, is an input / protected original, or
    was already given to another output of this job."""
    folder = Path(folder)
    taken = {os.path.normcase(str(t)) for t in taken}
    bad = {_norm(i) for i in inputs}
    k = 1
    while True:
        cand = folder / (f"{stem}{ext}" if k == 1 else f"{stem} ({k}){ext}")
        key = _norm(cand)
        if not cand.exists() and key not in bad and key not in taken and not protect.is_protected(cand):
            return cand
        k += 1


def _partial(dest):
    dest = Path(dest)
    return dest.with_name(f".{dest.stem}{PARTIAL}{dest.suffix}")


def _commit(tmp, dest, inputs):
    """Move a finished partial file into place: never onto an input or a protected file."""
    if _norm(dest) in {_norm(i) for i in inputs}:
        raise protect.ProtectedPathError(protect.MESSAGE)
    protect.check(dest)
    os.replace(tmp, dest)


def clean_partials(folder):
    """Remove partial files a stopped worker left behind (only Lazarus's own markers)."""
    n = 0
    try:
        for p in Path(folder).iterdir():
            if p.is_file() and p.name.startswith(".") and PARTIAL in p.name:
                p.unlink()
                n += 1
    except OSError:
        pass
    return n


def scrub(msg, path):
    """An error text without the file's name or path."""
    msg = str(msg) or "unknown error"
    p = Path(path)
    for s in sorted({str(p), str(p.resolve()), str(p.parent), p.name, p.stem}, key=len, reverse=True):
        if s and len(s) > 2:
            msg = msg.replace(s, "the file")
    return msg.strip()[:300]


# ---------------- the operations ----------------
def _pages(params, doc):
    from . import stage10 as T
    try:
        return T.parse_range(params.get("range", "all"), doc.page_count)
    except ValueError as e:
        raise FileFailed(f"{e} (this file has {doc.page_count} page(s))")


def op_ocr(doc, p, ctx):
    from . import stage10 as T
    from . import tess
    if not ctx.get("tess"):
        tess.configure()
        ctx["tess"] = True
    pages = _pages(p, doc)
    layers, skipped = {}, 0
    for k, pno in enumerate(pages):
        ctx["tick"](f"OCR page {k + 1} of {len(pages)}")
        pg = doc[pno]
        if not p.get("force") and T.has_real_text(pg):
            skipped += 1
            continue
        layers[pno] = T.ocr_page_pdf(pg, p.get("lang", "eng"))
        del pg
    if layers:
        T.add_ocr_layers(doc, layers)
    return f"OCR {len(layers)} page(s)" + (f", {skipped} already had text" if skipped else "")


def op_redact_terms(doc, p, ctx):
    from . import stage10 as T
    from .search import SearchIndex, compile_query
    terms = [t.strip() for t in (p.get("terms") or "").splitlines() if t.strip()]
    if not terms:
        raise FileFailed("Redact: no search terms were given")
    pats = []
    for t in terms:
        try:
            pat = compile_query(t, p.get("match_case", False), p.get("whole_word", False), p.get("regex", False))
        except re.error as e:
            raise FileFailed(f"Redact: bad pattern ({e})")
        if pat is not None:
            pats.append(pat)
    idx = SearchIndex(doc)
    marks = []
    try:
        for pno in range(idx.page_count):
            ctx["tick"](f"Searching page {pno + 1} of {idx.page_count}")
            for pat in pats:
                for a, b in idx.find(pno, pat):
                    marks.extend((pno, r) for r in idx.page(pno).rects(a, b))
    finally:
        idx.close()
    if not marks:
        return "Redact: no hits"
    T.mark_redactions(doc, marks, tuple(p.get("fill", (0, 0, 0))))
    if not p.get("apply", True):
        return f"Redact: {len(marks)} area(s) marked (not applied)"
    applied = T.apply_redactions(doc)
    left = T.verify_redactions(doc, applied)
    if left:
        raise FileFailed(f"Redact: {left} character(s) could still be extracted under the marks; not saved")
    return f"Redacted {len(marks)} area(s), verified clean"


def _targets(p, doc):
    from . import stage10 as T
    pages = _pages(p, doc)
    return pages, T.targets(pages, doc.page_count)


def op_find_replace(doc, p, ctx):
    """The stage-11 write path (fonts matched, reflow in the block, verified); hits it can't
    replace cleanly are skipped and counted."""
    import pymupdf as _pm
    from . import textedit as TE
    from .search import compile_query
    lock = ctx.get("source_lock")
    if lock is not None and not lock.owner_known and not (lock.permissions & _pm.PDF_PERM_MODIFY):
        raise FileFailed("Find and replace: the file doesn't allow changing its content (owner password unknown)")
    if not (p.get("find") or "").strip():
        raise FileFailed("Find and replace: nothing to find was given")
    try:
        pat = compile_query(p["find"], p.get("match_case", False), p.get("whole_word", False), p.get("regex", False))
    except re.error as e:
        raise FileFailed(f"Find and replace: bad pattern ({e})")
    pages = _pages(p, doc)
    ctx["tick"](f"{len(pages)} page(s)")
    n, items = TE.replace_all(doc, pages, pat, p.get("replace", ""), p.get("regex", False))
    skipped = sum(it.hits for it in items if it.status != "ok")
    return f"Replaced {n} hit(s)" + (f", {skipped} skipped" if skipped else "")


def op_header_footer(doc, p, ctx):
    from . import stage10 as T
    pages, tg = _targets(p, doc)
    slots = {k: p[k] for k in T.SLOTS if p.get(k)}
    T.header_footer(doc, tg, slots, p.get("font", "helv"), p.get("size", 9.0), tuple(p.get("color", (0.2, 0.2, 0.2))),
                    p.get("mx", 36.0), p.get("my", 24.0))
    return f"Header / footer on {len(pages)} page(s)"


def op_page_numbers(doc, p, ctx):
    from . import stage10 as T
    pages, tg = _targets(p, doc)
    T.page_numbers(doc, tg, p.get("slot", "bc"), p.get("fmt", "n"), p.get("start", 1), p.get("font", "helv"),
                   p.get("size", 10.0), tuple(p.get("color", (0, 0, 0))), p.get("mx", 36.0), p.get("my", 24.0))
    return f"Page numbers on {len(pages)} page(s)"


def op_bates(doc, p, ctx):
    """With "continue" on, numbering carries on from the previous file of the batch (only
    files that were written count)."""
    from . import stage10 as T
    pages, tg = _targets(p, doc)
    key = ctx["step_key"]
    start = ctx["bates"].get(key, p.get("start", 1)) if p.get("continue", True) else p.get("start", 1)
    T.bates(doc, tg, p.get("prefix", ""), start, p.get("digits", 6), p.get("suffix", ""), p.get("slot", "br"),
            p.get("font", "helv"), p.get("size", 9.0), tuple(p.get("color", (0, 0, 0))), p.get("mx", 36.0),
            p.get("my", 20.0))
    ctx["bates_pending"][key] = start + len(pages)
    first = T.bates_label(p.get("prefix", ""), start, p.get("digits", 6), p.get("suffix", ""))
    last = T.bates_label(p.get("prefix", ""), start + len(pages) - 1, p.get("digits", 6), p.get("suffix", ""))
    return f"Bates {first} to {last}"


def op_watermark(doc, p, ctx):
    from . import stage10 as T
    pages, tg = _targets(p, doc)
    if p.get("kind") == "image":
        png = p.get("image_png")
        if not png:
            raise FileFailed("Watermark: no image was chosen")
        T.watermark_image(doc, tg, png, p.get("position", "center"), p.get("width", 40.0), p.get("opacity", 25) / 100,
                          float(p.get("angle", 0.0)), p.get("behind", True))
    else:
        ang = None if p.get("position", "diagonal") == "diagonal" else float(p.get("angle", 0.0))
        ctx.setdefault("stamped", set()).add(" ".join((p.get("text", "") or "CONFIDENTIAL").split()))
        T.watermark_text(doc, tg, p.get("text", "") or "CONFIDENTIAL", p.get("position", "diagonal"), p.get("font", "hebo"),
                         p.get("size", 60.0), tuple(p.get("color", (0.75, 0.1, 0.1))), p.get("opacity", 25) / 100, ang,
                         p.get("behind", True))
    return f"Watermark on {len(pages)} page(s)"


def op_flatten(doc, p, ctx):
    from . import stage10 as T
    if not (p.get("annots", True) or p.get("widgets", True)):
        raise FileFailed("Flatten: choose annotations, form fields, or both")
    pages = _pages(p, doc)
    T.flatten(doc, pages, p.get("annots", True), p.get("widgets", True))
    return f"Flattened {len(pages)} page(s)"


def op_metadata(doc, p, ctx):
    from . import stage10 as T
    if p.get("mode") == "remove":
        T.remove_metadata(doc)
        return "Metadata removed"
    vals = {k: p[k] for k, _ in T.META_FIELDS if p.get(k)}
    T.set_metadata(doc, vals)
    return f"Metadata: {len(vals)} field(s) set"


def op_bookmarks(doc, p, ctx):
    from . import autobookmarks as A
    from . import outline as O
    an = A.analyze(doc)
    stamped = ctx.get("stamped") or set()
    if stamped:                                     # a watermark an earlier step put on is not a heading
        an.lines = [ln for ln in an.lines if ln["text"] not in stamped]
    if not an.has_text:
        return "Bookmarks: no text (a scan? put OCR earlier in the pipeline)"
    toc = A.from_headings(an, levels=p.get("levels", 3), min_delta=p.get("threshold", 0.5))
    if not toc:
        return "Bookmarks: no headings found"
    existing = O.read(doc)
    O.write(doc, A.merge(existing, toc, p.get("mode", "replace")))
    return f"{len(toc)} bookmark(s)"


def op_extract(doc, p, ctx):
    """The existing extraction (text layer, OCR where there is none) of the file AS IT IS at
    this point of the pipeline, exported to its own file next to the PDF output."""
    from . import export
    from . import extract as X
    fmt = p.get("format", "txt")
    ext = {k: e for k, _l, e, _f in export.FORMATS}[fmt]
    if not ctx.get("tess"):
        from . import tess
        tess.configure()
        ctx["tess"] = True
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)

    def prog(done, total, *_):
        ctx["tick"](f"Extracting page {min(done + 1, total)} of {total}")
    try:
        pages = X.extract(data, progress=prog, should_stop=ctx["stopped"])
    except X.Cancelled:
        raise Cancelled()
    dest = free_name(ctx["out_dir"], ctx["stem"], ext, ctx["taken"], ctx["inputs"])
    ctx["taken"].append(os.path.normcase(str(dest)))
    tmp = _partial(dest)
    ctx["pending"].append((tmp, dest))          # moved into place only if the whole file succeeds
    export.export(fmt, pages, str(tmp), ctx["stem"], p.get("page_headings", False))
    return f"Extracted {len(pages)} page(s) to {ext}"


def op_to_word(doc, p, ctx):
    """Stage 16: the file as it is at this point of the pipeline, converted to a .docx (pdf2docx)
    next to its PDF. Optional OCR first for pages without text (a temporary copy). Written only if
    the whole file succeeds."""
    import pymupdf as _pm
    from . import todocx as X
    lock = ctx.get("source_lock")
    if lock is not None and not lock.owner_known and not (lock.permissions & _pm.PDF_PERM_COPY):
        raise FileFailed("Convert to Word: the file doesn't allow copying its content (owner password unknown)")
    pages = _pages(p, doc)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)
    plain = pymupdf.open(stream=data, filetype="pdf")
    try:
        empty = X.no_text_pages(plain, pages)
    finally:
        plain.close()
    ocr = empty if p.get("ocr_first") else []
    dest = free_name(ctx["out_dir"], ctx["stem"], ".docx", ctx["taken"], ctx["inputs"])
    ctx["taken"].append(os.path.normcase(str(dest)))
    part = _partial(dest)
    ctx["pending"].append((part, dest))           # moved into place only if the whole file succeeds

    def progress(done, total):
        if not ctx["stopped"]():
            ctx["tick"](f"OCR page {-done} of {-total}" if total < 0 else f"page {(done + 1) // 2} of {total // 2}")
    try:
        X.convert(data, part, pages, {"images": p.get("images", True), "tables": p.get("tables", True),
                                      "columns": p.get("columns", True)}, ocr, progress=progress,
                  stopped=ctx["stopped"])
    except X.Cancelled:
        raise Cancelled()
    return (f"Word: {len(pages)} page(s)" + (f", OCR first on {len(ocr)}" if ocr else "")
            + (f", {len(empty)} without text (as pictures)" if empty and not ocr else ""))


def op_compress(doc, p, ctx):
    if p.get("downsample"):
        doc.rewrite_images(dpi_threshold=int(p.get("threshold", 225)), dpi_target=int(p.get("target", 150)),
                           quality=int(p.get("quality", 75)))
    ctx["compress"] = True
    return "Compressed" + (" (images resampled)" if p.get("downsample") else "")


def op_password(doc, p, ctx):
    from . import locked
    mode = p.get("mode", "set")
    if mode == "keep":
        ctx["protection"] = ctx["source_lock"]
        return "Password: kept as the file had it"
    if mode == "clear":
        ctx["protection"] = None
        return "Password: none"
    if mode == "match":
        if not p.get("open_pw"):
            raise FileFailed("Password: no password was given")
        ctx["protection"] = locked.match(p["open_pw"])
        return "Password: set (one password)"
    allowed = {k: bool(p.get(f"perm_{k}", True)) for k, _l, _f in locked.PERMISSIONS}
    lock = locked.custom(p.get("open_pw", ""), p.get("owner_pw", ""), allowed)
    if lock is None:
        raise FileFailed("Password: no password was given")
    ctx["protection"] = lock
    return "Password: set"


RUN = {"ocr": op_ocr, "redact_terms": op_redact_terms, "find_replace": op_find_replace, "header_footer": op_header_footer,
       "page_numbers": op_page_numbers, "bates": op_bates, "watermark": op_watermark, "flatten": op_flatten,
       "metadata": op_metadata, "bookmarks": op_bookmarks, "extract": op_extract, "to_word": op_to_word,
       "compress": op_compress,
       "password": op_password}


# ---------------- one file ----------------
def _open(path, password):
    from . import locked
    data = Path(path).read_bytes()
    tried = []

    def ask(attempt):
        if attempt == 0 and password:
            tried.append(1)
            return password
        return None
    try:
        return locked.open_pdf(data, ask)
    except locked.PasswordCancelled:
        raise FileFailed("Password-protected: the batch password did not open it" if tried
                         else "Password-protected: no batch password was given")


def _write_pdf(doc, dest, protection, compress, inputs):
    kw = protection.save_kwargs() if protection is not None else {"encryption": pymupdf.PDF_ENCRYPT_NONE}
    tmp = _partial(dest)
    try:
        if compress:
            doc.save(str(tmp), garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True,
                     use_objstms=1, **kw)
        else:
            doc.save(str(tmp), garbage=3, deflate=True, **kw)
        _commit(tmp, dest, inputs)
    finally:
        if tmp.exists():
            tmp.unlink()


def process_file(i, spec, state, emit, stopped):
    """Run the pipeline on input i. Returns (outputs, notes). Raises FileFailed / Cancelled."""
    path = Path(spec["inputs"][i])
    n = i + 1
    total = spec["total"]
    stem = expand_pattern(spec.get("pattern"), path.stem, n, total)
    doc, lock = _open(path, spec.get("open_password", ""))
    ctx = {"out_dir": spec["out_dir"], "stem": stem, "taken": state["taken"], "inputs": spec["inputs"],
           "outputs": [], "bates": state["bates"], "bates_pending": {}, "protection": lock, "source_lock": lock,
           "compress": False, "stopped": stopped, "tess": state.get("tess"), "pending": []}
    notes = []
    delay = float(spec.get("test_delay", 0) or 0)
    try:
        for k, step in enumerate(spec["pipeline"]):
            label = LABELS.get(step["op"], step["op"])

            def tick(text, k=k, label=label):
                if stopped():
                    raise Cancelled()
                emit(("step", i, k, f"{label}: {text}"))
            ctx["tick"] = tick
            ctx["step_key"] = k
            tick("…")
            if delay:
                time.sleep(delay)
                tick("…")
            notes.append(RUN[step["op"]](doc, dict(step.get("params") or {}), ctx))
        if stopped():
            raise Cancelled()
        if spec.get("save_pdf", True):
            emit(("step", i, len(spec["pipeline"]), "Saving"))
            dest = free_name(spec["out_dir"], stem, ".pdf", state["taken"], spec["inputs"])
            state["taken"].append(os.path.normcase(str(dest)))
            _write_pdf(doc, dest, ctx["protection"], ctx["compress"], spec["inputs"])
            ctx["outputs"].append(dest.name)
        for tmp, dest in ctx["pending"]:                # extract outputs: only for a file that succeeded
            _commit(tmp, dest, spec["inputs"])
            ctx["outputs"].append(dest.name)
    finally:
        doc.close()
        for tmp, _dest in ctx["pending"]:
            if tmp.exists():
                tmp.unlink()
    state["bates"].update(ctx["bates_pending"])           # only a written file uses up numbers
    state["tess"] = ctx.get("tess")
    return ctx["outputs"], [x for x in notes if x]


# ---------------- the worker process ----------------
def worker_main(spec, events, cancel):
    """Entry point of the worker process. spec: the job (see batch_ui.JobSession._spawn);
    events: the write end of a multiprocessing Pipe (each event is sent at once, so the UI has
    every event up to a crash); cancel: a multiprocessing Event."""
    for i, p in enumerate(spec["inputs"]):
        protect.register(f"batch-input:{i}", p)
    for i, p in enumerate(spec.get("protected", [])):
        protect.register(f"batch-open:{i}", p)
    state = {"taken": list(spec.get("taken", [])), "bates": {int(k): v for k, v in (spec.get("bates") or {}).items()}}
    emit = events.send
    stopped = cancel.is_set
    emit(("hello", os.getpid()))
    crash_at = spec.get("test_crash_at")
    for i in spec["todo"]:
        path = spec["inputs"][i]
        if stopped():
            break
        emit(("start", i))
        if crash_at is not None and i == crash_at:
            os._exit(3)                                  # self-test hook: a file that kills the worker
        try:
            outs, notes = process_file(i, spec, state, emit, stopped)
        except Cancelled:
            emit(("cancelled", i))
            break
        except FileFailed as e:
            emit(("failed", i, scrub(e, path)))
        except Exception as e:                           # anything else: that file failed, go on
            emit(("failed", i, scrub(f"{type(e).__name__}: {e}", path)))
        else:
            emit(("done", i, outs, notes, dict(state["bates"])))
    emit(("end", stopped()))
