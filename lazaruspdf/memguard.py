"""Memory budget for snapshot-undo steps, GLOBAL across every open document.

A snapshot step keeps a whole extra document in memory (see workdoc.WorkingDoc.snapshot_edit).
Before each one, reserve() checks two things:

- the budget: Settings › Undo memory, a percentage of TOTAL system RAM (default 30%);
- actual FREE RAM right now, so a snapshot never takes memory that isn't there, whatever the
  budget says.

If either would be exceeded, the OLDEST snapshot states (of any open document) are dropped:
they become that document's undo floor. The first time that happens, notice() is called once
(one notice per session, not one per edit). If free RAM is still too low with nothing left to
drop, the edit is refused (MemoryError) and nothing changes.

RAM figures come from Windows (GlobalMemoryStatusEx) via ctypes; no extra library."""
import ctypes
import itertools
import sys
import weakref

DEFAULT_PCT = 30
MIN_PCT, MAX_PCT = 5, 80
HEADROOM = 512 * 1024 * 1024      # always leave this much RAM free for Windows and the app
COST_FACTOR = 3                   # a kept document ~ its bytes x3 (serialized copy + parsed objects + caches)


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _status():
    st = _MemStatus()
    st.dwLength = ctypes.sizeof(_MemStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        raise OSError("GlobalMemoryStatusEx failed")
    return st


def total_ram():
    if sys.platform == "win32":
        return _status().ullTotalPhys
    import os
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def free_ram():
    if sys.platform == "win32":
        return _status().ullAvailPhys
    import os
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_AVPHYS_PAGES")


def cost_of(nbytes):
    return nbytes * COST_FACTOR


class Budget:
    def __init__(self):
        self.pct = DEFAULT_PCT
        self.docs = weakref.WeakSet()       # WorkingDocs with snapshot steps
        self.notice = None                  # callable(text), set by the UI
        self.notified = False
        self.order = itertools.count()      # global age of snapshot steps (oldest first)
        # test hooks: override what Windows reports
        self.total_override = None
        self.free_override = None

    # ---------- figures ----------
    def total(self):
        return self.total_override if self.total_override is not None else total_ram()

    def free(self):
        return self.free_override() if self.free_override is not None else free_ram()

    def limit(self):
        return int(self.total() * self.pct / 100)

    def used(self):
        return sum(w.snapshot_cost() for w in list(self.docs))

    def droppable(self, current=None):
        """Memory that dropping could free: every document's applied snapshot states, plus the
        redo branches of the OTHER documents."""
        return sum(w.droppable_cost() + (w.redo_cost() if w is not current else 0) for w in list(self.docs))

    def set_percent(self, pct):
        self.pct = max(MIN_PCT, min(MAX_PCT, int(pct)))

    # ---------- dropping ----------
    def _drop_other_redo(self, current):
        """Redo branches of OTHER documents go first: nobody is looking at them."""
        for w in list(self.docs):
            if w is not current and w.redo_cost() > 0:
                w.drop_redo()
                return True
        return False

    def _oldest(self):
        """(work, entry) of the oldest droppable snapshot state, any document."""
        best = None
        for w in list(self.docs):
            e = w.oldest_droppable()
            if e is not None and (best is None or e["age"] < best[1]["age"]):
                best = (w, e)
        return best

    def _drop_oldest(self, current=None):
        if self._drop_other_redo(current):
            self._tell()
            return True
        found = self._oldest()
        if found is None:
            return False
        work, entry = found
        work.drop_through(entry)
        self._tell()
        return True

    def _tell(self):
        if not self.notified:
            self.notified = True
            if self.notice:
                self.notice("Undo memory is full, so the oldest undo steps of big edits were dropped "
                            "(redo steps in other tabs go first): you can't undo past them now. "
                            "Settings › Undo memory sets the limit. (This notice shows once.)")

    # ---------- the check before every snapshot ----------
    def reserve(self, work, cost):
        """Make room for a snapshot step costing `cost`. Returns True if its 'before' state can be
        kept for undo, False if it can't (that edit becomes the undo floor). Raises MemoryError if
        even the edit itself doesn't fit in free RAM."""
        self.docs.add(work)
        # free RAM: the new copy must fit, with headroom. If even dropping every old state
        # couldn't make room, refuse now without throwing undo history away for nothing.
        need = cost + HEADROOM
        if self.free() + self.droppable(work) < need:
            raise MemoryError("Not enough free memory for this edit. Close other programs or "
                              "documents and try again. Nothing was changed.")
        while self.free() < need:
            if not self._drop_oldest(work):
                raise MemoryError("Not enough free memory for this edit. Close other programs or "
                                  "documents and try again. Nothing was changed.")
        # the budget: old states first, then (if this one alone is too big) don't keep it
        while self.used() + cost > self.limit():
            if not self._drop_oldest(work):
                self._tell()
                return False
        return True


BUDGET = Budget()
