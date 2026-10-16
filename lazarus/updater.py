"""Stage 19b: checking GitHub for a newer Lazarus, and updating to it. The engine (no Qt).

The ONLY network requests Lazarus makes:
  1. GET https://api.github.com/repos/Crazy8697/Lazarus/releases/latest (GitHub's documented
     "latest release" endpoint: drafts and prereleases are never returned), at startup when
     Settings › "Check for updates at startup" is on, or from Help › Check for updates…;
  2. when the user chooses Download and install: the release's .sha256 file and its Setup.exe.
Nothing is sent but the request itself (a plain "Lazarus" User-Agent, which GitHub requires): no
telemetry, no IDs, nothing about the user's files or settings.

A release is Crazy8697/Lazarus tag vX.Y.Z with assets Lazarus-Setup-X.Y.Z.exe and
Lazarus-Setup-X.Y.Z.exe.sha256. The download goes to %TEMP% (a partial file renamed into place
when complete) and is used only if its SHA-256 matches the published one; a missing or wrong
hash is refused. Setup is then started with Inno Setup's documented switches so it upgrades the
installed copy in place (same AppId), with one UAC prompt, keeping settings and the .pdf
registration, and starting Lazarus again (unelevated) when done; Lazarus exits itself first.

Dev / test hooks (never set in normal use): LAZARUS_UPDATE_URL points the check at another URL
(a local mock server); LAZARUS_UPDATE_DRYRUN=<file> writes the Setup command line there instead
of starting it; LAZARUS_UPDATE_TEST_ACTION=install answers the notice with "Download and install"
(scripted upgrade tests); LAZARUS_UPDATE_TIMEOUT overrides the network timeout (seconds)."""
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from . import __version__

LATEST_URL = "https://api.github.com/repos/Crazy8697/Lazarus/releases/latest"
RELEASES_PAGE = "https://github.com/Crazy8697/Lazarus/releases"
SETUP_ASSET = re.compile(r"^Lazarus-Setup-(\d+(?:\.\d+)*)\.exe$", re.I)
TIMEOUT = 10.0
CHUNK = 256 * 1024
SETUP_ARGS = ["/SILENT", "/SP-", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"]


class UpdateError(Exception):
    """Something the user should be told, in plain words."""


class Cancelled(Exception):
    pass


# ---------------- versions ----------------
def parse_version(text):
    """'v1.2.3' / '1.2' / '1.2.3-rc1' -> comparable tuple, or None if it isn't a version.
    A suffix (a prerelease) sorts below the same numbers without one."""
    m = re.fullmatch(r"\s*v?(\d+(?:\.\d+){0,3})(?:[-+.]?([0-9A-Za-z.]+))?\s*", str(text or ""))
    if not m:
        return None
    nums = [int(x) for x in m.group(1).split(".")]
    nums += [0] * (4 - len(nums))
    return tuple(nums) + ((0,) if m.group(2) else (1,))


def is_newer(tag, current=__version__):
    a, b = parse_version(tag), parse_version(current)
    return a is not None and b is not None and a > b


def can_install():
    """Only an installed (frozen) Lazarus updates itself: a source / dev run never installs."""
    return bool(getattr(sys, "frozen", False))


# ---------------- the check ----------------
def _timeout():
    try:
        return float(os.environ.get("LAZARUS_UPDATE_TIMEOUT") or TIMEOUT)
    except ValueError:
        return TIMEOUT


def _get(url, accept="application/vnd.github+json"):
    import urllib.request
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "Lazarus",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    return urllib.request.urlopen(req, timeout=_timeout())          # noqa: S310 (https / the test hook)


def _reason(e):
    import socket
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        if e.code in (403, 429) and (e.headers.get("X-RateLimit-Remaining") == "0" or e.code == 429):
            return "GitHub's rate limit for checks from this network was reached; try again later."
        return f"GitHub answered with an error ({e.code})."
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(e).lower():
        return "GitHub didn't answer in time."
    if isinstance(e, urllib.error.URLError):
        return "No connection to GitHub (offline, or blocked)."
    return f"{type(e).__name__}: {e}"


def check(current=__version__):
    """-> dict: status "newer" | "current" | "none" (nothing published yet) | "error";
    version, notes, setup_url, sha_url, setup_name, page; reason (for "error")."""
    import urllib.error
    url = os.environ.get("LAZARUS_UPDATE_URL") or LATEST_URL
    try:
        with _get(url) as r:
            body = r.read(2_000_000)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"status": "none"}
        return {"status": "error", "reason": _reason(e)}
    except Exception as e:                       # offline, timeout, anything: told, never raised
        return {"status": "error", "reason": _reason(e)}
    try:
        rel = json.loads(body.decode("utf-8"))
        if not isinstance(rel, dict):
            raise ValueError("not an object")
    except Exception:
        return {"status": "error", "reason": "GitHub's reply wasn't readable."}
    if rel.get("draft") or rel.get("prerelease"):
        return {"status": "none"}                # (the endpoint never returns these; a mock might)
    tag = str(rel.get("tag_name") or "")
    if parse_version(tag) is None:
        return {"status": "error", "reason": "The latest release has no version number."}
    out = {"version": tag.lstrip("vV"), "notes": str(rel.get("body") or "").strip(),
           "page": rel.get("html_url") or RELEASES_PAGE, "setup_url": None, "sha_url": None, "setup_name": None}
    assets = {a.get("name", ""): a.get("browser_download_url") for a in rel.get("assets") or [] if isinstance(a, dict)}
    for name, link in assets.items():
        if SETUP_ASSET.match(name):
            out["setup_name"], out["setup_url"] = name, link
            out["sha_url"] = assets.get(name + ".sha256")
            break
    out["status"] = "newer" if is_newer(tag, current) else "current"
    return out


