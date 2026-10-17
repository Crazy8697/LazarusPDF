"""Stage 22.3: remove the icon fonts older Lazarus PDF versions (through QtAwesome's shared instance)
copied into the user's Windows fonts folder and registered in HKCU.

QtAwesome 1.4.2 (the only version Lazarus / Lazarus PDF ever shipped) copied all 24 files of its
fonts folder (12 fonts + their charmaps) to %LOCALAPPDATA%\\Microsoft\\Windows\\Fonts, loaded each
font into the session and wrote one value per font under
HKCU\\Software\\Microsoft\\Windows NT\\CurrentVersion\\Fonts ("<font name> (TrueType)" = "<file name>").

What this removes, and nothing else:
  - a file in that folder ONLY when its name is one of those 24 AND its bytes are QtAwesome 1.4.2's
    (SHA-256 below). Another font with the same name, or any other font, is never touched;
  - a registry value ONLY when its data names one of those files and that file is (or was, just now)
    a matching QtAwesome copy. A value naming a file that isn't QtAwesome's is left alone.
A file that is locked or in use (Windows loads registered fonts at sign-in) stays; its registry value
goes, so after the next sign-in the file is free, and the next start removes it. Nothing is ever
raised at the user. When nothing matching is left, the settings record it and the check stops.

Runs as the user: every start of an installed copy (gui.run) and the uninstaller
(LazarusPDF.exe --clean-fonts). A source run and a test run (LAZARUS_SETTINGS_INI) never touch the
real folder or registry, and neither does any run with a test hook set (LAZARUS_SETTINGS_INI,
LAZARUS_ASSOC_ROOT, LAZARUS_OLD_SETTINGS_INI: e.g. a test install's uninstaller): tests point it at a
sandbox with LAZARUS_FONTCLEAN_DIR (a folder) and LAZARUS_FONTCLEAN_REG (a key below HKCU). Note: a program still using QtAwesome's shared instance
can copy the same fonts back; harmless, they are its own."""
import hashlib
import os
import sys
import winreg
from pathlib import Path

REG_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"
# any of these set = a test run: the real folder and registry are never touched (only the sandbox above)
TEST_HOOKS = ("LAZARUS_SETTINGS_INI", "LAZARUS_ASSOC_ROOT", "LAZARUS_OLD_SETTINGS_INI")
DONE_KEY = "icon_fonts_cleaned"
# QtAwesome 1.4.2's bundled fonts folder: file name -> SHA-256
KNOWN = {
    "codicon-0.0.36.ttf": "360527575395ad70f940eff1e43eb7c2ec64f4e69f89dc5861b73420d2a578f0",
    "codicon-charmap-0.0.36.json": "84a8d9e2c5cb0587df5876714fd5012c8b21973b81203865418effeec05123d0",
    "elusiveicons-webfont-2.0.ttf": "29b93f3fe666bf73dcc857869aee47532f710c0a16959c6275dc5bc2e3be4c9e",
    "elusiveicons-webfont-charmap-2.0.json": "be05507fc31c47f89910983b030fec906ee86861a79b8e03b1f24029699679e8",
    "fontawesome5-brands-webfont-5.15.4.ttf": "cda59d6efffa685830fd95b55f64ae9cb51279cd34b2410b69f84c7ec30157d9",
    "fontawesome5-brands-webfont-charmap-5.15.4.json": "d00ae829ec115926c670397b7bc470656fae8dc2bdd5b48a81c753dc854ca1fa",
    "fontawesome5-regular-webfont-5.15.4.ttf": "fb5143dfe010d47b0fdedd36e4bac1ac8ff2e39b9f20b64891a88a7a219422f5",
    "fontawesome5-regular-webfont-charmap-5.15.4.json": "a6d7282a3908283443feedafd07083e33bb7e2c56e813542d4ef79f8dadd2309",
    "fontawesome5-solid-webfont-5.15.4.ttf": "4c5431bc1b2abf8aed5c614f2f32120d666bdda640cb1a7678d70e08cdd4323f",
    "fontawesome5-solid-webfont-charmap-5.15.4.json": "1b0cac3ed4d3b144965a45aba9ad31e8654e4735d94b4ed9bced1b5eea9804d3",
    "fontawesome6-brands-webfont-6.7.2.ttf": "808443ae6c8204395add8543da8a90a60b9376fb0f87ed8e8ea37d109596d805",
    "fontawesome6-brands-webfont-charmap-6.7.2.json": "8f7ba0f01ed36b3f7a0c456556ed1f80d18f306006a9cf79929b2366e4d06649",
    "fontawesome6-regular-webfont-6.7.2.ttf": "cffc8c04d8257c600e0533bdf7cfbf0775b801b50bc39a6f8122ee0c48a1373d",
    "fontawesome6-regular-webfont-charmap-6.7.2.json": "769c282ba20a9cf5b672e90bbe6ed6d52ebb1c0fbbf5614a42ac01f2f12f9365",
    "fontawesome6-solid-webfont-6.7.2.ttf": "220d6d592b543eb991b8c11137ef0082c27d6ff6fdaa25e4335945f57a487c6c",
    "fontawesome6-solid-webfont-charmap-6.7.2.json": "48607f738a71763a3557abe077133ff36b48b1bfa27d2f0d25b5a5c1f8a89127",
    "materialdesignicons5-webfont-5.9.55.ttf": "f89ca374a7545105f70a6345919083f9011badade1ff17f5e99f97853e7c33da",
    "materialdesignicons5-webfont-charmap-5.9.55.json": "0eb43894facba7a61299088a385fcc6f293976501339682d1a2e0bdea248f68a",
    "materialdesignicons6-webfont-6.9.96.ttf": "be825c1294d3c4f046c8e496b18f8f0c7d2864e339c81bdab9297f17ffac1938",
    "materialdesignicons6-webfont-charmap-6.9.96.json": "6317242191c4c889dec2c174f98dda0f309745d28c23c50a19bbbaf02df930a9",
    "phosphor-1.3.0.ttf": "351874de64758f3b7fb405cd078ac0ed5ffb9717fb5b9b10013ac03d4c13680b",
    "phosphor-charmap-1.3.0.json": "cfb725868f72d741422002f1ed4f870bdddebc32344a6407b8becb6c841b92ca",
    "remixicon-2.5.0.ttf": "0ac8bc3ccea26ed47e96844ef26675969c669714d723c081101e51799d501520",
    "remixicon-charmap-2.5.0.json": "b90a112e76fc5f7f64d20bb5ec422fc9c7511d1f32be61c0f7d3b7a3933d4c0a",
}


