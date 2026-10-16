"""Never write to an original.

Every PDF Lazarus opens (PDF Editor or Extraction import) is registered here as a
protected source. Every write Lazarus makes (editor saves, extraction review files,
all exports) calls check() first. A target that resolves to a protected source is
refused with ProtectedPathError, and the UI asks for a different file. There is no
code path that writes to an opened source PDF."""
from pathlib import Path


class ProtectedPathError(ValueError):
    pass


_sources = {}          # owner -> resolved source path


def _norm(path):
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path).absolute()


def register(owner, path):
    """owner: who opened it ("editor", "extract"); replaces that owner's previous source."""
    _sources[owner] = _norm(path)


def unregister(owner):
    _sources.pop(owner, None)


def is_protected(path):
    p = _norm(path)
    return any(p == s for s in _sources.values())


MESSAGE = ("That's the original PDF. Lazarus never writes to an original. "
           "Choose a different file name or folder.")


def check(path):
    if is_protected(path):
        raise ProtectedPathError(MESSAGE)
