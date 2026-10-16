"""List the DLLs tesseract.exe actually needs from its own install folder.

Walks the PE import tables (pefile) from tesseract.exe, recursively, keeping only DLLs
that live in the Tesseract folder (system DLLs come from Windows). The build copies
exactly this set, plus tessdata/eng + osd, into dist\\LazarusPDF\\_internal\\tesseract.
The UB-Mannheim install also ships the training tools' DLLs; we leave those out.

usage: python tools/tess_deps.py "C:\\Program Files\\Tesseract-OCR"   -> one file name per line
"""
import sys
from pathlib import Path

import pefile


def needed(folder):
    folder = Path(folder)
    local = {p.name.lower(): p.name for p in folder.glob("*.dll")}
    todo, seen = ["tesseract.exe"], set()
    while todo:
        name = todo.pop()
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        pe = pefile.PE(str(folder / name), fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attr, []):
                dll = entry.dll.decode(errors="replace").lower()
                if dll in local and dll not in seen:
                    todo.append(local[dll])
        pe.close()
    return sorted(local[n] for n in seen if n in local)


if __name__ == "__main__":
    for n in needed(sys.argv[1] if len(sys.argv) > 1 else r"C:\Program Files\Tesseract-OCR"):
        print(n)
