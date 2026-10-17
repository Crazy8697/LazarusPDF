"""The program's icons (stage 22.3): Material Design Icons 6 from QtAwesome, loaded PRIVATELY.

QtAwesome's shared instance (qtawesome.icon) copies every font it bundles (12 fonts, 24 files) into
the Windows user fonts folder and registers them in HKCU on every PC it runs on. Lazarus PDF uses
one of them (prefix mdi6), so it keeps its own IconicFont holding only that font, loaded from an
explicit folder: QtAwesome then reads the file into the program's own Qt font database and never
installs anything. The folder is QtAwesome's own fonts folder: in the source tree's venv for a dev
run, and in an installed copy the bundled one (the spec ships only this font; build.ps1 refuses a
build with any other). fontclean.py removes what older versions left in the user fonts folder."""
import os

MDI_TTF = "materialdesignicons6-webfont-6.9.96.ttf"
MDI_CHARMAP = "materialdesignicons6-webfont-charmap-6.9.96.json"
_ICONIC = None


def fonts_dir():
    import qtawesome
    return os.path.join(os.path.dirname(qtawesome.__file__), "fonts")


def iconic():
    """Our own QtAwesome font set (mdi6 only), created once, after the QApplication exists."""
    global _ICONIC
    if _ICONIC is None:
        from qtawesome.iconic_font import IconicFont
        _ICONIC = IconicFont(("mdi6", MDI_TTF, MDI_CHARMAP, fonts_dir()))
    return _ICONIC


def icon(*names, **kwargs):
    """Drop-in for qtawesome.icon: same names ("mdi6.<name>") and options, from the private set."""
    return iconic().icon(*names, **kwargs)
