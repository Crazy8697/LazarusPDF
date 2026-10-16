"""Stage 15: scanning through WIA (Windows Image Acquisition) 2.0, no TWAIN. No Qt here.

The COM bridge is comtypes, late-bound (IDispatch): the documented WIA Automation objects
(WIA.DeviceManager, Device, Item, Properties) are driven by name, so nothing is generated or
written at run time (it works the same inside the frozen exe). Every call into a device runs on
the thread that opened it (COM apartments), which the UI keeps off the UI thread; COM calls go
through ctypes, which lets go of the GIL while the scanner works.

What a device can do is READ from it, never guessed: document handling capabilities (flatbed,
feeder, duplex), the data types / intents it lists, its resolution list or range, its bed and
feeder sizes, brightness / contrast ranges. Only those are offered.

A feeder is scanned until WIA says the feeder is empty, which is the normal end. A device error
mid-scan (jam, cover open, disconnect) stops the scan and keeps the pages already scanned; the
error says what happened in plain words.

MockWIA is a stand-in with the same interface (flatbed, feeder, duplex, feeder empty, jam,
disconnect, unsupported settings), so the self-test needs no hardware. LAZARUS_WIA_MOCK=<json>
selects it (tests only)."""
import io
import json
import os

FORMAT_PNG = "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}"
SCANNER_TYPE = 1
# device properties
P_HANDLING_CAPS = 3086
P_HANDLING_STATUS = 3087
P_HANDLING_SELECT = 3088
P_BED_W, P_BED_H = 3074, 3075                 # thousandths of an inch
P_FEED_W, P_FEED_H = 3076, 3077
P_PAGES = 3096
# item properties
P_INTENT = 6146
P_DATATYPE = 4103
P_XRES, P_YRES = 6147, 6148
P_XPOS, P_YPOS, P_XEXT, P_YEXT = 6149, 6150, 6151, 6152
P_BRIGHTNESS, P_CONTRAST = 6154, 6155
CAP_FEED, CAP_FLAT, CAP_DUP = 0x01, 0x02, 0x04
SEL_FEEDER, SEL_FLATBED, SEL_DUPLEX = 0x001, 0x002, 0x004
MODES = {"color": (3, 1), "gray": (2, 2), "bw": (0, 4)}          # mode -> (data type, intent)
STD_DPI = (75, 100, 150, 200, 240, 300, 400, 600, 1200)
PAGE_SIZES_IN = {"letter": (8.5, 11.0), "legal": (8.5, 14.0), "a4": (8.27, 11.69)}

# WIA errors (HRESULTs)
PAPER_EMPTY = 0x80210003
ERRORS = {0x80210002: "The paper jammed. Clear the jam, then scan the rest.",
          0x80210004: "There is a problem with the paper (feed or size). Check it, then scan the rest.",
          0x80210005: "The scanner went offline.",
          0x80210006: "The scanner is busy (maybe in use by another program).",
          0x80210007: "The scanner is still warming up. Wait a moment and scan again.",
          0x80210008: "The scanner needs attention (check its display or lights).",
          0x8021000A: "The scanner stopped responding (disconnected?).",
          0x8021000D: "The scanner is locked (in use by another program).",
          0x80210015: "No scanner is available (switched off or disconnected?).",
          0x80210016: "The scanner's cover is open. Close it, then scan again.",
          0x80210017: "The scanner's lamp is off.",
          0x8021000C: "The scanner doesn't accept those settings."}


class ScanError(Exception):
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


class FeederEmpty(Exception):
    """The normal end of a feeder scan."""


def _hresult(e):
    for a in (getattr(e, "hresult", None), *(getattr(e, "args", ()) or ())):
        if isinstance(a, int):
            return a & 0xFFFFFFFF
    return None


DISP_E_EXCEPTION = 0x80020009
# WIA Automation often wraps the device's own error in DISP_E_EXCEPTION, with only a description
# text; these words in it identify the case
TEXT_KINDS = (("not online", "offline"), ("offline", "offline"), ("paper empty", "empty"),
              ("no paper", "empty"), ("feeder is empty", "empty"), ("out of paper", "empty"), ("jam", "jam"),
              ("cover", "cover"), ("busy", "busy"), ("in use", "busy"), ("locked", "busy"),
              ("warming", "warming"), ("disconnect", "disconnect"), ("not found", "disconnect"))
