"""Stage 14: PDFs from images. The engine (no Qt). The build runs in a worker PROCESS
(images_ui): decoding a photo is CPU work and PyMuPDF holds the GIL while it embeds one, so a
thread would still stall the window; a process never does, and its memory is its own.

Inputs: PNG, JPEG, WebP, GIF (first frame), BMP, TIFF (every frame of a multi-page TIFF), the
other raster formats Pillow reads, and SVG (rasterised at 300 dpi, as Insert image does). EXIF
orientation is honoured, so phone photos come in upright. Input files are only ever read.

Images are streamed: one is opened, placed and let go before the next, so memory holds the PDF
being built plus a single decoded image, never the whole set.

"Keep original" embeds a JPEG's own bytes (no recompression; an EXIF or user rotation becomes
the placement's rotation) and a PNG's own bytes; other formats become lossless PNG. "Recompress"
writes JPEG at the chosen quality, downscaled so it is no sharper than the chosen DPI on the page.
Fill (crop) needs the cropped pixels, so it re-encodes that image (losslessly for "keep")."""
import io
import os
from datetime import datetime
from pathlib import Path

import pymupdf

EXTS = (".png", ".jpg", ".jpeg", ".jpe", ".webp", ".gif", ".bmp", ".dib", ".tif", ".tiff", ".svg", ".ico",
        ".pcx", ".ppm", ".pgm", ".pbm", ".tga", ".jfif")
PAGE_SIZES = {"letter": (612, 792), "legal": (612, 1008), "a4": (595.28, 841.89), "a5": (419.53, 595.28)}
DEFAULTS = {"page": "fit", "custom_w": 210.0, "custom_h": 297.0, "orientation": "auto", "placement": "fit",
            "margin_mm": 10.0, "align": "center", "quality": "keep", "jpeg_quality": 80, "max_dpi": 200,
            "per_page": 1, "spacing_mm": 5.0, "ocr": False}
MM = 72 / 25.4
EXIF_ORIENTATION = 0x0112
EXIF_DATETIME_ORIGINAL = 0x9003
ROT_FOR_EXIF = {3: 180, 6: 270, 8: 90}        # EXIF orientation -> anticlockwise degrees to show it upright


class Cancelled(Exception):
    pass


# ---------------- inputs ----------------
def is_image(path):
    return Path(path).suffix.lower() in EXTS


def collect(paths, recurse=False):
    """Files and folders -> image files (folders: sorted by name, optionally subfolders)."""
    out, seen = [], set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            it = p.rglob("*") if recurse else p.glob("*")
            found = sorted((x for x in it if x.is_file() and is_image(x)), key=lambda x: str(x).lower())
        else:
            found = [p] if is_image(p) else []
        for f in found:
            k = os.path.normcase(str(f.resolve()))
            if k not in seen:
                seen.add(k)
                out.append(f)
    return out


def frames(path):
    """How many pages an image file gives (multi-page TIFF: one per frame; GIF: the first)."""
    path = Path(path)
    if path.suffix.lower() == ".svg":
        return 1
    from PIL import Image
    try:
        with Image.open(path) as im:
            if (im.format or "").upper() == "TIFF":
                return max(1, getattr(im, "n_frames", 1))
    except Exception:
        return 1
    return 1


def items_for(paths):
    """[{"path", "frame", "rotate"}] for every page the images give, in order."""
    out = []
    for p in paths:
        for f in range(frames(p)):
            out.append({"path": str(p), "frame": f, "rotate": 0})
    return out


