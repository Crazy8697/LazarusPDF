"""The PDF Editor's working copy.

- The source file is read once into memory and opened from those bytes. The editor never
  holds the source open and never writes it: every save goes to a different file, and
  protect.check() refuses any target that resolves to an open original.
- Undo/redo is ONE history of two step types. edit(name, fn) is a PyMuPDF journal
  operation (cheap). snapshot_edit(name, fn) is for ops PyMuPDF refuses or can't undo inside
  a journal operation (declared per op in oprules.py): the current document object is kept
  untouched as the "before" state and the op runs on a fresh copy, which becomes self.doc;
  undo/redo swap the objects. self.doc can therefore CHANGE IDENTITY on undo/redo; callers
  re-read it (the editor rebuilds its views when it does). Snapshot memory is budgeted
  globally by memguard.
- Dirty = the op history differs from the history at the last save.
- A password-protected source is opened with the password the user types (locked.py).
  self.protection is what saves write (a locked.Lock, or None = no password). It starts as
  the source's own protection ("Keep password"); save_as(dest, protection) changes it, and
  plain save() repeats the last choice."""
import os
import tempfile
from pathlib import Path

import pymupdf

from . import locked, memguard, protect

KEEP_CHOICE = object()       # save_as/save_options default: use the current protection choice