# ---------------- download + verify ----------------
def expected_hash(sha_url):
    if not sha_url:
        raise UpdateError("The release has no .sha256 checksum, so the download can't be verified. Not installing.")
    try:
        with _get(sha_url, accept="application/octet-stream") as r:
            text = r.read(4096).decode("ascii", "replace")
    except Exception as e:
        raise UpdateError(f"Couldn't get the release's checksum: {_reason(e)}")
    m = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not m:
        raise UpdateError("The release's .sha256 file holds no SHA-256 checksum. Not installing.")
    return m.group(1).lower()


def download(info, progress=None, stopped=None, folder=None):
    """The release's Setup into %TEMP% (or folder), verified. -> its path. Raises UpdateError /
    Cancelled; nothing is left behind then."""
    if not info.get("setup_url") or not info.get("setup_name"):
        raise UpdateError("The release has no installer (Lazarus-Setup-<version>.exe) attached.")
    want = expected_hash(info.get("sha_url"))
    folder = Path(folder or Path(tempfile.gettempdir()) / "Lazarus-update")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / Path(info["setup_name"]).name
    part = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    try:
        with _get(info["setup_url"], accept="application/octet-stream") as r, open(part, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if stopped is not None and stopped():
                    raise Cancelled()
                buf = r.read(CHUNK)
                if not buf:
                    break
                f.write(buf)
                h.update(buf)
                done += len(buf)
                if progress is not None:
                    progress(done, total)
        if h.hexdigest() != want:
            raise UpdateError("The download doesn't match the release's SHA-256 checksum (damaged or not the "
                              "published file). It was deleted; nothing is installed.")
        os.replace(part, dest)
        return dest
    except (UpdateError, Cancelled):
        raise
    except Exception as e:
        raise UpdateError(f"The download failed: {_reason(e)}")
    finally:
        if part.exists():
            part.unlink()


def setup_command(setup):
    return [str(setup)] + SETUP_ARGS


def launch_setup(setup):
    """Start Setup the way Explorer would (ShellExecute: it asks for elevation itself, one UAC
    prompt). The dry-run hook writes the command line instead."""
    cmd = setup_command(setup)
    dry = os.environ.get("LAZARUS_UPDATE_DRYRUN")
    if dry:
        Path(dry).write_text(json.dumps(cmd), encoding="utf-8")
        return True
    import ctypes
    import subprocess
    r = ctypes.windll.shell32.ShellExecuteW(None, "open", cmd[0], subprocess.list2cmdline(cmd[1:]), None, 1)
    return r > 32