def date_taken(path):
    """EXIF date taken (as a sortable string), else the file's modified time."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            ex = im.getexif()
            v = ex.get(EXIF_DATETIME_ORIGINAL) or ex.get_ifd(0x8769).get(EXIF_DATETIME_ORIGINAL) or ex.get(0x0132)
            if v:
                return str(v)
    except Exception:
        pass
    return datetime.fromtimestamp(Path(path).stat().st_mtime).strftime("%Y:%m:%d %H:%M:%S")


def sort_items(items, key):
    if key == "name":
        return sorted(items, key=lambda it: (Path(it["path"]).name.lower(), it["frame"]))
    if key == "taken":
        return sorted(items, key=lambda it: (date_taken(it["path"]), it["path"].lower(), it["frame"]))
    if key == "modified":
        return sorted(items, key=lambda it: (Path(it["path"]).stat().st_mtime, it["path"].lower(), it["frame"]))
    return list(items)


# ---------------- one image -> (bytes for insert_image, width px, height px, dpi, rotate) ----------------
class Prepared:
    __slots__ = ("data", "w", "h", "dpi", "rotate", "passthrough")

    def __init__(self, data, w, h, dpi, rotate=0, passthrough=False):
        self.data, self.w, self.h, self.dpi, self.rotate, self.passthrough = data, w, h, dpi, rotate, passthrough

    def size_pt(self):
        """Natural size in points, as shown (after rotation)."""
        w, h = self.w * 72 / self.dpi[0], self.h * 72 / self.dpi[1]
        return (h, w) if self.rotate % 180 else (w, h)


def _dpi(im):
    try:
        dx, dy = (float(v) for v in im.info.get("dpi", (96, 96)))
    except Exception:
        dx = dy = 96.0
    # a missing or nonsense resolution (TIFF / JFIF often say 1) means: assume 96 dpi
    dx = dx if 50.0 <= dx <= 1200.0 else 96.0
    dy = dy if 50.0 <= dy <= 1200.0 else 96.0
    return (dx, dy)


def prepare(item, opts, target_pt=None, crop_to=None):
    """Read one image (one frame). target_pt: (w, h) points it will occupy, for "recompress"
    (no sharper than max_dpi); crop_to: the aspect (w / h) to centre-crop to (fill)."""
    from PIL import Image, ImageOps
    data = item.get("data")
    path = Path(item.get("path") or "scan.png")
    user_rot = int(item.get("rotate", 0)) % 360
    if data is None and path.suffix.lower() == ".svg":
        from .contentedit import load_image
        png, wpt, hpt, _k = load_image(path)
        with Image.open(io.BytesIO(png)) as im:
            w, h = im.size
        src = Prepared(png, w, h, (w * 72 / wpt, h * 72 / hpt), user_rot)
        if crop_to is None and opts.get("quality") != "recompress":
            return src
        im = Image.open(io.BytesIO(png))
        im.load()
        dpi = src.dpi
    else:
        im = Image.open(io.BytesIO(data) if data is not None else path)
        if item.get("frame"):
            im.seek(item["frame"])
        dpi = _dpi(im)
        fmt = (im.format or "").upper()
        orient = 1
        try:
            orient = int(im.getexif().get(EXIF_ORIENTATION, 1))
        except Exception:
            pass
        keep = opts.get("quality", "keep") == "keep"
        if keep and crop_to is None and orient in (1, 3, 6, 8) and item.get("frame", 0) == 0:
            if fmt == "JPEG" and im.mode in ("RGB", "L", "CMYK"):
                w, h = im.size
                im.close()
                return Prepared(data if data is not None else path.read_bytes(), w, h, dpi,
                                (ROT_FOR_EXIF.get(orient, 0) + user_rot) % 360, True)
            if fmt == "PNG" and orient == 1:
                w, h = im.size
                im.close()
                return Prepared(data if data is not None else path.read_bytes(), w, h, dpi, user_rot, True)
        im.load()
        im = ImageOps.exif_transpose(im)
    # decoded: convert, crop (fill), scale (recompress), encode
    if im.mode not in ("RGB", "RGBA", "L"):
        im = im.convert("RGBA" if ("A" in im.mode or im.mode == "P" and "transparency" in im.info) else "RGB")
    if user_rot:
        im = im.rotate(user_rot, expand=True)          # PIL turns anticlockwise, like the placement does
    if crop_to is not None:
        w, h = im.size
        if w / h > crop_to:
            nw = max(1, round(h * crop_to))
            im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:
            nh = max(1, round(w / crop_to))
            im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    out = io.BytesIO()
    if opts.get("quality") == "recompress":
        if target_pt is not None:
            mx = float(opts.get("max_dpi", 200))
            lim_w, lim_h = target_pt[0] / 72 * mx, target_pt[1] / 72 * mx
            f = min(1.0, lim_w / im.width, lim_h / im.height)
            if f < 1.0:
                im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        im.save(out, "JPEG", quality=int(opts.get("jpeg_quality", 80)), optimize=True)
    else:
        im.save(out, "PNG")
    w, h = im.size
    im.close()
    return Prepared(out.getvalue(), w, h, dpi, 0)


# ---------------- layout ----------------
def page_size(opts, natural):
    """(w, h) of a page in points: the image's own size ("fit"), or a fixed size turned to
    suit (orientation auto: the same way round as the content)."""
    if opts.get("page", "fit") == "fit":
        return natural
    if opts["page"] == "custom":
        w, h = float(opts.get("custom_w", 210)) * MM, float(opts.get("custom_h", 297)) * MM
    else:
        w, h = PAGE_SIZES[opts["page"]]
    o = opts.get("orientation", "auto")
    landscape = (natural[0] > natural[1]) if o == "auto" else (o == "landscape")
    if landscape != (w > h):
        w, h = h, w
    return w, h


def grid(n):
    return {1: (1, 1), 2: (1, 2), 4: (2, 2), 6: (2, 3)}[n]


def cells(page_w, page_h, opts, n):
    """Cell rectangles on a page for n images per page (columns x rows, spacing, margins)."""
    m = float(opts.get("margin_mm", 10)) * MM if opts.get("page", "fit") != "fit" else 0.0
    sp = float(opts.get("spacing_mm", 5)) * MM if n > 1 else 0.0
    cols, rows = grid(n)
    if page_w > page_h and n == 2:
        cols, rows = 2, 1
    if page_w > page_h and n == 6:
        cols, rows = 3, 2
    cw = (page_w - 2 * m - sp * (cols - 1)) / cols
    ch = (page_h - 2 * m - sp * (rows - 1)) / rows
    return [pymupdf.Rect(m + c * (cw + sp), m + r * (ch + sp), m + c * (cw + sp) + cw, m + r * (ch + sp) + ch)
            for r in range(rows) for c in range(cols)]


def place(cell, size, placement, align):
    """The image rectangle inside a cell: fit (keep aspect, aligned), fill (covers: the image
    was cropped to the cell's aspect), stretch."""
    if placement in ("stretch", "fill"):
        return pymupdf.Rect(cell)
    iw, ih = size
    s = min(cell.width / iw, cell.height / ih)
    w, h = iw * s, ih * s
    ax = {"left": 0.0, "right": 1.0}.get(align.split("-")[-1] if "-" in align else align, 0.5)
    ay = {"top": 0.0, "bottom": 1.0}.get(align.split("-")[0] if "-" in align else "center", 0.5)
    x = cell.x0 + (cell.width - w) * ax
    y = cell.y0 + (cell.height - h) * ay
    return pymupdf.Rect(x, y, x + w, y + h)


# ---------------- build ----------------
def natural_size(item):
    """Points the image would take at its own resolution, as shown (EXIF and user rotation
    applied). Reads the header only."""
    data = item.get("data")
    path = Path(item.get("path") or "scan.png")
    user_rot = int(item.get("rotate", 0)) % 360
    if data is None and path.suffix.lower() == ".svg":
        d = pymupdf.open(str(path))
        try:
            w, h = d[0].rect.width, d[0].rect.height
        finally:
            d.close()
    else:
        from PIL import Image
        with Image.open(io.BytesIO(data) if data is not None else path) as im:
            if item.get("frame"):
                im.seek(item["frame"])
            dpi = _dpi(im)
            w, h = im.width * 72 / dpi[0], im.height * 72 / dpi[1]
            try:
                orient = int(im.getexif().get(EXIF_ORIENTATION, 1))
            except Exception:
                orient = 1
            if orient in (5, 6, 7, 8):
                w, h = h, w
    return (h, w) if user_rot % 180 else (w, h)


def build(items, opts, progress=None, stopped=None, doc=None):
    """A new (plain) PDF with the images, or pages added to `doc`. Returns the document."""
    o = dict(DEFAULTS)
    o.update(opts or {})
    out = doc if doc is not None else pymupdf.open()
    n = int(o.get("per_page", 1))
    if n > 1 and o.get("page", "fit") == "fit":
        o["page"] = "a4"                         # a grid needs a page to lay out on
    fixed = o.get("page", "fit") != "fit"
    total = len(items)
    for k in range(0, total, n):
        group = items[k:k + n]
        if fixed:
            nat = natural_size(group[0]) if n == 1 else (595.28, 841.89)
            if n > 1 and o.get("orientation", "auto") == "auto":
                nat = (595.28, 841.89)           # grids default to portrait unless chosen
            pw, ph = page_size(o, nat)
        else:
            pw, ph = natural_size(group[0])
        page = out.new_page(width=pw, height=ph)
        cs = cells(pw, ph, o, n) if fixed else [page.rect]
        for j, it in enumerate(group):
            if stopped and stopped():
                raise Cancelled()
            if progress:
                progress(k + j, total)
            cell = cs[j]
            if fixed and o.get("placement") == "fill":
                pr = prepare(it, o, (cell.width, cell.height), crop_to=cell.width / cell.height)
            else:
                pr = prepare(it, o, (cell.width, cell.height))
            rect = place(cell, pr.size_pt(), o.get("placement", "fit") if fixed else "stretch",
                         o.get("align", "center"))
            page.insert_image(rect, stream=pr.data, rotate=pr.rotate, keep_proportion=False)
            pr.data = None                       # let it go before the next image
        del page
    if progress:
        progress(total, total)
    return out


def peak_memory():
    """This process's peak working set in bytes (Windows), or None."""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        f = k32.K32GetProcessMemoryInfo
        f.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        f.restype = wintypes.BOOL
        if f(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return int(pmc.PeakWorkingSetSize)
    except Exception:
        pass
    return None


def worker_main(spec, conn, cancel):
    """Build in the worker process; the finished PDF goes back as bytes (nothing is written)."""
    import time as _time
    delay = float(spec.get("test_delay", 0) or 0)

    def progress(done, total):
        if delay:
            _time.sleep(delay)
        conn.send(("progress", done, total))
    try:
        doc = build(spec["items"], spec.get("options"), progress, cancel.is_set)
        try:
            data = doc.tobytes(garbage=1, deflate=True)
        finally:
            doc.close()
        conn.send(("done", len(data), peak_memory()))
        conn.send_bytes(data)
    except Cancelled:
        conn.send(("cancelled",))
    except Exception as e:                      # shown on screen only; no image names
        conn.send(("failed", f"{type(e).__name__}: {e}"))
