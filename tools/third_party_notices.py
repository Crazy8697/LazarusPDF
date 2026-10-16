"""Regenerate THIRD-PARTY-NOTICES.txt: every component the Windows build ships, its version and
license, then the full license texts (from the installed packages' own license files, and the
canonical texts in licenses/). Run with the build venv after a dependency changes:

    python tools/third_party_notices.py

The table of non-Python components (Qt, Tesseract and the libraries it loads, the Python runtime,
the icon fonts) is kept by hand below."""
import importlib.metadata as md
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "THIRD-PARTY-NOTICES.txt"
LIC = ROOT / "licenses"

# (distribution, shown as, upstream)
PYTHON = [
    ("PyMuPDF", "PyMuPDF (with MuPDF)", "https://github.com/pymupdf/PyMuPDF"),
    ("PySide6_Essentials", "PySide6 / Qt for Python (Qt 6)", "https://www.qt.io/qt-for-python"),
    ("shiboken6", "Shiboken6", "https://www.qt.io/qt-for-python"),
    ("pytesseract", "pytesseract", "https://github.com/madmaze/pytesseract"),
    ("opencv-python-headless", "OpenCV (opencv-python-headless)", "https://github.com/opencv/opencv-python"),
    ("numpy", "NumPy", "https://numpy.org"),
    ("Pillow", "Pillow", "https://python-pillow.org"),
    ("python-docx", "python-docx", "https://github.com/python-openxml/python-docx"),
    ("EbookLib", "EbookLib", "https://github.com/aerkalov/ebooklib"),
    ("lxml", "lxml (with libxml2 / libxslt)", "https://lxml.de"),
    ("QtAwesome", "QtAwesome", "https://github.com/spyder-ide/qtawesome"),
    ("QtPy", "QtPy", "https://github.com/spyder-ide/qtpy"),
    ("comtypes", "comtypes", "https://github.com/enthought/comtypes"),
    ("pdf2docx", "pdf2docx", "https://github.com/ArtifexSoftware/pdf2docx"),
    ("fonttools", "fontTools", "https://github.com/fonttools/fonttools"),
    ("packaging", "packaging", "https://github.com/pypa/packaging"),
    ("six", "six", "https://github.com/benjaminp/six"),
    ("typing_extensions", "typing_extensions", "https://github.com/python/typing_extensions"),
    ("pyinstaller", "PyInstaller bootloader", "https://pyinstaller.org"),
]

# (component, version, license, upstream / source, canonical text file or None)
OTHER = [
    ("Python runtime", "3.11", "PSF-2.0", "https://www.python.org", "python-psf.txt"),
    ("OpenSSL (libssl, libcrypto)", "3.0", "Apache-2.0", "https://www.openssl.org", "apache-2.0.txt"),
    ("libffi", "3.4", "MIT", "https://sourceware.org/libffi/", None),
    ("Microsoft Visual C++ Runtime / Universal CRT (VCRUNTIME140, ucrtbase, api-ms-win-*)", "", "Microsoft redistributable",
     "https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files", None),
    ("Qt (Qt6Core, Qt6Gui, Qt6Widgets, Qt6Network, plugins)", "6.12", "LGPL-3.0-only (also GPL-2.0 / GPL-3.0)",
     "Source: https://download.qt.io/official_releases/qt/ ; third-party code inside Qt: "
     "https://doc.qt.io/qt-6/licenses-used-in-qt.html", "lgpl-3.0.txt"),
    ("Tesseract OCR (tesseract.exe, libtesseract)", "5.4.0", "Apache-2.0", "https://github.com/tesseract-ocr/tesseract",
     "apache-2.0.txt"),
    ("tessdata eng, osd", "", "Apache-2.0", "https://github.com/tesseract-ocr/tessdata_fast", "apache-2.0.txt"),
    ("Leptonica", "1.84.1", "BSD-2-Clause (Leptonica license)", "http://www.leptonica.org", None),
    ("libarchive", "", "BSD-2-Clause", "https://libarchive.org", None),
    ("libb2 (BLAKE2)", "", "CC0-1.0", "https://github.com/BLAKE2/libb2", None),
    ("bzip2 (libbz2)", "", "bzip2-1.0.6", "https://sourceware.org/bzip2/", None),
    ("libdeflate", "", "MIT", "https://github.com/ebiggers/libdeflate", None),
    ("Expat", "", "MIT", "https://libexpat.github.io", None),
    ("GCC runtime (libgcc_s_seh, libstdc++)", "", "GPL-3.0 with the GCC Runtime Library Exception 3.1",
     "https://gcc.gnu.org", "gpl-3.0.txt"),
    ("giflib", "5.2.1", "MIT", "https://giflib.sourceforge.net", None),
    ("GNU libiconv", "", "LGPL-2.1-or-later", "Source: https://ftp.gnu.org/pub/gnu/libiconv/", "lgpl-2.1.txt"),
    ("JBIG-KIT (libjbig)", "", "GPL-2.0-or-later", "Source: https://www.cl.cam.ac.uk/~mgk25/jbigkit/", "gpl-2.0.txt"),
    ("libjpeg-turbo", "3.0.1", "IJG AND BSD-3-Clause AND Zlib", "https://libjpeg-turbo.org", None),
    ("LERC (libLerc)", "", "Apache-2.0", "https://github.com/Esri/lerc", "apache-2.0.txt"),
    ("LZ4", "", "BSD-2-Clause", "https://github.com/lz4/lz4", None),
    ("XZ Utils (liblzma)", "", "0BSD / public domain", "https://tukaani.org/xz/", None),
    ("OpenJPEG", "2.5.2", "BSD-2-Clause", "https://www.openjpeg.org", None),
    ("libpng", "1.6.43", "libpng-2.0", "http://www.libpng.org", None),
    ("libwebp, libwebpmux, libsharpyuv", "1.4.0", "BSD-3-Clause", "https://chromium.googlesource.com/webm/libwebp", None),
    ("libtiff", "4.6.0", "libtiff", "https://libtiff.gitlab.io/libtiff/", None),
    ("winpthreads", "", "MIT AND ZPL-2.1", "https://www.mingw-w64.org", None),
    ("Zstandard (libzstd)", "", "BSD-3-Clause", "https://facebook.github.io/zstd/", None),
    ("zlib", "1.3", "Zlib", "https://zlib.net", None),
    ("Material Design Icons font (used for Lazarus PDF's icons; QtAwesome)", "6.9.96", "Apache-2.0",
     "https://pictogrammers.com/library/mdi/", "apache-2.0.txt"),
    ("Font Awesome fonts (bundled with QtAwesome)", "5.15.4 / 6.7.2", "SIL OFL 1.1 (fonts)", "https://fontawesome.com",
     "ofl-1.1.txt"),
    ("Elusive Icons font (bundled with QtAwesome)", "2.0", "SIL OFL 1.1", "https://github.com/dovy/elusive-icons",
     "ofl-1.1.txt"),
    ("Phosphor icons font (bundled with QtAwesome)", "1.3.0", "MIT", "https://phosphoricons.com", None),
    ("Remix Icon font (bundled with QtAwesome)", "2.5.0", "Apache-2.0", "https://remixicon.com", "apache-2.0.txt"),
    ("Codicons font (bundled with QtAwesome)", "0.0.36", "CC-BY-4.0", "https://github.com/microsoft/vscode-codicons",
     "cc-by-4.0.txt"),
]


