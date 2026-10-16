"""DEV / TEST ONLY: run a command UNELEVATED from an elevated shell, silently, and wait for it.

An elevated process can write into Program Files, which would hide exactly the bugs an
installed copy has to be tested for. This makes the token `runas /trustlevel:0x20000` makes
(the Safer "Normal User" level: the Administrators group becomes deny-only, admin privileges
are dropped), lowers its integrity to Medium (an ordinary unelevated process), and starts the
command with it, hidden (no console window, no focus change).

    python tools/run_unelevated.py [--cwd DIR] [--env NAME=VALUE ...] -- PROGRAM ARGS...

Exits with the program's exit code. --check runs `whoami /groups` that way and prints whether
the token is Medium integrity with Administrators deny-only (proof of de-elevation)."""
import argparse
import ctypes
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SAFER_SCOPEID_USER = 2
SAFER_LEVELID_NORMALUSER = 0x20000
SAFER_LEVEL_OPEN = 1
TokenIntegrityLevel = 25
SE_GROUP_INTEGRITY = 0x20
CREATE_NO_WINDOW = 0x08000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESHOWWINDOW = 0x1
STARTF_FORCEOFFFEEDBACK = 0x80
SW_HIDE = 0
INFINITE = 0xFFFFFFFF


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", SID_AND_ATTRIBUTES)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD)]


def _check(ok, what):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error(), what)


def unelevated_token():
    level = wintypes.HANDLE()
    _check(advapi32.SaferCreateLevel(SAFER_SCOPEID_USER, SAFER_LEVELID_NORMALUSER, SAFER_LEVEL_OPEN,
                                     ctypes.byref(level), None), "SaferCreateLevel")
    token = wintypes.HANDLE()
    try:
        _check(advapi32.SaferComputeTokenFromLevel(level, None, ctypes.byref(token), 0, None),
               "SaferComputeTokenFromLevel")
    finally:
        advapi32.SaferCloseLevel(level)
    sid = ctypes.c_void_p()
    _check(advapi32.ConvertStringSidToSidW("S-1-16-8192", ctypes.byref(sid)), "ConvertStringSidToSid")  # Medium
    tml = TOKEN_MANDATORY_LABEL(SID_AND_ATTRIBUTES(sid, SE_GROUP_INTEGRITY))
    size = ctypes.sizeof(tml) + advapi32.GetLengthSid(sid)
    _check(advapi32.SetTokenInformation(token, TokenIntegrityLevel, ctypes.byref(tml), size), "SetTokenInformation")
    kernel32.LocalFree(sid)
    return token


def run(argv, cwd=None, env=None, timeout=None):
    """Run argv (a list, or a ready command line string) de-elevated and hidden; wait for it.
    timeout (seconds): then it is terminated and None is returned."""
    token = unelevated_token()
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.dwFlags = STARTF_USESHOWWINDOW | STARTF_FORCEOFFFEEDBACK
    si.wShowWindow = SW_HIDE
    pi = PROCESS_INFORMATION()
    block = None
    if env is not None:
        text = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
        block = ctypes.create_unicode_buffer(text, len(text))
    # a list is quoted the C runtime way; a str is used as is (cmd.exe has its own quoting)
    cmd = ctypes.create_unicode_buffer(argv if isinstance(argv, str) else subprocess.list2cmdline(argv))
    try:
        _check(advapi32.CreateProcessAsUserW(token, None, cmd, None, None, False,
                                             CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT, block, cwd,
                                             ctypes.byref(si), ctypes.byref(pi)), "CreateProcessAsUser")
        ms = INFINITE if timeout is None else int(timeout * 1000)
        if kernel32.WaitForSingleObject(pi.hProcess, ms) != 0:          # WAIT_TIMEOUT
            kernel32.TerminateProcess(pi.hProcess, 1)
            kernel32.CloseHandle(pi.hThread)
            kernel32.CloseHandle(pi.hProcess)
            return None
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
        return code.value
    finally:
        kernel32.CloseHandle(token)


def check():
    d = tempfile.mkdtemp()
    out = os.path.join(d, "groups.csv")                  # (created by the unelevated child itself)
    try:
        who = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "whoami.exe")   # (not a PATH one)
        run(f'cmd.exe /c ""{who}" /groups /fo csv > "{out}""')
        text = open(out, encoding="mbcs", errors="replace").read() if os.path.exists(out) else ""
    finally:
        if os.path.exists(out):
            os.remove(out)
        os.rmdir(d)
    medium = "S-1-16-8192" in text
    admin_deny = any("S-1-5-32-544" in ln and "deny only" in ln.lower() for ln in text.splitlines())
    print(f"integrity Medium: {medium}; Administrators deny-only: {admin_deny}")
    return 0 if medium and admin_deny else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cwd")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if a.check:
        sys.exit(check())
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    env = dict(os.environ)
    for kv in a.env:
        k, _, v = kv.partition("=")
        env[k] = v
    sys.exit(run(cmd, a.cwd, env))


if __name__ == "__main__":
    main()
