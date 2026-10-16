"""Lazarus PDF as a PDF reader on Windows (stage 12): per-user file association, no admin rights.

The documented per-user registration (all under HKEY_CURRENT_USER, all keys our own):
  Software\\Classes\\LazarusPDF.PDF               our ProgID: friendly name, icon, open verb "exe" "%1"
  Software\\Classes\\.pdf\\OpenWithProgids         a value named LazarusPDF.PDF (adds us to "Open with")
  Software\\Classes\\Applications\\LazarusPDF.exe  the application entry ("Open with", supported types)
  Software\\LazarusPDF\\Capabilities                name, description, icon, .pdf -> LazarusPDF.PDF
  Software\\RegisteredApplications                a value named LazarusPDF -> the Capabilities key
That is what makes Lazarus PDF show up in "Open with" and in Settings › Default apps. It does NOT
make Lazarus PDF the default: Windows 10/11 only lets the user pick the default, in Settings
(the UserChoice key is protected by a hash only Windows writes). So "make default" registers,
then opens the Default apps page, and says the user has to confirm it there.

Nothing else is touched: never .pdf's own default value, never another app's ProgID or
UserChoice. Unregister removes exactly what register added (a value we added to a shared key,
not the key).

Stage 22 (Lazarus -> Lazarus PDF): migrate_old() moves a Lazarus 0.21.0 registration (Lazarus.PDF,
Applications\\Lazarus.exe, Software\\Lazarus\\Capabilities, RegisteredApplications "Lazarus") to the
names above. If Windows' own default choice (UserChoice, hash-protected: no program may rewrite it)
still names the old ProgID, that one key is kept as a forwarder that opens LazarusPDF.exe, so
double-clicked PDFs keep opening in Lazarus PDF; it goes as soon as UserChoice no longer names it
(cleanup_forwarders(), every start). Everything else of the old registration is removed.

The self-test runs all of this against a sandbox key (root=...) instead of the real places."""
import os
import sys
import winreg

PROGID = "LazarusPDF.PDF"
APP_NAME = "Lazarus PDF"                      # what Windows shows (Open with, Default apps)
REG_NAME = "LazarusPDF"                       # the RegisteredApplications value (identifier)
EXE_NAME = "LazarusPDF.exe"
FRIENDLY = "PDF Document (Lazarus PDF)"
DESCRIPTION = "Lazarus PDF: a local PDF reader and editor"
CAPS = r"Software\LazarusPDF\Capabilities"
# Lazarus 0.21.0 and older (stage 22 migration only)
OLD_PROGID = "Lazarus.PDF"
OLD_EXE_NAME = "Lazarus.exe"
OLD_CAPS = r"Software\Lazarus\Capabilities"
OLD_REG_NAME = "Lazarus"
USERCHOICE = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice"
HKCU = winreg.HKEY_CURRENT_USER


def command_line():
    """(open command, icon) for this installation: the frozen exe, or the source launcher."""
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return f'"{exe}" "%1"', f'"{exe}",0'
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    py = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(py):
        py = sys.executable
    icon = os.path.join(here, "lazaruspdf", "assets", "lazaruspdf.ico")
    return f'"{py}" "{os.path.join(here, "run_lazaruspdf.pyw")}" "%1"', f'"{icon}",0'