KIND_TEXT = {"offline": "The scanner isn't online (switched off, asleep or disconnected?). Switch it on, then "
                        "press Refresh.",
             "jam": ERRORS[0x80210002], "cover": ERRORS[0x80210016], "busy": ERRORS[0x80210006],
             "warming": ERRORS[0x80210007], "disconnect": ERRORS[0x8021000A]}


def _excepinfo_text(e):
    """The description inside a COM error's EXCEPINFO, if any."""
    args = getattr(e, "args", ()) or ()
    if len(args) >= 3 and isinstance(args[2], tuple) and args[2] and isinstance(args[2][0], str):
        return args[2][0].strip()
    return ""


def describe(e):
    """A COM error from a scan -> ScanError / FeederEmpty, in plain words."""
    hr = _hresult(e)
    if hr == PAPER_EMPTY:
        return FeederEmpty()
    if hr in ERRORS:
        kind = {0x80210002: "jam", 0x80210016: "cover", 0x80210005: "offline", 0x8021000A: "disconnect",
                0x80210015: "disconnect"}.get(hr, "device")
        return ScanError(kind, ERRORS[hr])
    text = _excepinfo_text(e)
    if text:
        low = text.lower()
        for words, kind in TEXT_KINDS:
            if words in low:
                if kind == "empty":
                    return FeederEmpty()
                return ScanError(kind, KIND_TEXT[kind])
        return ScanError("device", f"The scanner said: {text}")
    return ScanError("device", f"The scanner reported an error ({hr:#010x})." if hr else f"The scan failed: {e}")


# ---------------- capabilities ----------------
class Caps:
    """What one device offers (read from it)."""

    def __init__(self, sources, modes, resolutions, sizes, brightness=None, contrast=None, default_dpi=300):
        self.sources = sources               # subset of ["flatbed", "feeder", "duplex"]
        self.modes = modes                   # subset of ["color", "gray", "bw"]
        self.resolutions = resolutions       # sorted ints
        self.sizes = sizes                   # {"flatbed": (w_in, h_in), "feeder": (w_in, h_in)} maximum areas
        self.brightness = brightness         # (min, max) or None
        self.contrast = contrast
        self.default_dpi = min(resolutions, key=lambda r: abs(r - default_dpi)) if resolutions else default_dpi

    def page_sizes(self, source):
        """Standard sizes that fit the source's area, plus its whole area."""
        mw, mh = self.sizes.get("feeder" if source != "flatbed" else "flatbed", (8.5, 14.0))
        out = [k for k, (w, h) in PAGE_SIZES_IN.items() if w <= mw + 0.05 and h <= mh + 0.05]
        return out + ["max"]

    def as_dict(self):
        return dict(self.__dict__)


# ---------------- the real thing: WIA Automation through comtypes ----------------
def _props(obj):
    out = {}
    ps = obj.Properties
    for k in range(1, ps.Count + 1):
        p = ps.Item(k)
        out[int(p.PropertyID)] = p
    return out


def _sub_values(p):
    """A property's allowed values: (kind, values / (min, max, step))."""
    try:
        st = int(p.SubType)
    except Exception:
        return None, None
    if st == 2:                                 # list
        v = p.SubTypeValues
        return "list", [v.Item(k) for k in range(1, v.Count + 1)]
    if st == 1:                                 # range
        return "range", (p.SubTypeMin, p.SubTypeMax, p.SubTypeStep)
    if st == 3:                                 # flag
        v = p.SubTypeValues
        return "flag", [v.Item(k) for k in range(1, v.Count + 1)] if v is not None else None
    return None, None


