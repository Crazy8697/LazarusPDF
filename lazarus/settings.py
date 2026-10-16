"""Settings store: the existing QSettings("Lazarus", "Lazarus") (HKCU registry), extended.

Key names already in use are kept (out_dir, export_headings, last_dir, export_dir).
LAZARUS_SETTINGS_INI=<path> switches to an INI file. Tests use it so they never touch
the real settings."""
import os
from pathlib import Path

from PySide6.QtCore import QSettings

HOME = Path.home()

DEFAULTS = {
    # Output
    "out_dir": str(HOME / "extract-out"),
    "save_default": "in_place",            # what Ctrl+S / Save does: in_place | save_as
    # Extraction / gaps
    "gap_open_color": "#ffeb3b",
    "gap_filled_color": "#24452c",
    "export_headings": False,
    # View / zoom
    "default_zoom": "fit_width",           # fit_width | fit_page | 50 | 75 | 100 | 125 | 150 | 200
    # Undo memory: snapshot-undo budget, % of total RAM, across all open documents (memguard)
    "undo_ram_pct": 30,
    # Compare (stage 13): mark colours
    "compare_insert_color": "#2ea043",
    "compare_delete_color": "#e5534b",
    "compare_modify_color": "#d29922",
    # Updates (stage 19b): the one GitHub check at startup
    "update_check": True,
}

ZOOM_CHOICES = [("fit_width", "Fit width"), ("fit_page", "Fit page"), ("50", "50%"), ("75", "75%"),
                ("100", "100%"), ("125", "125%"), ("150", "150%"), ("200", "200%")]
SAVE_CHOICES = [("in_place", "Save in place (overwrite the review files)"),
                ("save_as", "Save As… (choose a folder each time)")]


def store():
    ini = os.environ.get("LAZARUS_SETTINGS_INI")
    if ini:
        return QSettings(ini, QSettings.IniFormat)
    return QSettings("Lazarus", "Lazarus")


def get(s, key):
    d = DEFAULTS[key]
    return s.value(key, d, type=type(d))


def put(s, key, value):
    s.setValue(key, value)


RETIRED_GROUPS = ["backup"]      # stage-5 backup settings, removed; purged from the store


def purge_retired(s):
    for g in RETIRED_GROUPS:
        s.remove(g)