def dist_license(name):
    m = md.metadata(name)
    lic = m.get("License-Expression") or m.get("License") or ""
    lic = lic.splitlines()[0][:120] if lic else ""
    texts = []
    for f in md.files(name) or []:
        s = str(f)
        base = s.replace("\\", "/").rsplit("/", 1)[-1].upper()
        if (base.startswith(("LICEN", "COPYING", "NOTICE", "AUTHORS")) and ".dist-info" in s
                and not base.endswith(".PY")):
            try:
                texts.append((s, Path(f.locate()).read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    return m["Version"], lic, texts


def main():
    lines = ["LAZARUS PDF - THIRD-PARTY NOTICES", "",
             "Lazarus PDF is free software under the GNU Affero General Public License v3.0 (see LICENSE).",
             "Source code: https://github.com/Crazy8697/LazarusPDF",
             "",
             "The Windows build of Lazarus PDF includes the components below, each under its own license.",
             "All of them allow distribution together with an AGPL-3.0 program.",
             "",
             "Corresponding source for the components under the GPL / LGPL (Qt and PySide6 / Shiboken6,",
             "GNU libiconv, JBIG-KIT, the GCC runtime) is available from the upstream locations listed",
             "below for the versions shipped; on request the Lazarus PDF maintainer will provide it too, for at",
             "least three years after a release: open an issue at https://github.com/Crazy8697/LazarusPDF.",
             "Qt and the other libraries are shipped as separate files (DLLs) in the program folder, so they",
             "can be replaced with modified versions.",
             "", "=" * 78, "COMPONENTS", "=" * 78, ""]
    full = []
    for dist, shown, url in PYTHON:
        ver, lic, texts = dist_license(dist)
        if dist == "pyinstaller":
            lic = "GPL-2.0-or-later with the PyInstaller bootloader exception"
        lines.append(f"- {shown} {ver}: {lic}  ({url})")
        for path, text in texts:
            full.append((f"{shown} {ver}: {path.replace(chr(92), '/').split('/', 1)[-1]}", text))
    for comp, ver, lic, url, text in OTHER:
        lines.append(f"- {comp}{(' ' + ver) if ver else ''}: {lic}  ({url})")
    lines += ["", "=" * 78, "LICENSE TEXTS", "=" * 78, ""]
    for title, text in full:
        lines += ["-" * 78, title, "-" * 78, text.strip(), ""]
    seen = set()
    for comp, ver, lic, url, text in OTHER:
        if text and text not in seen:
            seen.add(text)
            lines += ["-" * 78, f"{text[:-4].upper()} (canonical text)", "-" * 78,
                      (LIC / text).read_text(encoding="utf-8").strip(), ""]
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT.name}: {len(PYTHON)} Python packages, {len(OTHER)} other components, "
          f"{len(full) + len(seen)} license texts")


if __name__ == "__main__":
    sys.exit(main())