class RealWIA:
    """WIA 2.0 Automation. Create it on the thread that will use the device."""
    name = "wia"

    def __init__(self):
        import comtypes
        import comtypes.client as cc
        comtypes.CoInitialize()
        self._cc = cc
        self.dm = cc.CreateObject("WIA.DeviceManager", dynamic=True)
        self._dev = None
        self._dev_id = None

    def devices(self):
        """[(device id, name)] of the WIA scanners (nothing is connected or scanned)."""
        out = []
        infos = self.dm.DeviceInfos
        for k in range(1, infos.Count + 1):
            di = infos.Item(k)
            if int(di.Type) != SCANNER_TYPE:
                continue
            name = "Scanner"
            try:
                name = str(di.Properties.Item("Name").Value)
            except Exception:
                pass
            out.append((str(di.DeviceID), name))
        return out

    def _connect(self, dev_id):
        if self._dev is not None and self._dev_id == dev_id:
            return self._dev
        infos = self.dm.DeviceInfos
        for k in range(1, infos.Count + 1):
            di = infos.Item(k)
            if str(di.DeviceID) == dev_id:
                try:
                    self._dev = di.Connect()
                except Exception as e:
                    err = describe(e)
                    raise err if isinstance(err, ScanError) else ScanError("device", str(e))
                self._dev_id = dev_id
                return self._dev
        raise ScanError("disconnect", ERRORS[0x80210015])

    def caps(self, dev_id):
        dev = self._connect(dev_id)
        dp = _props(dev)
        handling = int(dp[P_HANDLING_CAPS].Value) if P_HANDLING_CAPS in dp else CAP_FLAT
        sources = ([s for s, bit in (("flatbed", CAP_FLAT), ("feeder", CAP_FEED)) if handling & bit]
                   + (["duplex"] if handling & CAP_DUP and handling & CAP_FEED else []))
        if not sources:
            sources = ["flatbed"]
        item = dev.Items.Item(1)
        ip = _props(item)
        modes = ["color", "gray", "bw"]
        if P_DATATYPE in ip:
            kind, vals = _sub_values(ip[P_DATATYPE])
            if kind == "list" and vals:
                modes = [m for m, (dt, _i) in MODES.items() if dt in [int(v) for v in vals]] or modes
        res = list(STD_DPI)
        if P_XRES in ip:
            kind, vals = _sub_values(ip[P_XRES])
            if kind == "list" and vals:
                res = sorted({int(v) for v in vals})
            elif kind == "range":
                lo, hi, _st = vals
                res = [r for r in STD_DPI if int(lo) <= r <= int(hi)] or [int(lo), int(hi)]

        def rng(pid):
            if pid in ip and not bool(getattr(ip[pid], "IsReadOnly", False)):
                kind, vals = _sub_values(ip[pid])
                if kind == "range":
                    return (int(vals[0]), int(vals[1]))
            return None
        sizes = {}
        if P_BED_W in dp and P_BED_H in dp:
            sizes["flatbed"] = (int(dp[P_BED_W].Value) / 1000.0, int(dp[P_BED_H].Value) / 1000.0)
        if P_FEED_W in dp and P_FEED_H in dp:
            sizes["feeder"] = (int(dp[P_FEED_W].Value) / 1000.0, int(dp[P_FEED_H].Value) / 1000.0)
        return Caps(sources, modes, res, sizes, rng(P_BRIGHTNESS), rng(P_CONTRAST))

    def _set(self, props, pid, value):
        if pid in props:
            try:
                props[pid].Value = value
            except Exception as e:
                hr = _hresult(e)
                raise ScanError("settings", ERRORS[0x8021000C] if hr is None or hr not in ERRORS else ERRORS[hr])

    def scan(self, dev_id, settings, on_page, stopped):
        """Scan with settings; on_page(png bytes, dpi) for each page. Flatbed: one page.
        Feeder / duplex: until the feeder is empty (returns normally). Raises ScanError (pages
        already delivered are kept by the caller)."""
        dev = self._connect(dev_id)
        dp = _props(dev)
        src = settings.get("source", "flatbed")
        sel = {"flatbed": SEL_FLATBED, "feeder": SEL_FEEDER, "duplex": SEL_FEEDER | SEL_DUPLEX}[src]
        self._set(dp, P_HANDLING_SELECT, sel)
        if src != "flatbed":
            self._set(dp, P_PAGES, 0)            # all the pages in the feeder
        item = dev.Items.Item(1)
        ip = _props(item)
        dpi = int(settings.get("dpi", 300))
        dt, intent = MODES[settings.get("mode", "color")]
        self._set(ip, P_INTENT, intent)
        self._set(ip, P_DATATYPE, dt)
        self._set(ip, P_XRES, dpi)
        self._set(ip, P_YRES, dpi)
        size = settings.get("size", "max")
        if size in PAGE_SIZES_IN:
            w_in, h_in = PAGE_SIZES_IN[size]
            self._set(ip, P_XPOS, 0)
            self._set(ip, P_YPOS, 0)
            self._set(ip, P_XEXT, int(w_in * dpi))
            self._set(ip, P_YEXT, int(h_in * dpi))
        for pid, key in ((P_BRIGHTNESS, "brightness"), (P_CONTRAST, "contrast")):
            if settings.get(key) is not None:
                self._set(ip, pid, int(settings[key]))
        n = 0
        while True:
            if stopped():
                return n
            try:
                img = item.Transfer(FORMAT_PNG)
            except Exception as e:
                err = describe(e)
                if isinstance(err, FeederEmpty):
                    if n == 0 and src != "flatbed":
                        raise ScanError("empty", "The feeder is empty: put the pages in and scan again.")
                    return n
                raise err
            data = bytes(bytearray(img.FileData.BinaryData))
            on_page(data, (dpi, dpi))
            n += 1
            if src == "flatbed":
                return n

    def acquire_with_stock_dialog(self):
        """The stock WIA scan dialog (Advanced…, for devices that need it) -> png bytes or None."""
        cd = self._cc.CreateObject("WIA.CommonDialog", dynamic=True)
        img = cd.ShowAcquireImage(SCANNER_TYPE, 0, 0, FORMAT_PNG, False, True, False)
        if img is None:
            return None
        return bytes(bytearray(img.FileData.BinaryData))


