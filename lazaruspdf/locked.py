"""Password-protected PDFs.

open_pdf() opens a PDF and, if it needs a password, asks for one (retrying on a wrong one)
and authenticates. It returns the document plus a Lock describing how to protect copies.

PyMuPDF writes an UNENCRYPTED file on a normal save, even from an encrypted source, so
every PDF Lazarus PDF writes from a protected document passes Lock.save_kwargs() explicitly:
AES-256 encryption (stage 10: Keep upgrades RC4 / AES-128 sources), the same permission flags,
and the password the user typed opens the
copy ("Keep password"). File > Save With Password also offers Set / Match / Clear
(custom(), match(), None).

Passwords stay in memory for the session only; they are never saved, logged or shown."""
import secrets

import pymupdf

# PyMuPDF's metadata["encryption"] reads like "Standard V5 R6 256-bit AES"
_METHODS = (("256-bit AES", pymupdf.PDF_ENCRYPT_AES_256), ("128-bit AES", pymupdf.PDF_ENCRYPT_AES_128),
            ("128-bit RC4", pymupdf.PDF_ENCRYPT_RC4_128), ("40-bit RC4", pymupdf.PDF_ENCRYPT_RC4_40))


class PasswordCancelled(Exception):
    """The user closed the password prompt: the file is not opened. Not an error."""


# permission checkboxes in Set passwords: (key, label, PyMuPDF flags they control)
PERMISSIONS = (
    ("print", "Allow printing", pymupdf.PDF_PERM_PRINT),
    ("print_hq", "Allow high-resolution printing", pymupdf.PDF_PERM_PRINT_HQ),
    ("copy", "Allow copying / extracting text", pymupdf.PDF_PERM_COPY),
    ("modify", "Allow editing content", pymupdf.PDF_PERM_MODIFY | pymupdf.PDF_PERM_ASSEMBLE),
    ("annotate", "Allow annotating", pymupdf.PDF_PERM_ANNOTATE),
    ("form", "Allow filling form fields", pymupdf.PDF_PERM_FORM),
)
ALL_PERMISSIONS = -1


class Lock:
    """How a written PDF is protected (None anywhere means: no protection).
    kind: "keep" (carried over from the source), "set" or "match"."""

    def __init__(self, method, label, user_pw, owner_pw, permissions, owner_known=True, kind="keep"):
        self.method, self.label = method, label
        self.user_pw, self.owner_pw, self.permissions = user_pw, owner_pw, permissions
        self.owner_known = owner_known      # False: the owner password is a random one nobody knows
        self.kind = kind

    @property
    def needs_password(self):
        return bool(self.user_pw)

    def save_kwargs(self):
        return dict(encryption=self.method, user_pw=self.user_pw, owner_pw=self.owner_pw,
                    permissions=self.permissions)


def lock_for(doc, password=None, code=0):
    """The Lock for an opened (already authenticated) document, or None if it isn't encrypted."""
    label = (doc.metadata or {}).get("encryption") or ""
    if not label:
        return None
    # every copy is written with AES-256, whatever the source used (RC4 and AES-128 are weak):
    # the same passwords and permissions, stronger encryption
    method = pymupdf.PDF_ENCRYPT_AES_256
    try:
        kind, val = doc.xref_get_key(-1, "Encrypt/P")      # the file's own flags, even when
        perms = int(val) if kind == "int" else doc.permissions   # opened with the owner password
    except Exception:
        perms = doc.permissions
    random_owner = secrets.token_urlsafe(24)
    if password and code & 4:            # owner password (or user == owner): it opens and owns the copy
        return Lock(method, label, password, password, perms)
    # otherwise the owner password is unknown here and becomes an unguessable one
    if password:                         # user password: same open password
        return Lock(method, label, password, random_owner, perms, owner_known=False)
    # no open password, only permission restrictions
    return Lock(method, label, "", random_owner, perms, owner_known=False)


def permissions_from(allowed):
    """allowed: {key: bool} for PERMISSIONS (missing = allowed) -> PyMuPDF permissions int."""
    perms = ALL_PERMISSIONS
    for key, _, flags in PERMISSIONS:
        if not allowed.get(key, True):
            perms &= ~flags
    return perms


def custom(open_pw, owner_pw, allowed=None):
    """Set passwords. A blank password is not set: both blank -> None (unprotected); open only ->
    a password to open, no permission lock; owner only -> opens freely, permissions restricted.
    Permission flags apply only with an owner password. New protection is AES-256."""
    if not open_pw and not owner_pw:
        return None
    perms = permissions_from(allowed or {}) if owner_pw else ALL_PERMISSIONS
    return Lock(pymupdf.PDF_ENCRYPT_AES_256, "AES-256", open_pw, owner_pw, perms, kind="set")


def match(password):
    """Match passwords: one password both opens the file and owns it (so no restrictions apply)."""
    if not password:
        return None
    return Lock(pymupdf.PDF_ENCRYPT_AES_256, "AES-256", password, password, ALL_PERMISSIONS, kind="match")


def describe(lock):
    """Short status-bar wording for the protection saves will write."""
    if lock is None:
        return "no password"
    if not lock.user_pw:
        return "permission-protected"
    if lock.kind == "keep":
        return "password-protected"
    return "password-protected (new password)" if lock.kind == "match" else "password-protected (new passwords)"


def open_pdf(src, ask=None):
    """Open a PDF from a path or bytes. ask(attempt) -> password or None (cancel).
    Returns (doc, lock). Raises PasswordCancelled if the prompt is cancelled."""
    if isinstance(src, (bytes, bytearray)):
        doc = pymupdf.open(stream=src, filetype="pdf")
    else:
        doc = pymupdf.open(str(src))
    # needs_pass only BEFORE authenticate(): reading it again afterwards (once metadata or the
    # /Encrypt dict has been read) resets the decryption, and page streams then fail to decode
    if not doc.needs_pass:
        return doc, lock_for(doc)
    attempt = 0
    while True:
        pw = ask(attempt) if ask else None
        if pw is None:
            doc.close()
            if ask is None:
                raise RuntimeError("The file is password-protected.")
            raise PasswordCancelled()
        code = doc.authenticate(pw)
        if code:
            return doc, lock_for(doc, pw, code)
        attempt += 1


def ask_password(parent, attempt, name=""):
    """Masked password prompt. Returns the password, or None if cancelled."""
    from PySide6.QtWidgets import QInputDialog, QLineEdit
    title = f"Password — {name}" if name else "Password"
    text = ("Wrong password. Try again:" if attempt else
            "This PDF is password-protected.\nEnter its password to open it:")
    pw, ok = QInputDialog.getText(parent, title, text, QLineEdit.Password)
    return pw if ok else None


def asker(parent, path):
    """An ask(attempt) callback for open_pdf that prompts with the file's name."""
    from pathlib import Path
    name = Path(path).name
    return lambda attempt: ask_password(parent, attempt, name)
