"""Processing modes. Only EXTRACT is built; the others are placeholders."""
from . import extract, output

EXTRACT = "extract"
SEARCHABLE = "searchable"   # LATER: original pages + invisible OCR text layer
REBUILT = "rebuilt"         # LATER: clean text set over the original drawings

MODES = [
    (EXTRACT, "Extract text (review copy)", True),
    (SEARCHABLE, "Searchable PDF — coming later", False),
    (REBUILT, "Rebuilt pages — coming later", False),
]


def run(mode, src, out_dir, progress=None, should_stop=None, password=None):
    """Returns (pages, paths, (gaps, figures))."""
    if mode == EXTRACT:
        pages = extract.extract(src, progress, should_stop, password)
        paths, counts = output.write_all(pages, src, out_dir)
        return pages, paths, counts
    if mode == SEARCHABLE:
        return run_searchable(src, out_dir, progress, should_stop)
    if mode == REBUILT:
        return run_rebuilt(src, out_dir, progress, should_stop)
    raise ValueError(mode)


def run_searchable(src, out_dir, progress=None, should_stop=None):
    """LATER: copy pages as-is, add invisible text (layer text or OCR) -> -searchable.pdf."""
    raise NotImplementedError("Searchable-PDF mode is not built yet.")


def run_rebuilt(src, out_dir, progress=None, should_stop=None):
    """LATER: per page, keep figure regions from the original, typeset extracted text around them."""
    raise NotImplementedError("Rebuilt-page mode is not built yet.")