# ---------------- the stand-in ----------------
class MockWIA:
    """Scenario (dict): {"devices": [{"id", "name", "sources", "modes", "resolutions",
    "sizes", "brightness", "contrast", "flatbed": [page], "feeder": [page], "fail": {"after": n,
    "kind": "jam" | "disconnect" | "cover"}, "reject": ["dpi" | ...]}]}. A page: {"text",
    "blank", "skew", "small"} (small: content in the middle of a white page)."""
    name = "mock"

    def __init__(self, scenario):
        self.s = scenario
        self.calls = 0
        self._flat_pos = {}

    def _dev(self, dev_id):
        for d in self.s.get("devices", []):
            if d["id"] == dev_id:
                if d.get("gone"):
                    raise ScanError("disconnect", ERRORS[0x80210015])
                return d
        raise ScanError("disconnect", ERRORS[0x80210015])

    def devices(self):
        return [(d["id"], d["name"]) for d in self.s.get("devices", []) if not d.get("gone")]

    def caps(self, dev_id):
        d = self._dev(dev_id)
        if d.get("offline_text"):                   # as real WIA reports it: DISP_E_EXCEPTION + a description
            raise describe(_FakeComError(-2147352567, "Exception occurred.", (d["offline_text"], None, None, 0, None)))
        return Caps(list(d.get("sources", ["flatbed"])), list(d.get("modes", ["color", "gray", "bw"])),
                    sorted(d.get("resolutions", [100, 150, 200, 300, 600])),
                    {k: tuple(v) for k, v in d.get("sizes", {"flatbed": [8.5, 11.7], "feeder": [8.5, 14.0]}).items()},
                    tuple(d["brightness"]) if d.get("brightness") else None,
                    tuple(d["contrast"]) if d.get("contrast") else None)

    def scan(self, dev_id, settings, on_page, stopped):
        import time as _time
        d = self._dev(dev_id)
        caps = self.caps(dev_id)
        src = settings.get("source", "flatbed")
        if src not in caps.sources:
            raise ScanError("settings", ERRORS[0x8021000C])
        if int(settings.get("dpi", 300)) not in caps.resolutions or "dpi" in d.get("reject", []):
            raise ScanError("settings", ERRORS[0x8021000C])
        fail = d.get("fail") or {}
        delay = float(d.get("delay", 0))
        n = 0
        if src == "flatbed":
            pages = d.get("flatbed", [{"text": "FLATBED"}])
            k = self._flat_pos.get(dev_id, 0)
            self._flat_pos[dev_id] = k + 1
            seq = [pages[k % len(pages)]]
        else:
            sheets = d.get("feeder", [])
            if not sheets:
                raise ScanError("empty", "The feeder is empty: put the pages in and scan again.")
            seq = []
            for sh in sheets:
                seq.append(dict(sh, side="front"))
                if src == "duplex":
                    seq.append(dict(sh, text=sh.get("back", sh.get("text", "") + " BACK"), side="back",
                                    blank=sh.get("back_blank", False)))
        for page in seq:
            if stopped():
                return n
            if fail and n >= fail.get("after", 0):
                code = {"jam": 0x80210002, "disconnect": 0x8021000A, "cover": 0x80210016}[fail.get("kind", "jam")]
                raise ScanError(fail.get("kind", "jam"), ERRORS[code])
            if delay:
                _time.sleep(delay)
            self.calls += 1
            on_page(mock_page(page, int(settings.get("dpi", 300)), settings.get("mode", "color")),
                    (int(settings.get("dpi", 300)),) * 2)
            n += 1
        return n