def _sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def is_qtawesome_copy(p):
    p = Path(p)
    try:
        return p.name in KNOWN and p.is_file() and _sha256(p) == KNOWN[p.name]
    except OSError:
        return False


def places():
    """(fonts folder, registry key below HKCU, sandboxed?) or None when this run must not touch them."""
    d, r = os.environ.get("LAZARUS_FONTCLEAN_DIR"), os.environ.get("LAZARUS_FONTCLEAN_REG")
    if d and r:
        return Path(d), r, True
    if not getattr(sys, "frozen", False) or any(os.environ.get(v) for v in TEST_HOOKS):
        return None                    # a source run, or ANY test run (an uninstall in a test included)
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "Microsoft" / "Windows" / "Fonts", REG_PATH, False


def _values(key):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            out, i = [], 0
            while True:
                try:
                    name, data, kind = winreg.EnumValue(k, i)
                except OSError:
                    return out
                out.append((name, data, kind))
                i += 1
    except OSError:
        return []


def clean(folder, key, sandbox):
    """-> {"files_removed", "values_removed", "left"}: left = matching files still there (locked)."""
    folder = Path(folder)
    matching = {n for n in KNOWN if is_qtawesome_copy(folder / n)}
    removed_files, removed_values, left = [], [], []
    # registry first: a value naming a matching copy goes (so a locked file is free after sign-in)
    for name, data, kind in _values(key):
        if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(data, str):
            continue
        target = Path(os.path.expandvars(data))
        fname = target.name
        if fname not in matching:
            continue
        if target.is_absolute() and os.path.normcase(str(target.parent)) != os.path.normcase(str(folder)):
            continue                                    # names a file somewhere else: not ours to judge
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
            removed_values.append(fname)
        except OSError:
            pass
    for n in sorted(matching):
        p = folder / n
        if not sandbox and p.suffix.lower() == ".ttf":
            try:
                import ctypes
                ctypes.windll.gdi32.RemoveFontResourceW(str(p))     # out of this session's font table
            except Exception:
                pass
        try:
            p.unlink()
            removed_files.append(n)
        except OSError:
            left.append(n)                              # locked / in use: next start
    return {"files_removed": removed_files, "values_removed": removed_values, "left": left}


def run(settings=None, force=False):
    """Every start (gui.run) and --clean-fonts (the uninstaller, force=True). Never raises.
    -> the result dict, or None when skipped (already done, or not a real/sandboxed run)."""
    try:
        where = places()
        if where is None:
            return None
        if settings is not None and not force and settings.value(DONE_KEY, False, type=bool):
            return None
        res = clean(*where)
        if settings is not None and not res["left"]:
            settings.setValue(DONE_KEY, True)
        return res
    except Exception:
        return None
