"""Refuse to publish if a tracked file of Lazarus PDF carries something private: a personal Windows user
path, a home-network address or hostname, a token or key. build.ps1 -Release runs it before the
source goes to the public repo; run it by hand any time:

    python tools/privacy_check.py        (exit 1 and a list of file:line if anything is found)

Binary files (images, fonts) are skipped; the patterns are generic, so nothing private is written
here either."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = [
    ("a personal user path", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!Public\b|<|%|\$|\{)[A-Za-z0-9_.-]+", re.I)),
    ("a private network address", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
    ("a tailnet name", re.compile(r"\btailscale\b|\.ts\.net\b", re.I)),
    ("a home-lab hostname", re.compile(r"\bnexus(?:-config|\.)|\bhal9000\b|\bhal69000\b", re.I)),
    ("a personal domain", re.compile(r"darkrelay\.net", re.I)),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("an e-mail address", re.compile(r"\b[A-Za-z0-9._%+-]+@(?!example\b|sample\b|users\.noreply\.github\.com\b)[A-Za-z0-9-]+\.[A-Za-z]{2,}\b")),
]
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".ico", ".svg", ".ttf", ".pdf", ".docx", ".epub"}
# license texts quote their authors' addresses; those aren't ours
SKIP_PREFIX = ("licenses/", "LICENSE", "THIRD-PARTY-NOTICES.txt")


def tracked():
    out = subprocess.run(["git", "ls-files", "-z", "."], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def main():
    found = []
    for rel in tracked():
        if Path(rel).suffix.lower() in SKIP_SUFFIX or rel.startswith(SKIP_PREFIX):
            continue
        try:
            text = (ROOT / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for what, rx in PATTERNS:
                if rx.search(line):
                    found.append(f"{rel}:{n}: {what}")
    for f in found:
        print(f)
    print(f"privacy check: {len(found)} finding(s) in {len(tracked())} tracked files")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
