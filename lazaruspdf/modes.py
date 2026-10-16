"""Extraction's processing: the text extraction that makes the review copy. Searchable PDF and
Rebuilt pages are made from the CORRECTED review afterwards (Export menu; rebuild.py)."""
from . import extract, output

EXTRACT = "extract"


def run(mode, src, out_dir, progress=None, should_stop=None, password=None):
    """Returns (pages, paths, (gaps, figures))."""
    if mode == EXTRACT:
        pages = extract.extract(src, progress, should_stop, password)
        paths, counts = output.write_all(pages, src, out_dir)
        return pages, paths, counts
    raise ValueError(mode)