class Registry:
    """HKCU, or a sandbox: every path is put under `root` (a key below HKCU)."""

    def __init__(self, root=None):
        self.root = root

    def _p(self, path):
        return f"{self.root}\\{path}" if self.root else path

    def set(self, path, name, value, kind=winreg.REG_SZ):
        with winreg.CreateKeyEx(HKCU, self._p(path), 0, winreg.KEY_WRITE) as k:
            winreg.SetValueEx(k, name, 0, kind, value)

    def get(self, path, name=""):
        try:
            with winreg.OpenKey(HKCU, self._p(path)) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def has(self, path, name):
        """The value exists (an empty REG_NONE value reads back as None, so get() can't tell)."""
        try:
            with winreg.OpenKey(HKCU, self._p(path)) as k:
                winreg.QueryValueEx(k, name)
            return True
        except OSError:
            return False

    def exists(self, path):
        try:
            winreg.OpenKey(HKCU, self._p(path)).Close()
            return True
        except OSError:
            return False

    def delete_value(self, path, name):
        try:
            with winreg.OpenKey(HKCU, self._p(path), 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass

    def delete_tree(self, path):
        full = self._p(path)
        try:
            with winreg.OpenKey(HKCU, full, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
                while True:
                    try:
                        sub = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    self.delete_tree(f"{path}\\{sub}")
            winreg.DeleteKey(HKCU, full)
        except OSError:
            pass

    def delete_if_empty(self, path):
        """Remove a key only if it holds nothing (no values, no subkeys)."""
        try:
            with winreg.OpenKey(HKCU, self._p(path)) as k:
                subs, vals, _ = winreg.QueryInfoKey(k)
            if not subs and not vals:
                winreg.DeleteKey(HKCU, self._p(path))
        except OSError:
            pass

    def dump(self, path=""):
        """{key path: {value name: value}} of everything under path (tests)."""
        out = {}

        def walk(p):
            try:
                k = winreg.OpenKey(HKCU, self._p(p) if p else self.root)
            except OSError:
                return
            with k:
                subs, nvals, _ = winreg.QueryInfoKey(k)
                out[p] = {winreg.EnumValue(k, i)[0]: winreg.EnumValue(k, i)[1] for i in range(nvals)}
                names = [winreg.EnumKey(k, i) for i in range(subs)]
            for s in names:
                walk(f"{p}\\{s}" if p else s)
        walk(path)
        return out


def registry():
    """The real per-user places, or a sandbox key when LAZARUS_ASSOC_ROOT is set (tests)."""
    return Registry(os.environ.get("LAZARUS_ASSOC_ROOT") or None)


def register(reg=None, command=None, icon=None):
    """Add Lazarus PDF's per-user registration. Returns the paths written."""
    reg = reg or Registry()
    cmd, ico = command_line()
    cmd, ico = command or cmd, icon or ico
    cls = rf"Software\Classes\{PROGID}"
    reg.set(cls, "", FRIENDLY)
    reg.set(cls, "FriendlyTypeName", FRIENDLY)
    reg.set(rf"{cls}\DefaultIcon", "", ico)
    reg.set(rf"{cls}\shell", "", "open")
    reg.set(rf"{cls}\shell\open", "FriendlyAppName", APP_NAME)
    reg.set(rf"{cls}\shell\open\command", "", cmd)
    reg.set(r"Software\Classes\.pdf\OpenWithProgids", PROGID, b"", winreg.REG_NONE)
    app = rf"Software\Classes\Applications\{EXE_NAME}"
    reg.set(app, "FriendlyAppName", APP_NAME)
    reg.set(rf"{app}\DefaultIcon", "", ico)
    reg.set(rf"{app}\SupportedTypes", ".pdf", "")
    reg.set(rf"{app}\shell\open\command", "", cmd)
    reg.set(CAPS, "ApplicationName", APP_NAME)
    reg.set(CAPS, "ApplicationDescription", DESCRIPTION)
    reg.set(CAPS, "ApplicationIcon", ico)
    reg.set(rf"{CAPS}\FileAssociations", ".pdf", PROGID)
    reg.set(r"Software\RegisteredApplications", REG_NAME, CAPS)
    if reg.root is None:
        notify_shell()
    return [cls, app, CAPS]


def unregister(reg=None, command=None):
    """Remove exactly what register() added. Other apps' keys and values stay."""
    reg = reg or Registry()
    reg.delete_tree(rf"Software\Classes\{PROGID}")
    reg.delete_value(r"Software\Classes\.pdf\OpenWithProgids", PROGID)
    reg.delete_if_empty(r"Software\Classes\.pdf\OpenWithProgids")
    reg.delete_if_empty(r"Software\Classes\.pdf")
    reg.delete_tree(rf"Software\Classes\Applications\{EXE_NAME}")
    reg.delete_tree(CAPS)
    reg.delete_if_empty(r"Software\LazarusPDF")
    reg.delete_value(r"Software\RegisteredApplications", REG_NAME)
    reg.delete_if_empty(r"Software\RegisteredApplications")
    here = os.path.normcase(command or command_line()[0])
    for key in (rf"Software\Classes\{OLD_PROGID}", rf"Software\Classes\Applications\{OLD_EXE_NAME}"):
        c = reg.get(key + r"\shell\open\command") or ""
        if c and os.path.normcase(c) == here:          # a stage 22 forwarder to this copy
            reg.delete_tree(key)
    if reg.root is None:
        notify_shell()


# ---------------- stage 22: the Lazarus 0.21.0 registration ----------------
def has_old(reg=None):
    reg = reg or Registry()
    return (reg.get(r"Software\RegisteredApplications", OLD_REG_NAME) == OLD_CAPS
            or reg.exists(rf"Software\Classes\{OLD_PROGID}") or reg.exists(OLD_CAPS)
            or reg.exists(rf"Software\Classes\Applications\{OLD_EXE_NAME}"))


def _old_choice(reg):
    """Does Windows' default choice for .pdf still name an old key? -> (progid kept?, app kept?)"""
    choice = current_default(reg)
    return choice == OLD_PROGID, choice == f"Applications\\{OLD_EXE_NAME}"


def _drop_old(reg, keep_forwarders=True):
    keep_progid, keep_app = _old_choice(reg) if keep_forwarders else (False, False)
    if not keep_progid:
        reg.delete_tree(rf"Software\Classes\{OLD_PROGID}")
    if not keep_app:
        reg.delete_tree(rf"Software\Classes\Applications\{OLD_EXE_NAME}")
    reg.delete_value(r"Software\Classes\.pdf\OpenWithProgids", OLD_PROGID)
    reg.delete_if_empty(r"Software\Classes\.pdf\OpenWithProgids")
    reg.delete_if_empty(r"Software\Classes\.pdf")
    reg.delete_tree(OLD_CAPS)
    reg.delete_if_empty(r"Software\Lazarus")
    if reg.get(r"Software\RegisteredApplications", OLD_REG_NAME) == OLD_CAPS:
        reg.delete_value(r"Software\RegisteredApplications", OLD_REG_NAME)
    reg.delete_if_empty(r"Software\RegisteredApplications")
    return keep_progid, keep_app


def migrate_old(reg=None, command=None, icon=None):
    """A Lazarus 0.21.0 registration -> this one. -> dict of what happened, or None if there was none.
    Registers the new names; removes the old ones, except an old key Windows' default choice still
    names: that one is pointed at this exe (a forwarder) so PDFs keep opening here."""
    reg = reg or Registry()
    if not has_old(reg):
        return None
    cmd, ico = command_line()
    cmd, ico = command or cmd, icon or ico
    register(reg, command=cmd, icon=ico)
    keep_progid, keep_app = _drop_old(reg)
    if keep_progid:
        cls = rf"Software\Classes\{OLD_PROGID}"
        reg.set(rf"{cls}\DefaultIcon", "", ico)
        reg.set(rf"{cls}\shell\open", "FriendlyAppName", APP_NAME)
        reg.set(rf"{cls}\shell\open\command", "", cmd)
    if keep_app:
        app = rf"Software\Classes\Applications\{OLD_EXE_NAME}"
        reg.set(app, "FriendlyAppName", APP_NAME)
        reg.set(rf"{app}\DefaultIcon", "", ico)
        reg.set(rf"{app}\shell\open\command", "", cmd)
    if reg.root is None:
        notify_shell()
    return {"registered": True, "forwarder_progid": keep_progid, "forwarder_app": keep_app}


def cleanup_forwarders(reg=None):
    """An old key kept only because Windows' default choice named it: gone once it no longer does."""
    reg = reg or Registry()
    keep_progid, keep_app = _old_choice(reg)
    removed = []
    if not keep_progid and reg.exists(rf"Software\Classes\{OLD_PROGID}"):
        reg.delete_tree(rf"Software\Classes\{OLD_PROGID}")
        removed.append(OLD_PROGID)
    if not keep_app and reg.exists(rf"Software\Classes\Applications\{OLD_EXE_NAME}"):
        reg.delete_tree(rf"Software\Classes\Applications\{OLD_EXE_NAME}")
        removed.append(OLD_EXE_NAME)
    if removed and reg.root is None:
        notify_shell()
    return removed


def is_registered(reg=None):
    reg = reg or Registry()
    return reg.get(r"Software\RegisteredApplications", REG_NAME) == CAPS and reg.exists(
        rf"Software\Classes\{PROGID}\shell\open\command")


def current_default(reg=None):
    """The ProgID the user picked for .pdf in Windows (read-only), or None."""
    reg = reg or Registry()
    return reg.get(USERCHOICE, "ProgId")


def is_default(reg=None):
    # (the old names count while they forward here: Windows' choice from Lazarus 0.21.0, stage 22)
    old = (OLD_PROGID, f"Applications\\{OLD_EXE_NAME}") if reg_points_here(reg) else ()
    return current_default(reg) in (PROGID, f"Applications\\{EXE_NAME}") + old


def reg_points_here(reg=None):
    """The kept old ProgID (a forwarder) opens this exe."""
    reg = reg or Registry()
    cmd = (reg.get(rf"Software\Classes\{OLD_PROGID}\shell\open\command")
           or reg.get(rf"Software\Classes\Applications\{OLD_EXE_NAME}\shell\open\command") or "")
    return bool(cmd) and os.path.normcase(cmd) == os.path.normcase(command_line()[0])


def notify_shell():
    """Tell Explorer the associations changed (SHChangeNotify SHCNE_ASSOCCHANGED)."""
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass


SETTINGS_URI = f"ms-settings:defaultapps?registeredAppUser={REG_NAME}"


def open_default_apps():
    """Open Settings › Default apps (on Lazarus PDF's own page where Windows supports it)."""
    for uri in (SETTINGS_URI, "ms-settings:defaultapps"):
        try:
            os.startfile(uri)              # noqa: S606 (a Windows Settings page)
            return True
        except OSError:
            continue
    return False


def installer(action):
    """The Windows installer's hooks (stage 19), run by LazarusPDF.exe --register-default /
    --unregister-default:
      register   (Setup's "Set Lazarus PDF as my default PDF reader", run as the ORIGINAL, unelevated
                 user, so it lands in that user's HKCU): the same as Settings › Make default:
                 register, open Settings › Default apps, say the user confirms there.
      unregister (Uninstall): remove the registration, exactly as Settings › Remove, but only when
                 it points at THIS exe (a registration made by another copy of Lazarus PDF stays).
    LAZARUS_ASSOC_ROOT sandboxes both (tests): no Settings page, no message then. Returns 0."""
    reg = registry()
    if action == "register":
        register(reg)
        if reg.root is None:
            open_default_apps()
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, CONFIRM_NOTE, APP_NAME, 0x40 | 0x40000)  # info, topmost
            except Exception:
                pass
        return 0
    if action == "unregister":
        cmd = reg.get(rf"Software\Classes\{PROGID}\shell\open\command") or ""
        if is_registered(reg) and os.path.normcase(cmd) == os.path.normcase(command_line()[0]):
            unregister(reg)
        return 0
    raise ValueError(action)


CONFIRM_NOTE = ("Lazarus PDF is now registered as a PDF reader for your Windows account.\n\n"
                "Windows doesn't let programs make themselves the default: you have to confirm it. "
                "Settings › Default apps opens now. Pick Lazarus PDF for .pdf there (\"Set default\").\n\n"
                "Until you do, double-clicked PDFs still open in your current default app. "
                "\"Open with › Lazarus PDF\" works right away.")
