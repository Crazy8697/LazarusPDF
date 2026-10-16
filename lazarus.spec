# PyInstaller spec for Lazarus (onedir, windowed).
# Build: .\build.ps1   ->  dist\Lazarus\Lazarus.exe   (-Setup packages it as an installer)
#
# Tesseract is NOT passed to Analysis. PyInstaller 6 treats DLLs given as data as
# binaries and copies their dependencies again at the top level (~140 MB of duplicates
# in the first build). Instead, after COLLECT, the exact files tesseract.exe needs
# (tools/tess_deps.py walks its import table) plus tessdata eng + osd are copied once into
# dist\Lazarus\_internal\tesseract. lazarus/tess.py finds them there via sys._MEIPASS.
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

# Qt modules Lazarus never uses (it needs QtCore/QtGui/QtWidgets, and QtNetwork for the
# single-instance local socket, stage 12).
QT_EXCLUDE = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
              "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtDataVisualization",
              "PySide6.QtCharts", "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDBus"]

a = Analysis(
    ["run_lazarus.pyw"],
    pathex=["."],
    datas=[("lazarus/assets/lazarus.ico", "lazarus/assets"),
           ("lazarus/assets/darkrelay-banner.jpg", "lazarus/assets"),     # the About box (stage 20)
           ("LICENSE", "."), ("THIRD-PARTY-NOTICES.txt", ".")]          # Help > Licenses
          + collect_data_files("docx")          # default.docx template etc.
          + collect_data_files("qtawesome"),    # icon fonts for the PDF Editor
    hiddenimports=["ebooklib.epub", "lxml.etree",
                   "comtypes", "comtypes.client", "comtypes.client.dynamic", "comtypes.automation",   # WIA (stage 15)
                   "pdf2docx", "fitz", "fontTools", "fontTools.ttLib"],                                # Word export (16)
    excludes=QT_EXCLUDE + ["tkinter", "matplotlib", "IPython", "pytest"],
    noarchive=False,
)

# Binaries pulled in by Qt plugins / OpenCV that nothing in Lazarus loads.
DROP = ("opencv_videoio_ffmpeg",                 # OpenCV video I/O (29 MB)
        "opengl32sw",                            # Qt software OpenGL fallback (20 MB)
        "qt6quick", "qt6qml", "qt6pdf", "qt6opengl", "qt6datavisualization",
        "qt6virtualkeyboard", "qt6charts", "qt6dbus",
        "qpdf.dll",                              # imageformats plugin that needs Qt6Pdf
        "qtvirtualkeyboardplugin")
dropped = [b for b in a.binaries if any(k in b[0].lower().replace("\\", "/").split("/")[-1] for k in DROP)]
a.binaries = [b for b in a.binaries if b not in dropped]
print("Lazarus spec: dropped binaries:", ", ".join(sorted({Path(b[0]).name for b in dropped})) or "none")

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Lazarus",
    icon="lazarus/assets/lazarus.ico",
    console=False,
    upx=False,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Lazarus", upx=False)

# --- bundle Tesseract once, after COLLECT ---
dest = Path(DISTPATH) / "Lazarus" / "_internal" / "tesseract"
shutil.rmtree(dest, ignore_errors=True)
(dest / "tessdata").mkdir(parents=True)
files = ["tesseract.exe"] + needed(tess_dir)
for f in files:
    shutil.copy2(tess_dir / f, dest / f)
for f in ("eng.traineddata", "osd.traineddata", "pdf.ttf"):    # pdf.ttf: Tesseract's PDF output (OCR layer)
    shutil.copy2(tess_dir / "tessdata" / f, dest / "tessdata" / f)
(dest / "tessdata" / "configs").mkdir()
for f in ("pdf", "hocr", "txt"):                                    # the output modes Lazarus asks for
    if (tess_dir / "tessdata" / "configs" / f).exists():
        shutil.copy2(tess_dir / "tessdata" / "configs" / f, dest / "tessdata" / "configs" / f)
print(f"Lazarus spec: bundled Tesseract: {len(files)} files + eng/osd traineddata, pdf.ttf, configs -> {dest}")
