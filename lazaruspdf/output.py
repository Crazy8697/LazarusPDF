"""Writers. Review outputs (-review.html, -text.txt, -final.pdf, -gaps.txt) and the
clean renderers the exporters reuse.

Segments in a Para: str (text), GAP (open gap), Filled (a gap the user filled)."""
import base64
import html
import io
from pathlib import Path

import pymupdf

from .extract import GAP, Figure, Filled


def gap_marker(p):
    return f"[[FILL IN - p.{p}]]"


def fig_marker(p):
    return f"[[FIGURE - p.{p}]]"


def output_paths(src, out_dir):
    base = Path(src).stem
    out = Path(out_dir)
    return {
        "review": out / f"{base}-review.html",
        "text": out / f"{base}-text.txt",
        "final": out / f"{base}-final.pdf",
        "gaps": out / f"{base}-gaps.txt",
    }


def counts(pages):
    """(open gaps, figures, filled gaps)."""
    gaps = figs = filled = 0
    for pr in pages:
        for it in pr.items:
            if isinstance(it, Figure):
                figs += 1
            else:
                gaps += sum(1 for s in it.segs if s is GAP)
                filled += sum(1 for s in it.segs if isinstance(s, Filled))
    return gaps, figs, filled


def spaced(segs):
    """Text pieces and markers as one run, with a space between neighbours."""
    out = []
    for s in segs:
        if out:
            prev = out[-1]
            if not (isinstance(prev, str) and prev.endswith((" ", "\n"))) and not (
                    isinstance(s, str) and s.startswith((" ", "\n", ",", ".", ";", ":", ")", "!", "?"))):
                out.append(" ")
        out.append(s)
    return out


def seg_text(s, page):
    if s is GAP:
        return gap_marker(page)
    if isinstance(s, Filled):
        return s.text
    return s


def para_text(it):
    return "".join(seg_text(s, it.page) for s in spaced(it.segs))


def seg_html(s, page, clean):
    if s is GAP:
        return f'<span class="gap">{html.escape(gap_marker(page))}</span>'
    if isinstance(s, Filled):
        t = html.escape(s.text)
        return t if clean else f'<span class="filled">{t}</span>'
    return html.escape(s)


def para_html(it, clean, tag="p"):
    inner = "".join(seg_html(s, it.page, clean) for s in spaced(it.segs))
    cls = ' class="ocr"' if (not clean and it.source == "ocr") else ""
    return f"<{tag}{cls}>{inner}</{tag}>"


# ---------- plain text ----------

def render_text(pages, page_headings=True):
    lines = []
    for pr in pages:
        if page_headings:
            lines += [f"===== p.{pr.number} =====", ""]
            if not pr.items:
                lines += ["(blank page)", ""]
        for it in pr.items:
            if isinstance(it, Figure):
                lines.append(fig_marker(pr.number))
                for lab in it.labels:
                    lines.append(f"    (figure text: {lab})")
            else:
                lines.append(para_text(it))
            lines.append("")
    return "\n".join(lines)


def write_text(pages, path):
    Path(path).write_text(render_text(pages), encoding="utf-8")


# ---------- gaps list ----------

def write_gaps(pages, path):
    entries = []
    for pr in pages:
        flat = []
        for it in pr.items:
            if isinstance(it, Figure):
                flat.append(" ")
            else:
                flat.extend(spaced(it.segs))
                flat.append(" ")
        for i, s in enumerate(flat):
            if s is GAP or isinstance(s, Filled):
                before = "".join(seg_text(x, pr.number) if x is not GAP else "[gap]"
                                 for x in flat[:i])[-60:].strip()
                after = "".join(seg_text(x, pr.number) if x is not GAP else "[gap]"
                                for x in flat[i + 1:])[:60].strip()
                entries.append((pr.number, before, after, s))
    open_n = sum(1 for e in entries if e[3] is GAP)
    out = [f"Lazarus PDF gap list: {open_n} open, {len(entries) - open_n} filled", ""]
    for n, (p, before, after, s) in enumerate(entries, 1):
        state = "OPEN" if s is GAP else f"filled: {s.text}"
        out.append(f"{n:4d}. p.{p}  [{state}]")
        out.append(f"      before: …{before}" if before else "      before: (start of page)")
        out.append(f"      after:  {after}…" if after else "      after:  (end of page)")
    if not entries:
        out.append("No gaps.")
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return open_n


# ---------- gap colours (Settings) ----------