class _FakeComError(Exception):
    """Shaped like comtypes' COMError: (hresult, text, (description, source, help, context, ...))."""

    def __init__(self, hr, text, details):
        super().__init__(hr, text, details)
        self.hresult = hr


def mock_page(page, dpi, mode="color"):
    """A synthetic scanned page (PNG with its resolution), drawn small for speed."""
    from PIL import Image, ImageDraw, ImageFont
    r = min(dpi, 100)
    w, h = int(8.5 * r), int(11 * r)
    im = Image.new("RGB", (w, h), (252, 252, 250))
    if not page.get("blank"):
        dr = ImageDraw.Draw(im)
        try:
            font = ImageFont.truetype("arial.ttf", max(12, r // 3))
        except Exception:
            font = ImageFont.load_default()
        x0, y0 = (w // 3, h // 3) if page.get("small") else (int(0.08 * w), int(0.08 * h))
        dr.text((x0, y0), page.get("text", "PAGE"), fill=(10, 10, 10), font=font)
        for k in range(6 if not page.get("small") else 2):
            dr.rectangle((x0, y0 + (k + 2) * r // 3, x0 + (w // 2 if not page.get("small") else w // 5),
                          y0 + (k + 2) * r // 3 + 3), fill=(30, 30, 30))
        if page.get("skew"):
            im = im.rotate(page["skew"], expand=False, fillcolor=(252, 252, 250), resample=Image.BICUBIC)
    if mode == "gray":
        im = im.convert("L")
    elif mode == "bw":
        im = im.convert("1")
    out = io.BytesIO()
    im.save(out, "PNG", dpi=(r, r))              # drawn at r dpi: the page is still Letter-sized
    return out.getvalue()


_BACKEND = None


def backend():
    """The WIA backend for this thread's use: the mock when one is set (tests) or named by
    LAZARUS_WIA_MOCK, else the real WIA."""
    if _BACKEND is not None:
        return _BACKEND
    env = os.environ.get("LAZARUS_WIA_MOCK")
    if env:
        with open(env, encoding="utf-8") as f:
            return MockWIA(json.load(f))
    return RealWIA()


def set_backend(b):
    global _BACKEND
    _BACKEND = b


# ---------------- after the scan: blank pages, deskew, crop ----------------
def _gray(png):
    import numpy as np
    from PIL import Image
    im = Image.open(io.BytesIO(png))
    im.load()
    g = np.asarray(im.convert("L"), dtype=np.uint8)
    return im, g


def is_blank(png, threshold=0.5):
    """True if less than threshold % of the page is ink (darker than light grey)."""
    _im, g = _gray(png)
    return 100.0 * float((g < 200).mean()) < float(threshold)


def deskew(png):
    """Straighten a slightly rotated scan (the angle estimate the OCR uses). Returns
    (png, angle in degrees)."""
    from PIL import Image
    from .ocr import estimate_skew
    im, g = _gray(png)
    a = estimate_skew(g)
    if abs(a) < 0.15:
        return png, 0.0
    dpi = im.info.get("dpi", (300, 300))
    im = im.convert("RGB").rotate(a, expand=False, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
    out = io.BytesIO()
    im.save(out, "PNG", dpi=dpi)
    return out.getvalue(), a


def crop(png, margin=0.02):
    """Crop to the content (whatever isn't background), with a small margin."""
    import numpy as np
    im, g = _gray(png)
    ys, xs = np.nonzero(g < 200)
    if not len(xs):
        return png
    h, w = g.shape
    mx, my = int(margin * w), int(margin * h)
    box = (max(0, int(xs.min()) - mx), max(0, int(ys.min()) - my), min(w, int(xs.max()) + 1 + mx),
           min(h, int(ys.max()) + 1 + my))
    dpi = im.info.get("dpi", (300, 300))
    out = io.BytesIO()
    im.crop(box).save(out, "PNG", dpi=dpi)
    return out.getvalue()


def rotate(png, deg):
    """Turn a page (anticlockwise)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png))
    im.load()
    dpi = im.info.get("dpi", (300, 300))
    out = io.BytesIO()
    im.rotate(deg, expand=True).save(out, "PNG", dpi=dpi)
    return out.getvalue()
