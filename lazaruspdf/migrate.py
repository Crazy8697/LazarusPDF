"""Stage 22: Lazarus became Lazarus PDF. A Lazarus 0.21.0 (or older) user's data moves over once, at
the first start of Lazarus PDF (and right after Setup's upgrade, LazarusPDF.exe --migrate):

  settings     QSettings("Lazarus", "Lazarus")  (HKCU\\Software\\Lazarus\\Lazarus)
               -> QSettings("LazarusPDF", "LazarusPDF"). Only when the new store is still empty:
               every key copied, read back and compared; the old key is removed only when every
               value matched (otherwise it stays, untouched, and nothing is lost).
  signatures   %APPDATA%\\Lazarus\\signatures -> %APPDATA%\\LazarusPDF\\signatures (copied, compared
               byte for byte, then the old files removed; the old folders only when empty).
  .pdf         assoc.migrate_old(): the old registration -> the new names (see assoc.py for the one
               key Windows' default choice may still need).

A source (development) run never migrates the real registry, only an installed one does, and never
while LAZARUS_SETTINGS_INI is set (a test run: the registration then moves only inside a sandbox). Tests use
LAZARUS_SETTINGS_INI (the new store) + LAZARUS_OLD_SETTINGS_INI (an old store as an INI file), and
LAZARUS_ASSOC_ROOT (a sandbox for the registration). Every step is safe to repeat: run() at every
start finds nothing to do once it's done. Nothing about the user's files is read or logged."""
import filecmp
import os
import shutil
import sys
from pathlib import Path

from . import APP_ID, OLD_APP_ID


def _frozen():
    return bool(getattr(sys, "frozen", False))


def _stores():
    """(old, new) QSettings, or None when this run must not touch the real ones."""
    from PySide6.QtCore import QSettings
    ini = os.environ.get("LAZARUS_SETTINGS_INI")
    if ini:
        old_ini = os.environ.get("LAZARUS_OLD_SETTINGS_INI")
        if not old_ini:
            return None
        return QSettings(old_ini, QSettings.IniFormat), QSettings(ini, QSettings.IniFormat)
    if not _frozen():
        return None
    return QSettings(OLD_APP_ID, OLD_APP_ID), QSettings(APP_ID, APP_ID)


def migrate_settings(old, new):
    """Copy every key of `old` into an EMPTY `new`, verify, then clear `old`.
    -> "none" (nothing old) | "skipped" (new already has settings) | "moved" | "kept" (a value
    didn't read back the same: the old store is left as it was)."""
    keys = old.allKeys()
    if not keys:
        return "none"
    if new.allKeys():
        return "skipped"
    for k in keys:
        new.setValue(k, old.value(k))
    new.sync()
    if any(new.value(k) != old.value(k) for k in keys) or sorted(new.allKeys()) != sorted(keys):
        return "kept"
    old.clear()
    old.sync()
    return "moved"


def _drop_empty_old_keys():
    """HKCU\\Software\\Lazarus\\Lazarus, then HKCU\\Software\\Lazarus, when they hold nothing."""
    from .assoc import Registry
    reg = Registry()
    reg.delete_if_empty(rf"Software\{OLD_APP_ID}\{OLD_APP_ID}")
    reg.delete_if_empty(rf"Software\{OLD_APP_ID}")


def _signature_dirs():
    ini = os.environ.get("LAZARUS_SETTINGS_INI")
    if ini:
        old_ini = os.environ.get("LAZARUS_OLD_SETTINGS_INI")
        if not old_ini:
            return None
        return Path(old_ini).parent / "lazarus-signatures", Path(ini).parent / "lazaruspdf-signatures"
    if not _frozen() or not os.environ.get("APPDATA"):
        return None
    base = Path(os.environ["APPDATA"])
    return base / OLD_APP_ID / "signatures", base / APP_ID / "signatures"


def migrate_signatures(old_dir, new_dir):
    """-> number of signature files moved (a name already in the new folder is left in the old one)."""
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    if not old_dir.is_dir():
        return 0
    moved = 0
    for f in sorted(old_dir.iterdir()):
        if not f.is_file():
            continue
        dest = new_dir / f.name
        if dest.exists():
            continue
        new_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
        if filecmp.cmp(f, dest, shallow=False):
            f.unlink()
            moved += 1
        else:
            dest.unlink()
    for d in (old_dir, old_dir.parent):
        try:
            d.rmdir()                       # only when empty
        except OSError:
            break
    return moved


def run():
    """Every start (and Setup's --migrate). -> dict of what happened (tests)."""
    out = {}
    try:
        stores = _stores()
        if stores is not None:
            out["settings"] = migrate_settings(*stores)
    except Exception as e:                  # never stop the program from starting
        out["settings_error"] = type(e).__name__
    try:
        dirs = _signature_dirs()
        if dirs is not None:
            out["signatures"] = migrate_signatures(*dirs)
    except Exception as e:
        out["signatures_error"] = type(e).__name__
    try:
        from . import assoc
        reg = assoc.registry()
        # a sandbox (tests), or the real registry only in a real (installed, not test-settings) run
        if reg.root is not None or (_frozen() and not os.environ.get("LAZARUS_SETTINGS_INI")):
            out["pdf_registration"] = assoc.migrate_old(reg)
            out["forwarders_removed"] = assoc.cleanup_forwarders(reg)
    except Exception as e:
        out["pdf_registration_error"] = type(e).__name__
    if _frozen() and not os.environ.get("LAZARUS_SETTINGS_INI") and out.get("settings") in ("moved", "none"):
        try:
            _drop_empty_old_keys()
        except Exception:
            pass
    return out