# Custom highlight colours from Settings for the written review HTML / PDF. None = the built-in
# look (which is what the defaults give, so outputs made with default settings don't change).
GAP_COLORS = {"open": None, "filled": None}
DEFAULT_GAP_COLORS = {"open": "#ffeb3b", "filled": "#24452c"}


def set_gap_colors(open_bg=None, filled_bg=None):
    """Called when settings apply. A colour equal to the default keeps the built-in look."""
    def norm(c, default):
        c = (c or "").strip().lower()
        return c if len(c) == 7 and c.startswith("#") and c != default else None
    GAP_COLORS["open"] = norm(open_bg, DEFAULT_GAP_COLORS["open"])
    GAP_COLORS["filled"] = norm(filled_bg, DEFAULT_GAP_COLORS["filled"])


def _dark(c):
    r, g, b = (int(c[i:i + 2], 16) for i in (1, 3, 5))
    return (max(r, g, b) + min(r, g, b)) / 510 < 0.5          # HSL lightness, as the editor uses


def _gap_css(pdf=False):
    """Extra CSS that puts the custom colours over the built-in ones ("" with default settings)."""
    bg = "background-color" if pdf else "background"
    out = []
    if GAP_COLORS["open"]:
        c = GAP_COLORS["open"]
        out.append(f".gap{{{bg}:{c};color:{'#ffd6d6' if _dark(c) else '#b71c1c'}}}")
    if GAP_COLORS["filled"]:
        c = GAP_COLORS["filled"]
        out.append(f".filled{{{bg}:{c};color:{'#a5d6a7' if _dark(c) else '#1b5e20'}}}")
    return "\n".join(out)


# ---------- HTML ----------

_CSS = """
:root{--bg:#f7f5ef;--fg:#1b1b1b;--muted:#6b6b6b;--rule:#d8d3c4;--card:#fff}
@media (prefers-color-scheme: dark){:root{--bg:#16181d;--fg:#e6e6e6;--muted:#9aa0a6;--rule:#2c313a;--card:#1e2128}}
body{background:var(--bg);color:var(--fg);font:16px/1.6 Georgia,'Times New Roman',serif;margin:0}
main{max-width:860px;margin:0 auto;padding:24px 20px 80px}
header{position:sticky;top:0;background:var(--card);border-bottom:1px solid var(--rule);
  font:14px/1.4 'Segoe UI',system-ui,sans-serif;padding:10px 20px;display:flex;gap:16px;align-items:center;z-index:5;flex-wrap:wrap}
header button{font:inherit;padding:4px 10px;cursor:pointer}
h2.pg{font:600 13px 'Segoe UI',system-ui,sans-serif;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;
  border-top:1px solid var(--rule);padding-top:14px;margin:36px 0 12px}
p{margin:0 0 .9em}
.gap{background:#ffeb3b;color:#000;border:2px solid #e53935;border-radius:3px;padding:0 4px;font:700 14px 'Segoe UI',sans-serif;white-space:nowrap}
.gap.cur{outline:3px solid #e53935;outline-offset:2px}
.filled{background:#c8e6c9;color:#1b5e20;border-bottom:2px dotted #2e7d32}
figure{margin:18px 0;padding:10px;background:var(--card);border:1px solid var(--rule);border-radius:6px}
figure img{max-width:100%;height:auto;display:block;margin:8px auto;background:#fff}
.fig{background:#4fc3f7;color:#000;border-radius:3px;padding:0 4px;font:700 14px 'Segoe UI',sans-serif}
figcaption{font:13px 'Segoe UI',sans-serif;color:var(--muted)}
.ocr{border-left:3px solid #9575cd;padding-left:8px}
"""

_CLEAN_CSS = """
body{font:17px/1.6 Georgia,'Times New Roman',serif;color:#1b1b1b;background:#fff;margin:0}
main{max-width:780px;margin:0 auto;padding:32px 20px}
p{margin:0 0 .9em}
h2.pg{font:600 12px sans-serif;color:#888;border-top:1px solid #ddd;padding-top:10px;margin:28px 0 10px}
.gap{background:#ffeb3b;color:#b71c1c;font-weight:bold}
.fig{background:#4fc3f7;color:#000;font-weight:bold}
figure{margin:18px 0;text-align:center}
figure img{max-width:100%;height:auto}
figcaption{font-size:13px;color:#666}
"""

_JS = """
const g=[...document.querySelectorAll('.gap')];let k=-1;
function go(d){if(!g.length)return;if(k>=0)g[k].classList.remove('cur');k=(k+d+g.length)%g.length;
g[k].classList.add('cur');g[k].scrollIntoView({block:'center'});document.getElementById('pos').textContent=(k+1)+' / '+g.length}
"""


