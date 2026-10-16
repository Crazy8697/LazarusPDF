"""DEV / TEST ONLY: validate exported EPUBs with W3C EPUBCheck (stage 18b).

Not used by Lazarus PDF, its build or its self-test: EPUB export has no Java or EPUBCheck
dependency. The EPUBCheck jar (and Java) live outside the repo, e.g. in a scratch folder.

    python tools/epubcheck_dev.py --jar <path to epubcheck.jar> FILE.epub [FILE.epub ...]

Runs java silently (no console window), prints only the counts and the messages for each file
(by file name, not path), and exits 1 if any file has errors or fatals."""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def find_java():
    j = shutil.which("java")
    if j:
        return j
    for base in (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft",
                 Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Eclipse Adoptium"):
        for exe in sorted(base.glob("*/bin/java.exe"), reverse=True):
            return str(exe)
    return None


def check(java, jar, epub):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    p = subprocess.run([java, "-Djava.awt.headless=true", "-jar", str(jar), Path(epub).name],
                       cwd=str(Path(epub).parent), capture_output=True, text=True, creationflags=flags)
    text = p.stdout + p.stderr
    m = re.search(r"Messages:\s*(\d+) fatals? / (\d+) errors? / (\d+) warnings?", text)
    counts = tuple(int(x) for x in m.groups()) if m else None
    msgs = [ln for ln in text.splitlines() if re.match(r"(FATAL|ERROR|WARNING)\(", ln)]
    return counts, msgs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--jar", required=True)
    ap.add_argument("epubs", nargs="+")
    a = ap.parse_args()
    java = find_java()
    if not java:
        sys.exit("No Java found (EPUBCheck needs a Java runtime; Lazarus PDF itself doesn't).")
    bad = 0
    for e in a.epubs:
        counts, msgs = check(java, a.jar, e)
        name = Path(e).name
        if counts is None:
            print(f"{name}: EPUBCheck didn't report (not an EPUB?)")
            bad += 1
            continue
        print(f"{name}: {counts[0]} fatal / {counts[1]} errors / {counts[2]} warnings")
        for ln in msgs:
            print("   " + ln)
        bad += counts[0] + counts[1] > 0
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
