# PyInstaller spec for Lazarus PDF (onedir, windowed).
# Build: .\build.ps1   ->  dist\LazarusPDF\LazarusPDF.exe   (-Setup packages it as an installer)
#
# Tesseract is NOT passed to Analysis. PyInstaller 6 treats DLLs given as data as
# binaries and copies their dependencies again at the top level (~140 MB of duplicates
# in the first build). Instead, after COLLECT, the exact files tesseract.exe needs
# (tools/tess_deps.py walks its import table) plus tessdata eng + osd are copied once into
# dist\LazarusPDF\_internal\tesseract. lazaruspdf/tess.py finds them there via sys._MEIPASS.
# Source of the copy: $env:TESSERACT_DIR, default C:\Program Files\Tesseract-OCR.
import os
import shutil
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

HERE = Path(SPECPATH)
sys.path.insert(0, str(HERE / "tools"))
from tess_deps import needed  # noqa: E402

tess_dir = Path(os.environ.get("TESSERACT_DIR", r"C:\Program Files\Tesseract-OCR"))
if not (tess_dir / "tesseract.exe").is_file():
    raise SystemExit(f"tesseract.exe not found in {tess_dir}; set TESSERACT_DIR")

# Qt modules Lazarus PDF never uses (it needs QtCore/QtGui/QtWidgets, and QtNetwork for the
# single-instance local socket, stage 12).
QT_EXCLUDE = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
              "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtDataVisualization",
              "PySide6.QtCharts", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDBus"]

a = Analysis(
    ["run_lazaruspdf.pyw"],
    pathex=["."],
    datas=[("lazaruspdf/assets/lazaruspdf.ico", "lazaruspdf/assets"),
           ("lazaruspdf/assets/darkrelay-banner.jpg", "lazaruspdf/assets"),     # the About box (stage 20)
           ("LICENSE", "."), ("THIRD-PARTY-NOTICES.txt", ".")]          # Help > Licenses
          + collect_data_files("docx")          # default.docx template etc.
          # only the icon font Lazarus PDF uses (Material Design Icons 6, lazaruspdf/icons.py)
          + collect_data_files("qtawesome", includes=["fonts/materialdesignicons6-webfont-6.9.96.ttf",
                                                       "fonts/materialdesignicons6-webfont-charmap-6.9.96.json"]),
    hiddenimports=["ebooklib.epub", "lxml.etree",
                   "comtypes", "comtypes.client", "comtypes.client.dynamic", "comtypes.automation",   # WIA (stage 15)
                   "pdf2docx", "fitz", "fontTools", "fontTools.ttLib"],                                # Word export (16)
    excludes=QT_EXCLUDE + ["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)

# PyInstaller's own QtAwesome hook collects every font QtAwesome bundles, whatever the datas above ask
# for. Only Material Design Icons 6 is used and covered by THIRD-PARTY-NOTICES.txt, so the rest are
# dropped here (build.ps1 refuses a build that ships any other icon font). Stage 22.3.
KEEP_FONT = "materialdesignicons6-webfont"
a.datas = [d for d in a.datas
           if not d[0].replace("\\", "/").startswith("qtawesome/fonts/") or KEEP_FONT in d[0]]
print("LazarusPDF spec: QtAwesome fonts kept:",
      ", ".join(sorted(Path(d[0]).name for d in a.datas
                       if d[0].replace("\\", "/").startswith("qtawesome/fonts/"))))

# Binaries pulled in by Qt plugins / OpenCV that nothing in Lazarus PDF loads.
DROP = ("opencv_videoio_ffmpeg",                 # OpenCV video I/O (29 MB)
        "opengl32sw",                            # Qt software OpenGL fallback (20 MB)
        "qt6quick", "qt6qml", "qt6pdf", "qt6opengl", "qt6datavisualization",
        "qt6virtualkeyboard", "qt6charts", "qt6dbus",
        "qpdf.dll",                              # imageformats plugin that needs Qt6Pdf
        "qtvirtualkeyboardplugin")
dropped = [b for b in a.binaries if any(k in b[0].lower().replace("\\", "/").split("/")[-1] for k in DROP)]
a.binaries = [b for b in a.binaries if b not in dropped]
print("LazarusPDF spec: dropped binaries:", ", ".join(sorted({Path(b[0]).name for b in dropped})) or "none")

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="LazarusPDF",
    icon="lazaruspdf/assets/lazaruspdf.ico",
    console=False,
    upx=False,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="LazarusPDF", upx=False)

# --- bundle Tesseract once, after COLLECT ---
dest = Path(DISTPATH) / "LazarusPDF" / "_internal" / "tesseract"
shutil.rmtree(dest, ignore_errors=True)
(dest / "tessdata").mkdir(parents=True)
files = ["tesseract.exe"] + needed(tess_dir)
for f in files:
    shutil.copy2(tess_dir / f, dest / f)
for f in ("eng.traineddata", "osd.traineddata", "pdf.ttf"):    # pdf.ttf: Tesseract's PDF output (OCR layer)
    shutil.copy2(tess_dir / "tessdata" / f, dest / "tessdata" / f)
(dest / "tessdata" / "configs").mkdir()
for f in ("pdf", "hocr", "txt"):                                    # the output modes Lazarus PDF asks for
    if (tess_dir / "tessdata" / "configs" / f).exists():
        shutil.copy2(tess_dir / "tessdata" / "configs" / f, dest / "tessdata" / "configs" / f)
print(f"LazarusPDF spec: bundled Tesseract: {len(files)} files + eng/osd traineddata, pdf.ttf, configs -> {dest}")