def figure_html(it, src):
    cap = "".join(f"<div>Text in figure: {html.escape(l)}</div>" for l in it.labels)
    return (f'<figure><span class="fig">{html.escape(fig_marker(it.page))}</span>'
            f'<img alt="Figure, page {it.page}" src="{src}"/>'
            + (f"<figcaption>{cap}</figcaption>" if cap else "") + "</figure>")


def data_uri(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


def render_html(pages, title, clean=False, page_headings=True):
    gaps, figs, filled = counts(pages)
    body = []
    for pr in pages:
        if page_headings:
            label = f"Page {pr.number}" + ("" if clean else f" · {pr.method}")
            body.append(f'<h2 class="pg" id="p{pr.number}">{label}</h2>')
            if not pr.items:
                body.append("<p><i>(blank page)</i></p>")
        for it in pr.items:
            body.append(figure_html(it, data_uri(it.png)) if isinstance(it, Figure)
                        else para_html(it, clean))
    t = html.escape(title)
    if clean:
        return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{t}</title><style>{_CLEAN_CSS}{_gap_css()}</style></head>"
                f"<body><main>{''.join(body)}</main></body></html>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t}: review</title><style>{_CSS}{_gap_css()}</style></head><body>
<header><b>Lazarus PDF review</b><span>{len(pages)} page(s) · {gaps} open gap(s) · {filled} filled · {figs} figure(s)</span>
<span><button onclick="go(-1)">◀ Prev gap</button> <button onclick="go(1)">Next gap ▶</button> <span id="pos"></span></span>
<span style="color:var(--muted)">Purple bar = OCR text · green = filled gap</span></header>
<main>{''.join(body)}</main><script>{_JS}</script></body></html>"""


def write_html(pages, path, title):
    Path(path).write_text(render_html(pages, title), encoding="utf-8")


# ---------- PDF ----------

_PDF_CSS = """
body{font-family:serif;font-size:11pt;line-height:1.4}
h2{font-family:sans-serif;font-size:9pt;color:#777777;margin-top:14pt;margin-bottom:6pt;border-top:1px solid #cccccc}
p{margin-top:0;margin-bottom:7pt}
.gap{background-color:#ffeb3b;color:#b71c1c;font-family:sans-serif;font-weight:bold}
.filled{background-color:#c8e6c9;color:#1b5e20}
.fig{background-color:#4fc3f7;color:#000000;font-family:sans-serif;font-weight:bold}
.cap{font-family:sans-serif;font-size:8pt;color:#555555}
"""

FIG_DPI = 150   # figures are rendered at this resolution


def render_pdf(pages, clean=False, page_headings=True):
    arch = pymupdf.Archive()
    body = []
    n = 0
    for pr in pages:
        if page_headings:
            body.append(f"<h2>PAGE {pr.number}</h2>")
            if not pr.items:
                body.append("<p><i>(blank page)</i></p>")
        for it in pr.items:
            if isinstance(it, Figure):
                n += 1
                name = f"fig{n}.png"
                arch.add(it.png, name)
                pix = pymupdf.Pixmap(it.png)
                w = min(468, pix.width * 72 / FIG_DPI)
                body.append(f'<p><span class="fig">{html.escape(fig_marker(pr.number))}</span></p>'
                            f'<p><img src="{name}" width="{int(w)}"/></p>')
                for lab in it.labels:
                    body.append(f'<p class="cap">Text in figure: {html.escape(lab)}</p>')
            else:
                body.append(para_html(it, clean))
    story = pymupdf.Story(html="<body>" + "".join(body) + "</body>", user_css=_PDF_CSS + _gap_css(pdf=True), archive=arch)
    mediabox = pymupdf.paper_rect("letter")
    where = mediabox + (72, 72, -72, -72)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return buf.getvalue()


def write_pdf(pages, path):
    Path(path).write_bytes(render_pdf(pages))


def write_review(pages, paths, title):
    """(Re)write the four review outputs in place."""
    from . import protect
    for p in paths.values():
        protect.check(p)                    # never write over an opened original
    write_html(pages, paths["review"], title)
    write_text(pages, paths["text"])
    write_pdf(pages, paths["final"])
    write_gaps(pages, paths["gaps"])


def write_all(pages, src, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = output_paths(src, out_dir)
    srcp = Path(src).resolve()
    if any(p.resolve() == srcp for p in paths.values()):
        raise RuntimeError("Output would overwrite the original; pick another folder.")
    write_review(pages, paths, Path(src).stem)
    g, f, _ = counts(pages)
    return paths, (g, f)