def write_pdf(data, dest, save_kwargs, pages=None, stopped=None):
    """THE PDF writer for Save, Save As, Save With Password and File › Export › PDF: the plain
    bytes of a document compacted into a NEW file atomically (a temp file in the same folder,
    then renamed), with save_kwargs' encryption. pages: only those (select() keeps the links
    between them). stopped() -> True cancels before anything is renamed into place. The caller
    has checked dest against protect (the registry lives in the UI process)."""
    dest = Path(dest)
    protect.check(dest)
    fd, tmp = tempfile.mkstemp(suffix=".pdf", dir=str(dest.parent))
    os.close(fd)
    try:
        out = pymupdf.open(stream=data, filetype="pdf")
        try:
            if pages is not None and list(pages) != list(range(out.page_count)):
                out.select(sorted(set(pages)))
            if stopped is not None and stopped():
                raise InterruptedError("cancelled")
            out.save(tmp, garbage=3, deflate=True, **save_kwargs)
        finally:
            out.close()
        if stopped is not None and stopped():
            raise InterruptedError("cancelled")
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class WorkingDoc:
    def __init__(self, path, ask=None, opened=None):
        """ask(attempt) -> password or None: called only for a password-protected PDF.
        opened: (doc, lock) already opened from the file's bytes by the background loader."""
        self.source = Path(path)
        if opened is not None:
            self.doc, self.lock = opened
        else:
            data = self.source.read_bytes()
            self.doc, self.lock = locked.open_pdf(data, ask)    # lock: None unless encrypted
        self.protection = self.lock          # what saves write; starts as "Keep password"
        self.owner_warned = False            # "Continue anyway" given for a random owner password
        self.doc.journal_enable()
        self.target = None                  # where Save writes; chosen by the first Save / Save As
        # ONE history: each entry is a journal step ("journal", on entry["doc"]) or a document
        # swap ("swap": before -> after). _pos = entries applied; entries[_pos:] = redo branch.
        self._entries = []
        self._pos = 0
        self._serial = 0
        self._base = ()                     # serials of steps dropped below the undo floor
        self._saved_sig = ()                # ops applied at the last save

    # ---------- state ----------
    def step(self):
        return self._pos

    def _sig(self):
        return self._base + tuple(e["serial"] for e in self._entries[:self._pos])

    def dirty(self):
        # compare the actual op history, not just the step number: "undo, then a different
        # edit" lands on the same step but is not the saved state
        return self._sig() != self._saved_sig

    def can_undo(self):
        return self._pos > 0

    def can_redo(self):
        return self._pos < len(self._entries)

    # ---------- edits ----------
    def _new_entry(self, kind, name, pages, **extra):
        self._serial += 1
        # a marker string (e.g. "structural") is kept as-is; page lists become tuples
        e = dict(kind=kind, name=name, serial=self._serial,
                 pages=pages if isinstance(pages, str) else tuple(pages), **extra)
        self._entries.append(e)
        self._pos = len(self._entries)
        return e

    def _discard_redo(self):
        """A new edit after undo: drop the redo branch and close the documents only it held."""
        gone = self._entries[self._pos:]
        del self._entries[self._pos:]
        for e in gone:
            if e["kind"] == "swap" and e["after"] is not self.doc and not e["after"].is_closed:
                e["after"].close()

    def edit(self, name, fn, pages=()):
        """Run fn(doc) as one undoable journal operation."""
        self._discard_redo()
        before = self.doc.journal_position()[0]
        self.doc.journal_start_op(name)
        try:
            result = fn(self.doc)
        except Exception:
            # a failed edit must leave nothing behind: roll back whatever it changed (only if it
            # recorded a step; otherwise undo would take back the previous, unrelated edit)
            self.doc.journal_stop_op()
            if self.doc.journal_position()[0] > before:
                self.doc.journal_undo()
                self.doc._reset_page_refs()
            raise
        self.doc.journal_stop_op()
        self._new_entry("journal", name, pages, doc=self.doc)
        return result

    def edit_steps(self, name, steps, pages=(), between=None):
        """Like edit(), for a big change made in parts: steps(doc) is a generator and each part
        between its yields is its own journal operation, because MuPDF's journal slows down
        sharply when ONE operation adds thousands of objects (a 1500-page insert: ~50 s as one
        operation, ~3 s in 50-page parts). The parts are ONE undo step.
        between(progress) runs after each part (the UI paints meanwhile); if it raises (Cancel),
        everything done so far is rolled back and the exception goes on to the caller."""
        self._discard_redo()
        gen = steps(self.doc)
        ops = 0
        try:
            while True:
                before = self.doc.journal_position()[0]
                self.doc.journal_start_op(name)
                try:
                    progress = next(gen)
                    done = False
                except StopIteration as e:
                    result, done = e.value, True
                except BaseException:
                    self.doc.journal_stop_op()
                    if self.doc.journal_position()[0] > before:
                        self.doc.journal_undo()
                    raise
                self.doc.journal_stop_op()
                if self.doc.journal_position()[0] > before:
                    ops += 1
                if done:
                    break
                if between is not None:
                    between(progress)
        except BaseException:
            gen.close()
            for _ in range(ops):
                self.doc.journal_undo()
            self.doc._reset_page_refs()
            raise
        self._new_entry("journal", name, pages, doc=self.doc, ops=ops)
        return result

    def snapshot_edit(self, name, fn, pages=()):
        """Run fn(doc) as one undoable SNAPSHOT step, for an op PyMuPDF refuses (or can't undo)
        inside a journal operation (see oprules.py). The current document stays alive and
        untouched as the 'before' state; fn runs on a fresh copy opened from its bytes, which
        becomes the working document. Undo/redo swap between the two.
        Raises MemoryError (nothing changed) if free RAM can't hold the copy."""
        self._discard_redo()
        data = self.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)   # in memory only
        cost = memguard.cost_of(len(data))
        keep = memguard.BUDGET.reserve(self, cost)
        after = pymupdf.open(stream=data, filetype="pdf")
        try:
            result = fn(after)                    # a plain document: nothing is refused here
            after.journal_enable()                # later journal steps record on the new copy
        except Exception:
            after.close()                         # the working document was never touched
            raise
        before, self.doc = self.doc, after
        e = self._new_entry("swap", name, pages, before=before, after=after, data=data, cost=cost,
                            age=next(memguard.BUDGET.order))
        if not keep:                              # over budget on its own: this is the undo floor
            self.drop_through(e)
        return result

    def pages_for_undo(self):
        return self._entries[self._pos - 1]["pages"] if self._pos else ()

    def pages_for_redo(self):
        return self._entries[self._pos]["pages"] if self.can_redo() else ()

    def undo(self):
        if not self.can_undo():
            return False
        e = self._entries[self._pos - 1]
        if e["kind"] == "swap":
            self.doc = e["before"]
        else:
            for _ in range(e.get("ops", 1)):  # (edit_steps: several journal operations)
                self.doc.journal_undo()
            self.doc._reset_page_refs()        # page count / order may have changed
        self._pos -= 1
        return True

    def redo(self):
        if not self.can_redo():
            return False
        e = self._entries[self._pos]
        if e["kind"] == "swap":
            self.doc = e["after"]
        else:
            for _ in range(e.get("ops", 1)):
                self.doc.journal_redo()
            self.doc._reset_page_refs()
        self._pos += 1
        return True

    # ---------- snapshot memory (memguard) ----------
    def snapshot_cost(self):
        """Memory held for undo/redo by this document's snapshot steps (one extra document each)."""
        return sum(e["cost"] for e in self._entries if e["kind"] == "swap")

    def redo_cost(self):
        return sum(e["cost"] for e in self._entries[self._pos:] if e["kind"] == "swap")

    def drop_redo(self):
        """Memory pressure from another tab: forget this document's redo branch."""
        self._discard_redo()

    def droppable_cost(self):
        return sum(e["cost"] for e in self._entries[:self._pos] if e["kind"] == "swap")

    def snapshot_count(self):
        return sum(1 for e in self._entries if e["kind"] == "swap")

    def oldest_droppable(self):
        """The oldest APPLIED snapshot step (its 'before' state can become the undo floor)."""
        for e in self._entries[:self._pos]:
            if e["kind"] == "swap":
                return e
        return None

    def drop_through(self, entry):
        """Make `entry` the undo floor: forget it and every older step, closing the documents
        only they held. Its 'after' document (current or later) stays."""
        k = self._entries.index(entry)
        dropped = self._entries[:k + 1]
        keep_docs = {id(self.doc)} | {id(e[d]) for e in self._entries[k + 1:] for d in ("doc", "before", "after")
                                      if d in e}
        for e in dropped:
            for d in ("doc", "before"):
                obj = e.get(d)
                if obj is not None and id(obj) not in keep_docs and not obj.is_closed:
                    obj.close()
        self._base += tuple(e["serial"] for e in dropped)
        del self._entries[:k + 1]
        self._pos -= k + 1

    def known_bytes(self):
        """Bytes of the current state when they already exist: right after undoing a snapshot
        step the document is that step's untouched 'before' state, whose bytes the step kept.
        The find index reuses them instead of serializing again. None otherwise."""
        if self.can_redo():
            e = self._entries[self._pos]
            if e["kind"] == "swap" and e["before"] is self.doc:
                return e["data"]
        return None

    # ---------- saving (never to the original) ----------
    def is_source(self, path):
        try:
            return Path(path).resolve() == self.source.resolve()
        except OSError:
            return False

    def save_options(self, protection=KEEP_CHOICE):
        """Encryption arguments for a save (default: the current choice)."""
        prot = self.protection if protection is KEEP_CHOICE else protection
        if prot is not None:
            return prot.save_kwargs()
        return {"encryption": pymupdf.PDF_ENCRYPT_NONE}

    def _write(self, dest, protection=KEEP_CHOICE):
        """Write the working copy atomically (temp file in the same folder, then replace)."""
        dest = Path(dest)
        if self.is_source(dest):
            raise protect.ProtectedPathError(protect.MESSAGE)
        protect.check(dest)
        # Never garbage-collect the live document: MuPDF's garbage collection rewrites it in
        # memory and the undo journal no longer restores earlier states. Serialise it as is,
        # then compact a plain copy (write_pdf).
        write_pdf(self.plain_bytes(), dest, self.save_options(protection))
        self._saved_sig = self._sig()

    def plain_bytes(self):
        """The working copy as it is now (unsaved edits included), unencrypted, for a writer."""
        return self.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE)

    def save_as(self, dest, protection=KEEP_CHOICE):
        """Write a new file; protection (a locked.Lock or None) becomes the choice Save repeats."""
        self._write(dest, protection)
        self.target = Path(dest)
        if protection is not KEEP_CHOICE:
            self.protection = protection

    def save(self):
        """Save to the chosen target. Returns False when there is none yet (the UI asks: Save As)."""
        if self.target is None:
            return False
        self._write(self.target)
        return True

    def close(self):
        docs = {id(self.doc): self.doc}
        for e in self._entries:
            for k in ("doc", "before", "after"):
                if k in e:
                    docs[id(e[k])] = e[k]
        for d in docs.values():
            if not d.is_closed:
                d.close()
        self._entries = []
        self._pos = 0
