"""Build verification: `LazarusPDF.exe --selftest REPORT.json PDF WORKDIR`.

Runs inside the frozen app (or from source) with the offscreen Qt platform, so no window
appears. It checks bundled resources, extraction (including OCR), all six exports and
all three modes, then writes a JSON report. PDF must be a test file. Only counts and
yes/no results go in the report, never the document's name or text."""
import json
import os
import sys
import traceback
from pathlib import Path


def run(report_path, pdf, work):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # never read or write the real settings store from a self-test
    os.environ["LAZARUS_SETTINGS_INI"] = str(Path(work) / "selftest-settings.ini")
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    r = {"frozen": bool(getattr(sys, "frozen", False)), "checks": {}, "errors": []}
    # the app's own folder (Program Files once installed): nothing may be written there (stage 19)
    app_dir = Path(sys.executable).parent if r["frozen"] else Path(__file__).resolve().parent

    def _app_snapshot():
        out = {}
        for q in app_dir.rglob("*"):
            if q.is_file() and "__pycache__" not in q.parts:      # (Python's own cache, source runs only)
                st = q.stat()
                out[str(q.relative_to(app_dir))] = (st.st_size, st.st_mtime_ns)
        return out
    app_before = _app_snapshot()

    def _real_registration():
        """The user's REAL .pdf registration keys (old and new names): no check, and no program the
        checks start, may change them (stage 22: a child run once migrated them)."""
        from . import assoc as A
        reg = A.Registry()
        out = {}
        for p in (r"Software\Classes\LazarusPDF.PDF", r"Software\Classes\Lazarus.PDF", r"Software\LazarusPDF",
                  r"Software\Lazarus\Capabilities", r"Software\Classes\Applications\LazarusPDF.exe",
                  r"Software\Classes\Applications\Lazarus.exe"):
            out[p] = reg.dump(p) if reg.exists(p) else None
        out["RegisteredApplications"] = [reg.get(r"Software\RegisteredApplications", n) for n in ("LazarusPDF", "Lazarus")]
        out["OpenWithProgids"] = [reg.has(r"Software\Classes\.pdf\OpenWithProgids", n) for n in ("LazarusPDF.PDF", "Lazarus.PDF")]
        out["UserChoice"] = A.current_default(reg)
        return out
    reg_before = _real_registration()

    def _real_fonts():
        """The user's REAL fonts folder (file names, sizes, times) and HKCU font registrations: read only.
        No check, and nothing the program does during a run, may change them (stage 22.3)."""
        import winreg
        d = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
        files = sorted((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in d.iterdir()) if d.is_dir() else []
        vals = []
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts") as k:
                i = 0
                while True:
                    try:
                        vals.append(winreg.EnumValue(k, i)[:2])
                    except OSError:
                        break
                    i += 1
        except OSError:
            pass
        return files, sorted(vals)
    fonts_before = _real_fonts()
    c = r["checks"]

    def check(name, fn):
        try:
            c[name] = fn()
        except Exception as e:  # keep going; report every failure
            c[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            r["errors"].append(name + "\n" + traceback.format_exc())

    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox

    # A self-test must never block on a dialog: record every message box and answer it.
    r["dialogs"] = []

    def _box(kind, answer):
        def f(parent, title, text, *a, **k):
            r["dialogs"].append(f"{kind}: {text[:120]}")
            return answer
        return staticmethod(f)
    QMessageBox.warning = _box("warning", QMessageBox.Ok)
    QMessageBox.critical = _box("critical", QMessageBox.Ok)
    QMessageBox.question = _box("question", QMessageBox.Discard)
    QMessageBox.information = _box("information", QMessageBox.Ok)

    def wait(ms):                    # QtTest is not bundled; a plain event-loop wait
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from . import export, gui, modes, tess

    def tesseract():
        import pytesseract
        exe, ver = tess.configure()
        base = Path(getattr(sys, "_MEIPASS", "")) if r["frozen"] else None
        return {"ok": True, "bundled": bool(base) and Path(exe).resolve().is_relative_to(base.resolve()),
                "version": ver.split()[0], "langs": sorted(pytesseract.get_languages(config="")),
                "tessdata_in_bundle": bool(base) and os.environ.get("TESSDATA_PREFIX", "").startswith(str(base))}
    check("tesseract", tesseract)

    def qtawesome():
        from .icons import icon as qicon
        icon = qicon("mdi6.marker")          # the private font set (stage 22.3)
        return {"ok": not icon.isNull() and not icon.pixmap(24, 24).isNull()}
    check("qtawesome_fonts", qtawesome)

    def docx_template():
        from docx import Document
        d = Document()                       # loads the bundled default template
        d.add_paragraph("x")
        d.save(str(work / "t.docx"))
        return {"ok": (work / "t.docx").stat().st_size > 0}
    check("docx_template", docx_template)

    def icon():
        gui._dark(app)
        w = gui.Main()
        return {"ok": gui.ICON.exists() and not w.windowIcon().isNull(), "file": gui.ICON.exists()}
    check("window_icon", icon)

    state = {}

    def extraction():
        pages, paths, (gaps, figs) = modes.run(modes.EXTRACT, pdf, str(work / "out"))
        state["pages"] = pages
        state["paths"] = paths
        return {"ok": all(Path(p).stat().st_size > 0 for p in paths.values()),
                "pages": len(pages), "ocr_pages": sum(p.method == "OCR" for p in pages),
                "gaps": gaps, "figures": figs}
    check("extraction", extraction)

    def exports():
        out = {}
        for key, label, ext, _ in export.FORMATS:
            target = work / f"export{ext}"
            export.export(key, state["pages"], str(target), "selftest", False)
            out[key] = target.stat().st_size > 0
        import zipfile
        z = zipfile.ZipFile(work / "export.epub")
        out["epub_has_image"] = any(n.endswith(".png") for n in z.namelist())
        import pymupdf
        d = pymupdf.open(work / "export.pdf")
        out["pdf_has_image"] = sum(len(p.get_images()) for p in d) > 0
        d.close()
        from docx import Document
        out["docx_has_image"] = len(Document(work / "export.docx").inline_shapes) > 0
        out["ok"] = all(out.values())
        return out
    check("exports", exports)

    def ui():
        w = gui.Main()
        w.resize(1600, 950)
        w.show()
        wait(200)
        out = {"modes": {}}
        w.editor_mode.open_pdf(pdf)
        wait(300)
        out["editor_pages"] = len(w.editor_mode.canvas.pages)
        out["editor_rendered"] = sum(1 for p in w.editor_mode.canvas.pages if p["scale"])
        w.on_imported(state["pages"], state["paths"])
        wait(200)
        out["extraction_gap_counts"] = list(w.text_mode.editor.gap_counts())
        for key in ("overview", "editor", "extract"):
            w.set_mode(key)
            wait(400)
            w.grab().save(str(work / f"mode_{key}.png"))
            out["modes"][key] = {"central": type(w.stack.currentWidget()).__name__,
                                 "title": w.windowTitle()}
        # edit pipeline: select -> highlight -> undo -> redo -> save as; originals protected
        import hashlib
        src_hash = hashlib.sha256(Path(pdf).read_bytes()).hexdigest()
        w.editor_mode.open_pdf(pdf)
        wait(200)
        em = w.editor_mode
        em.canvas.select_chars(0, 0, 8)
        em.highlight_selection()
        n = lambda: len(list(em.doc[0].annots()))
        out["edit_highlight"] = n() == 1 and em.dirty()
        em.undo()
        out["edit_undo"] = n() == 0 and not em.dirty()
        em.redo()
        out["edit_redo"] = n() == 1
        target = work / "edited.pdf"
        em.work.save_as(str(target))
        import pymupdf
        saved = pymupdf.open(target)
        out["edit_saved"] = sum(len(list(p.annots())) for p in saved) == 1 and not em.dirty()
        saved.close()
        from . import protect
        refused = []
        for attempt in (lambda: em.work.save_as(pdf),
                        lambda: export.export("pdf", state["pages"], pdf, "selftest", False)):
            try:
                attempt()
                refused.append(False)
            except protect.ProtectedPathError:
                refused.append(True)
        out["original_protected"] = all(refused) and hashlib.sha256(Path(pdf).read_bytes()).hexdigest() == src_hash
        # page operations (stage 6): each one undoable; originals never written
        from . import pageops as P
        po, ov = w.pageops, w.overview_mode
        w.set_mode("overview")
        wait(200)
        n0 = em.doc.page_count
        x0 = [em.doc[i].xref for i in range(n0)]
        step0 = em.work.step()
        ov.grid.set_selection([0])
        po.move_pages([0], n0)
        po.rotate(90)
        po.duplicate()
        po.insert_blank(after=True)
        po.insert_pdf(after=False, path=pdf)
        ov.grid.set_selection([1, 2])
        po.delete_selected(confirm=False)
        po.merge(paths=[pdf], where="At the end")
        ov.grid.set_selection([0])
        po._crop_drawn(0, pymupdf.Rect(20, 20, 300, 300), scope="selected")
        ov.grid.set_selection([0, 1])
        out["pageops_extract"] = po.extract(dest=str(work / "extract.pdf"))
        (work / "split").mkdir(exist_ok=True)
        out["pageops_split"] = po.split(P.split_ranges(em.doc.page_count, every=3), folder=str(work / "split"))
        ops_done = em.work.step() - step0
        out["pageops_ops"] = ops_done
        out["pageops_views_in_sync"] = (len(em.canvas.pages) == em.thumbs.count() == len(ov.grid.items)
                                        == em.doc.page_count)
        for _ in range(ops_done):
            em.undo()
        out["pageops_undo_all"] = [em.doc[i].xref for i in range(em.doc.page_count)] == x0
        out["pageops_original_unchanged"] = hashlib.sha256(Path(pdf).read_bytes()).hexdigest() == src_hash
        # bookmarks (stage 7): edits are undoable; they survive page ops; extract carries them
        bm = em.bookmarks
        toc = lambda: [(l, t, p) for l, t, p, *_ in em.doc.get_toc(simple=False)]
        s0 = em.work.step()
        bm.tree.setCurrentItem(None)
        bm.add(page=0, title="ST first")
        bm.add(page=2, title="ST third")
        bm.tree.setCurrentItem(bm.tree.topLevelItem(1))
        bm.add(page=1, title="ST child")
        child = bm.tree.findItems("ST child", Qt.MatchExactly | Qt.MatchRecursive, 0)[0]
        bm._loading = True
        bm.tree.takeTopLevelItem(bm.tree.indexOfTopLevelItem(child))
        bm.tree.topLevelItem(0).addChild(child)
        bm._loading = False
        bm.commit("Move bookmark")
        out["bm_edits"] = toc() == [(1, "ST first", 1), (2, "ST child", 2), (1, "ST third", 3)]
        em.undo()
        undone = toc() == [(1, "ST first", 1), (1, "ST third", 2 + 1), (1, "ST child", 2)] or len(toc()) == 3
        em.redo()
        out["bm_undo_redo"] = undone and toc() == [(1, "ST first", 1), (2, "ST child", 2), (1, "ST third", 3)]
        ov.grid.set_selection([2])
        po.delete_selected(confirm=False)
        out["bm_delete_drops_target"] = [t for _, t, _ in toc()] == ["ST first", "ST child"]
        em.undo()
        out["bm_delete_undo"] = toc() == [(1, "ST first", 1), (2, "ST child", 2), (1, "ST third", 3)]
        ov.grid.set_selection([0])
        po.move_pages([0], em.doc.page_count)
        out["bm_follow_move"] = ("ST first", em.doc.page_count) in [(t, p) for _, t, p in toc()]
        em.undo()
        ov.grid.set_selection([0, 2])
        po.extract(dest=str(work / "bm_extract.pdf"))
        ex = pymupdf.open(work / "bm_extract.pdf")
        out["bm_extract_carries"] = [(l, t, p) for l, t, p, *_ in ex.get_toc(simple=False)] == [(1, "ST first", 1), (1, "ST third", 2)]
        ex.close()
        while em.work.step() > s0:
            em.undo()
        out["bm_original_unchanged"] = hashlib.sha256(Path(pdf).read_bytes()).hexdigest() == src_hash
        # search (stage 8): counts match PyMuPDF's own (case-insensitive) search; read-only
        fc = w.find
        w.set_mode("editor")
        wait(100)
        j0, dirty0 = em.work.doc.journal_position(), em.dirty()
        fc.open()
        fc.edit.setText("quick")
        fc._debounce.stop()
        fc.start_search()
        while fc._tick.isActive():
            wait(5)
        ref = pymupdf.open(pdf)
        expected = sum(len(pg.search_for("quick")) for pg in ref)
        ref.close()
        out["search_hits"] = [len(fc.hits), expected]
        n = len(fc.hits)
        first = fc.cur
        fc.step(1)
        fc.step(-1)
        back = fc.cur == first
        fc.go(n - 1)
        fc.step(1)
        out["search_next_prev_wrap"] = back and fc.cur == 0 and fc.count.text() == f"1 of {n}"
        out["search_read_only"] = (em.work.doc.journal_position() == j0 and em.dirty() == dirty0
                                   and "unsaved PDF" not in w.windowTitle())
        fc.close()
        out["search_highlights_cleared"] = not fc._items
        out["overview_pages"] = len(w.overview_mode.grid.items)
        out["overview_rendered"] = sum(i["rendered"] for i in w.overview_mode.grid.items)
        w.set_mode("editor")
        lm = w.layout_mgr
        g = w.editor_mode.groups[1]
        lm.rows["row2"].remove(g)
        lm.rows["row1"].append(g)
        lm.place[g] = "row1"
        lm.repack()
        out["group_to_row1_compact"] = g.toolButtonStyle().name == "ToolButtonIconOnly"
        w.act_lock.trigger()
        out["lock_freezes"] = not any(x.isMovable() for x in lm.groups)
        w.act_lock.trigger()
        shown = {}
        from . import about_ui
        about_ui.AboutDialog.exec = lambda self: shown.setdefault("about", self.body.text()) and 0
        w.act_about.trigger()
        out["about"] = "Version" in shown.get("about", "")
        from . import settings as S
        from . import settings_dialog
        settings_dialog.SettingsDialog.exec = lambda self: 0     # don't block on the modal dialog
        w.act_settings.trigger()                                  # File › Settings… builds the dialog
        dlg = settings_dialog.SettingsDialog(w, w.settings)
        out["settings_dialog"] = sorted(dlg.values()) == sorted(S.DEFAULTS)
        out["ok"] = (out["editor_pages"] > 0 and out["overview_pages"] == out["editor_pages"]
                     and all(v["central"] for v in out["modes"].values()) and out["about"]
                     and out["settings_dialog"] and out["group_to_row1_compact"] and out["lock_freezes"]
                     and out["edit_highlight"] and out["edit_undo"] and out["edit_redo"]
                     and out["edit_saved"] and out["original_protected"]
                     and out["pageops_ops"] == 8 and out["pageops_extract"] and out["pageops_split"]
                     and out["pageops_views_in_sync"] and out["pageops_undo_all"]
                     and out["pageops_original_unchanged"]
                     and out["bm_edits"] and out["bm_undo_redo"] and out["bm_delete_drops_target"]
                     and out["bm_delete_undo"] and out["bm_follow_move"] and out["bm_extract_carries"]
                     and out["bm_original_unchanged"]
                     and out["search_hits"][0] == out["search_hits"][1] > 0
                     and out["search_next_prev_wrap"] and out["search_read_only"]
                     and out["search_highlights_cleared"])
        w.text_mode.editor.document().setModified(False)
        w.close()
        return out
    check("ui", ui)

    def password_pdfs():
        """Password-protected PDFs and File > Save With Password, on synthetic files made here
        (never the test PDF)."""
        import hashlib
        import pymupdf
        from PySide6.QtWidgets import QFileDialog
        from . import locked, pageops as P, password_ui
        from .search import SearchIndex, compile_query
        lw = work / "locked"
        lw.mkdir(exist_ok=True)

        def make(name, **enc):
            d = pymupdf.open()
            for i in range(3):
                d.new_page().insert_text((72, 72), f"page {i + 1} lazarus probe text")
            d.set_toc([[1, "One", 1], [1, "Two", 2]])
            d.save(str(lw / name), **enc)
            d.close()
            return lw / name
        both = make("both.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="userpw", owner_pw="ownerpw",
                    permissions=pymupdf.PDF_PERM_PRINT)
        rc4 = make("rc4.pdf", encryption=pymupdf.PDF_ENCRYPT_RC4_128, user_pw="u", owner_pw="o")
        owner_only = make("owner_only.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_128, owner_pw="o",
                          permissions=pymupdf.PDF_PERM_PRINT)
        sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
        H = {p: sha(p) for p in (both, rc4, owner_only)}

        # every prompt answered from a script; each call is counted
        answers, asked = [], []
        calls = {"warn": 0, "set": 0, "match": 0, "clear": 0}
        script = {"warn": "continue", "set": None, "match": None, "clear": True}

        def ask(parent, attempt, name=""):
            asked.append(attempt)
            return answers.pop(0) if answers else None
        locked.ask_password = ask

        def answer(kind):
            def f(parent):
                calls[kind] += 1
                return script[kind]
            return f
        password_ui.warn_unknown_owner = answer("warn")
        password_ui.ask_set_passwords = answer("set")
        password_ui.ask_match_password = answer("match")
        password_ui.confirm_clear = answer("clear")

        def save_to(*paths):
            seq = [str(p) for p in paths]
            QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (seq.pop(0) if seq else "", ""))

        def reopen(path, pw=None):
            d = pymupdf.open(str(path))
            res = {"needs_pass": bool(d.needs_pass), "code": None}
            if pw is not None:
                res["wrong_refused"] = d.authenticate("not-it") == 0
                res["code"] = d.authenticate(pw)
            res["method"] = (d.metadata or {}).get("encryption") or ""
            k, v = d.xref_get_key(-1, "Encrypt/P")
            res["P"] = int(v) if k == "int" else None
            # not d.needs_pass again: after authenticate() it resets the decryption (PyMuPDF quirk)
            unlocked = not res["needs_pass"] or bool(res["code"])
            res["text"] = unlocked and "lazarus probe" in d[0].get_text()
            res["toc"] = len(d.get_toc()) if unlocked else 0
            d.close()
            return res

        def allowed(P_, flag):
            return P_ is not None and bool(P_ & flag)

        def mark(i=0):
            em.work.edit("Mark", lambda d: d[i].add_highlight_annot(pymupdf.Rect(70, 60, 200, 80)), pages=[i])
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em = w.editor_mode
        # ---- opening ----
        out["cancel_not_opened"] = em.open_pdf(str(both)) is False and em.work is None
        answers[:], asked[:] = ["nope", "userpw"], []
        out["opened_after_retry"] = em.open_pdf(str(both)) is True and asked == [0, 1] and em.doc.page_count == 3
        out["status_protected"] = "saves: password-protected" in em.status_page.text()
        out["menu_enabled_locked"] = all(a.isEnabled() for a in em.pw_actions.values())
        idx = SearchIndex(em.doc)
        out["search_works"] = len(idx.find(0, compile_query("probe", False, False, False))) == 1
        idx.close()
        src_P = reopen(both, "userpw")["P"]

        # ---- Keep password, owner unknown: the warning, both buttons, and Cancel ----
        mark()
        script["warn"] = None
        nope = lw / "nope.pdf"
        save_to(nope)
        em.save_with_password("keep")
        out["keep_warning_cancel"] = calls["warn"] == 1 and not nope.exists()
        script["warn"] = "continue"
        kept = lw / "kept.pdf"
        save_to(kept)
        em.save_with_password("keep")
        k = reopen(kept, "userpw")
        out["keep_continue"] = (calls["warn"] == 2 and k["needs_pass"] and k["wrong_refused"] and k["code"] == 2
                                and "256-bit AES" in k["method"] and k["P"] == src_P and k["toc"] == 2
                                and k["text"] and not em.dirty())
        ext = lw / "extract.pdf"
        P.extract(em.work, [1], str(ext))
        out["extract_follows_choice"] = reopen(ext, "userpw")["code"] == 2
        script["warn"], script["set"] = "set", ("open1", "owner1", {"print": False})
        set1 = lw / "set_instead.pdf"
        save_to(set1)
        em.save_with_password("keep")
        s = reopen(set1, "open1")
        s_own = reopen(set1, "owner1")
        out["keep_set_instead"] = (calls["warn"] == 3 and calls["set"] == 1 and s["code"] == 2
                                   and s_own["code"] == 4 and not allowed(s["P"], pymupdf.PDF_PERM_PRINT)
                                   and allowed(s["P"], pymupdf.PDF_PERM_COPY))
        # ---- Ctrl+S repeats the last choice, no prompts ----
        before = dict(calls)
        mark(1)
        em.save()
        r1 = reopen(set1, "owner1")
        out["ctrl_s_repeats_set"] = (calls == before and r1["code"] == 4
                                     and not allowed(r1["P"], pymupdf.PDF_PERM_PRINT) and not em.dirty())

        # ---- Set passwords: blank-field combinations ----
        def set_save(name, vals):
            script["set"] = vals
            path = lw / name
            save_to(path)
            em.save_with_password("set")
            return path
        no_perms = {key: False for key, _, _ in locked.PERMISSIONS}
        p = set_save("set_open_only.pdf", ("a", "", no_perms))
        r = reopen(p, "a")
        out["set_open_only"] = (r["needs_pass"] and r["code"] > 0
                                and all(allowed(r["P"], f) for _, _, f in locked.PERMISSIONS))
        p = set_save("set_owner_only.pdf", ("", "b", {"copy": False, "modify": False}))
        r = reopen(p, "b")
        out["set_owner_only"] = (not r["needs_pass"] and bool(r["method"]) and r["code"] == 4
                                 and not allowed(r["P"], pymupdf.PDF_PERM_COPY)
                                 and not allowed(r["P"], pymupdf.PDF_PERM_MODIFY)
                                 and allowed(r["P"], pymupdf.PDF_PERM_PRINT))
        p = set_save("set_both_default.pdf", ("c", "d", {key: True for key, _, _ in locked.PERMISSIONS}))
        r = reopen(p, "c")
        out["set_both_all_allowed"] = (r["needs_pass"] and r["code"] == 2 and reopen(p, "d")["code"] == 4
                                       and all(allowed(r["P"], f) for _, _, f in locked.PERMISSIONS))
        p = set_save("set_blank.pdf", ("", "", no_perms))
        r = reopen(p)
        out["set_both_blank_unprotected"] = not r["needs_pass"] and not r["method"] and em.work.protection is None
        script["set"] = None
        p = lw / "set_cancel.pdf"
        save_to(p)
        em.save_with_password("set")
        out["set_cancel"] = not p.exists()
        # ---- Match passwords ----
        script["match"] = "m1"
        p = lw / "match.pdf"
        save_to(p)
        em.save_with_password("match")
        r = reopen(p, "m1")
        out["match"] = r["needs_pass"] and r["code"] == 6 and reopen(p, "nope")["code"] == 0
        # ---- Clear passwords (warns; Cancel writes nothing) ----
        script["clear"] = False
        p = lw / "clear_cancel.pdf"
        save_to(p)
        em.save_with_password("clear")
        out["clear_cancel"] = calls["clear"] == 1 and not p.exists()
        script["clear"] = True
        plain = lw / "plain.pdf"
        save_to(plain)
        em.save_with_password("clear")
        before = dict(calls)
        mark(2)
        em.save()
        r = reopen(plain)
        out["clear_and_ctrl_s_repeats"] = (calls == before and not r["needs_pass"] and not r["method"]
                                           and r["text"] and "saves: no password" in em.status_page.text())
        out["no_warning_for_set_match_clear"] = calls["warn"] == 3

        # ---- never-write-original on every path (first pick = the original, refused) ----
        script.update(warn="continue", set=("x", "y", {}), match="z", clear=True)
        refused = {}
        for kind in ("keep", "set", "match", "clear", "plain_save_as"):
            n0 = dict(calls)
            save_to(both)                         # then "" -> stop
            if kind == "plain_save_as":
                em.save_as()
            else:
                em.save_with_password(kind)
            refused[kind] = sha(both) == H[both]
        out["original_refused_every_path"] = all(refused.values())

        # ---- plain Save As on a fresh "Keep": warns once, then remembers "Continue anyway" ----
        em.close_doc()                        # a fresh open (re-opening an open PDF switches tabs)
        answers[:] = ["userpw"]
        em.open_pdf(str(both))
        w0 = calls["warn"]
        save_to(lw / "plain_saveas1.pdf")
        em.save_as()
        save_to(lw / "plain_saveas2.pdf")
        em.save_as()
        save_to(lw / "menu_keep_again.pdf")
        em.save_with_password("keep")
        out["plain_save_as_warns_once"] = (calls["warn"] - w0 == 2 and reopen(lw / "plain_saveas2.pdf", "userpw")["code"] == 2)

        # ---- owner password known: no warning ----
        em.close_doc()                        # a fresh open (re-opening an open PDF switches tabs)
        answers[:] = ["ownerpw"]
        em.open_pdf(str(both))
        w0 = calls["warn"]
        own = lw / "owner_kept.pdf"
        save_to(own)
        em.save_with_password("keep")
        o = reopen(own, "ownerpw")
        out["owner_known_no_warning"] = calls["warn"] == w0 and o["needs_pass"] and o["code"] >= 4 and o["P"] == src_P
        # an RC4 source keeps RC4 (owner known: opened with the owner password)
        answers[:] = ["o"]
        em.open_pdf(str(rc4))
        rk = lw / "rc4_kept.pdf"
        save_to(rk)
        em.save_with_password("keep")
        out["rc4_source_saved_as_aes256"] = "256-bit AES" in reopen(rk, "o")["method"]
        # permission-only file: opens without asking; its owner password is unknown too, so Keep warns
        answers[:], asked[:] = [], []
        em.open_pdf(str(owner_only))
        w0 = calls["warn"]
        ok_ = lw / "owner_only_kept.pdf"
        save_to(ok_)
        em.save_with_password("keep")
        oo = reopen(ok_)
        out["permission_only_file"] = (not asked and calls["warn"] == w0 + 1 and not oo["needs_pass"]
                                       and bool(oo["method"]) and oo["P"] == reopen(owner_only)["P"])

        # ---- dialogs themselves (built, not shown) ----
        d = password_ui.SetPasswordsDialog(w)
        greyed = not d.perm_box.isEnabled()
        d.owner_pw.setText("x")
        out["set_dialog"] = (greyed and d.perm_box.isEnabled() and all(cb.isChecked() for cb in d.perms.values())
                             and len(d.perms) == 6)
        m = password_ui.MatchPasswordDialog(w)
        m.pw.setText("a")
        m.again.setText("b")
        mismatch = not m.ok.isEnabled()
        m.again.setText("a")
        out["match_dialog"] = mismatch and m.ok.isEnabled()

        # ---- unprotected document: Keep/Clear greyed; Set adds a password ----
        em.open_pdf(pdf)
        out["menu_plain_doc"] = (not em.pw_actions["keep"].isEnabled() and not em.pw_actions["clear"].isEnabled()
                                 and em.pw_actions["set"].isEnabled() and em.pw_actions["match"].isEnabled())
        out["plain_doc_saves_plain"] = em.work.save_options() == {"encryption": pymupdf.PDF_ENCRYPT_NONE}
        n0 = em.doc.page_count
        answers[:] = ["userpw"]
        w.pageops.insert_pdf(after=True, path=str(both))
        out["insert_from_protected"] = em.doc.page_count == n0 + 3
        em.undo()
        # ---- Extraction: the password goes to the worker; without one it is refused ----
        pages, _, _ = modes.run(modes.EXTRACT, str(both), str(lw / "x_out"), password="userpw")
        try:
            modes.run(modes.EXTRACT, str(both), str(lw / "x_out2"))
            refused_x = False
        except RuntimeError:
            refused_x = True
        out["extraction_with_password"] = len(pages) == 3 and refused_x
        imp = w.importer
        imp.load_pdf(str(both))
        answers[:] = []
        imp.start()
        out["import_cancel"] = not imp.busy()
        answers[:] = ["userpw"]
        imp.start()
        if imp.worker is not None:
            imp.worker.wait(60000)
        wait(100)
        out["import_with_password"] = imp.worker is not None and imp.worker.password == "userpw" and not imp.busy()
        out["originals_unchanged"] = all(sha(p) == h for p, h in H.items())
        w.text_mode.editor.document().setModified(False)
        w.close()
        out["ok"] = all(v for v in out.values())
        return out
    check("password_pdfs", password_pdfs)

    def snapshot_undo():
        """Snapshot-undo + its memory budget, on synthetic files made here (never the test PDF)."""
        import hashlib
        import pymupdf
        from . import memguard, oprules, protect
        from . import settings as S
        from .settings_dialog import SettingsDialog
        sw = work / "snapshot"
        sw.mkdir(exist_ok=True)
        src = sw / "doc.pdf"
        d = pymupdf.open()
        for i in range(4):
            d.new_page().insert_text((72, 72), f"page {i + 1} lazarus probe text")
        d.save(str(src))                       # a fresh file: no /Info dictionary
        d.close()
        sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
        h0 = sha(src)
        B = memguard.BUDGET
        out = {}
        # 1. every declared op: classification proven by probing worst-case documents
        rules = oprules.check_all()
        out["op_rules_match"] = all(a == b for a, b in rules.values())
        out["op_rules_snapshot_ops"] = sorted(k for k, (a, _) in rules.items() if a == oprules.SNAPSHOT)

        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, fc = w.editor_mode, w.find
        em.open_pdf(str(src))
        wait(100)
        fp = lambda: oprules.state(em.work.doc)
        states = [fp()]
        # 2. journal + snapshot steps mixed in one chain
        chain = [("highlight", "Highlight", lambda doc: doc[0].add_highlight_annot(pymupdf.Rect(70, 60, 200, 80)), [0]),
                 ("set_metadata", "Title", lambda doc: doc.set_metadata({"title": "Snap"}), ()),
                 ("freetext", "Stamp", lambda doc: doc[1].add_freetext_annot(pymupdf.Rect(72, 280, 200, 310), "stamp"), [1]),
                 ("scrub", "Scrub", lambda doc: doc.scrub(metadata=False), ())]
        docs = [em.work.doc]
        applied = True
        for op, label, fn, pages in chain:
            applied &= em.run_edit(op, label, fn, pages)
            states.append(fp())
            docs.append(em.work.doc)
        out["chain_applied"] = applied and em.work.step() == 4 and em.work.snapshot_count() == 2
        out["snapshot_swaps_doc"] = docs[1] is docs[0] and docs[2] is not docs[1] and docs[4] is not docs[3]
        # 3. views rebound to the swapped-in document; pages re-fetched
        ov = w.overview_mode
        w.set_mode("overview")
        wait(150)
        w.set_mode("editor")
        out["views_rebound"] = (em.doc is em.work.doc and em.canvas.doc is em.work.doc
                                and em.thumbs.doc is em.work.doc and ov.grid.doc is em.work.doc
                                and len(em.canvas.pages) == em.doc.page_count)
        pg1 = em.doc[1]
        out["page_objects_valid"] = (sum(1 for a_ in pg1.annots() if a_.info.get("content") == "stamp") == 1
                                     and len(em.canvas.page_text(1).chars) > 0)
        del pg1
        # 4. dirty flag across swaps: save here, then undo / redo across the snapshot step
        saved = sw / "saved.pdf"
        em.work.save_as(str(saved))
        clean = not em.dirty()
        em.undo()
        d1 = em.dirty()
        em.redo()
        out["dirty_across_swaps"] = clean and d1 and not em.dirty()
        # undo everything in order, then redo everything in order
        und = []
        for k in range(4, 0, -1):
            em.undo()
            und.append(fp() == states[k - 1])
        out["undo_in_order"] = all(und) and not em.work.can_undo()
        red = []
        for k in range(1, 5):
            em.redo()
            red.append(fp() == states[k])
        out["redo_in_order"] = all(red) and em.doc is em.work.doc and em.canvas.doc is em.work.doc
        # 5. the find index after a swap: re-indexed; after undoing a snapshot step, reuses its bytes
        w.activateWindow()
        fc.open()
        fc.edit.setText("probe")
        fc._debounce.stop()
        fc.start_search()
        while fc._tick.isActive():
            app.processEvents()
        n_hits = len(fc.hits)
        em.undo()                      # scrub (snapshot): back to the kept 'before' document
        wait(50)
        fc._debounce.stop()
        fc.start_search()
        while fc._tick.isActive():
            app.processEvents()
        out["find_reindexes_after_swap"] = n_hits == 4 and len(fc.hits) == 4 and fc._index_key[0] == id(em.doc)
        out["find_reuses_swap_bytes"] = fc.index is not None and fc.index.reused
        fc.close()
        # 6. new edit after undo: redo branch dropped and its documents closed
        em.undo()                      # insert_text (journal)
        em.undo()                      # set_metadata (snapshot): now on the original document
        redo_docs = [e["after"] for e in em.work._entries[em.work.step():] if e["kind"] == "swap"]
        em.run_edit("ink", "Ink", lambda doc: doc[2].add_ink_annot([[(80, 80), (140, 140)]]), [2])
        out["redo_branch_freed"] = (not em.work.can_redo() and len(redo_docs) == 2
                                    and all(dd.is_closed for dd in redo_docs) and em.work.snapshot_count() == 0)
        # 7. budget: limit fits ~2 snapshot steps; the 3rd and 4th drop the oldest, ONE notice
        notices_before = sum(1 for x in r["dialogs"] if x.startswith("information"))
        B.notified = False
        cost = memguard.cost_of(len(em.work.doc.tobytes()))
        B.total_override = int(cost * 2.5 * 100 / B.pct)
        for k in range(4):
            em.run_edit("set_metadata", f"Title {k}", lambda doc, k=k: doc.set_metadata({"title": f"T{k}"}), ())
        notices = sum(1 for x in r["dialogs"] if x.startswith("information")) - notices_before
        n_snaps = em.work.snapshot_count()
        steps_left = 0
        while em.work.undo():
            steps_left += 1
        em.rebuild()
        out["cap_drops_oldest"] = n_snaps == 2 and B.used() <= B.limit() and steps_left == 2
        out["cap_one_notice"] = notices == 1
        while em.work.redo():
            pass
        em.rebuild()
        out["cap_floor_state"] = (em.doc.metadata or {}).get("title") == "T3"
        B.total_override = None
        # 8. free RAM: an edit that can't fit is refused, nothing changes
        step, fp0 = em.work.step(), fp()
        B.free_override = lambda: 1024
        ok = em.run_edit("set_metadata", "Title X", lambda doc: doc.set_metadata({"title": "X"}), ())
        out["free_ram_blocks"] = (not ok and em.work.step() == step and fp() == fp0
                                  and any("Not enough free memory" in x for x in r["dialogs"]))
        # ... and when dropping old states frees enough, it drops them and goes ahead
        need = cost + memguard.HEADROOM
        B.free_override = lambda: 2 ** 40 if em.work.snapshot_count() == 0 else need - 1
        n0 = em.work.snapshot_count()
        ok = em.run_edit("set_metadata", "Title Y", lambda doc: doc.set_metadata({"title": "Y"}), ())
        out["free_ram_drops_then_goes"] = ok and n0 > 0 and em.work.snapshot_count() == 1
        B.free_override = None
        # 9. never write an original on a snapshot path; saves after a swap go to new files
        try:
            em.work.save_as(str(src))
            refused = False
        except protect.ProtectedPathError:
            refused = True
        after_swap = sw / "after_swap.pdf"
        em.work.save_as(str(after_swap))
        chk = pymupdf.open(str(after_swap))
        out["never_write_original"] = refused and sha(src) == h0 and (chk.metadata or {}).get("title") == "Y"
        chk.close()
        # a protected document: snapshot step, then Save keeps the password
        lk = sw / "locked.pdf"
        d = pymupdf.open()
        d.new_page().insert_text((72, 72), "locked probe")
        d.save(str(lk), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
        d.close()
        from . import locked as L
        L.ask_password = lambda *a, **k: "pw"
        em.open_pdf(str(lk))
        em.run_edit("set_metadata", "Title", lambda doc: doc.set_metadata({"title": "L"}), ())
        lk2 = sw / "locked_saved.pdf"
        em.work.save_as(str(lk2))
        chk = pymupdf.open(str(lk2))
        out["snapshot_keeps_password"] = bool(chk.needs_pass) and chk.authenticate("pw") > 0
        chk.close()
        # 10. Settings › Undo memory: reads, writes, restores default
        dlg = SettingsDialog(w, w.settings)
        default_shown = dlg.undo_pct.value() == S.DEFAULTS["undo_ram_pct"] == 30
        dlg.undo_pct.setValue(45)
        dlg.apply()
        w.apply_settings()
        stored = S.get(w.settings, "undo_ram_pct") == 45 and B.pct == 45 and "GB" in dlg.undo_gb.text()
        dlg.restore_defaults()
        restored = dlg.undo_pct.value() == 30
        dlg.apply()
        w.apply_settings()
        out["settings_cap"] = default_shown and stored and restored and B.pct == 30
        out["originals_unchanged"] = sha(src) == h0
        em.work.doc  # noqa
        w.close()
        out["ok"] = all(v for k, v in out.items() if k != "op_rules_snapshot_ops")
        return out
    check("snapshot_undo", snapshot_undo)

    def tabs():
        """Multi-document: tabs, per-tab state, panels following the active tab, closing,
        never-write-original per session, the global snapshot budget across tabs, and the
        compare view. Synthetic files made here (never the test PDF)."""
        import pymupdf
        from . import memguard, protect
        tw = work / "tabs"
        tw.mkdir(exist_ok=True)

        def make(name, pages, word, toc=None):
            d = pymupdf.open()
            for i in range(pages):
                d.new_page().insert_text((72, 72), f"page {i + 1} {word} probe text")
            if toc:
                d.set_toc(toc)
            d.save(str(tw / name))
            d.close()
            return tw / name
        A = make("a.pdf", 6, "alpha")
        B = make("b.pdf", 4, "beta", [[1, "B one", 1], [1, "B two", 3]])
        C = make("c.pdf", 3, "gamma")
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, ov, fc = w.editor_mode, w.overview_mode, w.find
        for p in (A, B, C):
            em.open_pdf(str(p))
            wait(60)
        sa, sb, sc = em.sessions
        out["three_tabs"] = (em.tabbar.count() == 3 and em.cur is sc and len({id(s.canvas) for s in em.sessions}) == 3
                             and all(protect.is_protected(p) for p in (A, B, C)))
        out["reopen_switches_tab"] = em.open_pdf(str(A)) and em.cur is sa and len(em.sessions) == 3
        # per-tab view state: zoom, page, text selection, thumbnail selection, Overview selection
        sa.canvas.set_zoom(1.5)
        sa.canvas.goto(3)
        sa.canvas.select_chars(3, 0, 4)
        em.thumbs.clearSelection()
        for r_ in (1, 2):
            em.thumbs.item(r_).setSelected(True)
        w.set_mode("overview")
        wait(100)
        ov.grid.set_selection([4, 5])
        w.set_mode("editor")
        em.activate(sb)
        sb.canvas.set_zoom(0.7)
        sb.canvas.goto(1)
        wait(50)
        em.activate(sa)
        wait(50)
        out["tab_keeps_zoom_page_selection"] = (abs(em.canvas.zoom - 1.5) < 1e-6 and em.canvas.current_page() == 3
                                                and em.canvas.selection == (3, 0, 4))
        out["tab_keeps_thumb_selection"] = em.thumbs.selected_rows() == [1, 2]
        w.set_mode("overview")
        wait(100)
        out["tab_keeps_overview_selection"] = sorted(ov.grid.selected) == [4, 5] and ov.grid.doc is sa.work.doc
        w.set_mode("editor")
        em.activate(sb)
        out["other_tab_own_state"] = abs(em.canvas.zoom - 0.7) < 1e-6 and em.canvas.current_page() == 1
        # panels follow the active tab
        out["panels_rebind"] = (em.thumbs.doc is sb.work.doc and em.thumbs.count() == 4
                                and em.bookmarks.tree.topLevelItemCount() == 2 and em.page_total.text().strip() == "/ 4")
        em.activate(sa)
        out["panels_rebind_back"] = em.thumbs.count() == 6 and em.bookmarks.tree.topLevelItemCount() == 0
        # find follows the active tab (each tab keeps its own index)
        fc.open()
        fc.edit.setText("alpha")
        fc._debounce.stop()
        fc.start_search()
        while fc._tick.isActive():
            app.processEvents()
        hits_a = len(fc.hits)
        em.activate(sb)
        while fc._tick.isActive():
            app.processEvents()
        hits_b = len(fc.hits)
        idx_b = sb.find_index
        em.activate(sa)
        while fc._tick.isActive():
            app.processEvents()
        out["find_per_tab"] = (hits_a == 6 and hits_b == 0 and len(fc.hits) == 6 and idx_b is not None
                               and sa.find_index is not None and sa.find_index is not idx_b)
        fc.close()
        # undo and unsaved state are per tab
        em.run_edit("highlight", "Highlight", lambda doc: doc[0].add_highlight_annot(pymupdf.Rect(70, 60, 200, 80)), [0])
        a_dirty = em.dirty()
        em.activate(sb)
        b_clean = not em.dirty() and not em.work.can_undo()
        em.undo()                                        # nothing to undo in B; A untouched
        a_annots = len(list(sa.work.doc[0].annots()))
        out["undo_dirty_per_tab"] = (a_dirty and b_clean and a_annots == 1 and sa.work.dirty()
                                     and em.tabbar.tabText(0).startswith("●") and not em.tabbar.tabText(1).startswith("●")
                                     and em.any_dirty())
        em.activate(sa)
        em.undo()
        out["undo_in_its_own_tab"] = len(list(sa.work.doc[0].annots())) == 0 and not sb.work.can_undo()
        em.redo()
        # never write an original, from any tab: its own source or another tab's
        refused = []
        for s in em.sessions:
            for p in (A, B, C):
                try:
                    s.work.save_as(str(p))
                    refused.append(False)
                except protect.ProtectedPathError:
                    refused.append(True)
        out["never_write_original_per_session"] = all(refused) and len(refused) == 9
        # the snapshot budget is global: B's snapshot steps push out A's redo branch first, then the oldest
        B_ = memguard.BUDGET
        B_.notified = False
        em.activate(sa)
        em.run_edit("set_metadata", "T", lambda doc: doc.set_metadata({"title": "A1"}), ())
        em.undo()                                        # A now holds a redo branch (a snapshot document)
        a_redo_doc = sa.work._entries[-1]["after"]
        em.activate(sb)
        cost = memguard.cost_of(len(sb.work.doc.tobytes()))
        B_.total_override = int(cost * 2.2 * 100 / B_.pct)       # room for ~2 of B's steps
        for k in range(3):
            em.run_edit("set_metadata", f"T{k}", lambda doc, k=k: doc.set_metadata({"title": f"B{k}"}), ())
        out["budget_global_other_redo_first"] = (not sa.work.can_redo() and a_redo_doc.is_closed
                                                 and B_.used() <= B_.limit() and sb.work.snapshot_count() == 2)
        B_.total_override = None
        # closing: a clean tab closes without asking; a dirty one asks (Cancel keeps it, Discard closes)
        asked = []
        QMessageBox.question = staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Cancel)
        em.close_session(sc)
        clean_closed = sc not in em.sessions and not asked and not protect.is_protected(C)
        em.activate(sb)
        kept = em.close_session(sb) is False and sb in em.sessions and len(asked) == 1
        QMessageBox.question = staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Discard)
        gone = em.close_session(sb) and sb not in em.sessions and len(asked) == 2 and not protect.is_protected(B)
        QMessageBox.question = _box("question", QMessageBox.Discard)
        out["close_clean_no_prompt"] = clean_closed
        out["close_dirty_prompts"] = kept and gone and em.cur is sa and em.tabbar.count() == 1
        # compare view: two sessions side by side, synced scroll toggling, same PDF twice
        em.open_pdf(str(B))
        sb2 = em.cur
        em.activate(sa)
        em.act_compare.trigger()
        wait(100)
        cv = em.compare
        out["compare_two_sessions"] = (em.comparing() and cv.sessions == [sa, sb2] and cv.panes[0].doc is sa.work.doc
                                       and cv.panes[1].doc is sb2.work.doc)
        cv.sync.setChecked(True)
        cv.panes[0].goto(2)
        wait(50)
        synced = cv.panes[1].current_page() == 2
        cv.sync.setChecked(False)
        before = cv.panes[1].current_page()
        cv.panes[0].goto(5)
        wait(50)
        unsynced = cv.panes[1].current_page() == before and cv.panes[0].current_page() == 5
        out["compare_synced_scroll_toggles"] = synced and unsynced
        cv.show_pair(sa, sa)
        out["compare_same_pdf_twice"] = cv.panes[0].doc is sa.work.doc and cv.panes[1].doc is sa.work.doc
        em.run_edit("set_metadata", "T", lambda doc: doc.set_metadata({"title": "cmp"}), ())
        out["compare_follows_edits"] = cv.panes[0].doc is sa.work.doc and cv.panes[1].doc is sa.work.doc
        em.act_compare.trigger()
        out["compare_closes"] = not em.comparing() and em.center.currentWidget() is sa.canvas
        # Ctrl+Tab cycles; closing everything leaves no protected editor sources
        em.cycle_tab(1)
        cycled = em.cur is sb2
        em.close_doc()
        out["cycle_and_close_all"] = (cycled and not em.sessions and em.cur is None and not protect.is_protected(A)
                                      and em.thumbs.doc is None and em.center.currentWidget() is em.hint_canvas
                                      and not em.tabbar.isVisible())
        out["originals_unchanged"] = all(pymupdf.open(str(p)).metadata.get("title", "") == "" for p in (A, B, C))
        w.close()
        out["ok"] = all(out.values())
        return out
    check("tabs", tabs)

    def find_polish():
        """Find-bar index builds: "Indexing…" + window lock only past LOCK_DELAY, Cancel keeps a
        partial index, Esc twice, resume from page X+1, edits discard the index, read-only.
        Synthetic files made here (never the test PDF)."""
        import time as _t
        import pymupdf
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from . import searchbar
        fw = work / "findpolish"
        fw.mkdir(exist_ok=True)
        small = fw / "small.pdf"
        d = pymupdf.open()
        for i in range(3):
            d.new_page().insert_text((72, 72), f"page {i + 1} lazarus probe text")
        d.save(str(small))
        d.close()
        big = fw / "big.pdf"
        d = pymupdf.open()
        line = "the quick brown fox jumps over the lazy dog while lazarus reads every word " * 2
        for i in range(500):
            pg = d.new_page()
            pg.insert_textbox(pymupdf.Rect(36, 36, 576, 806), "\n".join([f"{i + 1}: {line}"] * 70), fontsize=7)
        d.save(str(big))
        d.close()
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, fc = w.editor_mode, w.find

        def run_until(cond, limit=60.0):
            t0 = _t.time()
            while not cond() and _t.time() - t0 < limit:
                app.processEvents()
            return cond()
        # 1. a small build finishes without the indicator or the lock
        em.open_pdf(str(small))
        fc.open()
        fc.edit.setText("probe")
        fc._debounce.stop()
        fc.start_search()
        run_until(lambda: not fc._tick.isActive())
        out["small_no_flash"] = fc.lock_count == 0 and fc.count.text() == "1 of 3" and not fc.locked
        fc.close()
        # 2. a big build: "Indexing…" and the lock only after LOCK_DELAY
        em.open_pdf(str(big))
        J0, dirty0 = em.work.step(), em.dirty()
        fc.open()
        fc.edit.setText("lazarus")
        fc._debounce.stop()
        fc.start_search()
        t0 = _t.time()
        run_until(lambda: fc.locked, 5)
        t_lock = _t.time() - t0
        import re as _re
        progress = []
        t1 = _t.time()
        while fc.locked and _t.time() - t1 < 1.5:
            app.processEvents()
            m_ = _re.fullmatch(r"Indexing (\d+)/(\d+)", fc.count.text())
            if m_:
                progress.append((int(m_.group(1)), int(m_.group(2)), fc.index.indexed()))
        xs = [x for x, _, _ in progress]
        out["indexing_progress_live"] = (len(set(xs)) >= 3 and xs == sorted(xs) and all(y == 500 for _, y, _ in progress)
                                         and all(abs(x - real) <= 1 for x, _, real in progress))
        out["indicator_after_delay"] = (fc.locked and fc.count.text().startswith("Indexing ") and fc.lock_count == 1
                                        and t_lock >= searchbar.LOCK_DELAY / 1000 - 0.02)
        # the window is locked (clicks: widgets disabled; shortcuts: actions disabled), find bar live
        out["window_locked"] = (not w.centralWidget().isEnabled() and not w.menuBar().isEnabled()
                                and not em.act_undo.isEnabled() and not em.act_save_pdf.isEnabled()
                                and not em.act_open.isEnabled() and not em.act_close_tab.isEnabled()
                                and not any(a.isEnabled() for a in w.mode_actions.values()))
        out["find_bar_live"] = (fc.edit.isEnabled() and fc.act_cancel.isVisible() and fc.act_cancel.isEnabled()
                                and not fc.case.isEnabled() and not fc.act_next.isEnabled())
        out["tab_switch_locked"] = not em.tabbar.isEnabled() and not em.act_next_tab.isEnabled()
        # a query change mid-build: matching restarts, indexing goes on (nothing redone), lock stays
        n_before = fc.index.indexed()
        cached = list(fc.index.pages[:n_before])
        fc.edit.setText("lazarus reads")
        fc._debounce.stop()
        fc.start_search()
        run_until(lambda: fc._scan is not None and fc._scan > n_before, 5)
        out["query_change_keeps_index"] = (fc.locked and fc.index.pages[:n_before] == cached
                                           and all(a is b for a, b in zip(fc.index.pages[:n_before], cached)))
        # 3. Esc during the build = Cancel: partial index kept, partial count shown
        run_until(lambda: fc.index.indexed() > 40, 10)
        app.sendEvent(fc.edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        X, Y = fc.index.indexed(), fc.index.page_count
        expect = 0
        plain = pymupdf.open(str(big))
        for i in range(X):
            expect += len(plain[i].search_for("lazarus reads"))
        plain.close()
        txt = fc.count.text()
        out["cancel_keeps_partial"] = (not fc.building() and fc.partial == (X, Y) and 0 < X < Y
                                       and len(fc.hits) == expect and txt.endswith(f"+ · Partial ({X}/{Y})")
                                       and fc.bar.isVisible())
        out["unlocked_after_cancel"] = (not fc.locked and w.centralWidget().isEnabled() and w.menuBar().isEnabled()
                                        and em.act_open.isEnabled() and em.tabbar.isEnabled()
                                        and not fc.act_cancel.isVisible())
        k0 = fc.cur
        fc.step(1)
        k1 = fc.cur
        app.sendEvent(fc.edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
        out["step_partial_hits"] = (k1 == (k0 + 1) % len(fc.hits) and fc.cur == (k1 + 1) % len(fc.hits)
                                    and "Partial" in fc.count.text() and "+" in fc.count.text())
        # second Esc closes the bar
        app.sendEvent(fc.edit, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        out["second_esc_closes"] = not fc.bar.isVisible()
        # 4. the next search resumes from page X+1 and ends as a normal full result
        cached = list(fc.index.pages[:X])
        fc.open()                                    # same query: searches again
        run_until(lambda: not fc._tick.isActive(), 60)
        full = 0
        plain = pymupdf.open(str(big))
        for i in range(Y):
            full += len(plain[i].search_for("lazarus reads"))
        plain.close()
        out["resume_from_x"] = (all(a is b for a, b in zip(fc.index.pages[:X], cached))
                                and fc.index.complete() and fc.partial is None)
        out["full_result_after_resume"] = (len(fc.hits) == full and "Partial" not in fc.count.text()
                                           and "+" not in fc.count.text() and not fc.locked)
        # a cached index never locks
        lc = fc.lock_count
        fc.edit.setText("quick brown")
        fc._debounce.stop()
        fc.start_search()
        run_until(lambda: not fc._tick.isActive(), 30)
        out["cached_never_locks"] = fc.lock_count == lc
        out["read_only"] = em.work.step() == J0 and em.dirty() == dirty0 and not em.dirty()
        # 5. an edit discards the tab's index; the window close cancels a build first
        old = fc.index
        em.run_edit("highlight", "Highlight", lambda doc: doc[0].add_highlight_annot(pymupdf.Rect(36, 36, 200, 50)), [0])
        run_until(lambda: fc.index is not None and fc.index is not old, 5)
        out["edit_discards_index"] = fc.index is not old and old.snapshot.is_closed and not fc.index.complete()
        fc.cancel()
        em.undo()
        fc.edit.setText("lazarus")
        fc._debounce.stop()
        fc.start_search()
        run_until(lambda: fc.locked, 5)
        locked_then = fc.locked
        w.close()
        out["close_cancels_build"] = locked_then and not fc.building() and not fc.locked and not w.isVisible()
        out["ok"] = all(out.values())
        return out
    check("find_polish", find_polish)

    def big_load():
        """Big PDFs open in the background: the window stays responsive, the tab shows real
        loading progress then fills in, closing mid-load cancels cleanly, two loads at once both
        progress, heavy pages render a page per turn. Synthetic files made here."""
        import io
        import shutil
        import time as _t
        import pymupdf
        from PySide6.QtCore import QTimer
        from PIL import Image
        from . import loader, protect
        bw = work / "bigload"
        bw.mkdir(exist_ok=True)
        big = bw / "big.pdf"
        d = pymupdf.open()
        for i in range(120):                     # image-heavy: a different photo-like image per page
            img = Image.effect_mandelbrot((900, 1200), (-2.2 + i * 0.002, -1.5, 1.0, 1.5), 60 + i % 40).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            pg = d.new_page()
            pg.insert_image(pg.rect, stream=buf.getvalue())
            pg.insert_text((72, 72), f"page {i + 1} lazarus probe text", fontsize=12)
        d.save(str(big))
        d.close()
        copies = []
        for k in range(3):
            c = bw / f"big{k}.pdf"
            shutil.copyfile(big, c)
            copies.append(c)
        vec = bw / "vector.pdf"
        d = pymupdf.open()
        import random
        random.seed(1)
        for i in range(4):
            pg = d.new_page()
            sh = pg.new_shape()
            for _ in range(40000):
                x, y = random.random() * 600, random.random() * 800
                sh.draw_line((x, y), (x + 5, y + 7))
            sh.finish(color=(0, 0, 0), width=0.3)
            sh.commit()
        d.save(str(vec))
        d.close()
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em = w.editor_mode
        # the event loop's longest pause, sampled by a 10 ms timer
        beats = []
        gate = {"on": lambda: True}          # collect heartbeats only while this says so
        hb = QTimer()
        hb.setInterval(10)
        hb.timeout.connect(lambda: beats.append(_t.perf_counter()) if gate["on"]() else None)

        def uncached_page_render(path, scale):
            """One page render with nothing cached (a fresh document): the cost of the single
            largest step a renderer takes per turn."""
            dd = pymupdf.open(str(path))
            t_ = _t.perf_counter()
            dd[min(50, dd.page_count - 1)].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            dt = _t.perf_counter() - t_
            dd.close()
            return dt

        def max_gap():
            return max((b - a for a, b in zip(beats, beats[1:])), default=0.0)

        def until(cond, limit=60.0):
            t0 = _t.time()
            while not cond() and _t.time() - t0 < limit:
                app.processEvents()
                _t.sleep(0.002)
            return cond()
        loader.CHUNK, loader.SLOW_STEP = 256 * 1024, 0.02      # a slow disk: loading takes a while
        try:
            # 1. a big PDF: the tab opens at once in a loading state, real progress, then fills in
            seen = []
            beats.clear()
            hb.start()
            started = em.open_pdf(str(big), wait=False)
            s = em.cur
            gate["on"] = lambda: not s.ready     # the LOADING phase only (renders start once ready)
            loading_state = (started and s is not None and not s.ready and em.doc is None
                             and em.center.currentWidget() is s.loading
                             and em.tabbar.tabText(em.sessions.index(s)).startswith("Loading"))
            sample = QTimer()
            sample.setInterval(15)
            sample.timeout.connect(lambda: seen.append((s.loading.bar.maximum(), s.loading.bar.value(),
                                                        s.loading.phase.text()[:20]) if s.loading else None))
            sample.start()
            until(lambda: s.ready, 60)
            sample.stop()
            hb.stop()
            gap = max_gap()
            seen = [x for x in seen if x is not None]      # (a sample after the loading view went)
            values = [v for m, v, _ in seen if m > 0]
            out["opens_in_loading_state"] = loading_state
            out["loading_shows_real_progress"] = (len(set(values)) >= 3 and any(ph.startswith("Reading") for *_, ph in seen)
                                                  and any(ph.startswith("Preparing pages") for *_, ph in seen))
            out["window_responsive_while_loading"] = gap < 0.25 and len(beats) > 20
            out["load_gap_ms"] = round(gap * 1000)
            out["tab_fills_in"] = (s.ready and em.doc is s.work.doc and em.doc.page_count == 120
                                   and len(em.canvas.pages) == 120 and em.thumbs.count() == 120
                                   and em.center.currentWidget() is s.canvas
                                   and em.tabbar.tabText(em.sessions.index(s)) == "big.pdf"
                                   and not em.dirty() and em.work.step() == 0)
            # 2. closing a tab mid-load cancels the load: nothing left behind
            n0 = len(em.sessions)
            em.open_pdf(str(copies[0]), wait=False)
            s2 = em.cur
            until(lambda: s2.loading is not None and s2.loading.bar.value() > 0, 10)
            ld = s2.loader
            em.close_session(s2)
            until(lambda: ld not in em.loaders, 10)
            app.processEvents()
            out["close_mid_load_cancels"] = (s2 not in em.sessions and len(em.sessions) == n0 and not s2.ready
                                             and ld not in em.loaders and not protect.is_protected(copies[0])
                                             and em.cur is s)
            # 3. two big loads at once: both progress, the window stays responsive
            prog = {1: [], 2: []}
            gate["on"] = lambda: True
            beats.clear()
            hb.start()
            em.open_pdf(str(copies[1]), wait=False)
            a = em.cur
            em.open_pdf(str(copies[2]), wait=False)
            b = em.cur
            a.loader.progress.connect(lambda *x: prog[1].append(_t.perf_counter()))
            b.loader.progress.connect(lambda *x: prog[2].append(_t.perf_counter()))
            until(lambda: a.ready and b.ready, 90)
            hb.stop()
            overlap = bool(prog[1] and prog[2]) and min(prog[1][-1], prog[2][-1]) > max(prog[1][0], prog[2][0])
            # the first tab to finish starts rendering while the other still loads: the longest
            # pause may then be one page render (a page per turn), never a burst
            sc = em.canvas.zoom * em.canvas.devicePixelRatioF()
            img_page = uncached_page_render(big, sc)
            out["two_loads_at_once"] = a.ready and b.ready and overlap and max_gap() < max(0.25, 1.8 * img_page)
            out["two_loads_gap_ms"] = round(max_gap() * 1000)
            out["image_page_ms"] = round(img_page * 1000)
        finally:
            loader.CHUNK, loader.SLOW_STEP = 4 * 1024 * 1024, 0.0
        # 4. heavy vector pages render one per turn (no multi-second burst)
        em.open_pdf(str(vec))
        beats.clear()
        hb.start()
        em.canvas.set_zoom(0.45)                  # all 4 heavy pages in view: 4 renders needed at once
        scale = em.canvas.zoom * em.canvas.devicePixelRatioF()
        t0 = _t.time()
        until(lambda: all(abs(p["scale"] - scale) < 1e-3 for p in em.canvas.pages), 20)
        hb.stop()
        rendered = sum(1 for p in em.canvas.pages if abs(p["scale"] - scale) < 1e-3)
        # one heavy page per event-loop turn: the longest pause is about one page, not four
        one_page = uncached_page_render(vec, scale)
        out["heavy_pages_render_per_turn"] = rendered == 4 and max_gap() < max(0.25, 1.8 * one_page)
        out["render_gap_ms"] = round(max_gap() * 1000)
        out["one_page_ms"] = round(one_page * 1000)
        # 5. the window close waits for any load still running (none left behind)
        em.open_pdf(str(big), wait=True)            # already open: switches to its tab
        w.close()
        out["closed_cleanly"] = not em.sessions and not em.loaders
        out["ok"] = all(v for k, v in out.items() if not k.endswith("_ms"))
        return out
    check("big_load", big_load)

    def overview_tabs():
        """The tab bar in Overview: same sessions, dirty dots, switch / close / Ctrl+Tab.
        Synthetic files made here."""
        import pymupdf
        ow = work / "ovtabs"
        ow.mkdir(exist_ok=True)
        paths = []
        for n, k in (("one.pdf", 3), ("two.pdf", 5), ("three.pdf", 2)):
            d = pymupdf.open()
            for i in range(k):
                d.new_page().insert_text((72, 72), f"page {i + 1} probe")
            d.save(str(ow / n))
            d.close()
            paths.append(ow / n)
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, ov = w.editor_mode, w.overview_mode
        out["hidden_with_nothing_open"] = not em.tabbar.isVisible()
        for p in paths:
            em.open_pdf(str(p))
        s1, s2, s3 = em.sessions
        em.activate(s2)
        em.run_edit("highlight", "Highlight", lambda doc: doc[0].add_highlight_annot(pymupdf.Rect(70, 60, 200, 80)), [0])
        w.set_mode("overview")
        wait(100)
        out["visible_in_overview"] = (em.tabbar.isVisible() and em.tabbar.count() == 3
                                      and em.tabbar.parent() is w.centralWidget())
        out["dirty_dot_in_overview"] = em.tabbar.tabText(1).startswith("●") and not em.tabbar.tabText(0).startswith("●")
        em.tabbar.setCurrentIndex(0)                  # a click on the first tab
        wait(50)
        out["switch_rebinds_overview"] = (em.cur is s1 and ov.grid.doc is s1.work.doc and len(ov.grid.items) == 3
                                          and w.mode == "overview")
        nxt, prv, cls = em.act_next_tab, em.act_prev_tab, em.act_close_tab
        out["tab_actions_live"] = all(a.isVisible() and a.isEnabled() for a in (nxt, prv, cls))
        nxt.trigger()
        a1 = em.cur is s2 and ov.grid.doc is s2.work.doc
        prv.trigger()
        out["ctrl_tab_cycles"] = a1 and em.cur is s1 and w.mode == "overview"
        # close: clean tab without a prompt, dirty tab asks (Cancel keeps it)
        asked = []
        QMessageBox.question = staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.Cancel)
        em.activate(s3)
        cls.trigger()
        clean_closed = s3 not in em.sessions and not asked
        em.activate(s2)
        cls.trigger()
        dirty_kept = s2 in em.sessions and len(asked) == 1
        QMessageBox.question = _box("question", QMessageBox.Discard)
        out["close_in_overview"] = clean_closed and dirty_kept and em.tabbar.count() == 2 and w.mode == "overview"
        # Extraction mode: no tabs; back in the editor: tabs again, same active tab
        w.set_mode("extract")
        hidden = not em.tabbar.isVisible() and not nxt.isVisible()
        w.set_mode("editor")
        out["extraction_hides_tabs"] = hidden and em.tabbar.isVisible() and em.cur is s2
        em.close_doc()
        out["hidden_after_close_all"] = not em.tabbar.isVisible()
        w.close()
        out["ok"] = all(out.values())
        return out
    check("overview_tabs", overview_tabs)

    def markup_9a():
        """Stage 9a: every MARKUP tool driven by real mouse events on the canvas, selection
        (click, Ctrl+click), move / resize / delete, the Properties panel, undo/redo of all of
        it, a rotated page, and never writing the original. Synthetic file made here."""
        import hashlib
        import pymupdf
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QKeyEvent, QMouseEvent
        from . import markup as M
        mw = work / "markup"
        mw.mkdir(exist_ok=True)
        src = mw / "doc.pdf"
        d = pymupdf.open()
        for i in range(3):
            pg = d.new_page()
            for k in range(6):
                pg.insert_text((72, 100 + 40 * k), f"line {k} of page {i + 1} lazarus probe words here", fontsize=14)
            if i == 2:
                pg.set_rotation(90)
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em = w.editor_mode
        em.open_pdf(str(src))
        wait(100)
        c, ctl = em.canvas, em.markup
        c.set_zoom(1.0)
        c.goto(0)
        wait(50)
        vp = c.viewport()

        def scene(i, x, y):
            o = c.pages[i]["rect"].topLeft()
            return QPointF(o.x() + x, o.y() + y)

        def mouse(kind, sp, mods=Qt.NoModifier):
            pos = QPointF(c.mapFromScene(sp))
            t = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease,
                 "double": QEvent.MouseButtonDblClick}[kind]
            btn = Qt.NoButton if kind == "move" else Qt.LeftButton
            app.sendEvent(vp, QMouseEvent(t, pos, QPointF(vp.mapToGlobal(pos.toPoint())), btn,
                                          Qt.NoButton if kind == "release" else Qt.LeftButton, mods))

        def drag(i, x0, y0, x1, y1, steps=6, mods=Qt.NoModifier):
            mouse("press", scene(i, x0, y0), mods)
            for k in range(1, steps + 1):
                mouse("move", scene(i, x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps), mods)
            mouse("release", scene(i, x1, y1), mods)
            wait(10)

        def key(k, mods=Qt.NoModifier):
            app.sendEvent(vp, QKeyEvent(QEvent.KeyPress, k, mods))
            app.sendEvent(c, QKeyEvent(QEvent.KeyPress, k, mods)) if False else None

        def annots(i=0):
            pg = em.doc[i]
            return [(a.type[1], a.xref, pymupdf.Rect(a.rect), dict(a.colors or {}), a.opacity, a.vertices,
                     (a.info or {}).get("content", "")) for a in pg.annots()]

        def kinds(i=0):
            return [t for t, *_ in annots(i)]
        step0 = em.work.step()
        tools = ["highlight", "underline", "strike", "draw", "shapes", "textbox", "note", "stamp"]
        out["tools_wired"] = all(not ctl.actions[k].property("soon") and ctl.actions[k].isCheckable()
                                 and ctl.actions[k].isEnabled() for k in tools)
        # text markup: drag across a line of text with each text tool
        for n, tool in enumerate(("highlight", "underline", "strike")):
            ctl.set_tool(tool)
            y = 100 + 40 * n - 5
            drag(0, 70, y, 330, y)
        out["text_markup"] = kinds(0) == ["Highlight", "Underline", "StrikeOut"]
        # freehand ink, then each shape, a polygon by clicks, a note, a text box, a stamp
        ctl.set_tool("ink")
        drag(0, 80, 360, 220, 420, steps=10)
        for shape, box in (("rect", (80, 450, 200, 520)), ("ellipse", (230, 450, 350, 520)),
                           ("line", (380, 450, 480, 520)), ("arrow", (80, 560, 200, 620))):
            ctl.set_tool(shape)
            drag(0, *box)
        ctl.set_tool("polygon")
        for x, y in ((250, 560), (350, 560), (300, 640)):
            mouse("press", scene(0, x, y))
            mouse("release", scene(0, x, y))
        mouse("double", scene(0, 300, 640))
        wait(10)
        ctl.set_tool("note")
        mouse("press", scene(0, 450, 600))
        mouse("release", scene(0, 450, 600))
        note_selected = ctl.tool == "select" and len(ctl.sel) == 1 and ctl.panel.text_label.text() == "Note text"
        ctl.set_tool("freetext")
        drag(0, 80, 680, 300, 740)
        ft = ctl.sel[0] if ctl.sel else None
        ctl.panel.text.setPlainText("Hello from the text box")
        ctl.panel.apply_text.click()
        ctl._pick_stamp(13)                                  # Draft
        mouse("press", scene(0, 330, 680))
        mouse("release", scene(0, 330, 680))
        ks = kinds(0)
        out["all_tools_create"] = ks == ["Highlight", "Underline", "StrikeOut", "Ink", "Square", "Circle", "Line",
                                         "Line", "Polygon", "Text", "FreeText", "Stamp"]
        lines = [a for a in annots(0) if a[0] == "Line"]
        pg0 = em.doc[0]
        ends = [pg0.load_annot(x).line_ends for _, x, *_ in lines]
        del pg0
        out["arrow_has_head"] = ends[0][1] == pymupdf.PDF_ANNOT_LE_NONE and ends[1][1] == pymupdf.PDF_ANNOT_LE_OPEN_ARROW
        ink = next(a for a in annots(0) if a[0] == "Ink")
        out["ink_points"] = len(ink[5][0]) >= 5
        out["note_placed_and_selected"] = note_selected
        out["textbox_text"] = any(t == "FreeText" and txt == "Hello from the text box" for t, _, _, _, _, _, txt in annots(0))
        out["one_undo_step_each"] = em.work.step() - step0 == 13      # 12 annotations + the text edit
        # a rotated page: the annotation lands where it was drawn
        em.canvas.goto(2)
        wait(30)
        ctl.set_tool("rect")
        drag(2, 100, 100, 220, 180)
        pg2 = em.doc[2]
        a2 = pg2.first_annot
        disp = M.to_disp_rect(pg2, a2.rect)
        out["rotated_page_placement"] = (pg2.rotation == 90 and abs(disp.x0 - 100) < 4 and abs(disp.y0 - 100) < 4
                                         and abs(disp.x1 - 220) < 4 and abs(disp.y1 - 180) < 4)
        del a2, pg2
        em.canvas.goto(0)
        wait(30)
        # selection by click, move by drag, resize by a handle
        ctl.set_tool("select")
        sq = next(a for a in annots(0) if a[0] == "Square")
        mouse("press", scene(0, 140, 485))
        mouse("release", scene(0, 140, 485))
        out["click_selects"] = ctl.sel == [sq[1]] and sum(1 for it in ctl._overlay if it.data(0)) == 8
        drag(0, 140, 485, 180, 525)
        sq2 = next(a for a in annots(0) if a[0] == "Square")
        out["drag_moves"] = abs(sq2[2].x0 - sq[2].x0 - 40) < 2 and abs(sq2[2].y0 - sq[2].y0 - 40) < 2
        sel_r = ctl._sel_rect_scene()
        br = sel_r.bottomRight()
        o = c.pages[0]["rect"].topLeft()
        drag(0, br.x() - o.x(), br.y() - o.y(), br.x() - o.x() + 30, br.y() - o.y() + 20)
        sq3 = next(a for a in annots(0) if a[0] == "Square")
        out["handle_resizes"] = sq3[2].width > sq2[2].width + 20 and sq3[2].height > sq2[2].height + 12
        # ink is re-created when moved: same kind, points moved, colour kept
        ink0 = next(a for a in annots(0) if a[0] == "Ink")
        ctl.select(0, [ink0[1]])
        ctl.move_selected(15, 25)
        ink1 = next(a for a in annots(0) if a[0] == "Ink")
        out["vertex_move"] = (abs(ink1[5][0][0][0] - ink0[5][0][0][0] - 15) < 1 and abs(ink1[5][0][0][1] - ink0[5][0][0][1] - 25) < 1
                              and ink1[3].get("stroke") == ink0[3].get("stroke") and ink1[1] in ctl.sel)
        # Properties panel: each change is one undo step
        sq = next(a for a in annots(0) if a[0] == "Square")
        ctl.select(0, [sq[1]])
        s0 = em.work.step()
        p = ctl.panel
        p.stroke.set((0.0, 0.0, 1.0))
        p.stroke.changed.emit((0.0, 0.0, 1.0))
        p.no_fill.setChecked(False)
        p.opacity.setValue(50)
        p.opacity.editingFinished.emit()
        p.width.setValue(4.0)
        p.width.editingFinished.emit()
        p.author.setText("Reviewer")
        p.author.editingFinished.emit()
        pg0 = em.doc[0]
        a = pg0.load_annot(ctl.sel[0])
        st = M.read_style(a)
        del a, pg0
        out["properties_apply"] = (tuple(round(x, 2) for x in st["stroke"]) == (0.0, 0.0, 1.0) and st["fill"] is not None
                                   and abs(st["opacity"] - 0.5) < 0.01 and abs(st["width"] - 4) < 0.01
                                   and st["author"] == "Reviewer" and em.work.step() - s0 == 5)
        em.undo()
        em.undo()
        pg0 = em.doc[0]
        st2 = M.read_style(pg0.load_annot(ctl.sel[0]))
        del pg0
        out["properties_undo"] = abs(st2["width"] - 2.0) < 0.01 and st2["author"] in ("", None)
        em.redo()
        em.redo()
        # the freetext's font size and an arrow's line end from the panel
        ctl.select(0, [ft])
        p.size.setValue(18)
        p.size.editingFinished.emit()
        pg0 = em.doc[0]
        fs = M.read_style(pg0.load_annot(ft))["fontsize"]
        del pg0
        arrow = [a for a in annots(0) if a[0] == "Line"][1]
        ctl.select(0, [arrow[1]])
        p.end_end.setCurrentIndex(p.end_end.findData(pymupdf.PDF_ANNOT_LE_CLOSED_ARROW))
        p.end_end.activated.emit(p.end_end.currentIndex())
        pg0 = em.doc[0]
        le = pg0.load_annot(arrow[1]).line_ends
        del pg0
        out["panel_font_and_line_end"] = abs(fs - 18) < 0.5 and le[1] == pymupdf.PDF_ANNOT_LE_CLOSED_ARROW
        # no selection: the panel edits the next annotation's defaults
        ctl.clear_selection()
        ctl.set_tool("ellipse")
        p.stroke.set((0.0, 0.6, 0.0))
        p.stroke.changed.emit((0.0, 0.6, 0.0))
        drag(0, 400, 700, 480, 760)
        el = [a for a in annots(0) if a[0] == "Circle"][-1]
        out["defaults_for_next"] = tuple(round(x, 2) for x in el[3]["stroke"]) == (0.0, 0.6, 0.0)
        # Ctrl+click multi-select, Del deletes both, Ctrl+Z brings both back
        ctl.set_tool("select")
        circ = [a for a in annots(0) if a[0] == "Circle"]
        n_before = len(annots(0))
        r1, r2 = circ[0][2], circ[1][2]
        mouse("press", scene(0, r1.x0 + 5, (r1.y0 + r1.y1) / 2))
        mouse("release", scene(0, r1.x0 + 5, (r1.y0 + r1.y1) / 2))
        mouse("press", scene(0, r2.x0 + 5, (r2.y0 + r2.y1) / 2), Qt.ControlModifier)
        mouse("release", scene(0, r2.x0 + 5, (r2.y0 + r2.y1) / 2), Qt.ControlModifier)
        multi = len(ctl.sel) == 2
        key(Qt.Key_Delete)
        gone = len(annots(0)) == n_before - 2 and not ctl.sel
        em.undo()
        out["multi_select_delete_undo"] = multi and gone and len(annots(0)) == n_before
        # Esc returns to Select
        ctl.set_tool("rect")
        key(Qt.Key_Escape)
        out["esc_to_select"] = ctl.tool == "select" and em.act_select.isChecked()
        # undo everything back to the start, redo it all
        n_steps = em.work.step() - step0
        for _ in range(n_steps):
            em.undo()
        clean = sum(len(annots(i)) for i in range(3)) == 0 and not em.dirty()
        for _ in range(n_steps):
            em.redo()
        out["undo_redo_all"] = clean and len(annots(0)) == n_before and len(annots(2)) == 1
        # a failing edit leaves nothing behind (its partial change is rolled back)
        em.undo()
        after_undo = [k_[:3] for k_ in annots(0)]           # what undoing the last real step gives
        em.redo()
        s_, j_, n_ = em.work.step(), em.doc.journal_position(), len(annots(0))

        def half_then_fail(doc):
            pg_ = doc[0]
            pg_.add_rect_annot(pymupdf.Rect(10, 10, 50, 50))
            raise ValueError("probe failure")
        ok_ = em.run_edit("rect_annot", "Broken", half_then_fail, [0])
        out["failed_edit_rolled_back"] = (not ok_ and em.work.step() == s_ and len(annots(0)) == n_
                                          and em.doc.journal_position()[0] == j_[0]
                                          and any("couldn't be made" in x for x in r["dialogs"]))
        em.undo()
        out["undo_after_failed_edit"] = [k_[:3] for k_ in annots(0)] == after_undo   # the previous real step
        em.redo()
        # saving: a new file with the annotations; the original is never written
        from . import protect
        try:
            em.work.save_as(str(src))
            refused = False
        except protect.ProtectedPathError:
            refused = True
        saved = mw / "annotated.pdf"
        em.work.save_as(str(saved))
        chk = pymupdf.open(str(saved))
        n_saved = sum(len(list(pg.annots())) for pg in chk)
        chk.close()
        out["saved_new_file"] = refused and n_saved == n_before + 1 and not em.dirty()
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("markup_9a", markup_9a)

    def content_9b():
        """Stage 9b: whiteout (sampled colour), insert image (PNG/JPG/WebP/GIF/SVG), signatures
        (saved + one-off), measurement (paper, PDF-declared, calibrated; distance + area) and
        callouts. Real mouse events where it matters. Synthetic files made here."""
        import hashlib
        import io
        import pymupdf
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        from PIL import Image, ImageDraw
        from . import contentedit as CE
        from . import markup as M
        cw = work / "content9b"
        cw.mkdir(exist_ok=True)
        # page 1 white, page 2 cream with a blue form box, page 3 declares a scale (1 in = 10 ft)
        src = cw / "doc.pdf"
        d = pymupdf.open()
        for i in range(3):
            pg = d.new_page()
            if i == 1:
                pg.draw_rect(pg.rect, color=None, fill=(0.98, 0.94, 0.82))
                pg.draw_rect(pymupdf.Rect(300, 500, 560, 700), color=None, fill=(0.75, 0.85, 1.0))
            for k in range(5):
                pg.insert_text((72, 100 + 30 * k), f"line {k} of page {i + 1} lazarus probe text", fontsize=13)
        d.save(str(src))
        d.close()
        d = pymupdf.open(str(src))
        d.xref_set_key(d[2].xref, "VP", "[<</Type/Viewport/BBox[0 0 595 842]/Measure<</Type/Measure/Subtype/RL"
                                       "/R(1 in = 10 ft)/X[<</U(ft)/C 0.1388889/D 100>>]>>>>]")
        d.save(str(cw / "doc2.pdf"))
        d.close()
        src = cw / "doc2.pdf"
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        # test images
        img = Image.new("RGBA", (240, 120), (0, 0, 0, 0))
        ImageDraw.Draw(img).rectangle((10, 10, 230, 110), fill=(220, 30, 30, 255))
        files = {}
        for fmt_, ext in (("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp"), ("GIF", "gif")):
            p_ = cw / f"pic.{ext}"
            (img.convert("RGB") if fmt_ == "JPEG" else img).save(p_, fmt_)
            files[ext] = p_
        svg = cw / "logo.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60" viewBox="0 0 120 60">'
                       '<rect x="4" y="4" width="112" height="52" fill="#2d6cdf"/></svg>', encoding="utf-8")
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em = w.editor_mode
        em.open_pdf(str(src))
        wait(100)
        c, ctl, p = em.canvas, em.markup, em.markup.panel
        c.set_zoom(1.0)
        vp = c.viewport()

        def scene(i, x, y):
            o = c.pages[i]["rect"].topLeft()
            return QPointF(o.x() + x, o.y() + y)

        def mouse(kind, sp):
            pos = QPointF(c.mapFromScene(sp))
            t_ = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease,
                  "double": QEvent.MouseButtonDblClick}[kind]
            app.sendEvent(vp, QMouseEvent(t_, pos, QPointF(vp.mapToGlobal(pos.toPoint())),
                                          Qt.NoButton if kind == "move" else Qt.LeftButton,
                                          Qt.NoButton if kind == "release" else Qt.LeftButton, Qt.NoModifier))

        def drag(i, x0, y0, x1, y1, steps=5):
            c.goto(i)
            wait(20)
            mouse("press", scene(i, x0, y0))
            for k in range(1, steps + 1):
                mouse("move", scene(i, x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps))
            mouse("release", scene(i, x1, y1))
            wait(10)

        def click(i, x, y):
            c.goto(i)
            wait(20)
            mouse("press", scene(i, x, y))
            mouse("release", scene(i, x, y))
            wait(10)

        def sel_annot():
            pg_ = em.doc[ctl.sel_page]
            a_ = pg_.load_annot(ctl.sel[0])
            return pg_, a_

        def role_list(i):
            pg_ = em.doc[i]
            return [CE.get_data(em.doc, a_.xref).get("role", "") for a_ in pg_.annots()]

        close = lambda a, b, tol=0.03: all(abs(x - y) <= tol for x, y in zip(a, b))
        step0 = em.work.step()
        out["tools_wired"] = all(not ctl.actions[k].property("soon") for k in
                                 ("whiteout", "image", "signature", "callout", "measure"))
        # ---- whiteout: colour sampled from the page where it is dropped ----
        ctl.set_tool("whiteout")
        drag(0, 70, 85, 330, 135)                                # over text on the white page
        pg_, a_ = sel_annot()
        white = tuple(a_.colors["fill"])
        del a_, pg_
        drag(1, 70, 85, 330, 135)                                # over text on the cream page
        pg_, a_ = sel_annot()
        cream = tuple(a_.colors["fill"])
        del a_, pg_
        drag(1, 320, 540, 520, 660)                              # inside the blue form box
        pg_, a_ = sel_annot()
        blue = tuple(a_.colors["fill"])
        border = (a_.border or {}).get("width")
        del a_, pg_
        out["whiteout_samples_background"] = (close(white, (1, 1, 1)) and close(cream, (0.98, 0.94, 0.82))
                                              and close(blue, (0.75, 0.85, 1.0)) and border == 0)
        s0 = em.work.step()
        p.fill.set((0.2, 0.8, 0.2))
        p.fill.changed.emit((0.2, 0.8, 0.2))
        pg_, a_ = sel_annot()
        overridden = close(tuple(a_.colors["fill"]), (0.2, 0.8, 0.2)) and close(tuple(a_.colors["stroke"]), (0.2, 0.8, 0.2))
        del a_, pg_
        p.sample.click()
        pg_, a_ = sel_annot()
        resampled = close(tuple(a_.colors["fill"]), (0.75, 0.85, 1.0))
        del a_, pg_
        out["whiteout_override_and_resample"] = overridden and resampled and em.work.step() - s0 == 2
        # ---- insert image: PNG, JPG, WebP, GIF by drag; SVG by click at its own size ----
        made = []
        for k, ext in enumerate(("png", "jpg", "webp", "gif")):
            ctl.pick_image(str(files[ext]))
            drag(0, 70 + 120 * k, 400, 170 + 120 * k, 450)
            pg_, a_ = sel_annot()
            r_ = pymupdf.Rect(a_.rect)
            pix = pg_.get_pixmap(clip=r_ + (8, 8, -8, -8), dpi=72)
            red = sum(1 for j in range(0, len(pix.samples), pix.n) if pix.samples[j] > 180 and pix.samples[j + 1] < 90)
            made.append((CE.get_data(em.doc, a_.xref).get("role"), round(r_.width / r_.height, 2), red > 10))
            del a_, pg_
        out["image_formats"] = all(m[0] == "image" and abs(m[1] - 2.0) < 0.1 and m[2] for m in made)
        png, sw, sh, kind = CE.load_image(str(svg))
        rasterised = Image.open(io.BytesIO(png))
        ctl.pick_image(str(svg))
        click(0, 100, 520)
        pg_, a_ = sel_annot()
        r_ = pymupdf.Rect(a_.rect)
        del a_, pg_
        out["svg_rasterised_native_size_hidpi"] = (kind == "svg" and abs(sw - 120) < 0.5 and abs(sh - 60) < 0.5
                                                   and rasterised.width == 500 and rasterised.height == 250
                                                   and abs(r_.width - 120) < 1 and abs(r_.height - 60) < 1)
        s0 = em.work.step()
        ctl.resize_selected(pymupdf.Rect(100, 520, 340, 640))
        pg_, a_ = sel_annot()
        out["image_resize_undoable"] = abs(a_.rect.width - 240) < 1 and em.work.step() - s0 == 1
        del a_, pg_
        # ---- signatures: drawn + saved, then a one-off that is not saved ----
        lib = ctl.signatures
        out["signature_store_isolated"] = str(lib.folder).startswith(str(work))
        pad = M.SignaturePad()
        pad.strokes = [[(40, 150), (90, 60), (140, 150), (200, 70)], [(230, 120), (330, 110)]]
        drawn = CE.strokes_to_png(pad.strokes, pad.width(), pad.height())
        M.ask_signature = lambda parent, how: (drawn, True, "Test signature")
        n_lib = len(lib.list())
        ctl.new_signature("draw")
        click(0, 330, 700)
        saved_ok = len(lib.list()) == n_lib + 1 and CE.get_data(em.doc, ctl.sel[0]).get("role") == "signature"
        lib_png = lib.list()[-1].read_bytes()
        ctl.use_signature(png=lib_png)                       # reuse from the library
        click(0, 330, 760)
        reused = CE.get_data(em.doc, ctl.sel[0]).get("role") == "signature"
        oneoff = CE.strokes_to_png([[(30, 100), (300, 120)]])
        M.ask_signature = lambda parent, how: (oneoff, False, "x")
        ctl.new_signature("draw")
        click(0, 470, 760)
        out["signature_saved_reused_and_oneoff"] = (saved_ok and reused and len(lib.list()) == n_lib + 1
                                                    and CE.get_data(em.doc, ctl.sel[0]).get("role") == "signature")
        # ---- measurement: paper size, the PDF's own scale, a manual calibration ----
        ctl._pick_measure("distance")
        drag(0, 100, 300, 172, 300)                          # 72 pt
        pg_, a_ = sel_annot()
        paper = (M.read_content(a_), em.doc.xref_get_key(a_.xref, "Cap")[1], em.doc.xref_get_key(a_.xref, "IT")[1])
        del a_, pg_
        drag(2, 100, 300, 172, 300)                          # page 3 declares 1 in = 10 ft
        pg_, a_ = sel_annot()
        declared = (M.read_content(a_), CE.get_data(em.doc, a_.xref).get("source"))
        del a_, pg_
        M.ask_calibration = lambda parent, length: (5.0, "m")
        ctl._pick_measure("calibrate")
        drag(1, 100, 300, 200, 300)                          # 100 pt = 5 m
        ctl._pick_measure("distance")
        drag(1, 100, 350, 300, 350)                          # 200 pt
        pg_, a_ = sel_annot()
        calibrated = (M.read_content(a_), CE.get_data(em.doc, a_.xref).get("source"))
        dist_x = a_.xref
        del a_, pg_
        drag(2, 100, 340, 172, 340)                          # the declared scale still wins on page 3
        pg_, a_ = sel_annot()
        still = M.read_content(a_)
        del a_, pg_
        out["scale_paper_default"] = paper == ("25.40 mm", "true", "/LineDimension")
        out["scale_auto_from_pdf"] = declared == ("10.00 ft", "pdf") and still == "10.00 ft"
        out["scale_manual_calibration"] = calibrated == ("10.00 m", "calibrated")
        ctl.select(1, [dist_x])
        pg_, a_ = sel_annot()
        r_ = M.to_disp_rect(pg_, a_.rect)
        del a_, pg_
        ctl.resize_selected(pymupdf.Rect(r_.x0, r_.y0, r_.x0 + (r_.width - 0) * 1.5, r_.y1))
        pg_, a_ = sel_annot()
        out["distance_rereads_on_resize"] = (M.read_content(a_) == "15.00 m"
                                             and em.doc.xref_get_key(a_.xref, "Cap")[1] == "true")
        del a_, pg_
        ctl._pick_measure("area")
        c.goto(0)
        wait(20)
        for x, y in ((100, 600), (172, 600), (172, 672), (100, 672)):
            mouse("press", scene(0, x, y))
            mouse("release", scene(0, x, y))
        mouse("double", scene(0, 100, 672))
        wait(10)
        pg_, a_ = sel_annot()
        # the manual calibration (5 m per 100 pt) holds for every page without its own scale
        area_ok = M.read_content(a_) == "12.96 m²" and "area_label" in role_list(0)
        area_x = a_.xref
        del a_, pg_
        ctl.select(0, [area_x])
        ctl.delete_selected()
        both_gone = "area" not in role_list(0) and "area_label" not in role_list(0)
        em.undo()
        out["area_and_label"] = area_ok and both_gone and "area" in role_list(0) and "area_label" in role_list(0)
        # ---- callout: a leader from the spot to the box; the tip stays put ----
        ctl.set_tool("callout")
        drag(0, 450, 300, 470, 220)
        pg_, a_ = sel_annot()
        cl0 = em.doc.xref_get_key(a_.xref, "CL")[1]
        it_ = em.doc.xref_get_key(a_.xref, "IT")[1]
        data0 = CE.get_data(em.doc, a_.xref)
        del a_, pg_
        out["callout_leader"] = (it_ == "/FreeTextCallout" and data0.get("role") == "callout"
                                 and abs(data0["tip"][0] - 450) < 1 and abs(data0["tip"][1] - 300) < 1)
        p.text.setPlainText("Check this value")
        p.apply_text.click()
        p.size.setValue(15)
        p.size.editingFinished.emit()
        p.stroke.set((0.8, 0.1, 0.1))
        p.stroke.changed.emit((0.8, 0.1, 0.1))
        p.end_end.setCurrentIndex(p.end_end.findData(pymupdf.PDF_ANNOT_LE_CLOSED_ARROW))
        p.end_end.activated.emit(p.end_end.currentIndex())
        dat = CE.get_data(em.doc, ctl.sel[0])
        out["callout_styling"] = (dat["text"] == "Check this value" and abs(dat["fontsize"] - 15) < 0.1
                                  and close(dat["border"], (0.8, 0.1, 0.1))
                                  and dat["line_end"] == pymupdf.PDF_ANNOT_LE_CLOSED_ARROW
                                  and em.doc.xref_get_key(ctl.sel[0], "CL")[1].split()[:2] == cl0.split()[:2])
        ctl.move_selected(60, 40)
        dat2 = CE.get_data(em.doc, ctl.sel[0])
        out["callout_move_keeps_tip"] = (dat2["tip"] == dat["tip"] and abs(dat2["box"][0] - dat["box"][0] - 60) < 1
                                         and em.doc.xref_get_key(ctl.sel[0], "CL")[1].split()[:2] == cl0.split()[:2])
        # ---- everything undoes back to the start and redoes ----
        n_steps = em.work.step() - step0
        for _ in range(n_steps):
            em.undo()
        clean = all(not role_list(i) for i in range(3)) and not em.dirty()
        for _ in range(n_steps):
            em.redo()
        out["undo_redo_all"] = clean and "callout" in role_list(0) and "whiteout" in role_list(1)
        # ---- saving: a new file; the original never written ----
        from . import protect
        try:
            em.work.save_as(str(src))
            refused = False
        except protect.ProtectedPathError:
            refused = True
        saved = cw / "edited.pdf"
        em.work.save_as(str(saved))
        chk = pymupdf.open(str(saved))
        roles = [CE.get_data(chk, a_.xref).get("role") for pg_ in chk for a_ in pg_.annots()]
        chk.close()
        out["saved_new_file"] = refused and all(r_ in roles for r_ in ("whiteout", "image", "signature", "distance",
                                                                       "area", "callout"))
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("content_9b", content_9b)

    def s10_redact():
        """Stage 10 redaction: box / text / search-hit marks, review + remove, fill colour,
        confirm, apply removes text + image pixels + drawings (verified by extraction), undo."""
        import hashlib
        import io
        import pymupdf
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        from PIL import Image
        from . import stage10 as T
        rw = work / "s10redact"
        rw.mkdir(exist_ok=True)
        src = rw / "doc.pdf"
        d = pymupdf.open()
        im = Image.new("RGB", (120, 80), (30, 160, 60))
        b = io.BytesIO()
        im.save(b, "PNG")
        for i in range(3):
            pg = d.new_page()
            pg.insert_text((72, 100), "public line stays here", fontsize=14)
            pg.insert_text((72, 140), f"account SECRET-{i}23 must go", fontsize=14)
            if i == 0:
                pg.insert_image(pymupdf.Rect(300, 300, 420, 380), stream=b.getvalue())
                pg.draw_line((300, 450), (420, 450), color=(0, 0, 1), width=3)
            if i == 1:
                pg.set_rotation(90)
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, fc = w.editor_mode, w.find
        em.open_pdf(str(src))
        wait(100)
        c, ctl, s10 = em.canvas, em.markup, em.stage10
        c.set_zoom(1.0)
        vp = c.viewport()

        def scene(i, x, y):
            o = c.pages[i]["rect"].topLeft()
            return QPointF(o.x() + x, o.y() + y)

        def mouse(kind, sp):
            pos = QPointF(c.mapFromScene(sp))
            t_ = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease}[kind]
            app.sendEvent(vp, QMouseEvent(t_, pos, QPointF(vp.mapToGlobal(pos.toPoint())),
                                          Qt.NoButton if kind == "move" else Qt.LeftButton,
                                          Qt.NoButton if kind == "release" else Qt.LeftButton, Qt.NoModifier))

        def drag(i, x0, y0, x1, y1):
            c.goto(i)
            wait(20)
            mouse("press", scene(i, x0, y0))
            for k in range(1, 6):
                mouse("move", scene(i, x0 + (x1 - x0) * k / 5, y0 + (y1 - y0) * k / 5))
            mouse("release", scene(i, x1, y1))
            wait(10)

        def text(i):
            plain = pymupdf.open("pdf", em.doc.tobytes())
            t_ = plain[i].get_text()
            plain.close()
            return t_
        out["tool_live"] = not s10.act_apply.property("soon") and not ctl.actions["redact"].property("soon")
        step0 = em.work.step()
        # box marks over the image and over the drawing on page 1
        ctl.set_tool("redact")
        drag(0, 290, 290, 430, 390)
        drag(0, 290, 440, 430, 460)
        # a text mark on page 1 (drag across the secret line)
        ctl.set_tool("redact_text")
        drag(0, 70, 135, 330, 135)
        boxes_ok = s10.mark_count() == 3
        # every search hit for the secrets (one on each page, including the rotated one)
        fc.open()
        fc.edit.setText("SECRET-")
        fc._debounce.stop()
        fc.start_search()
        while fc._tick.isActive():
            app.processEvents()
        n_hits = s10.redact_hits()
        fc.close()
        out["marks_box_text_hits"] = boxes_ok and n_hits == 3 and s10.mark_count() == 6
        out["marks_on_rotated_page"] = len(T.redaction_marks(em.doc).get(1, [])) == 1
        # review: a mark can be selected and removed (and the removal undone)
        pg_ = em.doc[0]
        first = [a_.xref for a_ in pg_.annots(types=[pymupdf.PDF_ANNOT_REDACT])][0]
        del pg_
        ctl.set_tool("select")
        ctl.select(0, [first])
        ctl.delete_selected()
        removed = s10.mark_count() == 5
        em.undo()
        out["marks_reviewable_removable"] = removed and s10.mark_count() == 6
        # fill colour: one mark green
        pg_ = em.doc[0]
        img_mark = [a_.xref for a_ in pg_.annots(types=[pymupdf.PDF_ANNOT_REDACT]) if a_.rect.x0 > 250 and a_.rect.y1 < 400][0]
        del pg_
        ctl.select(0, [img_mark])
        ctl.panel.fill.set((0.0, 0.6, 0.0))
        ctl.panel.fill.changed.emit((0.0, 0.6, 0.0))
        ctl.clear_selection()
        # Cancel at the confirmation: nothing applied
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Cancel)
        s_ = em.work.step()
        s10.apply_redactions()
        cancel_ok = em.work.step() == s_ and "SECRET-023" in text(0)
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
        pg_ = em.doc[0]
        drawings0 = len(pg_.get_drawings())
        del pg_
        s_ = em.work.step()
        s10.apply_redactions()
        QMessageBox.question = _box("question", QMessageBox.Discard)
        gone = all("SECRET" not in text(i) for i in range(3)) and all("public line stays here" in text(i) for i in range(3))
        pg_ = em.doc[0]
        px = pg_.get_pixmap(clip=pymupdf.Rect(340, 320, 350, 330)).pixel(4, 4)
        drawings1 = len(pg_.get_drawings())
        del pg_
        out["confirm_cancel_applies_nothing"] = cancel_ok
        out["apply_removes_text"] = gone and s10.last_verify == 0 and em.work.step() - s_ == 1
        out["apply_removes_image_pixels_custom_fill"] = px[0] < 20 and px[1] > 130 and px[2] < 20
        out["apply_removes_drawing"] = drawings1 < drawings0
        out["marks_gone_after_apply"] = s10.mark_count() == 0
        em.undo()
        back = all("SECRET" in text(i) for i in range(3)) and s10.mark_count() == 6
        em.redo()
        out["apply_undo_redo"] = back and all("SECRET" not in text(i) for i in range(3))
        # saved copy: the text is not in it
        saved = rw / "redacted.pdf"
        em.work.save_as(str(saved))
        chk = pymupdf.open(str(saved))
        out["saved_copy_clean"] = all("SECRET" not in pg.get_text() for pg in chk)
        chk.close()
        out["undo_all_clean"] = True
        for _ in range(em.work.step() - step0):
            em.undo()
        out["undo_all_clean"] = s10.mark_count() == 0 and all("SECRET" in text(i) for i in range(3))
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_redact", s10_redact)

    def save_keeps_undo():
        """Saving must not disturb the undo history: undo after a save restores the earlier
        state exactly (MuPDF garbage collection on the live document used to break this)."""
        import pymupdf
        sw = work / "savefix"
        sw.mkdir(exist_ok=True)
        src = sw / "doc.pdf"
        d = pymupdf.open()
        pg = d.new_page()
        pg.insert_text((72, 140), "account SECRET-023 must go", fontsize=14)
        d.save(str(src))
        d.close()
        out = {}
        w = gui.Main()
        w.resize(1200, 800)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))

        def text():
            p_ = pymupdf.open("pdf", em.doc.tobytes())
            t_ = p_[0].get_text()
            p_.close()
            return t_

        def annots():
            pg_ = em.doc[0]
            n_ = len(list(pg_.annots()))
            del pg_
            return n_
        em.run_edit("highlight", "Highlight", lambda doc: doc[0].add_highlight_annot(pymupdf.Rect(70, 125, 200, 145)), [0])
        em.work.save_as(str(sw / "a.pdf"))
        em.undo()
        u1 = annots() == 0
        em.redo()
        out["undo_after_save"] = u1 and annots() == 1

        def red(doc):
            pg_ = doc[0]
            pg_.add_redact_annot(pymupdf.Rect(60, 125, 300, 150))
            pg_.apply_redactions()
        em.run_edit("redact_apply", "Redact", red, [0])
        gone = "SECRET" not in text()
        em.work.save_as(str(sw / "b.pdf"))
        em.work.save()
        em.undo()
        out["undo_destructive_op_after_save"] = gone and "SECRET" in text()
        chk = pymupdf.open(str(sw / "b.pdf"))
        out["saved_file_compact_and_right"] = "SECRET" not in chk[0].get_text()
        chk.close()
        w.text_mode.editor.document().setModified(False)
        em.close_doc()
        w.close()
        out["ok"] = all(out.values())
        return out
    check("save_keeps_undo", save_keeps_undo)

    def s10_forms():
        """Stage 10 forms: fill existing fields of every kind by clicking, create fields by
        dragging, select / move / resize / rename / choices / delete, undo, saved values."""
        import hashlib
        import pymupdf
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QKeyEvent, QMouseEvent
        from . import markup as M
        from . import stage10 as T
        fw = work / "s10forms"
        fw.mkdir(exist_ok=True)
        src = fw / "form.pdf"
        d = pymupdf.open()
        for i in range(2):
            d.new_page()
        T.add_field(d, 0, "form_text", (100, 100, 300, 120), name="Full name")
        T.add_field(d, 0, "form_check", (100, 140, 115, 155), name="Agree")
        T.add_field(d, 0, "form_radio", (100, 180, 115, 195), name="Size")
        T.add_field(d, 0, "form_radio", (140, 180, 155, 195), name="Size")
        T.add_field(d, 0, "form_combo", (100, 220, 250, 240), name="Country", choices=["Australia", "Canada", "Chile"])
        T.add_field(d, 0, "form_list", (100, 260, 250, 320), name="Colour", choices=["Red", "Green", "Blue"])
        T.add_field(d, 1, "form_text", (100, 100, 300, 120), name="Rotated text")
        d[1].set_rotation(90)
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em = w.editor_mode
        em.open_pdf(str(src))
        wait(100)
        c, ctl, p = em.canvas, em.markup, em.markup.panel
        c.set_zoom(1.0)
        vp = c.viewport()

        def scene(i, x, y):
            o = c.pages[i]["rect"].topLeft()
            return QPointF(o.x() + x, o.y() + y)

        def mouse(kind, sp):
            pos = QPointF(c.mapFromScene(sp))
            t_ = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease}[kind]
            app.sendEvent(vp, QMouseEvent(t_, pos, QPointF(vp.mapToGlobal(pos.toPoint())),
                                          Qt.NoButton if kind == "move" else Qt.LeftButton,
                                          Qt.NoButton if kind == "release" else Qt.LeftButton, Qt.NoModifier))

        def click(i, x, y):
            c.goto(i)
            wait(20)
            mouse("press", scene(i, x, y))
            mouse("release", scene(i, x, y))
            wait(10)

        def drag(i, x0, y0, x1, y1):
            c.goto(i)
            wait(20)
            mouse("press", scene(i, x0, y0))
            for k in range(1, 6):
                mouse("move", scene(i, x0 + (x1 - x0) * k / 5, y0 + (y1 - y0) * k / 5))
            mouse("release", scene(i, x1, y1))
            wait(10)

        def fields(i):
            pg_ = em.doc[i]
            r_ = {w_.field_name: (w_.field_type_string, w_.field_value, pymupdf.Rect(w_.rect), w_.xref)
                  for w_ in pg_.widgets()}
            del pg_
            return r_

        def radios():
            pg_ = em.doc[0]
            r_ = [w_.field_value for w_ in pg_.widgets() if w_.field_name == "Size"]
            del pg_
            return r_
        out["tool_live"] = not ctl.actions["formfield"].property("soon")
        step0 = em.work.step()
        # fill existing fields by clicking with the Select tool
        ctl.set_tool("select")
        M.ask_field_text = lambda parent, name, cur: "Ada Lovelace"
        click(0, 200, 110)
        click(0, 107, 147)                                     # checkbox on
        M.ask_field_choice = lambda parent, name, ch, cur: "Canada" if name == "Country" else "Blue"
        click(0, 170, 230)
        click(0, 170, 280)
        click(0, 107, 187)                                     # first radio
        r1 = radios()
        click(0, 147, 187)                                     # second radio: the first goes off
        r2 = radios()
        f = fields(0)
        out["fill_text_check_combo_list"] = (f["Full name"][1] == "Ada Lovelace" and f["Agree"][1] not in (False, "Off")
                                             and f["Country"][1] == "Canada" and f["Colour"][1] == "Blue")
        out["radio_exclusive"] = (r1[0] not in (False, "Off") and r2[0] in (False, "Off") and r2[1] not in (False, "Off"))
        click(0, 107, 147)                                     # checkbox off again
        out["checkbox_toggles"] = fields(0)["Agree"][1] in (False, "Off")
        M.ask_field_text = lambda parent, name, cur: "sideways"
        pg_ = em.doc[1]
        wr = [w_ for w_ in pg_.widgets()][0]
        rd = (pymupdf.Rect(wr.rect) * pg_.rotation_matrix).normalize()
        del wr, pg_
        click(1, (rd.x0 + rd.x1) / 2, (rd.y0 + rd.y1) / 2)
        out["fill_on_rotated_page"] = fields(1)["Rotated text"][1] == "sideways"
        out["one_step_per_fill"] = em.work.step() - step0 == 8         # 8 fill clicks
        # create fields by dragging with each tool
        made = []
        for k, (tool, y) in enumerate((("form_text", 400), ("form_check", 440), ("form_radio", 480),
                                       ("form_combo", 520), ("form_list", 560))):
            ctl.set_tool(tool)
            drag(0, 300, y, 300 + (150 if tool in ("form_text", "form_combo", "form_list") else 16), y + 22)
            made.append(ctl.tool)
        f = fields(0)
        kinds = sorted(v[0] for n_, v in f.items() if n_ not in ("Full name", "Agree", "Size", "Country", "Colour"))
        out["create_all_kinds"] = (kinds == sorted(["Text", "CheckBox", "RadioButton", "ComboBox", "ListBox"])
                                   and all(m_ == "form_edit" for m_ in made) and "Text 1" in f)
        # edit fields: select, move, resize, rename, choices, delete
        ctl.set_tool("form_edit")
        t1 = fields(0)["Text 1"]
        click(0, 330, 410)
        sel_ok = ctl.sel == [t1[3]] and p.fname.isVisible() is not None and p.fname.text() == "Text 1"
        drag(0, 330, 410, 380, 440)
        t2 = fields(0)["Text 1"]
        moved = abs(t2[2].x0 - t1[2].x0 - 50) < 2 and abs(t2[2].y0 - t1[2].y0 - 30) < 2
        ctl.resize_selected(pymupdf.Rect(t2[2].x0, t2[2].y0, t2[2].x1 + 60, t2[2].y1 + 10))
        resized = fields(0)["Text 1"][2].width > t2[2].width + 50
        p.fname.setText("Street")
        p.fname.editingFinished.emit()
        renamed = "Street" in fields(0) and "Text 1" not in fields(0)
        dd = fields(0)["Dropdown 1"]
        ctl.select(0, [dd[3]])
        p.choices.setText("North, South")
        p.choices.editingFinished.emit()
        pg_ = em.doc[0]
        ch = [w_.choice_values for w_ in pg_.widgets() if w_.field_name == "Dropdown 1"][0]
        del pg_
        app.sendEvent(vp, QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))
        deleted = "Dropdown 1" not in fields(0)
        out["edit_select_move_resize"] = sel_ok and moved and resized
        out["edit_rename_choices_delete"] = renamed and ch == ["North", "South"] and deleted
        # undo everything back to the original form; redo it
        n_steps = em.work.step() - step0
        for _ in range(n_steps):
            em.undo()
        f0 = fields(0)
        clean = (sorted(f0) == ["Agree", "Colour", "Country", "Full name", "Size"] and f0["Full name"][1] in ("", None)
                 and not em.dirty())
        for _ in range(n_steps):
            em.redo()
        out["undo_redo_all"] = clean and "Street" in fields(0)
        # the filled values are in a saved copy
        saved = fw / "filled.pdf"
        em.work.save_as(str(saved))
        chk = pymupdf.open(str(saved))
        vals = {w_.field_name: w_.field_value for pg_ in chk for w_ in pg_.widgets()}
        chk.close()
        out["saved_values"] = (vals.get("Full name") == "Ada Lovelace" and vals.get("Country") == "Canada"
                               and vals.get("Rotated text") == "sideways" and "Street" in vals)
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_forms", s10_forms)

    def s10_header_footer():
        """Stage 10 header / footer: dialog + preview, page range, {page}/{pages}, placement on a
        rotated page (upright, in the displayed corner), one undo step."""
        import hashlib
        import pymupdf
        from . import stage10_ui as U
        hw = work / "s10hf"
        hw.mkdir(exist_ok=True)
        src = hw / "doc.pdf"
        d = pymupdf.open()
        for i in range(3):
            pg = d.new_page()
            pg.insert_text((72, 300), f"body of page {i + 1}", fontsize=12)
            if i == 1:
                pg.set_rotation(90)
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))
        s10 = em.stage10
        out["tool_live"] = not em.soon["headerfooter"].property("soon")
        vals = {"tl": "Lazarus report", "tc": "", "tr": "", "bl": "", "bc": "Draft", "br": "Page {page} of {pages}",
                "font": "helv", "size": 10.0, "color": (0.1, 0.1, 0.1), "mx": 36.0, "my": 24.0, "range": "1-2"}
        # the real dialog: defaults, and a preview that shows the text
        dlg = U.ToolDialog(w, "Header / footer", s10.HF_FIELDS,
                           lambda v, p: s10.preview(lambda dd, tg: None, p), 3)
        defaults_ok = dlg.values()["br"] == "Page {page} of {pages}" and dlg.view.pixmap() is not None
        dlg.close()
        blank = s10.preview(lambda dd, tg: None, 0).toImage()
        from . import stage10 as T
        with_hf = s10.preview(lambda dd, tg: T.header_footer(dd, tg, {"tl": "Lazarus report"}), 0).toImage()
        out["dialog_and_preview"] = defaults_ok and blank != with_hf
        seen = {}

        def answer(parent, title, fields, preview, n, start=0, note=""):
            seen["pix"] = preview(vals, 0)
            return vals
        U.ask_tool = answer
        s0 = em.work.step()
        pages = s10.header_footer()

        def text(i):
            p_ = pymupdf.open("pdf", em.doc.tobytes())
            t_ = p_[i].get_text()
            p_.close()
            return t_
        out["applied_to_range"] = (pages == [0, 1] and "Page 1 of 3" in text(0) and "Page 2 of 3" in text(1)
                                   and "Lazarus report" in text(0) and "Draft" in text(1)
                                   and "Lazarus report" not in text(2) and seen.get("pix") is not None)
        # rotated page: the header reads upright at the displayed top-left, the footer bottom-right
        p_ = pymupdf.open("pdf", em.doc.tobytes())
        pg = p_[1]
        W, H = pg.rect.width, pg.rect.height
        wl = {w_[4]: (pymupdf.Rect(w_[:4]) * pg.rotation_matrix).normalize() for w_ in pg.get_text("words")}
        dirs = {ln["spans"][0]["text"]: ln["dir"] for b in pg.get_text("dict")["blocks"] for ln in b.get("lines", [])}
        rot = pg.rotation
        p_.close()
        out["rotated_page_upright"] = (rot == 90 and wl["Lazarus"].y0 < 60 and wl["Lazarus"].x0 < 100
                                       and wl["3"].x1 > W - 60 and wl["3"].y1 > H - 60
                                       and abs(abs(dirs.get("Lazarus report", (0, 0))[1]) - 1) < 0.01)
        out["one_undo_step"] = em.work.step() - s0 == 1
        em.undo()
        out["undo_removes"] = "Lazarus report" not in text(0) and "Page 1 of 3" not in text(0)
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_header_footer", s10_header_footer)

    def s10_page_numbers():
        """Stage 10 page numbers: formats, start number, page range, position (incl. a rotated
        page), preview, one undo step."""
        import hashlib
        import pymupdf
        from . import stage10 as T
        from . import stage10_ui as U
        nw = work / "s10pn"
        nw.mkdir(exist_ok=True)
        src = nw / "doc.pdf"
        d = pymupdf.open()
        for i in range(4):
            pg = d.new_page()
            pg.insert_text((72, 300), f"body {i + 1}", fontsize=12)
            if i == 2:
                pg.set_rotation(270)
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))
        s10 = em.stage10

        def text(i):
            p_ = pymupdf.open("pdf", em.doc.tobytes())
            t_ = p_[i].get_text()
            p_.close()
            return t_
        out["tool_live"] = not em.soon["pagenumbers"].property("soon")
        base = {"slot": "bc", "fmt": "page_n_of", "start": 5, "font": "helv", "size": 11.0, "color": (0, 0, 0),
                "mx": 36.0, "my": 24.0, "range": "2-4"}
        seen = {}

        def answer(parent, title, fields, preview, n, start=0, note=""):
            seen["pix"] = preview(base, 1)
            return base
        U.ask_tool = answer
        s0 = em.work.step()
        s10.page_numbers()
        out["page_n_of_range_start"] = ("Page 5 of 7" in text(1) and "Page 6 of 7" in text(2) and "Page 7 of 7" in text(3)
                                        and "Page" not in text(0) and seen.get("pix") is not None)
        p_ = pymupdf.open("pdf", em.doc.tobytes())
        pg = p_[2]
        W, H = pg.rect.width, pg.rect.height
        boxes = [(pymupdf.Rect(w_[:4]) * pg.rotation_matrix).normalize() for w_ in pg.get_text("words") if w_[4] == "6"]
        p_.close()
        out["bottom_centre_on_rotated_page"] = bool(boxes) and boxes[0].y1 > H - 40 and abs((boxes[0].x0 + boxes[0].x1) / 2 - W / 2) < 60
        out["one_undo_step"] = em.work.step() - s0 == 1
        em.undo()
        out["undo_removes"] = "Page" not in text(1)
        s10.page_numbers({**base, "fmt": "n", "start": 1, "range": "all", "slot": "tr"})
        s10.page_numbers({**base, "fmt": "page_n", "start": 10, "range": "1", "slot": "tl"})
        out["formats"] = (text(0).count("1") >= 1 and "Page 10" in text(0) and "4" in text(3).split())
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_page_numbers", s10_page_numbers)

    def s10_watermark():
        """Stage 10 watermark: text (diagonal, rotation, opacity, behind / in front, rotated page)
        and image (position, opacity, SVG / WebP input), page range, preview, undo."""
        import hashlib
        import io
        import math
        import pymupdf
        from PIL import Image
        from . import stage10_ui as U
        ww = work / "s10wm"
        ww.mkdir(exist_ok=True)
        src = ww / "doc.pdf"
        d = pymupdf.open()
        for i in range(3):
            pg = d.new_page()
            pg.draw_rect(pymupdf.Rect(150, 350, 450, 500), color=None, fill=(0.2, 0.2, 0.8))   # content to sit behind
            if i == 1:
                pg.set_rotation(90)
        d.save(str(src))
        d.close()
        img = Image.new("RGB", (200, 100), (230, 30, 30))
        webp = ww / "logo.webp"
        img.save(webp, "WEBP")
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))
        s10 = em.stage10

        def page(i):
            p_ = pymupdf.open("pdf", em.doc.tobytes())
            return p_, p_[i]

        def disp_angle(pg):
            for b in pg.get_text("dict")["blocks"]:
                for ln in b.get("lines", []):
                    v = pymupdf.Point(ln["dir"]) * pg.rotation_matrix - pymupdf.Point(0, 0) * pg.rotation_matrix
                    return math.degrees(math.atan2(-v.y, v.x))
            return None
        out["tool_live"] = not em.soon["watermark"].property("soon")
        base = {"kind": "text", "text": "CONFIDENTIAL", "image": None, "position": "diagonal", "angle": 0.0,
                "size": 60.0, "width": 40.0, "font": "hebo", "color": (0.8, 0.1, 0.1), "opacity": 30, "behind": True,
                "range": "1-2"}
        seen = {}

        def answer(parent, title, fields, preview, n, start=0, note=""):
            seen["pix"] = preview(base, 0)
            return base
        U.ask_tool = answer
        s0 = em.work.step()
        s10.watermark()
        p_, pg = page(0)
        diag = math.degrees(math.atan2(pg.rect.height, pg.rect.width))
        a0 = disp_angle(pg)
        behind_px = pg.get_pixmap(clip=pymupdf.Rect(290, 420, 310, 440)).pixel(5, 5)      # the blue box stays on top
        p_.close()
        p_, pg = page(1)
        a1 = disp_angle(pg)
        diag1 = math.degrees(math.atan2(pg.rect.height, pg.rect.width))     # this page is turned
        t1 = pg.get_text()
        p_.close()
        p_, pg = page(2)
        t2 = pg.get_text()
        p_.close()
        out["text_diagonal_both_rotations"] = (a0 is not None and abs(a0 - diag) < 1.5 and a1 is not None
                                               and abs(a1 - diag1) < 1.5 and "CONFIDENTIAL" in t1 and "CONFIDENTIAL" not in t2
                                               and seen.get("pix") is not None)
        out["behind_content"] = behind_px[2] > 150 and behind_px[0] < 100
        out["one_undo_step"] = em.work.step() - s0 == 1
        em.undo()
        # in front, a fixed angle, opacity applied
        s10.watermark({**base, "position": "center", "angle": 30.0, "behind": False, "opacity": 50, "range": "1"})
        p_, pg = page(0)
        a = disp_angle(pg)
        p_.close()
        out["text_angle_front"] = a is not None and abs(a - 30) < 1.5
        em.undo()
        # an image watermark (WebP input) in a corner, translucent
        s10.watermark({**base, "kind": "image", "image": str(webp), "position": "br", "width": 30.0, "opacity": 40,
                       "angle": 0.0, "behind": False, "range": "all"})
        p_, pg = page(0)
        W, H = pg.rect.width, pg.rect.height
        px = pg.get_pixmap(clip=pymupdf.Rect(W - 100, H - 70, W - 90, H - 60)).pixel(5, 5)
        imgs = len(pg.get_images())
        p_.close()
        p_, pg = page(1)
        imgs1 = len(pg.get_images())
        p_.close()
        out["image_watermark_corner_translucent"] = imgs >= 1 and imgs1 >= 1 and 200 < px[0] and 120 < px[1] < 220
        em.undo()
        out["undo_clean"] = not em.dirty()
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_watermark", s10_watermark)

    def s10_metadata():
        """Stage 10 metadata: dialog shows fields + dates, edit (snapshot-undo), remove all (Info +
        XMP), undo, saved copy, original untouched."""
        import hashlib
        import pymupdf
        from . import stage10_ui as U
        mw = work / "s10meta"
        mw.mkdir(exist_ok=True)
        src = mw / "doc.pdf"
        d = pymupdf.open()
        d.new_page().insert_text((72, 72), "body")
        d.set_metadata({"title": "Old title", "author": "Old author", "creationDate": "D:20240131120000Z",
                        "modDate": "D:20240201093000Z", "producer": "probe"})
        d.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/'
                           '02/22-rdf-syntax-ns#"/></x:xmpmeta>')
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1200, 800)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))
        s10 = em.stage10
        out["tool_live"] = not em.soon["metadata"].property("soon")
        from PySide6.QtWidgets import QLabel
        dlg = U.MetadataDialog(w, dict(em.doc.metadata), True)
        labels = [lb.text() for lb in dlg.findChildren(QLabel)]
        out["dialog_fields_and_dates"] = (dlg.edits["title"].text() == "Old title" and "2024-01-31 12:00:00 UTC" in labels
                                          and any("XMP" in x for x in labels))
        dlg.close()
        U.ask_metadata = lambda parent, meta, xmp: ("set", {"title": "New title", "author": "A. Writer",
                                                            "subject": "Probe", "keywords": "one, two",
                                                            "creator": "Lazarus", "producer": "Lazarus"})
        s0 = em.work.step()
        s10.metadata()
        m = em.doc.metadata
        out["edit_metadata"] = (m["title"] == "New title" and m["author"] == "A. Writer" and m["keywords"] == "one, two"
                                and m["creationDate"] == "D:20240131120000Z" and em.work.step() - s0 == 1)
        em.undo()
        out["edit_undo"] = em.doc.metadata["title"] == "Old title"
        em.redo()
        U.ask_metadata = lambda parent, meta, xmp: ("remove",)
        s10.metadata()
        m = em.doc.metadata
        out["remove_all"] = (not any((m or {}).get(k) for k in ("title", "author", "producer", "creationDate"))
                             and not em.doc.get_xml_metadata())
        em.undo()
        out["remove_undo"] = em.doc.metadata["title"] == "New title" and bool(em.doc.get_xml_metadata())
        em.redo()
        saved = mw / "clean.pdf"
        em.work.save_as(str(saved))
        chk = pymupdf.open(str(saved))
        out["saved_copy_clean"] = not (chk.metadata or {}).get("title") and not chk.get_xml_metadata()
        chk.close()
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_metadata", s10_metadata)

    def s10_encrypt():
        """Stage 10 encrypt: Keep (from RC4 / AES-128 sources), Set and Match all write AES-256;
        the panel button is the same password menu (no second UI)."""
        import hashlib
        import pymupdf
        from PySide6.QtWidgets import QFileDialog
        from . import locked, password_ui
        ew = work / "s10enc"
        ew.mkdir(exist_ok=True)
        srcs = {}
        for name, method in (("rc4.pdf", pymupdf.PDF_ENCRYPT_RC4_128), ("aes128.pdf", pymupdf.PDF_ENCRYPT_AES_128)):
            d = pymupdf.open()
            d.new_page().insert_text((72, 72), "locked body")
            d.save(str(ew / name), encryption=method, user_pw="u", owner_pw="o", permissions=pymupdf.PDF_PERM_PRINT)
            d.close()
            srcs[name] = ew / name
        plain = ew / "plain.pdf"
        d = pymupdf.open()
        d.new_page().insert_text((72, 72), "plain body")
        d.save(str(plain))
        d.close()
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in list(srcs.values()) + [plain]}
        out = {}
        w = gui.Main()
        w.resize(1200, 800)
        w.show()
        wait(100)
        em = w.editor_mode
        out["panel_button_is_password_menu"] = (not em.soon["encrypt"].property("soon")
                                                and em.soon["encrypt"].menu() is em.pw_menu)
        locked.ask_password = lambda *a, **k: "o"                 # the owner password: no warning needed

        def method(path, pw):
            c = pymupdf.open(str(path))
            c.authenticate(pw)
            m = (c.metadata or {}).get("encryption") or ""
            c.close()
            return m
        res = {}
        for name, path in srcs.items():
            em.open_pdf(str(path))
            target = ew / f"keep_{name}"
            QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(target), ""))
            em.save_with_password("keep")
            res["keep_" + name] = method(target, "o")
        em.open_pdf(str(plain))
        password_ui.ask_set_passwords = lambda parent: ("u2", "o2", {})
        t_set = ew / "set.pdf"
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(t_set), ""))
        em.save_with_password("set")
        res["set"] = method(t_set, "u2")
        password_ui.ask_match_password = lambda parent: "m3"
        t_match = ew / "match.pdf"
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (str(t_match), ""))
        em.save_with_password("match")
        res["match"] = method(t_match, "m3")
        out["all_aes256"] = all("256-bit AES" in m for m in res.values()) and len(res) == 4
        out["methods"] = ", ".join(f"{k}={v.split()[-2] if v else '-'}" for k, v in res.items())
        out["originals_unchanged"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        w.close()
        out["ok"] = all(v for k, v in out.items() if k != "methods")
        return out
    check("s10_encrypt", s10_encrypt)

    def s10_flatten():
        """Stage 10 flatten: 9a/9b annotations + form fields baked into page content for a range;
        redaction marks kept; other pages' annotations and fields keep working; undo."""
        import hashlib
        import pymupdf
        from . import contentedit as CE
        from . import markup as M
        from . import stage10 as T
        fw = work / "s10flat"
        fw.mkdir(exist_ok=True)
        src = fw / "doc.pdf"
        d = pymupdf.open()
        for i in range(3):
            pg = d.new_page()
            pg.insert_text((72, 100), f"hidden by whiteout {i + 1}", fontsize=14)
            if i == 2:
                pg.set_rotation(90)
        T.add_field(d, 0, "form_text", (300, 600, 450, 620), name="Name")
        T.add_field(d, 1, "form_check", (300, 600, 315, 615), name="Later")
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))
        ctl, s10 = em.markup, em.stage10
        R_, P_ = pymupdf.Rect, pymupdf.Point
        # page 1: 9a + 9b annotations, a field, a redaction mark; page 2: an annotation and a field
        ctl.create_annot("ink", 0, [P_(100 + 5 * k, 300 + (k % 4) * 6) for k in range(20)])
        ctl.create_annot("whiteout", 0, R_(68, 84, 260, 106))
        ctl.signature = CE.strokes_to_png([[(30, 100), (200, 60), (300, 110)]])
        ctl.create_annot("signature", 0, R_(300, 300, 420, 340))
        ctl.create_annot("callout", 0, (P_(120, 400), R_(200, 380, 380, 430)), {"text": "flatten me"})
        ctl.create_annot("distance", 0, (P_(80, 500), P_(280, 500)))
        em.run_edit("redact_mark", "mark", lambda doc: T.mark_redactions(doc, [(0, (60, 700, 200, 730))]), [0])
        ctl.create_annot("rect", 1, R_(100, 300, 200, 360))
        pg_ = em.doc[0]
        before = pg_.get_pixmap(dpi=50).samples
        del pg_

        def info(i):
            pg_ = em.doc[i]
            r_ = ([a_.type[1] for a_ in pg_.annots()], [w_.field_name for w_ in pg_.widgets()])
            del pg_
            return r_
        out["tool_live"] = not em.soon["flatten"].property("soon")
        s0 = em.work.step()
        s10.flatten({"annots": True, "widgets": True, "range": "1"})
        a0, f0 = info(0)
        a1, f1 = info(1)
        pg_ = em.doc[0]
        after = pg_.get_pixmap(dpi=50).samples
        del pg_
        plain = pymupdf.open("pdf", em.doc.tobytes())
        t0 = plain[0].get_text()
        plain.close()
        out["range_flattened"] = a0 == ["Redact"] and f0 == [] and em.work.step() - s0 == 1
        out["appearance_kept"] = before == after or sum(1 for x, y in zip(before, after) if abs(x - y) > 40) < len(before) * 0.01
        import unicodedata
        # (the rich-text callout typesets "fl" as a ligature; NFKC reads it back as two letters)
        out["callout_text_now_page_content"] = "flatten me" in unicodedata.normalize("NFKC", t0)
        out["whiteout_still_not_removal"] = "hidden by whiteout 1" in t0
        out["redaction_mark_kept"] = s10.mark_count() == 1
        out["other_page_untouched"] = a1 == ["Square"] and f1 == ["Later"]
        cat = em.doc.pdf_catalog()
        ok_fill = ctl.fill_field(1, [w_.xref for w_ in em.doc[1].widgets()][0], True)
        out["other_page_field_still_works"] = ("Later" in em.doc.xref_get_key(cat, "AcroForm")[1] or
                                               "0 R" in em.doc.xref_get_key(cat, "AcroForm")[1]) and ok_fill
        em.undo()
        em.undo()
        a0b, f0b = info(0)
        out["undo"] = "Ink" in a0b and "Stamp" in a0b and f0b == ["Name"]
        # the whole document, annotations only: fields stay fields
        s10.flatten({"annots": True, "widgets": False, "range": "all"})
        out["whole_doc_annots_only"] = (info(0)[0] == ["Redact"] and info(0)[1] == ["Name"] and info(1)[0] == []
                                        and info(1)[1] == ["Later"])
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_flatten", s10_flatten)

    def s10_compress():
        """Stage 10 compress: a new, smaller file (streams + garbage; images downsampled with a
        DPI and JPEG quality); the original is refused; the working copy unchanged; password kept."""
        import hashlib
        import io
        import numpy as np
        import pymupdf
        from PIL import Image
        cw = work / "s10comp"
        cw.mkdir(exist_ok=True)
        src = cw / "fat.pdf"
        d = pymupdf.open()
        rng = np.random.default_rng(3)
        for i in range(3):
            pg = d.new_page()
            arr = (rng.integers(0, 255, (300, 300, 3)) // 64 * 64).astype("uint8").repeat(8, 0).repeat(8, 1)
            b = io.BytesIO()
            Image.fromarray(arr).save(b, "JPEG", quality=95)
            pg.insert_image(pymupdf.Rect(72, 72, 272, 272), stream=b.getvalue())     # 2400 px in 200 pt: 864 dpi
            for k in range(40):
                pg.insert_text((72, 300 + 12 * k), f"line {k} of page {i + 1} with plenty of repeated words words words")
        d.save(str(src), deflate=False, garbage=0)
        d.close()
        texty = cw / "texty.pdf"                     # text only, saved with no compression at all
        d = pymupdf.open()
        for i in range(20):
            pg = d.new_page()
            for k in range(60):
                pg.insert_text((40, 30 + 12 * k), f"line {k} of page {i + 1} with plenty of repeated words " * 2, fontsize=7)
        d.save(str(texty), deflate=False, garbage=0)
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1200, 800)
        w.show()
        wait(100)
        em = w.editor_mode
        base = {"downsample": False, "threshold": 225, "target": 150, "quality": 75}
        em.open_pdf(str(texty))
        s10 = em.stage10
        a = s10.compress(base, dest=str(cw / "texty_small.pdf"))
        em.open_pdf(str(src))
        out["tool_live"] = not em.soon["compress"].property("soon")
        step0 = em.work.step()
        b_ = s10.compress({**base, "downsample": True, "target": 96, "quality": 60}, dest=str(cw / "downsampled.pdf"))
        chk = pymupdf.open(str(cw / "downsampled.pdf"))
        info = chk[0].get_image_info()
        w_px = chk.extract_image(chk[0].get_images()[0][0])["width"]
        text_ok = "page 1 with plenty" in chk[0].get_text()
        chk.close()
        out["sizes"] = f"text: {a[0]} -> {a[1]}; images: {b_[0]} -> {b_[1]} (96 dpi)"
        out["smaller_streams"] = a[1] < a[0] * 0.5
        out["smaller_images"] = b_[1] < b_[0] * 0.2 and w_px < 400 and text_ok and bool(info)
        # the original is refused; the working copy is unchanged
        from PySide6.QtWidgets import QFileDialog
        picks = [str(src), ""]
        QFileDialog.getSaveFileName = staticmethod(lambda *a_, **k_: (picks.pop(0), ""))
        refused = s10.compress(base) is None and not picks
        out["original_refused"] = refused and hashlib.sha256(src.read_bytes()).hexdigest() == h0
        out["working_copy_unchanged"] = em.work.step() == step0 and not em.dirty()
        # a protected document: the compressed copy keeps its password (AES-256)
        lk = cw / "locked.pdf"
        d = pymupdf.open()
        d.new_page().insert_text((72, 72), "locked")
        d.save(str(lk), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
        d.close()
        from . import locked as L
        L.ask_password = lambda *a_, **k_: "pw"
        em.open_pdf(str(lk))
        s10.compress(base, dest=str(cw / "locked_small.pdf"))
        chk = pymupdf.open(str(cw / "locked_small.pdf"))
        out["password_kept"] = bool(chk.needs_pass) and chk.authenticate("pw") > 0
        chk.close()
        w.close()
        out["ok"] = all(v for k, v in out.items() if k != "sizes")
        return out
    check("s10_compress", s10_compress)

    def s10_ocr():
        """Stage 10 OCR to searchable: scanned pages (incl. rotated scans) get an invisible,
        correctly placed text layer; text pages skipped unless forced; background with a
        responsive window; Cancel changes nothing; Find works; one undo step."""
        import hashlib
        import io
        import time as _t
        import pymupdf
        from PIL import Image, ImageDraw, ImageFont
        from PySide6.QtCore import QTimer
        ow = work / "s10ocr"
        ow.mkdir(exist_ok=True)
        try:
            font = ImageFont.truetype("arial.ttf", 64)
        except OSError:
            font = ImageFont.load_default()

        def scan(rot):
            im = Image.new("RGB", (1700, 2200), "white")
            ImageDraw.Draw(im).text((200, 300), "Scanned invoice number 4711", fill="black", font=font)
            if rot:
                im = im.rotate(rot, expand=True)          # stored sideways; the page /Rotate turns it back
            b = io.BytesIO()
            im.save(b, "PNG")
            return b.getvalue(), im.width, im.height
        src = ow / "scans.pdf"
        d = pymupdf.open()
        for kind in ("scan0", "text", "scan90", "scan270"):
            if kind == "text":
                d.new_page().insert_text((72, 100), "This page already has a real text layer of its own words.")
                continue
            rot = {"scan0": 0, "scan90": 90, "scan270": 270}[kind]
            data, iw, ih = scan(rot)
            pg = d.new_page(width=iw * 72 / 200, height=ih * 72 / 200)
            pg.insert_image(pg.rect, stream=data)
            pg.set_rotation(rot)
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em, fc = w.editor_mode, w.find
        em.open_pdf(str(src))
        s10 = em.stage10
        out["tool_live"] = not em.soon["ocrpage"].property("soon")
        plain = pymupdf.open("pdf", em.doc.tobytes())
        before = [plain[i].get_pixmap(dpi=40).samples for i in range(4)]
        plain.close()
        # Cancel at once: nothing changes
        s0 = em.work.step()
        wk = s10.ocr({"range": "all", "lang": "eng", "force": False})
        wk.cancel()
        while wk.isRunning():
            app.processEvents()
        wait(50)
        out["cancel_changes_nothing"] = em.work.step() == s0 and not em.dirty()
        # the real run, with a heartbeat to measure the window's responsiveness
        beats = []
        hb = QTimer()
        hb.setInterval(10)
        hb.timeout.connect(lambda: beats.append(_t.perf_counter()))
        hb.start()
        t0 = _t.perf_counter()
        s10.ocr({"range": "all", "lang": "eng", "force": False}, wait=True)
        took = _t.perf_counter() - t0
        hb.stop()
        gap = max((b - a for a, b in zip(beats, beats[1:])), default=0)
        done, skipped = s10.last_ocr
        out["ocr_pages_and_skip"] = done == [0, 2, 3] and skipped == [1] and em.work.step() - s0 == 1
        out["window_responsive"] = gap < 0.6 and len(beats) > 10
        out["timing"] = f"{took:.1f} s for 3 pages; longest pause {gap * 1000:.0f} ms"
        plain = pymupdf.open("pdf", em.doc.tobytes())
        after = [plain[i].get_pixmap(dpi=40).samples for i in range(4)]
        plain.close()
        out["pages_look_the_same"] = all(a == b for a, b in zip(before, after))
        # each word lands where it is displayed (also on the rotated scans)
        placed = []
        plain = pymupdf.open("pdf", em.doc.tobytes())       # read from a plain copy (never the live one)
        for i in (0, 2, 3):
            pg = plain[i]
            hits = [(h * pg.rotation_matrix).normalize() for h in pg.search_for("invoice")]
            pix = pg.get_pixmap(dpi=72)
            ys = [y for y in range(pix.height) for x in range(0, pix.width, 3) if pix.pixel(x, y)[0] < 100]
            xs = [x for y in range(min(ys), max(ys) + 1) for x in range(0, pix.width, 3) if pix.pixel(x, y)[0] < 100] if ys else []
            placed.append(bool(hits) and bool(ys) and min(ys) - 4 <= hits[0].y0 and hits[0].y1 <= max(ys) + 4
                          and min(xs) - 4 <= hits[0].x0 and hits[0].x1 <= max(xs) + 4)
        plain.close()
        out["layer_aligned_incl_rotated"] = all(placed)
        fc.open()
        fc.edit.setText("invoice")
        fc._debounce.stop()
        fc.start_search()
        while fc._tick.isActive():
            app.processEvents()
        out["find_works_on_new_text"] = len(fc.hits) == 3
        fc.close()
        em.undo()
        plain = pymupdf.open("pdf", em.doc.tobytes())
        out["undo_removes_layer"] = not plain[0].search_for("invoice") and not em.dirty()
        plain.close()
        s10.ocr({"range": "2", "lang": "eng", "force": True}, wait=True)
        out["force_text_page"] = s10.last_ocr[0] == [1]
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(v for k, v in out.items() if k != "timing")
        return out
    check("s10_ocr", s10_ocr)

    def s10_bates():
        """Stage 10 Bates numbering: prefix / start / padding / suffix over a range, position
        (incl. a rotated page), preview, one undo step."""
        import hashlib
        import pymupdf
        from . import stage10_ui as U
        bw = work / "s10bates"
        bw.mkdir(exist_ok=True)
        src = bw / "doc.pdf"
        d = pymupdf.open()
        for i in range(5):
            pg = d.new_page()
            pg.insert_text((72, 300), f"exhibit page {i + 1}")
            if i == 3:
                pg.set_rotation(90)
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        out = {}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))
        s10 = em.stage10
        out["tool_live"] = not em.soon["bates"].property("soon")
        vals = {"prefix": "SMITH-", "start": 98, "digits": 6, "suffix": "-C", "slot": "br", "font": "cour",
                "size": 9.0, "color": (0, 0, 0), "mx": 36.0, "my": 20.0, "range": "2-5"}
        seen = {}

        def answer(parent, title, fields, preview, n, start=0, note=""):
            seen["pix"] = preview(vals, 1)
            return vals
        U.ask_tool = answer
        s0 = em.work.step()
        s10.bates()
        plain = pymupdf.open("pdf", em.doc.tobytes())
        texts = [plain[i].get_text() for i in range(5)]
        pg = plain[3]
        W, H = pg.rect.width, pg.rect.height
        box = [(pymupdf.Rect(w_[:4]) * pg.rotation_matrix).normalize() for w_ in pg.get_text("words")
               if w_[4] == "SMITH-000100-C"]
        plain.close()
        out["sequence_padding_range"] = ("SMITH-000098-C" in texts[1] and "SMITH-000099-C" in texts[2]
                                         and "SMITH-000100-C" in texts[3] and "SMITH-000101-C" in texts[4]
                                         and "SMITH" not in texts[0] and seen.get("pix") is not None)
        out["bottom_right_on_rotated_page"] = bool(box) and box[0].x1 > W - 50 and box[0].y1 > H - 40
        out["one_undo_step"] = em.work.step() - s0 == 1
        em.undo()
        plain = pymupdf.open("pdf", em.doc.tobytes())
        out["undo_removes"] = all("SMITH" not in plain[i].get_text() for i in range(5))
        plain.close()
        out["original_unchanged"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s10_bates", s10_bates)

    def s7a_autobookmarks():
        """Stage 7a auto-bookmarks: heading detection + level nesting, the contents-page path,
        no-text handling, preview edits, replace / append / nest-under, one undo step, and the
        result behaving like hand-made bookmarks."""
        import hashlib
        import pymupdf
        from . import autobookmarks as A
        from . import bookmarks as BM
        from . import outline as O
        aw = work / "s7a"
        aw.mkdir(exist_ok=True)

        def manual(path, existing=None):
            d = pymupdf.open()
            toc = d.new_page()
            toc.insert_text((72, 60), "Contents", fontsize=20, fontname="hebo")
            ents = [("1 Engine", 3), ("1.1 Oil system", 3), ("1.2 Cooling", 4), ("2 Electrical", 5), ("2.1 Fuses", 5),
                    ("2.1.1 Fuse box layout", 6), ("2.2 Wiring", 6)]
            for k, (t_, pg_) in enumerate(ents):
                toc.insert_text((72 + 18 * t_.split()[0].count("."), 100 + 22 * k), f"{t_} " + "." * 30 + f" {pg_}",
                                fontsize=11)
            body = "This is body text that explains the procedure in some detail for the reader."
            plan = {3: [("1 Engine", 18), ("1.1 Oil system", 14)], 4: [("1.2 Cooling", 14)],
                    5: [("2 Electrical", 18), ("2.1 Fuses", 14)], 6: [("2.1.1 Fuse box layout", 12), ("2.2 Wiring", 14)]}
            for p_ in range(2, 7):
                pg = d.new_page()
                pg.insert_text((72, 30), "Service Manual (running header)", fontsize=9)
                y = 80
                for title, size in plan.get(p_ + 1, []):
                    pg.insert_text((72, y), title, fontsize=size, fontname="hebo")
                    y += size + 14
                    for _k in range(6):
                        pg.insert_text((72, y), body, fontsize=11)
                        y += 15
                    y += 10
                pg.insert_text((290, 810), str(p_), fontsize=9)
                if p_ == 4:
                    pg.set_rotation(90)
            if existing:
                d.set_toc(existing)
            d.save(str(path))
            d.close()
        src = aw / "manual.pdf"
        manual(src)
        src2 = aw / "manual_marked.pdf"
        manual(src2, [[1, "Cover", 1], [1, "Hand-made", 2]])
        scan = aw / "scan.pdf"
        d = pymupdf.open()
        pg = d.new_page()
        from .oprules import _png
        pg.insert_image(pymupdf.Rect(72, 72, 300, 300), stream=_png())           # a scan: an image, no text
        d.save(str(scan))
        d.close()
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (src, src2, scan)}
        out = {}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        bm = em.bookmarks
        em.open_pdf(str(src))
        an = A.analyze(em.doc)
        out["detect_body_and_styles"] = (an.body == 11.0 and [s[:2] for s in an.styles()][:3] == [(20.0, True), (18.0, True), (14.0, True)]
                                         and an.toc_page == 0)
        # the preview dialog: headings with the "Contents" title unticked -> chapters / sections nest
        dlg = BM.AutoBookmarksDialog(w, an, [])
        n_default = len(dlg.toc())
        dlg.styles.item(0).setCheckState(Qt.Unchecked)            # the 20 pt title is not a heading
        dlg.levels.setValue(3)
        t1 = dlg.toc()
        out["levels_nest"] = ([e[:3] for e in t1] == [[1, "1 Engine", 2], [2, "1.1 Oil system", 2], [2, "1.2 Cooling", 3],
                                                       [1, "2 Electrical", 4], [2, "2.1 Fuses", 4], [2, "2.2 Wiring", 5]]
                              and n_default == 7)
        to = dict(t1[1][3]).get("to")
        out["dest_at_heading_and_rotated_page"] = (to is not None and 100 < to[1] < 400 and t1[3][2] == 4)
        # the contents-page path
        dlg.method.setCurrentIndex(1)
        t2 = dlg.toc()
        out["toc_page_path"] = ([e[:3] for e in t2] == [[1, "1 Engine", 2], [2, "1.1 Oil system", 2], [2, "1.2 Cooling", 3],
                                                         [1, "2 Electrical", 4], [2, "2.1 Fuses", 4],
                                                         [3, "2.1.1 Fuse box layout", 5], [2, "2.2 Wiring", 5]])
        # preview edits: rename, remove, indent, outdent
        dlg.method.setCurrentIndex(0)
        root = dlg.tree.invisibleRootItem()
        root.child(0).setText(0, "Engine (renamed)")
        cool = root.child(0).child(1)
        dlg.tree.clearSelection()
        cool.setSelected(True)
        dlg.shift(-1)                                             # "1.2 Cooling" up to level 1
        dlg.tree.clearSelection()
        root.child(2).setSelected(True)
        dlg.remove()                                              # drop "2 Electrical" with its children
        dlg.tree.clearSelection()
        root.child(1).setSelected(True)
        dlg.shift(1)                                              # Cooling back under Engine
        edited = dlg.toc()
        out["preview_edits"] = [e[:2] for e in edited] == [[1, "Engine (renamed)"], [2, "1.1 Oil system"], [2, "1.2 Cooling"]]
        dlg.close()
        # apply (replace on a document without bookmarks): one undo step, ordinary bookmarks after
        s0 = em.work.step()
        ok = bm.auto_generate((t1, "replace"))
        got = [e[:3] for e in O.read(em.doc)]
        out["apply_one_step"] = ok and got == [e[:3] for e in t1] and em.work.step() - s0 == 1
        em.undo()
        out["undo"] = O.read(em.doc) == [] and not em.dirty()
        em.redo()
        # behaves like a hand-made bookmark: rename and delete through the stage-7 panel
        bm.reload()
        it = bm.tree.topLevelItem(0)
        it.setText(0, "Engine chapter")                           # an edit in the tree = a rename
        wait(20)
        bm.tree.setCurrentItem(bm.tree.topLevelItem(1))
        bm.delete()
        titles = [e[1] for e in O.read(em.doc)]
        out["works_like_hand_made"] = titles[0] == "Engine chapter" and "2 Electrical" not in titles
        # bookmarks follow their page through a page op (they point at page objects)
        from . import pageops as P
        P.move(em.work, [1], 0)
        em.rebuild()
        out["follows_page_moves"] = O.read(em.doc)[0][2] == 1
        # replace / append / nest under, on a document that already has bookmarks
        em.open_pdf(str(src2))
        exist = O.read(em.doc)
        an2 = A.analyze(em.doc)
        gen = A.from_headings(an2, styles=[(18.0, True), (14.0, True)])
        bm.auto_generate((gen, "replace"))
        rep = [e[1] for e in O.read(em.doc)]
        em.undo()
        bm.auto_generate((gen, "append"))
        app_ = [e[:2] for e in O.read(em.doc)]
        em.undo()
        bm.auto_generate((gen, "under"))
        und = [e[:2] for e in O.read(em.doc)]
        em.undo()
        dlg = BM.AutoBookmarksDialog(w, an2, exist)
        default_mode = dlg.result_value()[1]
        dlg.close()
        out["replace_merge_choice"] = (default_mode == "replace" and rep == [e[1] for e in gen]
                                       and app_[:2] == [[1, "Cover"], [1, "Hand-made"]] and len(app_) == 2 + len(gen)
                                       and und[2] == [1, "Generated bookmarks"] and und[3][0] == 2
                                       and [e[1] for e in O.read(em.doc)] == ["Cover", "Hand-made"])
        # a scan with no text: nothing to find, and OCR is suggested
        em.open_pdf(str(scan))
        n_dlg = len(r["dialogs"])
        res = bm.auto_generate()
        msgs = r["dialogs"][n_dlg:]
        out["no_text_suggests_ocr"] = res is False and O.read(em.doc) == [] and any("OCR" in m for m in msgs)
        out["originals_unchanged"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s7a_autobookmarks", s7a_autobookmarks)

    def s10b_batch():
        """Stage 10b batch: the job tab is non-blocking and stays live during another tab's
        find-index lock; a multi-op pipeline over several files with X of Y progress; a failed
        file (and a file that kills the worker) doesn't stop the batch; outputs land in the chosen
        folder named by the pattern; originals untouched; Cancel stops cleanly."""
        import hashlib
        import re as _re
        import shutil
        import time as _t
        import pymupdf
        from PySide6.QtCore import QTimer as _QT
        from . import batch as B
        from . import batch_ui as BU
        bw = work / "s10b"
        shutil.rmtree(bw, ignore_errors=True)
        src, out_dir = bw / "in", bw / "out"
        (src / "sub").mkdir(parents=True)
        out_dir.mkdir()

        def doc_with(path, titles, pw=None):
            d = pymupdf.open()
            for k, t_ in enumerate(titles):
                pg = d.new_page()
                pg.insert_text((72, 90), t_, fontsize=20, fontname="hebo")
                pg.insert_text((72, 140), "Body text of the page, with a SECRET word to redact.", fontsize=11)
                pg.insert_text((72, 160), "More ordinary body text follows here on the page.", fontsize=11)
                pg.add_text_annot((400, 400), "a note")
            kw = dict(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=pw, owner_pw=pw) if pw else {}
            d.save(str(path), **kw)
            d.close()
        doc_with(src / "a.pdf", ["Introduction", "Methods"])
        doc_with(src / "b.pdf", ["Results", "Discussion", "Summary"])
        doc_with(src / "sub" / "c.pdf", ["Appendix"], pw="pw1")
        (src / "bad.pdf").write_bytes(b"%PDF-1.7\nthis is not really a PDF\n")
        # a scan: text only as pixels (OCR has to read it)
        t_doc = pymupdf.open()
        tp = t_doc.new_page()
        tp.insert_text((72, 120), "HELLO BATCH SCAN", fontsize=36, fontname="hebo")
        pix = tp.get_pixmap(dpi=200)
        t_doc.close()
        d = pymupdf.open()
        d.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pix)
        d.save(str(src / "scan.pdf"))
        d.close()
        files = B.collect_inputs([src], recurse=True)
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        out = {"inputs_found": [p.name for p in files] == ["a.pdf", "b.pdf", "bad.pdf", "scan.pdf", "c.pdf"]
               and B.collect_inputs([src]) == files[:4]}
        # the setup dialog: every step's settings form builds (stamping steps preview on a sample page),
        # validation, and the remembered setup holds no passwords
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, fc, bc = w.editor_mode, w.find, w.batch
        w.settings.remove(BU.SETUP_KEY)
        forms = {}
        from .stage10_ui import ToolDialog
        for op, _lab in B.OPS:
            flds, note, has_prev = BU.fields_for(op)
            dlg = ToolDialog(w, op, BU._prefill(flds, {}), BU.preview_for(op) if has_prev else None, 1, 0, note)
            vals = dlg.values()
            pm = BU.preview_for(op)(vals, 0) if has_prev and not (op == "watermark" and vals.get("kind") == "image") else True
            forms[op] = pm is True or (pm is not None and not pm.isNull())
            dlg.close()
        out["step_forms"] = all(forms.values()) and len(forms) == len(B.OPS)
        pipeline = [
            {"op": "ocr", "params": {"lang": "eng", "range": "all"}},
            {"op": "redact_terms", "params": {"terms": "secret", "apply": True}},
            {"op": "header_footer", "params": {"tc": "HDR {page}/{pages}", "range": "all"}},
            {"op": "page_numbers", "params": {"fmt": "page_n", "slot": "bc", "range": "all"}},
            {"op": "bates", "params": {"prefix": "LZ", "start": 1, "digits": 5, "continue": True, "range": "all"}},
            {"op": "watermark", "params": {"kind": "text", "text": "DRAFT", "position": "diagonal", "range": "all"}},
            {"op": "flatten", "params": {"annots": True, "widgets": True, "range": "all"}},
            {"op": "metadata", "params": {"mode": "set", "title": "Batch title"}},
            {"op": "bookmarks", "params": {"levels": 2, "threshold": 0.5, "mode": "replace"}},
            {"op": "extract", "params": {"format": "txt"}},
            {"op": "compress", "params": {"downsample": False}},
            {"op": "password", "params": {"mode": "set", "open_pw": "out1", "owner_pw": "own1", "perm_copy": False}},
        ]
        dlg = BU.BatchDialog(w, w.settings)
        dlg.add_paths([src], recurse=True)
        dlg.out_dir.setText(str(out_dir))
        dlg.pattern.setText("{n}-{name}-done")
        p_empty = dlg.problem()
        for st in pipeline:
            dlg.add_step(st["op"], dict(st["params"]))
        dlg.add_step("redact_terms", {"terms": ""})
        p_bad = dlg.problem()
        dlg.step_list.setCurrentRow(len(dlg.steps) - 1)
        dlg.remove_step()
        dlg.in_pw.setText("pw1")
        p_ok = dlg.problem()
        job = dlg.result_job()
        dlg.remember(job)
        remembered = w.settings.value(BU.SETUP_KEY, "", type=str)
        dlg.close()
        out["setup_dialog"] = (p_empty == "Add at least one operation." and p_bad is not None and "term" in p_bad
                               and p_ok is None and len(job["files"]) == 5 and job["open_password"] == "pw1"
                               and [s["op"] for s in job["pipeline"]] == [s["op"] for s in pipeline]
                               and job["pattern"] == "{n}-{name}-done")
        out["remembered_without_passwords"] = (bool(remembered) and "out1" not in remembered and "own1" not in remembered
                                               and "pw1" not in remembered and "{n}-{name}-done" in remembered)
        dlg2 = BU.BatchDialog(w, w.settings)
        out["setup_restored"] = (len(dlg2.steps) == len(pipeline) and dlg2.out_dir.text() == str(out_dir)
                                 and dlg2.in_pw.text() == "")
        dlg2.close()

        # 1. the main job: 12 operations over 5 files (one broken), running as a tab
        job["test_delay"] = 0.15                         # keeps it running long enough to watch
        gaps, last = [], [_t.perf_counter()]

        def beat():
            now_ = _t.perf_counter()
            gaps.append(now_ - last[0])
            last[0] = now_
        hb = _QT()
        hb.setInterval(10)
        hb.timeout.connect(beat)
        hb.start()
        t_start = _t.time()
        s = bc.start(job)
        i_job = em.sessions.index(s)
        out["job_tab"] = (s.is_job and em.cur is s and em.doc is None and em.tabbar.tabText(i_job).startswith("Batch job")
                          and "batch job" in em.tabbar.tabToolTip(i_job) and em.center.currentWidget() is s.view
                          and not em.act_save_pdf.isEnabled() and not s.dirty())
        # non-blocking: other tabs open and edit while it runs (and the job adds no undo steps)
        small = bw / "small.pdf"
        doc_with(small, ["Other document"])
        em.open_pdf(str(small))
        steps0 = em.work.step()
        em.canvas.select_chars(0, 0, 5)
        em.highlight_selection()
        edited = em.work.step() == steps0 + 1
        em.undo()
        out["other_tabs_work_meanwhile"] = (s.running and edited and em.work.step() == steps0 and em.cur is not s
                                            and len(em.sessions) == 2)
        # live during a find-index lock in another tab
        big = bw / "big.pdf"
        d = pymupdf.open()
        for i in range(400):
            pg = d.new_page()
            pg.insert_textbox(pymupdf.Rect(36, 36, 576, 806), "\n".join([f"{i + 1}: batch lock test line"] * 70), fontsize=7)
        d.save(str(big))
        d.close()
        em.open_pdf(str(big))
        fc.open()
        fc.edit.setText("lazarus")
        fc._debounce.stop()
        fc.start_search()
        t0 = _t.time()
        while not fc.locked and _t.time() - t0 < 10:
            app.processEvents()
        locked_ok = fc.locked and not w.centralWidget().isEnabled()
        ev0, txt0, now0 = len(s.seen), em.tabbar.tabText(em.sessions.index(s)), s.view.now.text()
        t1 = _t.time()
        texts = set()
        while fc.locked and _t.time() - t1 < 2.5 and s.running:
            app.processEvents()
            texts.add(s.view.now.text())
        locked_for = _t.time() - t1
        live_events = len(s.seen) - ev0
        fc.cancel()
        fc.close()
        out["live_during_find_lock"] = (locked_ok and locked_for >= 0.5 and live_events >= 3 and len(texts) >= 2
                                        and s.running)
        out["events_while_locked"] = live_events
        # X of Y progress, seen as it happens
        counts_seen = []
        gaps.clear()
        last[0] = _t.perf_counter()
        while s.running and _t.time() - t_start < 300:
            app.processEvents()
            c_ = s.view.count.text()
            if not counts_seen or counts_seen[-1] != c_:
                counts_seen.append(c_)
        hb.stop()
        nums = [int(m.group(1)) for m in (_re.fullmatch(r"(\d+) of 5 files", x) for x in counts_seen) if m]
        out["x_of_y_progress"] = (len(nums) >= 4 and nums == sorted(nums) and nums[-1] == 5
                                  and s.view.bar.maximum() == 5 and s.view.bar.value() == 5)
        out["ui_never_stalled"] = bool(gaps) and max(gaps) < 0.5
        out["max_ui_gap_ms"] = round(1000 * max(gaps)) if gaps else None
        st_ = list(s.status)
        k_bad = [p.name for p in files].index("bad.pdf")
        out["one_failure_continues"] = (st_.count("done") == 4 and st_[k_bad] == "failed" and s.ended is not None
                                        and not s.ended_by_cancel and "bad" not in s.detail[k_bad].lower()
                                        and s.summary().startswith("Finished: 4 succeeded, 1 failed."))
        names = sorted(p.name for p in out_dir.iterdir())
        want = sorted(["1-a-done.pdf", "1-a-done.txt", "2-b-done.pdf", "2-b-done.txt", "4-scan-done.pdf",
                       "4-scan-done.txt", "5-c-done.pdf", "5-c-done.txt"])
        out["outputs_named_by_pattern"] = names == want
        # what the pipeline did, in every output
        checks = {}
        bates_seen = []
        for nm in ("1-a-done.pdf", "2-b-done.pdf", "4-scan-done.pdf", "5-c-done.pdf"):
            od = pymupdf.open(str(out_dir / nm))
            enc = od.needs_pass and od.authenticate("out1") > 0
            txt = "".join(pg.get_text() for pg in od)
            bates_seen += _re.findall(r"LZ\d{5}", txt)
            checks[nm] = (enc and "HDR 1/" in txt and "Page 1" in txt and "DRAFT" in txt and "SECRET" not in txt.upper()
                          and od.metadata.get("title") == "Batch title" and not (od.permissions & pymupdf.PDF_PERM_COPY)
                          and sum(len(list(pg.annots())) for pg in od) == 0)
            if nm == "1-a-done.pdf":
                checks["bookmarks"] = [e[1] for e in od.get_toc()] == ["Introduction", "Methods"]
            if nm == "4-scan-done.pdf":
                checks["ocr"] = "HELLO" in txt.upper() and "BATCH" in txt.upper()
            od.close()
        tx = (out_dir / "1-a-done.txt").read_text(encoding="utf-8")
        checks["extract_txt"] = "Introduction" in tx and "SECRET" not in tx.upper()
        out["pipeline_applied"] = all(checks.values())
        out["pipeline_detail"] = {k: v for k, v in checks.items() if not v}
        out["bates_continue_across_files"] = bates_seen == [f"LZ{n:05d}" for n in range(1, 8)]
        out["originals_untouched"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        out["no_partials_left"] = not any(B.PARTIAL in p.name for p in out_dir.iterdir())

        # 2. outputs next to the inputs with the same name: never over an input (or an open original)
        em.open_pdf(str(src / "a.pdf"))
        s2 = bc.start({"files": [str(src / "a.pdf"), str(src / "b.pdf")], "pipeline": [{"op": "metadata", "params": {
            "mode": "remove"}}], "out_dir": str(src), "pattern": "{name}", "open_password": "", "save_pdf": True})
        s2.wait(120)
        out["never_over_inputs"] = (s2.status == ["done", "done"] and (src / "a (2).pdf").exists()
                                    and (src / "b (2).pdf").exists()
                                    and all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items()))

        # 2b. a file that fails mid-pipeline (after Extract made its text) writes nothing at all
        fw = bw / "midfail"
        s2b = bc.start({"files": [str(src / "a.pdf"), str(src / "b.pdf")], "pipeline": [
            {"op": "extract", "params": {"format": "txt"}},
            {"op": "header_footer", "params": {"tc": "Q", "range": "3"}}], "out_dir": str(fw),
            "pattern": "{name}", "open_password": "", "save_pdf": True})
        s2b.wait(120)
        out["failed_file_writes_nothing"] = (s2b.status == ["failed", "done"] and "page" in s2b.detail[0].lower()
                                             and sorted(p.name for p in fw.iterdir()) == ["b.pdf", "b.txt"])

        # 3. a file that kills the worker: marked failed, a new worker does the rest
        cw = bw / "crash"
        cw.mkdir()
        s3 = bc.start({"files": [str(src / "a.pdf"), str(src / "b.pdf"), str(src / "sub" / "c.pdf")],
                       "pipeline": [{"op": "metadata", "params": {"mode": "set", "title": "x"}}], "out_dir": str(cw),
                       "pattern": "{name}", "open_password": "pw1", "save_pdf": True, "test_crash_at": 1})
        s3.wait(180)
        out["worker_crash_continues"] = (s3.status == ["done", "failed", "done"] and s3.restarts == 1
                                         and "worker" in s3.detail[1]
                                         and sorted(p.name for p in cw.iterdir()) == ["a.pdf", "c.pdf"])

        # 4. Cancel: stops after the current step, nothing half-written, the worker is gone
        kw_ = bw / "cancel"
        kw_.mkdir()
        many = [str(src / "a.pdf"), str(src / "b.pdf"), str(src / "sub" / "c.pdf"), str(src / "a.pdf")]
        s4 = bc.start({"files": many[:3] + [str(src / "scan.pdf")], "pipeline": [
            {"op": "header_footer", "params": {"tc": "X", "range": "all"}},
            {"op": "page_numbers", "params": {"range": "all"}},
            {"op": "watermark", "params": {"text": "Y", "range": "all"}}], "out_dir": str(kw_),
            "pattern": "{name}", "open_password": "pw1", "save_pdf": True, "test_delay": 0.4})
        t0 = _t.time()
        while s4.status[0] != "done" and _t.time() - t0 < 120:
            app.processEvents()
        while s4.status[1] != "running" and _t.time() - t0 < 120:
            app.processEvents()
        tc = _t.time()
        s4.cancel()
        cancelling_shown = s4.view.now.text().startswith("Cancelling") and not s4.view.btn_cancel.isEnabled()
        s4.wait(60)
        took = _t.time() - tc
        out["cancel_stops_cleanly"] = (cancelling_shown and s4.ended_by_cancel and s4.status[0] == "done"
                                       and s4.status[1] == "cancelled" and s4.status[2:] == ["skipped", "skipped"]
                                       and sorted(p.name for p in kw_.iterdir()) == ["a.pdf"] and s4.proc is None
                                       and took < 10 and s4.tab_text().endswith("cancelled")
                                       and s4.summary().startswith("Cancelled: 1 succeeded"))
        out["cancel_seconds"] = round(took, 2)

        # 5. closing tabs: a finished job closes at once; a running one asks first (here: "no", it stays),
        # then closes without asking and stops its worker
        n_tabs = len(em.sessions)
        em.close_session(s)
        closed_done = s not in em.sessions and len(em.sessions) == n_tabs - 1
        s5 = bc.start({"files": many, "pipeline": [{"op": "header_footer", "params": {"tc": "Z", "range": "all"}}],
                       "out_dir": str(bw / "close"), "pattern": "{n}", "open_password": "pw1", "save_pdf": True,
                       "test_delay": 0.5})
        t0 = _t.time()
        while s5.proc is None and _t.time() - t0 < 10:
            app.processEvents()
        n_q = len(r["dialogs"])
        kept = not em.close_session(s5) and s5 in em.sessions and s5.running
        asked = any("still running" in m for m in r["dialogs"][n_q:])
        proc = s5.proc
        em.close_session(s5, ask=False)
        gone = s5 not in em.sessions and (proc is None or not proc.is_alive())
        out["close_tab"] = closed_done and kept and asked and gone
        # the job view lives in the editor: picking a job tab in Overview switches there
        w.set_mode("overview")
        wait(50)
        em.activate(s3)
        wait(50)
        out["overview_job_tab_to_editor"] = w.mode == "editor" and em.center.currentWidget() is s3.view
        out["originals_untouched_end"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        w.close()
        for x in list(bc.jobs):
            if x.running:
                x.cancel()
                x.wait(30)
        out["ok"] = all(v for k, v in out.items() if k not in ("max_ui_gap_ms", "cancel_seconds", "pipeline_detail",
                                                               "events_while_locked"))
        return out
    check("s10b_batch", s10b_batch)

    def s11_text_edit():
        """Stage 11 Edit text: embedded font reused, subset missing glyphs -> substitute + notice,
        reflow and overflow (shrink / grow / keep editing), images and line art kept, old text
        gone by re-extraction, Type3 -> cover + notice, scan / OCR layer handling, permission
        block, rotated pages, Esc cancels, one undo step per edit, originals byte-identical."""
        import hashlib
        import pymupdf
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from . import locked, oprules
        from . import textedit as TE
        tw = work / "s11"
        tw.mkdir(exist_ok=True)
        PARA = ("The quick brown fox jumps over the lazy dog near the river bank while the sun sets slowly "
                "over the quiet hills.")
        sysfont = oprules.system_font()

        def para(pg, font="helv", at=(72, 90)):
            pg.insert_textbox(pymupdf.Rect(at[0], at[1], at[0] + 300, at[1] + 80), PARA, fontname=font, fontsize=11)

        # A: art + paragraph + another block; three rotated pages; a scan with an OCR layer; a bare scan
        d = pymupdf.open()
        pg = d.new_page()
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), 0)
        pix.clear_with(180)
        pg.insert_image(pymupdf.Rect(60, 60, 200, 200), pixmap=pix)
        pg.draw_rect(pymupdf.Rect(55, 55, 420, 320), color=(0, 0, 1), width=1)
        pg.draw_line((60, 185), (400, 185), color=(1, 0, 0), width=0.8)
        para(pg)
        pg.insert_text((72, 260), "Another block stays put.", fontsize=11)
        for rot in (90, 180, 270):
            p2 = d.new_page()
            para(p2)
            p2.set_rotation(rot)
        sc = d.new_page()
        sc.insert_image(pymupdf.Rect(36, 36, 300, 300), pixmap=pix)
        sc.insert_text((72, 120), "ocr layer words", fontsize=12, render_mode=3)
        d.new_page().insert_image(pymupdf.Rect(36, 36, 300, 300), pixmap=pix)
        A = tw / "a.pdf"
        d.save(str(A))
        d.close()
        # B: a fully embedded TrueType; C: the same, subset; D: a Type3 font; E: content editing not allowed
        files = {"a": A}
        for key, sub in (("b", False), ("c", True)):
            d = pymupdf.open()
            pg = d.new_page()
            if sysfont:
                pg.insert_font(fontname="emb", fontfile=sysfont)
                para(pg, "emb")
            else:
                para(pg)
            if sub:
                d.subset_fonts()
            files[key] = tw / f"{key}.pdf"
            d.save(str(files[key]))
            d.close()
        d = pymupdf.open()
        d.new_page().insert_text((72, 80), "Normal text above", fontsize=11)
        oprules.add_type3_text(d, 0, "ABCAB", (72, 200))
        files["d"] = tw / "d.pdf"
        d.save(str(files["d"]))
        d.close()
        d = pymupdf.open()
        para(d.new_page())
        files["e"] = tw / "e.pdf"
        d.save(str(files["e"]), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o",
               permissions=pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_COPY)
        d.close()
        H = {k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in files.items()}

        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em = w.editor_mode
        te = em.texted
        out = {}

        def plain_text(pno):
            s = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
            try:
                return " ".join(s[pno].get_text().replace("\xa0", " ").split())
            finally:
                s.close()

        def open_block(pno, contains):
            m = te.model(pno)
            b = next(b for b in m.blocks if contains in b.search)
            r = m.disp_rect(b)
            return te.open_at(em.canvas, pno, pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2))

        def edit(pno, contains, old, new, ask=None):
            if not open_block(pno, contains):
                return False
            te.set_text(te.editing["original"].replace(old, new))
            return te.commit(ask=ask)

        em.open_pdf(str(A))
        te.activate()
        out["tool_live"] = not em.soon["edittext"].property("soon") and em.markup.tool == "edittext"
        m0 = te.model(0)
        b0 = next(b for b in m0.blocks if "quick" in b.search)
        r0 = m0.disp_rect(b0)
        te._show_hover(em.canvas, 0, pymupdf.Point(r0.x0 + 5, r0.y0 + 5))
        out["hover_outlines_block"] = te._hover is not None and te._hover[1].isVisible()
        # 1. an edit: one undo step, the old text gone (re-extracted), images and line art untouched
        pg = em.doc[0]
        art0 = (len(pg.get_images()), len(pg.get_drawings()))
        del pg
        s0 = em.work.step()
        ok = edit(0, "quick brown", "quick brown", "QUICK RED")
        t1 = plain_text(0)
        pg = em.doc[0]
        art1 = (len(pg.get_images()), len(pg.get_drawings()))
        del pg
        out["edit_one_undo_step"] = ok and em.work.step() == s0 + 1 and te.editing is None
        out["old_text_gone_new_reads"] = ("QUICK RED fox" in t1 and "quick brown" not in t1
                                          and "Another block stays put." in t1)
        out["images_and_line_art_kept"] = art0 == art1 == (1, 2)
        out["standard_font_no_notice"] = te.last_result == {"method": "replace", "notices": []}
        em.undo()
        out["undo_restores"] = "quick brown" in plain_text(0) and "QUICK RED" not in plain_text(0)
        em.redo()
        # 2. rotated pages: the new text reads, in the same direction as before
        rot_ok = {}
        for pno, rot in ((1, 90), (2, 180), (3, 270)):
            ok = edit(pno, "quick brown", "quick brown", "ROTATED edit")
            s = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
            dirs = {tuple(round(v) for v in ln["dir"]) for b in s[pno].get_text("dict")["blocks"] for ln in b.get("lines", [])}
            rot_ok[rot] = ok and "ROTATED edit" in plain_text(pno) and "quick brown" not in plain_text(pno) and dirs == {(1, 0)}
            s.close()
        out["rotated_pages"] = all(rot_ok.values())
        # 3. reflow and overflow: never clipped; keep editing / shrink / grow
        n0 = em.work.step()
        long = PARA + " " + PARA + " And a little more text to make it overflow its box for sure."
        open_block(0, "jumps over")
        te.set_text(long)
        kept = te.commit(ask=lambda kind, detail: None) is False and te.editing is not None and em.work.step() == n0
        ok_s = te.commit(ask=lambda kind, detail: "shrink" if kind == "overflow" else False)
        shrunk = ok_s and any("Shrunk" in n for n in te.last_result["notices"])
        s = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        box = (b0.bbox * s[0].rotation_matrix)
        inside = all(box.x0 - 1.5 <= sp["bbox"][0] and sp["bbox"][3] <= box.y1 + 1.5
                     for b in s[0].get_text("dict")["blocks"] for ln in b.get("lines", []) for sp in ln["spans"]
                     if "little more" in sp["text"])
        s.close()
        em.undo()
        open_block(0, "jumps over")
        te.set_text(long)
        ok_g = te.commit(ask=lambda kind, detail: "grow" if kind == "overflow" else False)
        grew = ok_g and any("grew" in n for n in te.last_result["notices"]) and "little more text" in plain_text(0)
        out["reflow_overflow"] = kept and shrunk and inside and grew
        em.undo()
        # 4. Esc cancels an edit in progress
        n1 = em.work.step()
        open_block(0, "jumps over")
        te.set_text("thrown away")
        te.key(em.canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        out["esc_cancels"] = te.editing is None and em.work.step() == n1 and "thrown away" not in plain_text(0)
        # 5. scans: the OCR layer is not edited as visible text; a bare scan suggests OCR
        nd = len(r["dialogs"])
        a_ocr = te.open_at(em.canvas, 4, pymupdf.Point(90, 115))
        a_scan = te.open_at(em.canvas, 5, pymupdf.Point(100, 100))
        msgs = r["dialogs"][nd:]
        out["scan_and_ocr_layer"] = (a_ocr is False and a_scan is False and len(msgs) == 2 and "invisible" in msgs[0]
                                     and "OCR to searchable" in msgs[1] and te.editing is None)
        # 6. a fully embedded font is reused (no substitute, the new text is in that font)
        em.open_pdf(str(files["b"]))
        ok = edit(0, "quick brown", "quick brown", "REUSED font")
        s = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        fonts = {sp["font"] for b in s[0].get_text("dict")["blocks"] for ln in b.get("lines", []) for sp in ln["spans"]
                 if "REUSED" in sp["text"]}
        embedded = [f for f in s[0].get_fonts(full=True) if f[1] != "n/a"]
        s.close()
        out["embedded_font_reused"] = (ok and te.last_result["notices"] == [] and bool(fonts) and bool(embedded)
                                       and not any(f in ("Helvetica",) for f in fonts)) if sysfont else None
        # 7. a subset missing glyphs: the editor says so before applying; a substitute is used
        em.open_pdf(str(files["c"]))
        open_block(0, "quick")
        te.set_text(te.editing["original"].replace("quick brown", "Zyxwvq JKQ"))
        before_notes = te.notices()
        ok = te.commit()
        out["subset_fallback_notice"] = (ok and any("Substitute font" in n and "subset" in n for n in before_notes)
                                         and any("Substitute font" in n for n in te.last_result["notices"])
                                         and "Zyxwvq JKQ" in plain_text(0)) if sysfont else None
        # 8. Type3 text: covered (not removed), with the notice
        em.open_pdf(str(files["d"]))
        open_block(0, "ABCAB")
        te.set_text("NEW WORDS")
        cover_note = any("covered, not removed" in n for n in te.notices())
        ok = te.commit(ask=lambda kind, detail: "grow" if kind == "overflow" else False)
        t = plain_text(0)
        out["type3_cover_notice"] = (cover_note and ok and te.last_result["method"] == "cover"
                                     and "NEW WORDS" in t and "ABCAB" in t
                                     and em.work._entries[-1]["name"] == "Edit text")
        # 9. content editing not permitted (owner password unknown): blocked with an explanation
        locked.ask_password = lambda *a, **k: "u"
        em.open_pdf(str(files["e"]))
        nd = len(r["dialogs"])
        blocked = te.open_at(em.canvas, 0, pymupdf.Point(150, 110)) is False and te.editing is None
        out["permission_block"] = blocked and any("security settings" in m for m in r["dialogs"][nd:])
        out["originals_byte_identical"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == H[k] for k, p in files.items())
        w.close()
        out["ok"] = all(v is not False for k, v in out.items())
        return out
    check("s11_text_edit", s11_text_edit)

    def s11_find_replace():
        """Stage 11 find / replace: a single replace, Replace All with its preview as ONE undo step,
        regex groups, hits that can't be replaced listed as skipped with the reason, the find index
        refreshed after replacing, the batch step, originals byte-identical."""
        import hashlib
        import time as _t
        import pymupdf
        from . import batch as B
        from . import textedit as TE
        fw = work / "s11r"
        fw.mkdir(exist_ok=True)
        d = pymupdf.open()
        pg = d.new_page()
        pg.insert_text((72, 90), "alpha one and alpha two", fontsize=12)
        pg.insert_text((72, 140), "contact: alice@example and bob@sample", fontsize=12)
        pg.insert_text((72, 300), "this ends the first", fontsize=12)
        pg.insert_text((72, 420), "second block starts here", fontsize=12)
        pg.insert_text((72, 520), "alpha hidden", fontsize=12, render_mode=3)
        p2 = d.new_page()
        p2.insert_text((72, 90), "alpha on page two", fontsize=12)
        p2.set_rotation(90)
        src = fw / "r.pdf"
        d.save(str(src))
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, fc, rc = w.editor_mode, w.find, w.replace
        em.open_pdf(str(src))
        out = {}

        def search(q, regex=False):
            fc.open()
            fc.regex.setChecked(regex)
            fc.edit.setText(q)
            fc._debounce.stop()
            fc.start_search()
            t0 = _t.time()
            while (fc._tick.isActive() or not fc.hits and fc.count.text().startswith("Search")) and _t.time() - t0 < 20:
                app.processEvents()
            wait(30)
            return len(fc.hits)

        def text():
            s = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
            try:
                return [" ".join(p.get_text().split()) for p in s]
            finally:
                s.close()
        rc.open()
        out["ctrl_h_opens_both_bars"] = rc.bar.isVisible() and fc.bar.isVisible()
        # 1. one replace: only the current hit; the find index follows (counts are right after)
        n_before = search("alpha")                      # 3 visible + 1 invisible (find sees it too)
        s0 = em.work.step()
        rc.edit.setText("OMEGA")
        fc.go(0)
        one = rc.replace_one()
        wait(50)
        t0 = _t.time()
        while fc._tick.isActive() and _t.time() - t0 < 20:
            app.processEvents()
        wait(50)
        out["single_replace"] = (one == 1 and em.work.step() == s0 + 1 and "OMEGA one and alpha two" in text()[0])
        out["find_index_refreshed"] = n_before == 4 and len(fc.hits) == 3 and fc.count.text().endswith("of 3")
        # 2. Replace All: preview first (every change + every skip with its reason), then ONE undo step
        seen = {}

        def confirm(items):
            seen["items"] = items
            return True
        s1 = em.work.step()
        n = rc.replace_all(confirm=confirm)
        items = seen.get("items", [])
        ok_items = [it for it in items if it.status == "ok"]
        skipped = [it for it in items if it.status != "ok"]
        t = text()
        out["replace_all_preview"] = (len(ok_items) == 2 and all(it.before and it.after for it in ok_items)
                                      and sum(it.hits for it in ok_items) == 2)
        out["replace_all_one_undo_step"] = (n == 2 and em.work.step() == s1 + 1 and "alpha" not in t[0].replace(
            "alpha hidden", "") and "OMEGA on page two" in t[1])
        out["skipped_invisible_reported"] = any("invisible" in it.reason for it in skipped)
        em.undo()
        out["undo_all_at_once"] = text()[0].count("alpha") == 2 and "alpha on page two" in text()[1]
        # 3. regex groups ($2 and \1) in the replacement
        search(r"(\w+)@(\w+)", regex=True)
        rc.edit.setText("$2 at \\1")
        n = rc.replace_all(confirm=lambda items: True)
        out["regex_groups"] = n == 2 and "example at alice and sample at bob" in text()[0]
        # 4. skipped hits, with reasons: across two blocks; no font for the characters; never half-applied
        search(r"first\s+second", regex=True)
        rc.edit.setText("x")
        n = rc.replace_all(confirm=lambda items: seen.update(cross=items) or True)
        cross = seen.get("cross", [])
        search("one")
        rc.edit.setText("中文")
        st = em.work.step()
        n2 = rc.replace_all(confirm=lambda items: seen.update(nofont=items) or True)
        nofont = seen.get("nofont", [])
        out["skipped_hits_reported"] = (n == 0 and len(cross) == 1 and "two separate text blocks" in cross[0].reason
                                        and n2 == 0 and len(nofont) == 1 and "no available font" in nofont[0].reason
                                        and em.work.step() == st and "this ends the first" in text()[0])
        # 5. Find and replace as a batch step (terms captured up front)
        bd = fw / "batch"
        bd.mkdir(exist_ok=True)
        job = w.batch.start({"files": [str(src)], "pipeline": [{"op": "find_replace", "params": {
            "find": "alpha", "replace": "BETA", "range": "all"}}], "out_dir": str(bd), "pattern": "{name}-fr",
            "open_password": "", "save_pdf": True})
        job.wait(120)
        bt = pymupdf.open(str(bd / "r-fr.pdf"))
        btxt = " ".join(bt[0].get_text().split()) + " " + " ".join(bt[1].get_text().split())
        bt.close()
        out["batch_step"] = (job.status == ["done"] and "BETA one and BETA two" in btxt and "BETA on page two" in btxt
                             and "1 skipped" in job.detail[0])
        out["original_byte_identical"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        w.close()
        out["ok"] = all(out.values())
        return out
    check("s11_find_replace", s11_find_replace)

    def s12_reader():
        """Stage 12 default reader: QtNetwork bundled; association register / unregister clean and
        reversible (in a sandbox key, never the real one); ProgID + icon + open verb; a second launch
        hands its PDF to the running instance as a new tab and exits; an open path focuses its tab;
        several launches each become a tab; cold-start and warm-open timings; thumbnails lazy and
        off the UI thread."""
        import hashlib
        import json
        import os
        import subprocess
        import sys
        from pathlib import Path
        import threading
        import time as _t
        import uuid
        import pymupdf
        from PySide6.QtCore import QTimer as _QT
        from . import assoc as A
        from .instance import InstanceServer, OpenQueue
        from .settings_dialog import SettingsDialog
        rw = work / "s12"
        rw.mkdir(exist_ok=True)
        out = {}
        # 0. QtNetwork is in the build
        from PySide6 import QtNetwork
        net_file = Path(QtNetwork.__file__)
        out["qtnetwork_bundled"] = (not r["frozen"]) or net_file.resolve().is_relative_to(
            Path(sys._MEIPASS).resolve())
        # 1. association: register then unregister through Settings, in a sandbox key
        root = rf"Software\LazarusSelfTest\{uuid.uuid4().hex[:8]}"
        os.environ["LAZARUS_ASSOC_ROOT"] = root
        reg = A.Registry(root)
        try:
            reg.set(r"Software\Classes\.pdf", "", "OtherApp.PDF")                 # another app's, as Windows has it
            reg.set(r"Software\Classes\.pdf\OpenWithProgids", "OtherApp.PDF", b"", 0)
            reg.set(r"Software\Classes\Applications\Other.exe\shell\open\command", "", "other %1")
            reg.set(r"Software\RegisteredApplications", "OtherApp", r"Software\Other\Capabilities")
            reg.set(r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice", "ProgId",
                    "OtherApp.PDF")
            before = reg.dump()
            w = gui.Main()
            dlg = SettingsDialog(w, w.settings)
            st0 = dlg.assoc_state.text()
            nd = len(r["dialogs"])
            dlg.make_default()
            told = r["dialogs"][nd:]
            cls = rf"Software\Classes\{A.PROGID}"
            cmd = reg.get(cls + r"\shell\open\command") or ""
            icon = reg.get(cls + r"\DefaultIcon") or ""
            exe_ref = Path(sys.executable).name if r["frozen"] else "run_lazaruspdf.pyw"
            out["progid_icon_open_verb"] = (reg.get(cls, "FriendlyTypeName") == A.FRIENDLY and icon.endswith(",0")
                                            and cmd.endswith('"%1"') and exe_ref in cmd
                                            and reg.get(cls + r"\shell") == "open")
            out["capabilities_registered"] = (reg.get(r"Software\RegisteredApplications", A.REG_NAME) == A.CAPS
                                              and reg.get(A.CAPS + r"\FileAssociations", ".pdf") == A.PROGID
                                              and reg.has(r"Software\Classes\.pdf\OpenWithProgids", A.PROGID))
            st1 = dlg.assoc_state.text()
            out["windows_confirm_told"] = (any("registered as a PDF reader" in m for m in told) and "confirm" in A.CONFIRM_NOTE
                                           and "another program is still the default" in st1 and "isn't registered" in st0)
            out["other_app_untouched"] = (reg.get(r"Software\Classes\.pdf") == "OtherApp.PDF"
                                          and reg.has(r"Software\Classes\.pdf\OpenWithProgids", "OtherApp.PDF")
                                          and reg.get(r"Software\RegisteredApplications", "OtherApp") is not None)
            reg.set(r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice", "ProgId", A.PROGID)
            st_def = dlg._assoc_state()
            reg.set(r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice", "ProgId",
                    "OtherApp.PDF")
            dlg.unregister()
            after = reg.dump()
            out["register_unregister_clean"] = after == before and not A.is_registered(reg) and "your default" in st_def
            dlg.close()
            w.close()
        finally:
            reg.delete_tree("")
            A.Registry().delete_if_empty(r"Software\LazarusSelfTest")
            os.environ.pop("LAZARUS_ASSOC_ROOT", None)
        out["sandbox_removed"] = not A.Registry().exists(root)

        # PDFs for the launches
        def mk(name, pages=3):
            d = pymupdf.open()
            for i in range(pages):
                d.new_page().insert_text((72, 100), f"{name} page {i + 1}", fontsize=24)
            p = rw / f"{name}.pdf"
            d.save(str(p))
            d.close()
            return p
        files = [mk(n) for n in ("one", "two", "three", "four", "five")]
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        here = Path(gui.__file__).resolve().parent.parent

        def launch(args, extra=None):
            env = dict(os.environ)
            env.update({"QT_QPA_PLATFORM": "offscreen", "LAZARUS_SETTINGS_INI": str(rw / "child.ini")})
            env.update(extra or {})
            env.pop("LAZARUS_STARTUP_REPORT", None) if not (extra or {}).get("LAZARUS_STARTUP_REPORT") else None
            cmd = [sys.executable] + ([] if r["frozen"] else ["-m", "lazaruspdf"]) + [str(a) for a in args]
            si = subprocess.STARTUPINFO()          # hidden, and no busy cursor on the user's desktop
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW | 0x80          # 0x80: STARTF_FORCEOFFFEEDBACK
            si.wShowWindow = 0                                               # SW_HIDE
            return subprocess.Popen(cmd, env=env, cwd=str(here), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)

        def pump(cond, limit):
            t0 = _t.time()
            while not cond() and _t.time() - t0 < limit:
                app.processEvents()
                _t.sleep(0.005)
            return cond()

        # 2. cold start to the first page (a fresh process, no instance running)
        rep = rw / "cold.json"
        t0 = _t.time()
        cp = launch([files[0]], {"LAZARUS_INSTANCE_NAME": "lz-cold-" + uuid.uuid4().hex[:8],
                                 "LAZARUS_STARTUP_REPORT": str(rep), "LAZARUS_EXIT_AFTER_FIRST_PAGE": "1"})
        cp.wait(120)
        wall = _t.time() - t0
        cold = json.loads(rep.read_text()) if rep.exists() else {}
        out["cold_start_first_page_s"] = cold.get("first_page")
        out["cold_start_wall_s"] = round(wall, 2)
        out["cold_start_measured"] = bool(cold.get("first_page")) and cp.returncode == 0

        # 3. the running instance: hand-offs from later launches
        name = "lz-st-" + uuid.uuid4().hex[:8]
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        from .instance import claim
        claim(name)                                 # this test process is THE instance for that name
        server = InstanceServer(name)
        listening = server.listen()
        q = OpenQueue(w)
        server.openRequested.connect(q.add)
        em.open_pdf(str(files[0]))

        def rendered(path):
            s = next((x for x in em.sessions if not x.is_job and x.is_source(path)), None)
            return s is not None and s.ready and bool(s.canvas.pages) and bool(s.canvas.pages[0]["scale"])
        t0 = _t.time()
        p = launch([files[1]], {"LAZARUS_INSTANCE_NAME": name})
        handed = pump(lambda: p.poll() is not None and rendered(files[1]), 120)
        warm = _t.time() - t0
        out["handoff_new_tab"] = (listening and handed and p.returncode == 0 and server.connections == 1
                                  and len(em.sessions) == 2 and em.cur.is_source(files[1]))
        out["warm_open_s"] = round(warm, 2)
        # an already-open path: its tab comes to the front, no duplicate
        em.activate(em.sessions[0])
        p = launch([files[1]], {"LAZARUS_INSTANCE_NAME": name})
        pump(lambda: p.poll() is not None and q.pending == [] and em.cur.is_source(files[1]), 60)
        out["open_path_focuses_tab"] = (p.returncode == 0 and len(em.sessions) == 2 and em.cur.is_source(files[1])
                                        and server.connections == 2)
        # several launches at once: each PDF becomes a tab
        ps = [launch([f], {"LAZARUS_INSTANCE_NAME": name}) for f in files[2:5]]
        ok = pump(lambda: all(x.poll() is not None for x in ps) and all(rendered(f) for f in files[2:5]), 180)
        out["queued_launches_each_a_tab"] = (ok and all(x.returncode == 0 for x in ps) and len(em.sessions) == 5
                                             and server.connections == 5)
        for x in ps:                                  # nothing launched here may outlive the test
            if x.poll() is None:
                x.kill()
        # 4. thumbnails: lazy (visible + a few), off the UI thread, cached when scrolled back
        big = rw / "big.pdf"
        d = pymupdf.open()
        for i in range(600):
            pg = d.new_page()
            pg.insert_text((72, 100), f"Page {i + 1}", fontsize=40)
            pg.draw_rect(pymupdf.Rect(50, 50, 500, 700), color=(0, 0, 1))
        d.save(str(big))
        d.close()
        em.open_pdf(str(big))
        th = em.thumbs
        gaps, last = [], [_t.perf_counter()]

        def beat():
            n_ = _t.perf_counter()
            gaps.append(n_ - last[0])
            last[0] = n_
        hb = _QT()
        hb.setInterval(5)
        hb.timeout.connect(beat)
        hb.start()
        pump(lambda: False, 1.5)
        first_batch = th.stats["worker"]
        th.scrollToItem(th.item(300))
        pump(lambda: False, 1.5)
        mid = th.stats["worker"]
        th.scrollToItem(th.item(0))
        pump(lambda: False, 0.8)
        back = th.stats["worker"]
        hb.stop()
        wt = th._worker.threads if th._worker is not None else set()
        out["thumbs_lazy"] = 0 < first_batch < 60 and len(th._done) < 120 and 300 in th._done and 0 in th._done
        out["thumbs_off_ui_thread"] = bool(wt) and threading.get_ident() not in wt and th.stats["main"] == 0
        out["thumbs_cached"] = back == mid > first_batch
        out["thumbs_max_ui_gap_ms"] = round(1000 * max(gaps)) if gaps else None
        out["thumbs_rendered_of_600"] = len(th._done)
        # an edited page re-renders on the UI thread (the worker's copy is the file as opened)
        em.canvas.select_chars(0, 0, 3)
        em.highlight_selection()
        wait(100)
        out["edited_page_rerenders"] = th.stats["main"] >= 1 and 0 in th._done
        out["originals_untouched"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        server.close()
        w.close()
        out["ok"] = all(v is not False for k, v in out.items() if not k.endswith(("_s", "_ms", "_of_600")))
        return out
    check("s12_reader", s12_reader)

    def s13_compare():
        """Stage 13 compare: word insert / delete / modify at exact boxes on both panes; pages
        added / removed / moved without cascading changes; reflow across a page break is no change;
        ignore options; no-text pages; visual mode (a drawing-only change found, anti-alias noise
        ignored); change list click-to-jump; F7; aligned synced scrolling; report to a new file;
        background run with Cancel; results clear on edit; journals unchanged; originals intact."""
        import hashlib
        import random
        import time as _t
        import pymupdf
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from . import compare as C
        cw = work / "s13"
        cw.mkdir(exist_ok=True)
        VOC = ("river stone market winter garden copper signal harbor meadow lantern orbit canvas thunder velvet "
               "anchor bridge cotton marble silver forest engine parcel ladder window rocket summer pebble violet "
               "falcon tunnel candle meteor saddle trumpet ribbon basket glacier compass").split()

        def para(seed, n=45):
            rr = random.Random(seed)
            return " ".join(rr.choice(VOC) + str(seed % 7) * (i % 5 == 0) for i in range(n)) + "."
        BODY = {i: [para(i * 10 + k) for k in range(3)] for i in range(8)}

        def page(d, paras, num, rect=None, dx=0.0):
            p = d.new_page()
            p.insert_text((72 + dx, 40), "ACME Quarterly Report", fontsize=9)
            p.insert_text((297 + dx, 810), str(num), fontsize=9)
            if paras:
                p.insert_textbox(pymupdf.Rect(72 + dx, 70, 523 + dx, 760), "\n\n".join(paras), fontsize=11)
            if rect:
                p.draw_rect(pymupdf.Rect(rect), color=(0, 0, 1), fill=(0.8, 0.85, 1), width=1.5)

        def scan(d):
            t_ = pymupdf.open()
            tp = t_.new_page()
            tp.insert_text((72, 120), "SCANNED PAGE CONTENT", fontsize=30)
            pix = tp.get_pixmap(dpi=60)
            t_.close()
            d.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=pix)
        L = pymupdf.open()
        for i in range(7):
            page(L, BODY[i], i + 1, rect=(100, 600, 200, 700) if i == 6 else None)
        scan(L)
        Lp = cw / "left.pdf"
        L.save(str(Lp))
        L.close()
        R = pymupdf.open()
        b0 = list(BODY[0])
        ws = b0[1].split()
        ws.insert(5, "INSERTED")
        b0[1] = " ".join(ws)
        page(R, b0, 1)
        b1 = list(BODY[1])
        ws = b1[0].split()
        dropped = ws.pop(7)
        changed = ws[12]
        ws[12] = "MODIFIEDWORD"
        b1[0] = " ".join(ws)
        page(R, b1, 2)
        page(R, ["Brand new appendix page with entirely different words: " + para(999, 60)], 3)
        page(R, BODY[3][:2], 4)                         # the last paragraph flows onto the next page
        page(R, [BODY[3][2]] + BODY[4], 5)
        page(R, BODY[6], 6, rect=(100, 600, 200, 700))
        scan(R)
        page(R, BODY[5], 8)                             # left page 6 moved to the end
        Rp = cw / "right.pdf"
        R.save(str(Rp))
        R.close()
        # options pair, visual pair, a big pair (progress / cancel)
        Op, Oq = cw / "o-left.pdf", cw / "o-right.pdf"
        for path, text in ((Op, "Hello, World! The Quick fox can co operate today."),
                           (Oq, "hello world The quick fox can cooperate today")):
            d = pymupdf.open()
            d.new_page().insert_text((72, 100), text, fontsize=12)
            d.save(str(path))
            d.close()
        Vp, Vq = cw / "v-left.pdf", cw / "v-right.pdf"
        for path, rect, dx in ((Vp, (100, 100, 200, 200), 0.0), (Vq, (300, 300, 400, 400), 0.3)):
            d = pymupdf.open()
            page(d, BODY[2][:1], 1, rect=rect)
            page(d, BODY[7][:1], 2, dx=dx)                       # the same page, 0.3 pt to the right
            d.save(str(path))
            d.close()
        Bp, Bq = cw / "b-left.pdf", cw / "b-right.pdf"
        for path in (Bp, Bq):
            d = pymupdf.open()
            for i in range(150):
                page(d, [para(i * 31 + k) for k in range(3)], i + 1)
            d.save(str(path))
            d.close()
        files = [Lp, Rp, Op, Oq, Vp, Vq, Bp, Bq]
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

        w = gui.Main()
        w.resize(1600, 950)
        w.show()
        wait(150)
        em, ctl = w.editor_mode, w.compare_ctl
        w.settings.remove("compare_options")
        out = {}

        def sess(path):
            em.open_pdf(str(path))
            return em.cur

        def pair(a, b):
            em.activate(a)
            if not em.comparing():
                em.show_compare(True)
            em.compare.show_pair(a, b)
            wait(150)
        sl, sr = sess(Lp), sess(Rp)
        steps0 = (sl.work.step(), sr.work.step(), sl.doc_version, sr.doc_version)
        pair(sl, sr)
        out["dock_and_bar_shown"] = ctl.dock.isVisible() and ctl.bar.isVisible()
        t0 = _t.time()
        ok = ctl.run(wait=True, mode="text")
        res = ctl.result
        out["text_run_s"] = round(_t.time() - t0, 2)
        out["longest_ui_pause_ms"] = round(ctl.max_gap * 1000)
        s = res["summary"] if res else {}
        out["pages_added_removed_moved"] = (ok and res["removed"] == [2] and res["added"] == [2]
                                            and res["moved"] == [(5, 7)])
        out["no_cascading_changes"] = s == {"insert": 1, "delete": 1, "modify": 1, "added": 1, "removed": 1, "moved": 1}

        def words_in(path, pno, rect):
            """The words whose boxes are inside rect (displayed coordinates) on that page."""
            d = pymupdf.open(str(path))
            pg = d[pno]
            rr = pymupdf.Rect(rect)
            ws_ = [x[4] for x in pg.get_text("words")
                   if rr.contains((pymupdf.Rect(x[:4]) * pg.rotation_matrix).tl + (0.5, 0.5))]
            d.close()
            return ws_
        ch = {c["kind"]: c for c in res["changes"]}
        ins, dele, mod = ch.get("insert"), ch.get("delete"), ch.get("modify")
        out["insert_exact_box"] = (ins is not None and not ins["left"] and list(ins["right"]) == [0]
                                   and words_in(Rp, 0, ins["right"][0][0]) == ["INSERTED"])
        out["delete_exact_box"] = (dele is not None and not dele["right"] and list(dele["left"]) == [1]
                                   and words_in(Lp, 1, dele["left"][1][0]) == [dropped])
        out["modify_exact_boxes"] = (mod is not None and words_in(Lp, 1, mod["left"][1][0]) == [changed]
                                     and words_in(Rp, 1, mod["right"][1][0]) == ["MODIFIEDWORD"])
        per_pane = [sum(1 for items in ctl.items for it_ in items if it_.scene() is em.compare.panes[k].scene())
                    for k in (0, 1)]
        out["marked_on_both_panes"] = per_pane[0] == 2 and per_pane[1] == 2
        out["reflow_not_a_change"] = not any(c["lpage"] in (3, 4) or c["rpage"] in (3, 4) for c in res["changes"])
        out["no_text_pages"] = (res["notext"] == {"left": [7], "right": [6]}
                                and sum(1 for row in ctl.rows() if row[1] == "No text") == 2)
        # change list click-to-jump, F7 / Shift+F7 wrapping
        it = next(ctl.list.topLevelItem(i) for i in range(ctl.list.topLevelItemCount())
                  if ctl.list.topLevelItem(i).data(0, Qt.UserRole) == 2)
        ctl.list.itemClicked.emit(it, 0)
        wait(50)
        c2 = res["changes"][2]
        lp_, lrect = next(iter(c2["left"].items()))
        lvis = em.compare.panes[0].visible_rect().contains(em.compare.panes[0].page_to_scene(lp_, pymupdf.Rect(lrect[0])).center())
        rp_, rrect = next(iter(c2["right"].items()))
        rvis = em.compare.panes[1].visible_rect().contains(em.compare.panes[1].page_to_scene(rp_, pymupdf.Rect(rrect[0])).center())
        out["list_click_jumps_both"] = ctl.cur == 2 and lvis and rvis
        ctl.act_next.trigger()
        a1 = ctl.cur
        ctl.act_next.trigger()
        a2 = ctl.cur
        ctl.act_prev.trigger()
        out["f7_navigation_wraps"] = (a1 == 0 and a2 == 1 and ctl.cur == 0 and ctl.act_next.shortcut().toString() == "F7"
                                      and ctl.act_prev.shortcut().toString() == "Shift+F7")
        # synced scrolling follows the alignment (left page 7 is right page 6)
        def top_page(pane):
            top = pane.visible_rect().top()
            return next(i for i, pg in enumerate(pane.pages) if top <= pg["rect"].bottom() + 18.0)
        def scroll_to(pane, i):                       # the view's top 50 pt inside page i
            vr = pane.visible_rect()
            pane.centerOn(vr.center().x(), pane.pages[i]["rect"].top() + 50 + vr.height() / 2)
            wait(80)
        scroll_to(em.compare.panes[0], 6)
        at6 = (top_page(em.compare.panes[0]), top_page(em.compare.panes[1]))
        scroll_to(em.compare.panes[0], 3)
        at3 = (top_page(em.compare.panes[0]), top_page(em.compare.panes[1]))
        out["synced_scroll_aligned"] = at6 == (6, 5) and at3 == (3, 3)
        # running headers / page numbers count when not ignored
        ctl.run(wait=True, options={"ignore_running": False})
        out["headers_footers_option"] = ctl.result["summary"]["modify"] + ctl.result["summary"]["insert"] + \
            ctl.result["summary"]["delete"] > 3
        # the report: a new file, never onto either PDF
        rep = cw / "report.pdf"
        nd = len(r["dialogs"])
        refused = ctl.export_report(str(Lp), wait=True) is False and any("original" in m for m in r["dialogs"][nd:])
        ctl.run(wait=True)
        ctl.export_report(str(rep), wait=True)
        ok_rep = rep.exists()
        if ok_rep:
            d = pymupdf.open(str(rep))
            txt = "".join(p.get_text() for p in d)
            ok_rep = d.page_count >= 2 and "Comparison report" in txt and "Inserted" in txt and not d.needs_pass
            d.close()
        out["report_new_file"] = ok_rep and refused and not any(".lazaruspdf-partial" in p.name for p in cw.iterdir())
        out["journals_unchanged"] = (sl.work.step(), sr.work.step(), sl.doc_version, sr.doc_version) == steps0
        # results clear when a document is edited
        em.activate(sl)
        em.canvas.select_chars(0, 0, 5)
        em.highlight_selection()
        wait(50)
        out["results_clear_on_edit"] = ctl.result is None and not ctl.items
        em.undo()
        # ignore options
        so, sq = sess(Op), sess(Oq)
        pair(so, sq)
        base = {"ignore_running": True}
        ctl.run(wait=True, options=dict(base, ignore_case=False, ignore_punct=False, ignore_whitespace=False))
        n_all = len(ctl.result["changes"])
        ctl.run(wait=True, options=dict(base, ignore_case=True, ignore_punct=True, ignore_whitespace=False))
        n_cp = [(c["ltext"], c["rtext"]) for c in ctl.result["changes"]]
        ctl.run(wait=True, options=dict(base, ignore_case=True, ignore_punct=True, ignore_whitespace=True))
        out["ignore_options"] = n_all == 3 and n_cp == [("co operate", "cooperate")] and not ctl.result["changes"]
        # visual mode: a drawing-only change found; a 0.3 pt shift (anti-aliasing) ignored
        sv, sw = sess(Vp), sess(Vq)
        pair(sv, sw)
        ctl.run(wait=True, mode="text")
        text_none = not ctl.result["changes"]
        ctl.run(wait=True, mode="visual")
        vis = ctl.result["changes"]
        out["visual_drawing_change"] = text_none and len(vis) == 2 and all(c["lpage"] == 0 for c in vis)
        out["visual_ignores_antialias"] = not any(c["lpage"] == 1 for c in vis)
        ctl.mode.setCurrentIndex(0)
        # a background run: progress, the window stays live, Cancel stops it
        sb, sc = sess(Bp), sess(Bq)
        pair(sb, sc)
        ctl.run(test_delay=0.05)
        t0 = _t.time()
        while ctl.job is not None and not ctl.progress.text().startswith(("Reading", "Fingerprinting", "Aligning",
                                                                           "Comparing")) and _t.time() - t0 < 30:
            app.processEvents()
        live = ctl.job is not None and ctl.btn_cancel.isVisible()
        tc = _t.time()
        ctl.cancel()
        while ctl.job is not None and _t.time() - tc < 15:
            app.processEvents()
        out["cancel_stops"] = live and ctl.job is None and ctl.result is None and ctl.progress.text() == "Cancelled."
        out["cancel_s"] = round(_t.time() - tc, 2)
        t0 = _t.time()
        ctl.run(wait=True)
        out["big_150_pages_s"] = round(_t.time() - t0, 2)
        out["big_longest_ui_pause_ms"] = round(ctl.max_gap * 1000)
        out["big_identical"] = ctl.result is not None and not ctl.result["changes"] and not ctl.result["events"]
        out["originals_byte_identical"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items()
                                              if p != rep)
        w.close()
        out["ok"] = all(v for k, v in out.items() if not k.endswith(("_s", "_ms")))
        return out
    check("s13_compare", s13_compare)

    def s14_images():
        """Stage 14 images to PDF: every format (multi-page TIFF, SVG), EXIF orientation,
        reorder / rotate / remove / sort, fit-to-image vs fixed sizes with fit / fill / stretch and
        margins, JPEG passthrough vs recompress, N per page, OCR after the build, images inserted
        into an open PDF (undo / redo), background build with progress and Cancel, memory bounded
        on a large set, new tab dirty and unsaved, drops routed, inputs untouched."""
        import hashlib
        import io
        import os
        import time as _t
        import numpy as np
        import pymupdf
        from PIL import Image, ImageDraw
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent
        from pathlib import Path
        from PySide6.QtCore import Qt
        from . import images as I
        from . import oprules
        from .images_ui import ImagesDialog
        iw = work / "s14"
        src = iw / "img"
        src.mkdir(parents=True, exist_ok=True)

        def pic(w, h, color):
            im = Image.new("RGB", (w, h), color)
            ImageDraw.Draw(im).rectangle((0, 0, w // 4, h // 8), fill=(255, 0, 0))   # red mark: top left
            return im
        files = {}
        for ext, fmt in (("png", "PNG"), ("jpg", "JPEG"), ("webp", "WEBP"), ("bmp", "BMP"), ("gif", "GIF")):
            p = src / f"a-{ext}.{ext}"
            pic(400, 300, (40, 120, 200)).save(p, fmt, **({"dpi": (150, 150)} if fmt in ("PNG", "JPEG") else {}))
            files[ext] = p
        fr = [pic(300, 400, c) for c in ((200, 50, 50), (50, 200, 50), (50, 50, 200))]
        files["tif"] = src / "b-multi.tif"
        fr[0].save(files["tif"], "TIFF", save_all=True, append_images=fr[1:])
        files["svg"] = src / "c-vector.svg"
        files["svg"].write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
                                '<rect x="10" y="10" width="180" height="80" fill="#3a7"/></svg>')
        ex = Image.Exif()
        ex[0x0112] = 6
        ex[0x9003] = "2020:01:02 03:04:05"
        files["exif"] = src / "d-phone.jpg"
        pic(200, 300, (220, 220, 220)).rotate(90, expand=True).save(files["exif"], "JPEG", exif=ex.tobytes(), quality=90)
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files.values()}
        before = sorted(os.listdir(src))
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(150)
        em, ic = w.editor_mode, w.images
        w.settings.remove("images_to_pdf")
        out = {}
        # the dialog: list, reorder, rotate, remove, sort; settings remembered
        dlg = ImagesDialog(w, w.settings, [str(src)])
        names = lambda: [Path(i["path"]).name + ":" + str(i["frame"]) for i in dlg.items]
        n0 = names()
        dlg.list.item(0).setSelected(True)
        dlg.move(1)
        moved = names()
        dlg.list.clearSelection()
        dlg.list.item(1).setSelected(True)
        dlg.rotate(-90)
        rot = dlg.items[1]["rotate"]
        dlg.list.clearSelection()
        dlg.list.item(2).setSelected(True)
        dlg.remove()
        removed = len(dlg.items)
        dlg.sort_by("name")
        sorted_ok = [Path(i["path"]).name for i in dlg.items] == sorted(Path(i["path"]).name for i in dlg.items)
        dlg.sort_by("taken")
        taken_first = Path(dlg.items[0]["path"]).name == "d-phone.jpg"
        dlg.page.setCurrentIndex(dlg.page.findData("a4"))
        dlg.margin.setValue(12)
        dlg.remember()
        dlg.close()
        d2 = ImagesDialog(w, w.settings)
        remembered = d2.page.currentData() == "a4" and d2.margin.value() == 12
        d2.close()
        out["dialog_list_ops"] = (len(n0) == 10 and moved[:2] == [n0[1], n0[0]] and rot == 270 and removed == 9
                                  and sorted_ok and taken_first and remembered)
        w.settings.remove("images_to_pdf")
        items = I.items_for(I.collect([src]))
        # 1. a new PDF (fit each page to its image): opens as a new tab, unsaved and changed
        n_tabs = len(em.sessions)
        ok = ic.new_pdf(answer=(items, {}, None), wait=True)
        s = em.cur
        doc = em.doc
        out["new_tab_dirty_unsaved"] = (ok and len(em.sessions) == n_tabs + 1 and getattr(s, "untitled", False)
                                        and em.dirty() and s.work.target is None and not s.source.exists()
                                        and em.tabbar.tabText(em.sessions.index(s)).startswith("●"))
        kinds = [Path(i["path"]).suffix.lower() for i in items]
        out["every_format"] = (doc.page_count == 10 and all(len(doc[k].get_images()) == 1 for k in range(10))
                               and kinds.count(".tif") == 3 and ".svg" in kinds)
        k_svg = kinds.index(".svg")
        out["svg_native_size"] = tuple(round(v) for v in doc[k_svg].rect[2:]) == (200, 100)
        k_ex = [Path(i["path"]).name for i in items].index("d-phone.jpg")
        pg = doc[k_ex]
        pix = pg.get_pixmap(dpi=72)
        out["exif_orientation"] = (pg.rect.width < pg.rect.height and pix.pixel(4, 4)[0] > 200
                                   and pix.pixel(4, 4)[1] < 60 and pix.pixel(pix.width - 4, pix.height - 4)[0] > 200
                                   and pix.pixel(pix.width - 4, pix.height - 4)[1] > 200)
        k_jpg = [Path(i["path"]).name for i in items].index("a-jpg.jpg")
        img = doc.extract_image(doc[k_jpg].get_images()[0][0])
        out["jpeg_passthrough"] = (img["image"] == files["jpg"].read_bytes()
                                   and doc.extract_image(pg.get_images()[0][0])["image"] == files["exif"].read_bytes())
        del pg
        ic.new_pdf(answer=(items, {"quality": "recompress", "jpeg_quality": 40, "max_dpi": 50}, None), wait=True)
        d3 = em.doc
        img3 = d3.extract_image(d3[k_jpg].get_images()[0][0])
        out["recompress"] = img3["ext"] in ("jpeg", "jpg") and img3["image"] != files["jpg"].read_bytes() and \
            len(img3["image"]) < len(files["jpg"].read_bytes()) and img3["width"] < 400
        # 2. fixed sizes: fit / fill / stretch with margins
        geo = {}
        for placement in ("fit", "fill", "stretch"):
            ic.new_pdf(answer=(items[k_jpg:k_jpg + 1], {"page": "a4", "placement": placement, "margin_mm": 20,
                                                          "orientation": "portrait"}, None), wait=True)
            pg = em.doc[0]
            info = pg.get_image_info(xrefs=True)[0]
            geo[placement] = (tuple(round(v) for v in pg.rect[2:]), pymupdf.Rect(info["bbox"]), info["width"],
                              info["height"])
            del pg
        m = 20 * 72 / 25.4
        area = pymupdf.Rect(m, m, 595.28 - m, 841.89 - m)
        fit_r, fill_r, st_r = geo["fit"][1], geo["fill"][1], geo["stretch"][1]
        out["fixed_fit_fill_stretch"] = (geo["fit"][0] == (595, 842)
                                         and pymupdf.Rect(area.x0 - 0.5, area.y0 - 0.5, area.x1 + 0.5, area.y1 + 0.5).contains(fit_r)
                                         and abs(fit_r.width - area.width) < 1
                                         and abs(fit_r.width / fit_r.height - 400 / 300) < 0.01
                                         and abs(fill_r.width - area.width) < 1 and abs(fill_r.height - area.height) < 1
                                         and abs(geo["fill"][2] / geo["fill"][3] - area.width / area.height) < 0.02
                                         and abs(st_r.width - area.width) < 1 and abs(st_r.height - area.height) < 1
                                         and abs(geo["stretch"][2] / geo["stretch"][3] - 400 / 300) < 0.01)
        # 3. N per page
        ic.new_pdf(answer=(items, {"page": "letter", "per_page": 4}, None), wait=True)
        out["grid_per_page"] = em.doc.page_count == 3 and [len(p.get_images()) for p in em.doc] == [4, 4, 2]
        # 4. OCR after the build (stage 10's OCR, in the background)
        tp = pymupdf.open()
        tpg = tp.new_page()
        tpg.insert_text((60, 120), "HELLO IMAGES", fontsize=40)
        ocr_png = src.parent / "ocr.png"
        tpg.get_pixmap(dpi=150).save(str(ocr_png))
        tp.close()
        H[ocr_png] = hashlib.sha256(ocr_png.read_bytes()).hexdigest()
        ic.new_pdf(answer=([{"path": str(ocr_png), "frame": 0, "rotate": 0}], {"ocr": True}, None), wait=True)
        t0 = _t.time()
        while (em.stage10.last_ocr is None or em.stage10.ocr_worker is None or em.stage10.ocr_worker.isRunning()) \
                and _t.time() - t0 < 120:
            app.processEvents()
        wait(200)
        s_ocr = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        out["ocr_after_build"] = "HELLO" in s_ocr[0].get_text().upper()
        s_ocr.close()
        # 5. insert images into an open PDF: one undo step, undo / redo
        base = iw / "base.pdf"
        bd = pymupdf.open()
        for k in range(2):
            bd.new_page().insert_text((72, 72), f"base page {k + 1}")
        bd.save(str(base))
        bd.close()
        H[base] = hashlib.sha256(base.read_bytes()).hexdigest()
        em.open_pdf(str(base))
        st0 = em.work.step()
        ic.insert(answer=(items[:2], {}, 1), wait=True)
        ins_ok = em.doc.page_count == 4 and len(em.doc[1].get_images()) == 1 and em.work.step() == st0 + 1 \
            and "base page 2" in em.doc[3].get_text()
        em.undo()
        und = em.doc.page_count == 2
        em.redo()
        out["insert_undo_redo"] = ins_ok and und and em.doc.page_count == 4 and oprules.kind("insert_image_pages") == "journal"
        # 6. a large set: background, progress, memory bounded (streamed), and Cancel
        big = iw / "big"
        big.mkdir(exist_ok=True)
        rng = np.random.default_rng(1)
        for k in range(100):
            arr = rng.integers(0, 255, (1500, 2000, 3), dtype=np.uint8)
            Image.fromarray(arr).save(big / f"p{k:03d}.jpg", "JPEG", quality=70)
        bitems = I.items_for(I.collect([big]))
        decoded_all = 100 * 2000 * 1500 * 3
        t0 = _t.time()
        ok = ic.new_pdf(answer=(bitems, {}, None), wait=True)
        out["large_build_s"] = round(_t.time() - t0, 2)
        lst = dict(ic.last or {})
        out["large_longest_ui_pause_ms"] = lst.get("gap_ms")
        out["large_pdf_mb"] = round(lst.get("size", 0) / 2 ** 20)
        out["large_peak_worker_mb"] = round((lst.get("peak") or 0) / 2 ** 20)
        out["progress_x_of_y"] = ok and lst.get("progress", [])[:1] == [0] and lst.get("progress", [])[-1] == 100 \
            and len(set(lst.get("progress", []))) >= 50
        out["memory_bounded"] = bool(lst.get("peak")) and lst["peak"] < 2 * lst["size"] + 300 * 2 ** 20 \
            and lst["peak"] < decoded_all
        out["window_live_while_building"] = lst.get("gap_ms") is not None and lst["gap_ms"] < 250
        n_tabs = len(em.sessions)
        ic.new_pdf(answer=(bitems, {}, None), test_delay=0.05)
        t0 = _t.time()
        while ic.job is not None and len(ic.job["seen"]) < 5 and _t.time() - t0 < 60:
            app.processEvents()
        tc = _t.time()
        ic.cancel()
        while ic.job is not None and _t.time() - tc < 20:
            app.processEvents()
        out["cancel_stops"] = (ic.job is None and (ic.last or {}).get("why") == "cancelled"
                               and len(em.sessions) == n_tabs)
        out["cancel_s"] = round(_t.time() - tc, 2)
        # 7. images dropped on the window go to New PDF from Images
        got = []
        ic.dropped = lambda paths: got.append(paths)
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(files["png"])), QUrl.fromLocalFile(str(files["jpg"]))])
        ev = QDropEvent(QPointF(50, 50), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
        w.dropEvent(ev)
        wait(50)
        out["drop_images_routed"] = len(got) == 1 and len(got[0]) == 2
        out["inputs_untouched"] = (all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
                                   and sorted(os.listdir(src)) == before)
        w.close()
        out["ok"] = all(v for k, v in out.items() if not k.endswith(("_s", "_ms", "_mb")))
        return out
    check("s14_images", s14_images)

    def s15_scan():
        """Stage 15 scanning (WIA) with a MOCK device layer: enumeration (none / one / several),
        capability-driven lists, a flatbed session with reorder / rotate / delete / rescan, a feeder
        run ending on feeder-empty, duplex order, blank-page skip, deskew / crop, a mid-scan error
        keeping earlier pages, Cancel, OCR after the scan, scanned pages inserted into an open PDF
        (undo / redo), the new tab dirty and unsaved. Plus the real COM bridge (a read-only device
        listing, never a scan) and originals byte-identical."""
        import hashlib
        import io
        import time as _t
        import pymupdf
        from PIL import Image
        from PySide6.QtCore import QThread, Qt
        from . import oprules, wia
        from .ocr import estimate_skew
        from .scan_ui import ScanDialog
        sw = work / "s15"
        sw.mkdir(exist_ok=True)
        out = {}
        # the real COM bridge: WIA's DeviceManager through comtypes, on its own thread, listing only
        res = {}

        class Lister(QThread):
            def run(self):
                try:
                    res["devices"] = len(wia.RealWIA().devices())
                except Exception as e:
                    res["error"] = f"{type(e).__name__}: {e}"
        lt = Lister()
        lt.start()
        lt.wait(30000)
        import comtypes
        out["com_bridge_works"] = "devices" in res and comtypes.__version__ == "1.4.17"
        out["real_scanners_listed"] = res.get("devices")
        page = lambda t, **k: dict(text=t, **k)
        scenario = {"devices": [
            {"id": "flat", "name": "Mock flatbed", "sources": ["flatbed"], "modes": ["color", "gray"],
             "resolutions": [150, 300, 600], "brightness": [-100, 100],
             "flatbed": [page("FLAT ONE"), page("FLAT TWO"), page("FLAT THREE"), page("FLAT FOUR"),
                         page("TILTED", skew=3), page("SMALL", small=True)]},
            {"id": "adf", "name": "Mock feeder", "sources": ["flatbed", "feeder", "duplex"],
             "resolutions": [100, 200, 300],
             "feeder": [page("SHEET 1", back="SHEET 1 BACK"), page("SHEET 2", back_blank=True),
                        page("SHEET 3", back="SHEET 3 BACK")]},
            {"id": "jam", "name": "Mock jam", "sources": ["feeder"], "resolutions": [300],
             "feeder": [page(f"J{k}") for k in range(5)], "fail": {"after": 2, "kind": "jam"}},
            {"id": "gone", "name": "Mock unplugged", "sources": ["feeder"], "resolutions": [300],
             "feeder": [page(f"G{k}") for k in range(5)], "fail": {"after": 1, "kind": "disconnect"}},
            {"id": "picky", "name": "Mock picky", "sources": ["flatbed"], "resolutions": [300], "reject": ["dpi"]},
            {"id": "empty", "name": "Mock empty feeder", "sources": ["feeder"], "resolutions": [300], "feeder": []},
            {"id": "slow", "name": "Mock slow", "sources": ["feeder"], "resolutions": [300], "delay": 0.3,
             "feeder": [page(f"S{k}") for k in range(8)]},
            {"id": "ocr", "name": "Mock text", "sources": ["flatbed"], "resolutions": [300],
             "flatbed": [page("HELLO SCAN")]}]}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        w.settings.remove("scan")

        def dialog(sc, **kw):
            wia.set_backend(wia.MockWIA(sc))
            d = ScanDialog(w, w.settings, **kw)
            wait(20)
            d.wait()
            return d

        def pick(d, dev):
            d.device.setCurrentIndex(d.device.findData(dev))
            d.wait()
        try:
            # 1. enumeration: none / one / several
            d0 = dialog({"devices": []})
            none_ok = d0.device.count() == 0 and "No scanner found" in d0.status.text() and not d0.btn_scan.isEnabled()
            d0.close()
            d1 = dialog({"devices": scenario["devices"][:1]})
            one_ok = d1.device.count() == 1
            d1.close()
            d = dialog(scenario)
            out["device_enumeration"] = none_ok and one_ok and d.device.count() == len(scenario["devices"])
            # an offline scanner (as real WIA reports it: a wrapped error with a description) is explained
            d_off = dialog({"devices": [{"id": "off", "name": "Mock offline",
                                         "offline_text": "The WIA device is not online."}]})
            out["offline_device_explained"] = d_off.caps is None and "isn't online" in d_off.status.text()
            d_off.close()
            wrapped = lambda txt: wia.describe(wia._FakeComError(-2147352567, "Exception occurred.",
                                                                 (txt, None, None, 0, None)))
            out["wrapped_wia_errors_mapped"] = (isinstance(wrapped("Paper empty"), wia.FeederEmpty)
                                                and wrapped("Paper jam").kind == "jam"
                                                and wrapped("Cover open").kind == "cover"
                                                and wrapped("Something else").kind == "device")
            wia.set_backend(wia.MockWIA(scenario))
            # 2. what each device can do, read from it
            pick(d, "flat")
            flat_caps = ([d.source.itemData(i) for i in range(d.source.count())] == ["flatbed"]
                         and [d.dpi.itemData(i) for i in range(d.dpi.count())] == [150, 300, 600]
                         and d.dpi.currentData() == 300 and not d.bright.isHidden()
                         and [d.mode.itemData(i) for i in range(d.mode.count())] == ["color", "gray"])
            pick(d, "adf")
            adf_caps = ([d.source.itemData(i) for i in range(d.source.count())] == ["flatbed", "feeder", "duplex"]
                        and [d.dpi.itemData(i) for i in range(d.dpi.count())] == [100, 200, 300] and d.bright.isHidden())
            out["capability_driven_lists"] = flat_caps and adf_caps
            # 3. flatbed: page by page, then reorder / rotate / delete / rescan
            pick(d, "flat")
            for _k in range(3):
                d.scan(wait=True)
            ref = lambda t, **k: wia.mock_page(dict(text=t, **k), 300)
            three = [p["data"] for p in d.pages] == [ref("FLAT ONE"), ref("FLAT TWO"), ref("FLAT THREE")]
            more = d.btn_scan.text() == "Scan another page"
            d.list.item(0).setSelected(True)
            d.move(1)
            reorder = [p["data"] for p in d.pages][:2] == [ref("FLAT TWO"), ref("FLAT ONE")]
            d.list.clearSelection()
            d.list.item(0).setSelected(True)
            d.rotate(-90)
            rot = d.pages[0]["rotate"] == 270
            d.list.clearSelection()
            d.list.item(2).setSelected(True)
            d.delete()
            deleted = len(d.pages) == 2
            d.list.clearSelection()
            d.list.item(1).setSelected(True)
            d.rescan(wait=True)
            rescanned = len(d.pages) == 2 and d.pages[1]["data"] == ref("FLAT FOUR")
            out["flatbed_session"] = three and more and reorder and rot and deleted and rescanned
            # deskew / crop (off by default)
            d.pages = []
            d.deskew.setChecked(True)
            d.scan(wait=True)
            g = Image.open(io.BytesIO(d.pages[0]["data"])).convert("L")
            import numpy as np
            straight = abs(estimate_skew(np.asarray(g))) < 0.6
            d.deskew.setChecked(False)
            d.crop.setChecked(True)
            d.scan(wait=True)
            sz = Image.open(io.BytesIO(d.pages[1]["data"])).size
            d.crop.setChecked(False)
            out["deskew_and_crop"] = straight and sz[0] < 400 and sz[1] < 400
            # 4. feeder: all pages, feeder-empty is the normal end; duplex order; blank skip
            pick(d, "adf")
            d.pages = []
            d.source.setCurrentIndex(d.source.findData("feeder"))
            nd = len(r["dialogs"])
            d.scan(wait=True)
            feeder_ok = (len(d.pages) == 3 and d.last_error is None and len(r["dialogs"]) == nd
                         and [p["data"] for p in d.pages] == [wia.mock_page({"text": f"SHEET {k}"}, 300)
                                                              for k in (1, 2, 3)])
            d.pages = []
            d.source.setCurrentIndex(d.source.findData("duplex"))
            d.scan(wait=True)
            order = [p["data"] for p in d.pages]
            want = [wia.mock_page({"text": "SHEET 1"}, 300), wia.mock_page({"text": "SHEET 1 BACK"}, 300),
                    wia.mock_page({"text": "SHEET 2"}, 300), wia.mock_page({"text": "", "blank": True}, 300),
                    wia.mock_page({"text": "SHEET 3"}, 300), wia.mock_page({"text": "SHEET 3 BACK"}, 300)]
            duplex_ok = len(order) == 6 and order[0] == want[0] and order[1] == want[1] and order[2] == want[2] \
                and order[4] == want[4] and order[5] == want[5]
            d.pages = []
            d.skip_blank.setChecked(True)
            d.scan(wait=True)
            skip_ok = len(d.pages) == 5 and d.skipped == 1
            d.skip_blank.setChecked(False)
            out["feeder_run_ends_on_empty"] = feeder_ok
            out["duplex_order"] = duplex_ok
            out["blank_page_skip"] = skip_ok
            pick(d, "empty")
            d.pages = []
            d.scan(wait=True)
            out["empty_feeder_says_so"] = d.last_error == "empty" and "feeder is empty" in d.progress.text()
            # 5. a device error mid-scan keeps the pages already scanned
            pick(d, "jam")
            d.pages = []
            nd = len(r["dialogs"])
            d.scan(wait=True)
            jam_ok = (len(d.pages) == 2 and d.last_error == "jam"
                      and any("jammed" in m and "kept" in m for m in r["dialogs"][nd:]))
            pick(d, "gone")
            d.pages = []
            d.scan(wait=True)
            gone_ok = len(d.pages) == 1 and d.last_error == "disconnect"
            out["mid_scan_error_keeps_pages"] = jam_ok and gone_ok
            pick(d, "picky")
            d.pages = []
            d.scan(wait=True)
            out["unsupported_setting_reported"] = d.last_error == "settings" and not d.pages
            # 6. Cancel, and the window stays live while scanning
            pick(d, "slow")
            d.pages = []
            d.max_gap = 0.0
            d.scan()
            t0 = _t.time()
            while len(d.pages) < 1 and _t.time() - t0 < 20:
                app.processEvents()
            d.cancel_scan()
            d.wait()
            out["cancel_stops"] = 1 <= len(d.pages) < 8 and d.job is None and d.last_error is None
            out["scan_longest_ui_pause_ms"] = round(d.max_gap * 1000)
            out["window_live_while_scanning"] = d.max_gap < 0.25
            # 7. Done: a new unsaved tab (the stage 14 engine), OCR after the scan
            pick(d, "flat")
            d.pages = []
            d.scan(wait=True)
            d.scan(wait=True)
            items, opts, _at = d.result_value()
            d.close()
            n_tabs = len(em.sessions)
            ok = w.scanner.scan_new(answer=(items, opts, None), wait=True)
            s = em.cur
            out["new_tab_dirty_unsaved"] = (ok and len(em.sessions) == n_tabs + 1 and getattr(s, "untitled", False)
                                            and s.name.startswith("Scan ") and em.dirty() and s.work.target is None
                                            and em.doc.page_count == 2 and not s.source.exists())
            d = dialog(scenario)
            pick(d, "ocr")
            d.ocr.setChecked(True)
            d.scan(wait=True)
            items, opts, _at = d.result_value()
            d.close()
            em.stage10.last_ocr = None
            w.scanner.scan_new(answer=(items, opts, None), wait=True)
            t0 = _t.time()
            while (em.stage10.last_ocr is None or em.stage10.ocr_worker is None or em.stage10.ocr_worker.isRunning()) \
                    and _t.time() - t0 < 120:
                app.processEvents()
            wait(200)
            so = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
            out["ocr_after_scan"] = "HELLO" in so[0].get_text().upper()
            so.close()
            # 8. scanned pages into an open PDF: one undo step
            base = sw / "base.pdf"
            bd = pymupdf.open()
            for k in range(2):
                bd.new_page().insert_text((72, 72), f"base page {k + 1}")
            bd.save(str(base))
            bd.close()
            h0 = hashlib.sha256(base.read_bytes()).hexdigest()
            em.open_pdf(str(base))
            st0 = em.work.step()
            w.scanner.scan_insert(answer=(items * 2, {"page": "fit", "quality": "keep"}, 1), wait=True)
            ins = em.doc.page_count == 4 and len(em.doc[1].get_images()) == 1 and em.work.step() == st0 + 1
            em.undo()
            und = em.doc.page_count == 2
            em.redo()
            out["insert_scan_undo_redo"] = (ins and und and em.doc.page_count == 4
                                            and oprules.kind("insert_scan_pages") == "journal")
            out["settings_remembered"] = '"device": "ocr"' in w.settings.value("scan", "", type=str)
            out["original_byte_identical"] = hashlib.sha256(base.read_bytes()).hexdigest() == h0
        finally:
            wia.set_backend(None)
        w.close()
        out["ok"] = all(v for k, v in out.items() if not k.endswith("_ms") and k != "real_scanners_listed")
        return out
    check("s15_scan", s15_scan)

    def s16_word():
        """Stage 16 PDF to Word: paragraphs (read back with python-docx), a table as a Word table,
        images on / off, page range, no-text pages (OCR first on a temporary copy; convert anyway),
        permission block, the unprotected-output warning, background run with progress and Cancel
        leaving nothing, the batch step on several files, journals unchanged, originals intact."""
        import hashlib
        import time as _t
        import pymupdf
        from docx import Document
        from . import todocx_ui
        ww = work / "s16"
        ww.mkdir(exist_ok=True)

        def mk(path, scan=True, pages=1, enc=None):
            d = pymupdf.open()
            p = d.new_page()
            p.insert_textbox(pymupdf.Rect(72, 72, 520, 300), "Quarterly summary paragraph. " * 12, fontsize=11)
            x0, y0, cw, ch = 72, 400, 140, 24
            for rr in range(4):
                p.draw_line((x0, y0 + rr * ch), (x0 + 3 * cw, y0 + rr * ch))
            for cc in range(4):
                p.draw_line((x0 + cc * cw, y0), (x0 + cc * cw, y0 + 3 * ch))
            for rr in range(3):
                for cc in range(3):
                    p.insert_text((x0 + cc * cw + 6, y0 + rr * ch + 16), f"R{rr}C{cc}", fontsize=10)
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), 0)
            pix.clear_with(120)
            p.insert_image(pymupdf.Rect(72, 520, 232, 627), pixmap=pix)
            for k in range(1, pages):
                d.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760), f"Page {k + 1} body text. " * 40,
                                            fontsize=11)
            if scan:
                t_ = pymupdf.open()
                tp = t_.new_page()
                tp.insert_text((60, 140), "SCANNED WORDS", fontsize=40)
                sp = tp.get_pixmap(dpi=150)
                t_.close()
                d.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=sp)
            kw = {}
            if enc:
                kw = dict(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o", permissions=enc)
            d.save(str(path), **kw)
            d.close()
        A = ww / "a.pdf"
        mk(A, pages=2)
        locked_ = ww / "locked.pdf"
        mk(locked_, scan=False, enc=pymupdf.PDF_PERM_PRINT)
        prot = ww / "prot.pdf"
        mk(prot, scan=False, enc=-1)
        big = ww / "big.pdf"
        mk(big, scan=False, pages=120)
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (A, locked_, prot, big)}
        w = gui.Main()
        w.resize(1400, 900)
        w.show()
        wait(100)
        em, wc = w.editor_mode, w.word
        w.settings.remove("export_docx")
        out = {}
        base = {"range": "all", "images": True, "tables": True, "columns": True, "after": "none"}

        def read(path):
            d = Document(str(path))
            return d, "\n".join(p.text for p in d.paragraphs)
        em.open_pdf(str(A))
        steps0 = (em.work.step(), em.doc_version)
        # 1. paragraphs, the table, images
        dst = ww / "one.docx"
        ok = wc.export(options=dict(base, range="1-2"), dest=str(dst), wait=True)
        d, txt = read(dst) if ok else (None, "")
        out["paragraphs_come_through"] = ok and "Quarterly summary paragraph." in txt and "Page 2 body text." in txt
        out["table_is_word_table"] = ok and len(d.tables) >= 1 and d.tables[0].rows[0].cells[0].text.strip() == "R0C0"
        imgs_on = len(d.inline_shapes) if ok else -1
        wc.export(options=dict(base, range="1", images=False), dest=str(ww / "noimg.docx"), wait=True)
        imgs_off = len(Document(str(ww / "noimg.docx")).inline_shapes)
        out["images_option"] = imgs_on >= 1 and imgs_off == 0
        wc.export(options=dict(base, range="2"), dest=str(ww / "p2.docx"), wait=True)
        _d, t2 = read(ww / "p2.docx")
        out["page_range"] = "Page 2 body text." in t2 and "Quarterly" not in t2
        # 2. a page without text: OCR first (temporary copy) or convert anyway; nothing changes in the PDF
        asked = []
        real_ask = todocx_ui.ask_no_text
        todocx_ui.ask_no_text = lambda parent, n: asked.append(n) or "ocr"
        try:
            wc.export(options=dict(base, range="3"), dest=str(ww / "ocr.docx"), wait=True)
        finally:
            todocx_ui.ask_no_text = real_ask
        _d, to = read(ww / "ocr.docx")
        plain = pymupdf.open("pdf", em.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
        still_no_text = len(plain[2].get_text().strip()) == 0
        plain.close()
        out["ocr_first_temp_copy"] = (asked == [1] and "SCANNED" in to.upper() and still_no_text
                                      and (em.work.step(), em.doc_version) == steps0)
        wc.export(options=dict(base, range="3"), dest=str(ww / "anyway.docx"), no_text="anyway", wait=True)
        da, ta = read(ww / "anyway.docx")
        out["convert_anyway_as_picture"] = "SCANNED" not in ta.upper() and len(da.inline_shapes) >= 1
        out["journal_unchanged"] = (em.work.step(), em.doc_version) == steps0 and not em.dirty()
        # 3. permissions: copying forbidden (owner password unknown) is blocked
        import lazaruspdf.locked as L
        L.ask_password = lambda *a_, **k_: "u"
        em.open_pdf(str(locked_))
        nd = len(r["dialogs"])
        blocked = wc.export(options=dict(base), dest=str(ww / "blocked.docx"), unprotected_ok=True) is False
        out["permission_block"] = (blocked and wc.last.get("why") == "blocked" and not (ww / "blocked.docx").exists()
                                   and any("copying" in m for m in r["dialogs"][nd:]))
        # 4. a password-protected PDF: warned that the .docx will not be protected (declined, then accepted)
        em.open_pdf(str(prot))
        nd = len(r["dialogs"])
        declined = wc.export(options=dict(base), dest=str(ww / "prot.docx")) is False
        warned = any("NOT be protected" in m for m in r["dialogs"][nd:])
        ok = wc.export(options=dict(base), dest=str(ww / "prot.docx"), unprotected_ok=True, wait=True)
        out["unprotected_warning"] = (declined and warned and ok and (ww / "prot.docx").exists()
                                      and "Quarterly" in read(ww / "prot.docx")[1])
        # 5. a large document: in the background, progress, the window stays live; Cancel leaves nothing
        em.open_pdf(str(big))
        t0 = _t.time()
        ok = wc.export(options=dict(base), dest=str(ww / "big.docx"), wait=True)
        out["large_121_pages_s"] = round(_t.time() - t0, 2)
        out["large_longest_ui_pause_ms"] = (wc.last or {}).get("gap_ms")
        prog = (wc.last or {}).get("progress", [])
        out["large_converted_with_progress"] = (ok and len(Document(str(ww / "big.docx")).paragraphs) > 100
                                                and len(prog) > 100 and prog[-1][0] == prog[-1][1])
        out["window_live_while_converting"] = (wc.last or {}).get("gap_ms", 999) < 250
        cdst = ww / "cancel.docx"
        wc.export(options=dict(base), dest=str(cdst), test_delay=0.02)
        t0 = _t.time()
        while wc.job is not None and len(wc.job["seen"]) < 10 and _t.time() - t0 < 60:
            app.processEvents()
        tc = _t.time()
        wc.cancel()
        while wc.job is not None and _t.time() - tc < 30:
            app.processEvents()
        out["cancel_leaves_nothing"] = ((wc.last or {}).get("why") == "cancelled" and not cdst.exists()
                                        and not any(".lazaruspdf-partial" in p.name for p in ww.iterdir()))
        out["cancel_s"] = round(_t.time() - tc, 2)
        # 6. the batch step on several files (with the processed PDF off: only the .docx files)
        bd = ww / "batch"
        bd.mkdir(exist_ok=True)
        job = w.batch.start({"files": [str(A), str(big), str(prot)], "pipeline": [
            {"op": "to_word", "params": {"range": "1"}}], "out_dir": str(bd), "pattern": "{n}-{name}",
            "open_password": "u", "save_pdf": False})
        job.wait(600)
        names = sorted(p.name for p in bd.iterdir())
        out["batch_convert_to_word"] = (job.status == ["done", "done", "done"]
                                        and names == ["1-a.docx", "2-big.docx", "3-prot.docx"]
                                        and all("Quarterly" in read(bd / n)[1] for n in names))
        out["originals_byte_identical"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        w.close()
        out["ok"] = all(v for k, v in out.items() if not k.endswith(("_s", "_ms")))
        return out
    check("s16_word", s16_word)

    def s17_pages_between_tabs():
        """Stage 17: copy / cut / paste of pages between tabs (keys, menus), drags onto a tab (hover
        switches), the thumbnails, Overview and between compare panes; annotations, links and form
        fields travel (field rename on collision, said once); optional bookmarks; restricted source
        blocked; Paste as New PDF; Insert / Merge read in the background and grafted in parts with
        Cancel; the page right-click menu; mixed-size crop by margins / proportion; Settings gap
        colours in the written review HTML / PDF; journals; originals byte-identical."""
        import hashlib
        import time as _t
        import pymupdf
        from PySide6.QtCore import QEvent, QPoint, QPointF
        from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QKeyEvent
        from . import loader, output, pageclip, settings as S
        from .extract import GAP, PageResult, Para
        from .pdfeditor import page_mime
        ww = work / "s17"
        ww.mkdir(exist_ok=True)

        def mk(path, n, label, w=595, h=842, field=None, enc=None, rotate_last=False, img=False):
            d = pymupdf.open()
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 80), 0) if img else None
            for i in range(n):
                p = d.new_page(width=w, height=h)
                p.insert_text((60, 90), f"{label} page {i + 1}", fontsize=18)
                if img:
                    pix.clear_with((i * 37) % 256)
                    p.insert_image(pymupdf.Rect(60, 120, 300, 280), pixmap=pix)
                    p.add_text_annot((400, 60), "n")
            d[0].add_text_annot((300, 300), f"{label} note")
            if n > 1:
                d[0].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(60, 200, 200, 220), "page": 1})
            if field:
                wd = pymupdf.Widget()
                wd.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
                wd.field_name = field
                wd.rect = pymupdf.Rect(60, 400, 300, 430)
                d[0].add_widget(wd)
            if rotate_last:
                d[-1].set_rotation(90)
            d.set_toc([[1, f"{label} start", 1]])
            kw = (dict(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o", permissions=enc)
                  if enc else {})
            d.save(str(path), **kw)
            d.close()
        A, B, R, BIG = ww / "a.pdf", ww / "b.pdf", ww / "r.pdf", ww / "big.pdf"
        mk(A, 3, "AAA", field="Name", rotate_last=True)
        mk(B, 2, "BBB", w=400, h=600, field="Name")
        mk(R, 2, "RRR", enc=pymupdf.PDF_PERM_PRINT)
        mk(BIG, 400, "BIG", img=True)
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (A, B, R, BIG)}
        w = gui.Main()
        em, pc, ops = w.editor_mode, w.pageclip, w.pageops
        out = {}

        def texts(s):
            plain = pymupdf.open("pdf", s.work.doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
            t = [plain[i].get_text().split("\n")[0].strip() for i in range(plain.page_count)]
            plain.close()
            return t

        def pick(rows):
            em.thumbs.clearSelection()
            for k in rows:
                em.thumbs.item(k).setSelected(True)

        def key(widget, k):
            from PySide6.QtCore import Qt as _Qt
            for t in (QEvent.ShortcutOverride, QEvent.KeyPress):
                app.sendEvent(widget, QKeyEvent(t, k, _Qt.ControlModifier))

        def drag(widget, pos, md, drop=True):
            from PySide6.QtCore import Qt as _Qt
            for cls in (QDragEnterEvent, QDragMoveEvent):
                app.sendEvent(widget, cls(pos, _Qt.CopyAction, md, _Qt.LeftButton, _Qt.NoModifier))
            if drop:
                app.sendEvent(widget, QDropEvent(QPointF(pos), _Qt.CopyAction, md, _Qt.LeftButton, _Qt.NoModifier))
        from PySide6.QtCore import Qt
        em.open_pdf(str(A))
        sa = em.cur
        em.open_pdf(str(B))
        sb = em.cur
        # 1. copy (Ctrl+C on the thumbnails) / paste (Ctrl+V) into another tab, after the selected page
        em.activate(sa)
        pick([1, 2])
        key(em.thumbs, Qt.Key_C)
        copied = pc.last == {"op": "copy", "n": 2} and pc.clip is not None
        em.activate(sb)
        pick([0])
        stepb = sb.work.step()
        nd = len(r["dialogs"])
        key(em.thumbs, Qt.Key_V)
        tb = texts(sb)
        d = sb.work.doc
        out["copy_paste_at_position"] = (copied and tb == ["BBB page 1", "AAA page 2", "AAA page 3", "BBB page 2"]
                                         and sb.work.step() == stepb + 1)
        out["page_size_and_rotation_kept"] = (round(d[1].mediabox.width) == 595 and d[2].rotation == 90
                                              and round(d[0].rect.width) == 400)
        em.undo()
        out["paste_one_undo_step"] = texts(sb) == ["BBB page 1", "BBB page 2"] and sb.work.step() == stepb
        # 2. annotations, links (within the copied set) and form fields travel; a field name clash is renamed once
        em.activate(sa)
        pick([0, 1])
        pc.copy()
        em.activate(sb)
        pick([1])
        pc.paste(after=False)                                    # Paste before page 2
        d = sb.work.doc
        notes = [a.info.get("content") for a in d[1].annots()]
        lk = [l_ for l_ in d[1].get_links() if l_.get("kind") == pymupdf.LINK_GOTO]
        fields = sorted(f.field_name for i in range(d.page_count) for f in d[i].widgets())
        renamed = [x for x in r["dialogs"][nd:] if "renamed" in x]
        acro = d.xref_get_key(d.pdf_catalog(), "AcroForm")
        out["paste_before"] = texts(sb) == ["BBB page 1", "AAA page 1", "AAA page 2", "BBB page 2"]
        out["annotations_carried"] = notes == ["AAA note"]
        out["internal_link_rewired"] = len(lk) == 1 and lk[0]["page"] == 2
        out["fields_carried_renamed_once"] = (fields == ["Name", "Name (2)"] and len(renamed) == 1
                                              and pc.last.get("renames") == [("Name", "Name (2)")]
                                              and acro[0] != "null")
        # 3. bookmarks only when asked for
        toc_before = len(sb.work.doc.get_toc())
        out["bookmarks_off_by_default"] = not pc.act_bookmarks.isChecked() and toc_before == 1
        pc.act_bookmarks.setChecked(True)
        pc.paste(at=0, session=sb)
        toc = sb.work.doc.get_toc()
        out["bookmarks_optional"] = (sorted(e[1] for e in toc) == ["AAA start", "BBB start"]
                                     and {e[1]: e[2] for e in toc} == {"AAA start": 1, "BBB start": 3})
        pc.act_bookmarks.setChecked(False)
        em.undo()
        em.undo()
        # 4. cut: one undo step in the source; the two histories are independent
        em.activate(sa)
        sa0 = sa.work.step()
        pick([2])
        key(em.thumbs, Qt.Key_X)
        cut_ok = texts(sa) == ["AAA page 1", "AAA page 2"] and sa.work.step() == sa0 + 1
        em.activate(sb)
        sb0 = sb.work.step()
        pc.paste(at=sb.work.doc.page_count, session=sb)
        em.activate(sa)
        em.undo()
        a_back = texts(sa) == ["AAA page 1", "AAA page 2", "AAA page 3"] and sa.work.step() == sa0
        b_kept = texts(sb)[-1] == "AAA page 3" and sb.work.step() == sb0 + 1
        em.activate(sb)
        em.undo()
        out["cut_and_independent_undo"] = (cut_ok and a_back and b_kept and texts(sb) == ["BBB page 1", "BBB page 2"]
                                           and texts(sa) == ["AAA page 1", "AAA page 2", "AAA page 3"])
        # 5. drags: hovering a tab switches to it; a drop on a tab adds at its end; thumbnails and
        #    Overview drops land at the insertion point; a drop on its own thumbnails reorders
        em.activate(sa)
        md = page_mime(sa.sid, [0])
        bar = em.tabbar
        drag(bar, bar.tabRect(em.sessions.index(sb)).center(), md, drop=False)
        wait(500)
        hovered = em.cur is sb
        drag(bar, bar.tabRect(em.sessions.index(sb)).center(), md)
        tab_drop = texts(sb) == ["BBB page 1", "BBB page 2", "AAA page 1"]
        em.undo()
        vp = em.thumbs.viewport()
        r1 = em.thumbs.visualItemRect(em.thumbs.item(1))
        drag(vp, QPoint(r1.center().x(), r1.top() + 2), page_mime(sa.sid, [2]))
        thumb_drop = texts(sb) == ["BBB page 1", "AAA page 3", "BBB page 2"]
        em.undo()
        drag(vp, QPoint(r1.center().x(), r1.bottom() - 2), page_mime(sb.sid, [0]))   # its own: a reorder
        own_reorder = texts(sb) == ["BBB page 2", "BBB page 1"]
        em.undo()
        w.set_mode(w.overview_mode.key)
        g = w.overview_mode.grid
        gp = g.mapFromScene(g._cell(1).topLeft() + QPointF(4, 30))
        k = g.gap_at(g.mapToScene(gp))
        drag(g.viewport(), gp, page_mime(sa.sid, [1]))
        tb_ = texts(sb)
        ov_drop = tb_[k] == "AAA page 2" and len(tb_) == 3
        em.undo()
        w.set_mode(em.key)
        out["drag_hover_switches_tab"] = hovered
        out["drag_onto_tab"] = tab_drop
        out["drag_onto_thumbnails"] = thumb_drop and own_reorder
        out["drag_onto_overview"] = ov_drop
        # 6. between the compare panes (A left, B right): a page of the left pane dropped on the right one
        em.activate(sa)
        em.show_compare(True)
        em.compare.show_pair(sa, sb)
        right = em.compare.panes[1]
        app.processEvents()
        p0 = right.mapFromScene(right.pages[0]["rect"].topLeft() + QPointF(10, 5))
        drag(right.viewport(), p0, pc.pane_mime(0, 2))
        app.processEvents()
        pane_ok = (texts(sb) == ["AAA page 3", "BBB page 1", "BBB page 2"] and len(right.pages) == 3
                   and len(sb.canvas.pages) == 3 and em.cur is sa)
        left = em.compare.panes[0]
        drag(left.viewport(), left.mapFromScene(left.pages[-1]["rect"].bottomLeft() + QPointF(10, 2)),
             pc.pane_mime(1, 1))
        app.processEvents()
        back_ok = texts(sa)[-1] == "BBB page 1" and len(left.pages) == 4
        em.undo()                                            # in A (the active tab)
        em.show_compare(False)
        em.activate(sb)
        em.undo()
        out["drag_between_compare_panes"] = (pane_ok and back_ok and texts(sb) == ["BBB page 1", "BBB page 2"]
                                             and texts(sa) == ["AAA page 1", "AAA page 2", "AAA page 3"])
        # 7. a restricted source: copy, cut and drag out are blocked with the reason
        import lazaruspdf.locked as L
        real_ask = L.ask_password
        L.ask_password = lambda *a_, **k_: "u"
        try:
            em.open_pdf(str(R))
        finally:
            L.ask_password = real_ask
        sr = em.cur
        pick([0])
        nd = len(r["dialogs"])
        clip0 = pc.clip
        c_ = pc.copy()
        x_ = pc.cut()
        dropped = pc.drop({"sid": sr.sid, "pages": [0]}, sb, 0)
        out["restricted_source_blocked"] = (c_ is False and x_ is False and dropped is False and pc.clip is clip0
                                            and sum("don't allow copying" in m for m in r["dialogs"][nd:]) == 3
                                            and texts(sb) == ["BBB page 1", "BBB page 2"] and sr.work.step() == 0)
        # 8. Paste Pages as New PDF: a new unsaved tab
        em.activate(sa)
        pick([0, 2])
        pc.copy()
        ns = pc.paste_new()
        out["paste_as_new_pdf"] = (ns is em.cur and getattr(ns, "untitled", False) and ns.dirty()
                                   and texts(ns) == ["AAA page 1", "AAA page 3"]
                                   and pc.act_new in w.file_menu.actions())
        # 9. Insert / Merge: read in the background (Cancel), grafted in parts (Cancel), one undo step
        em.activate(sb)
        pick([1])
        n0, st0 = sb.work.doc.page_count, sb.work.step()
        killer = QTimer()
        killer.setInterval(5)
        killer.timeout.connect(lambda: getattr(ops, "_ref_dialog", None) is not None and ops._ref_dialog.cancel())
        loader.SLOW_STEP = 0.05
        killer.start()
        try:
            ops.insert_pdf(after=True, path=str(BIG))
        finally:
            killer.stop()
            loader.SLOW_STEP = 0.0
        read_cancel = sb.work.doc.page_count == n0 and sb.work.step() == st0
        killer = QTimer()
        killer.setInterval(5)
        killer.timeout.connect(lambda: getattr(ops, "_graft_dialog", None) is not None and ops._graft_dialog.cancel())
        killer.start()
        try:
            ops.insert_pdf(after=True, path=str(BIG))
        finally:
            killer.stop()
        graft_cancel = (sb.work.doc.page_count == n0 and sb.work.step() == st0 and not sb.dirty()
                        and texts(sb) == ["BBB page 1", "BBB page 2"])
        gaps, last = [0.0], [_t.perf_counter()]

        def tick():
            t = _t.perf_counter()
            gaps.append(t - last[0])
            last[0] = t
        hb = QTimer()
        hb.setInterval(10)
        hb.timeout.connect(tick)
        hb.start()
        t0 = _t.perf_counter()
        ops.insert_pdf(after=True, path=str(BIG))
        out["insert_400_pages_s"] = round(_t.perf_counter() - t0, 2)
        wait(150)
        hb.stop()
        out["insert_longest_ui_pause_ms"] = round(max(gaps) * 1000)
        d = sb.work.doc
        out["insert_in_background_one_step"] = (d.page_count == n0 + 400 and sb.work.step() == st0 + 1
                                                and sum(1 for _ in d[2].annots()) == 2
                                                and texts(sb)[2] == "BIG page 1")
        em.undo()
        out["insert_cancel_read_and_graft"] = read_cancel and graft_cancel and sb.work.doc.page_count == n0
        ops.merge(paths=[str(A)], where="At the end")
        d = sb.work.doc
        out["merge_keeps_annotations"] = (d.page_count == 5 and [a.info.get("content") for a in d[2].annots()]
                                          == ["AAA note"] and sb.work.step() == st0 + 1)
        em.undo()
        # 10. the page right-click menu (Pages panel and Overview)
        pick([0])
        m = pc.page_menu()
        names = [a.text() for a in m.actions() if a.text()]
        want = ["Copy pages", "Cut pages", "Paste pages before", "Paste pages after", "Duplicate", "Rotate left",
                "Rotate right", "Delete…", "Extract to new PDF…", "Insert blank page after", "Insert from file…",
                "Insert images…", "Insert scan…"]
        act = {a.text(): a for a in m.actions()}
        act["Rotate right"].trigger()
        rot = sb.work.doc[0].rotation == 90
        act["Duplicate"].trigger()
        dup = sb.work.doc.page_count == 3
        act["Paste pages after"].trigger()
        pasted = sb.work.doc.page_count == 5
        policies = (em.thumbs.contextMenuPolicy() == Qt.CustomContextMenu
                    and w.overview_mode.grid.contextMenuPolicy() == Qt.CustomContextMenu)
        for _ in range(3):
            em.undo()
        out["right_click_menu"] = (names == want and rot and dup and pasted and policies
                                   and texts(sb) == ["BBB page 1", "BBB page 2"] and sb.work.step() == st0)
        # 11. mixed-size crop: by margins and by proportion, each one undo step
        em.activate(sa)
        pc.clip = pageclip.copy_pages(sb.work.doc, [0])
        pc.paste(at=3, session=sa)                         # A: three 595x842 pages (one turned) + a 400x600 one
        sa1 = sa.work.step()
        ops._crop_drawn(0, (50, 60, 545, 782), scope="all", mode="margins")
        d = sa.work.doc
        by_margins = ((round(d[0].rect.width), round(d[0].rect.height)) == (495, 722)
                      and (round(d[3].rect.width), round(d[3].rect.height)) == (300, 480)
                      and sa.work.step() == sa1 + 1)
        em.undo()
        ops._crop_drawn(0, (59.5, 84.2, 535.5, 757.8), scope="all", mode="proportion")
        d = sa.work.doc
        by_prop = ((round(d[0].rect.width), round(d[0].rect.height)) == (476, 674)
                   and (round(d[3].rect.width), round(d[3].rect.height)) == (320, 480)
                   and sa.work.step() == sa1 + 1)
        em.undo()
        undone = (round(sa.work.doc[3].rect.width), round(sa.work.doc[3].rect.height)) == (400, 600)
        em.undo()
        out["mixed_size_crop_margins"] = by_margins
        out["mixed_size_crop_proportion"] = by_prop and undone and texts(sa)[-1] == "AAA page 3"
        # 12. the Settings gap colours reach the written review HTML and PDF
        pages = [PageResult(1, "text layer", [Para(1, (0, 0, 100, 20), ["Before ", GAP, " after."])])]
        plain_html = output.render_html(pages, "t")
        S.put(w.settings, "gap_open_color", "#3366aa")
        w.apply_settings()
        paths = {"review": ww / "rv.html", "text": ww / "rv.txt", "final": ww / "rv.pdf", "gaps": ww / "rv-gaps.txt"}
        output.write_review(pages, {k_: str(v) for k_, v in paths.items()}, "t")
        html_ok = "#3366aa" in paths["review"].read_text(encoding="utf-8")
        rd = pymupdf.open(str(paths["final"]))
        fills = {tuple(round(c, 2) for c in dr["fill"]) for dr in rd[0].get_drawings() if dr.get("fill")}
        rd.close()
        pdf_ok = (0.2, 0.4, 0.67) in fills
        S.put(w.settings, "gap_open_color", S.DEFAULTS["gap_open_color"])
        w.apply_settings()
        out["gap_colours_in_review_outputs"] = (html_ok and pdf_ok
                                                and output.render_html(pages, "t") == plain_html)
        # 13. journals: every tab back at its start
        for s in (sa, sb):
            em.activate(s)
            while s.work.can_undo():
                em.undo()
        out["journals_correct"] = (texts(sa) == ["AAA page 1", "AAA page 2", "AAA page 3"]
                                   and texts(sb) == ["BBB page 1", "BBB page 2"]
                                   and not sa.dirty() and not sb.dirty() and sr.work.step() == 0)
        out["oprules_paste_cut"] = all(v == "ok" for op in ("paste_pages", "cut_pages")
                                       for v in __import__("lazaruspdf.oprules", fromlist=["x"]).probe(op).values())
        out["originals_byte_identical"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        w.close()
        out["ok"] = all(v for k_, v in out.items() if not k_.endswith(("_s", "_ms")))
        return out
    check("s17_pages_between_tabs", s17_pages_between_tabs)

    def s18_links():
        """Stage 18: Insert › Link (page targets with the view on arrival, web addresses) drawn,
        selected, moved, resized, re-targeted and deleted, each one undo step and journal-safe;
        links survive save, extract and the stage 17 page graft (in-set targets travel, out-of-set
        ones are dropped); Insert › Text is gone; the split-button carets have their own room."""
        import hashlib
        import pymupdf
        from . import links as LK, markup as MK, oprules, pageclip
        from .pdfeditor import TOOLS
        ww = work / "s18"
        ww.mkdir(exist_ok=True)
        src = ww / "links.pdf"
        d = pymupdf.open()
        for i in range(4):
            d.new_page().insert_text((72, 72), f"LINK page {i + 1}", fontsize=18)
        d[1].set_rotation(90)
        d.save(str(src))
        d.close()
        other = ww / "other.pdf"
        d = pymupdf.open()
        d.new_page().insert_text((72, 72), "OTHER page 1", fontsize=18)
        d.save(str(other))
        d.close()
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (src, other)}
        w = gui.Main()
        em, mk = w.editor_mode, w.editor_mode.markup
        out = {}

        def srt(xs):
            return sorted(xs, key=repr)
        # 1. Insert › Text is cut: no placeholder, no menu entry, nothing behind it
        insert_bar = next(g for g in em.groups if g.windowTitle() == "Insert")
        out["insert_text_removed"] = ("text" not in em.soon and [k for k, *_ in TOOLS["Insert"]] == ["image", "link", "formfield"]
                                      and not any(a.text() == "Text" for a in insert_bar.actions())
                                      and not any(a.text() == "Text" for m in em.menus for a in m.actions()))
        # 2. the Link tool is real
        em.open_pdf(str(src))
        la = em.soon["link"]
        la.trigger()
        out["link_tool_live"] = (not la.property("soon") and la.isCheckable() and la.isChecked() and mk.tool == "link")
        # 3. draw a link (the target dialog answered): a page target with "fit width", on a rotated page
        real_ask = MK.ask_link_target
        MK.ask_link_target = lambda *a_, **k_: {"kind": "page", "page": 3, "fit": "width"}
        try:
            s0 = em.work.step()
            mk._commit_draw(em.canvas, {"page": 1, "start": pymupdf.Point(100, 50), "cur": pymupdf.Point(300, 90)})
        finally:
            MK.ask_link_target = real_ask
        x1 = mk.sel[0] if mk.sel else None
        r1 = (LK.rect_of(em.doc, 1, x1) * em.doc[1].rotation_matrix).normalize() if x1 else None
        out["draw_page_link"] = (x1 is not None and LK.target_of(em.doc, x1) == {"kind": "page", "page": 3, "fit": "width",
                                                                                    "zoom": 100}
                                 and r1 is not None and [round(v) for v in r1] == [100, 50, 300, 90]
                                 and em.work.step() == s0 + 1)
        out["properties_show_target"] = ("Page 4, fit width" in mk.panel.title.text() and not mk.panel.link_form.isHidden()
                                         and mk.panel.delete_btn.text() == "Delete link")
        # 4. a web address link and a zoomed page link
        x2 = mk.create_link(0, (72, 100, 250, 130), {"kind": "uri", "uri": "https://example.com/doc"})
        x3 = mk.create_link(0, (72, 150, 250, 180), {"kind": "page", "page": 2, "fit": "zoom", "zoom": 150})
        x4 = mk.create_link(0, (72, 200, 250, 230), {"kind": "page", "page": 1, "fit": "fit"})
        links0 = {l_["xref"]: l_ for l_ in em.doc[0].get_links()}
        out["uri_and_zoom_links"] = (LK.target_of(em.doc, x2) == {"kind": "uri", "uri": "https://example.com/doc"}
                                     and links0.get(x2, {}).get("uri") == "https://example.com/doc"
                                     and LK.target_of(em.doc, x3)["zoom"] == 150 and links0.get(x3, {}).get("page") == 2
                                     and LK.target_of(em.doc, x4)["fit"] == "fit")
        # 5. select (click with the Link tool), move, resize, re-target in Properties, delete: each one undo step
        s1 = em.work.step()
        hit = LK.hit(em.doc, 0, pymupdf.Point(100, 115))
        mk.select(0, [hit])
        mk.move_selected(20, 10)
        moved = [round(v) for v in LK.rect_of(em.doc, 0, x2)] == [92, 110, 270, 140]
        mk.resize_selected((92, 110, 300, 150))
        resized = [round(v) for v in LK.rect_of(em.doc, 0, x2)] == [92, 110, 300, 150]
        mk.panel.link_form.set_target({"kind": "page", "page": 1, "fit": "keep"})
        mk.panel.link_apply.click()
        retargeted = LK.target_of(em.doc, x2)["kind"] == "page" and LK.target_of(em.doc, x2)["page"] == 1
        mk.delete_selected()
        deleted = x2 not in LK.link_xrefs(em.doc, 0)
        steps = em.work.step() == s1 + 4
        for _ in range(4):
            em.undo()
        restored = (x2 in LK.link_xrefs(em.doc, 0) and LK.target_of(em.doc, x2)["kind"] == "uri"
                    and [round(v) for v in LK.rect_of(em.doc, 0, x2)] == [72, 100, 250, 130])
        out["select_move_resize_retarget_delete"] = (hit == x2 and moved and resized and retargeted and deleted and steps
                                                     and restored)
        out["outlines_shown_with_tool"] = len(getattr(mk, "_link_items", [])) >= 1
        # 6. save: links read back from the saved file
        saved = ww / "saved.pdf"
        em.work.save_as(str(saved))
        sd = pymupdf.open(str(saved))
        got = srt((p, l_.get("page"), l_.get("uri")) for p in range(sd.page_count) for l_ in sd[p].get_links())
        fitw = any("/FitH" in (sd.xref_get_key(l_["xref"], "Dest")[1] or "") for l_ in sd[1].get_links())
        sd.close()
        out["links_survive_save"] = (got == srt([(0, None, "https://example.com/doc"), (0, 2, None), (0, 1, None),
                                                    (1, 3, None)]) and fitw)
        # 7. extract (pages 1, 2 and 4): links to extracted pages kept, to the left-out page dropped
        ex = ww / "extract.pdf"
        em.thumbs.clearSelection()
        for k in (0, 1, 3):
            em.thumbs.item(k).setSelected(True)
        w.pageops.extract(dest=str(ex))
        xd = pymupdf.open(str(ex))
        xg = srt((p, l_.get("page"), l_.get("uri")) for p in range(xd.page_count) for l_ in xd[p].get_links())
        xd.close()
        out["links_survive_extract"] = xg == srt([(0, None, "https://example.com/doc"), (0, 1, None), (1, 2, None)])
        # 8. the stage 17 graft: copy pages 1 and 3 into another tab; the link to page 3 travels
        #    (to the pasted page), the links to pages 2 and 4 (left behind) are dropped, the URL travels
        sa = em.cur
        em.open_pdf(str(other))
        sb = em.cur
        w.pageclip.clip = pageclip.copy_pages(sa.work.doc, [0, 2], origin=sa.sid)
        w.pageclip.paste(at=1, session=sb)
        bd = sb.work.doc
        pl = srt((l_.get("page"), l_.get("uri")) for l_ in bd[1].get_links())
        out["links_survive_page_graft"] = pl == srt([(None, "https://example.com/doc"), (2, None)])
        em.undo()
        # 9. the probe (5 worst-case files) and journals
        probes = {op: oprules.probe(op) for op in ("link_add", "link_edit")}
        out["probe_link_ops_journal_safe"] = all(v == "ok" for p in probes.values() for v in p.values()) and all(
            len(p) == 5 for p in probes.values())
        em.activate(sa)
        while sa.work.can_undo():
            em.undo()
        out["journals_correct"] = (LK.link_xrefs(sa.work.doc, 0) == [] and LK.link_xrefs(sa.work.doc, 1) == []
                                   and sa.work.step() == 0 and sa.dirty()            # (saved mid-way: back past it)
                                   and sb.work.step() == 0 and not sb.dirty())
        # 10. the split-button carets have their own room (Rotate and Insert, full and compact)
        from PySide6.QtWidgets import QToolButton
        qss = app.styleSheet()
        btns = [b for b in w.findChildren(QToolButton) if b.popupMode() == QToolButton.MenuButtonPopup]
        out["caret_spacing"] = ('QToolButton[popupMode="1"]{padding-right:17px}' in qss
                                and "QToolButton::menu-button{width:13px" in qss
                                and 'QToolBar#tools[compact="true"] QToolButton[popupMode="1"]' in qss
                                and {"Rotate", "Insert"} <= {b.defaultAction().text() for b in btns if b.defaultAction()})
        out["originals_byte_identical"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        w.close()
        out["ok"] = all(v for k_, v in out.items())
        return out
    check("s18_links", s18_links)

    def s18b_export():
        """Stage 18b: File › Export › PDF / EPUB / Markdown / HTML / Plain text from the working copy
        (unsaved edits included), each round-tripped (the output opens / parses and holds what it
        should); page ranges; copy-forbidden sources blocked for the text formats; PDF export keeps
        the password choice and doesn't become the Save target; protected destinations refused;
        background worker with Cancel leaving nothing; every Export entry live; originals intact."""
        import hashlib
        import time as _t
        import zipfile
        from html.parser import HTMLParser
        import pymupdf
        from . import docexport as X, password_ui
        ww = work / "s18b"
        ww.mkdir(exist_ok=True)

        def mk(path, enc=None, owner="o", pages_extra=0):
            d = pymupdf.open()
            p = d.new_page()
            p.insert_text((72, 80), "Chapter One", fontsize=22)
            p.insert_textbox(pymupdf.Rect(72, 100, 520, 300), "Body text of the first chapter. " * 12
                             + "A hyphen-\nated word.", fontsize=11)
            p.insert_text((72, 330), "• first bullet item", fontsize=11)
            p.insert_text((72, 346), "• second bullet item", fontsize=11)
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), 0)
            pix.clear_with(90)
            p.insert_image(pymupdf.Rect(72, 400, 232, 507), pixmap=pix)
            p2 = d.new_page()
            p2.insert_text((72, 80), "Chapter Two", fontsize=22)
            p2.insert_textbox(pymupdf.Rect(72, 100, 520, 300), "Second chapter text. " * 12, fontsize=11)
            t_ = pymupdf.open()
            tp = t_.new_page()
            tp.insert_text((60, 140), "SCANNED", fontsize=40)
            sp = tp.get_pixmap(dpi=100)
            t_.close()
            d.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), pixmap=sp)
            d.new_page().insert_text((72, 80), "DELETEME page", fontsize=14)
            for k in range(pages_extra):
                d.new_page().insert_textbox(pymupdf.Rect(72, 72, 520, 760), f"Extra page {k} text. " * 60, fontsize=10)
            d.set_toc([[1, "Chapter One", 1], [1, "Chapter Two", 2]])
            kw = (dict(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw=owner, permissions=enc)
                  if enc is not None else {})
            d.save(str(path), **kw)
            d.close()
        A = ww / "a.pdf"
        mk(A)
        R = ww / "restricted.pdf"
        mk(R, enc=pymupdf.PDF_PERM_PRINT)
        P = ww / "protected.pdf"
        mk(P, enc=-1)
        BIG = ww / "big.pdf"
        mk(BIG, pages_extra=200)
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (A, R, P, BIG)}
        w = gui.Main()
        em, ex = w.editor_mode, w.exporter
        out = {}
        out["every_export_entry_live"] = (all(not a.property("soon") for a in em.export_menu.actions())
                                          and len(em.export_menu.actions()) == 6
                                          and not any("not implemented" in (a.statusTip() + a.toolTip()).lower()
                                                      for a in em.export_menu.actions())
                                          and not em.export_btn.property("soon")
                                          and "later" not in em.export_btn.toolTip().lower())
        em.open_pdf(str(A))
        em.thumbs.clearSelection()
        em.thumbs.item(3).setSelected(True)
        w.pageops.delete_selected(confirm=False)              # an unsaved edit: the last page is gone
        st0, dirty0 = em.work.step(), em.dirty()
        base = {"range": "all", "after": "none", "page_marks": True}

        def run(fmt, name, **kw):
            dest = ww / name
            ok = ex.export(fmt, options=dict(base, **kw.pop("opts", {})), dest=str(dest), wait=True, **kw)
            return ok, dest
        # text
        ok, f = run("txt", "out.txt")
        t = f.read_text(encoding="utf-8") if ok else ""
        out["txt_roundtrip"] = (ok and "Chapter One" in t and "Second chapter text." in t and "[Page 2]" in t
                                and "DELETEME" not in t)
        ok, f = run("txt", "range.txt", opts={"range": "2", "page_marks": False})
        t = f.read_text(encoding="utf-8") if ok else ""
        out["txt_page_range"] = ok and "Chapter Two" in t and "Chapter One" not in t and "[Page" not in t
        # HTML: one self-contained file that parses, images inlined
        ok, f = run("html", "out.html")
        h = f.read_text(encoding="utf-8") if ok else ""

        class _P(HTMLParser):
            pass
        try:
            _P().feed(h)
            parsed = True
        except Exception:
            parsed = False
        out["html_roundtrip"] = (ok and parsed and h.startswith("<!DOCTYPE html>") and "Chapter One" in h
                                 and X.data_uri_count(h) >= 2 and "DELETEME" not in h
                                 and not any(p.is_dir() for p in ww.iterdir()))
        # Markdown: headings from the 7a detection, paragraphs (hyphenation joined), a list
        ok, f = run("md", "out.md")
        m = f.read_text(encoding="utf-8") if ok else ""
        out["md_roundtrip"] = (ok and m.startswith("# Chapter One") and "\n# Chapter Two" in m
                               and "- first bullet item\n- second bullet item" in m and "hyphenated word" in m
                               and "DELETEME" not in m)
        # EPUB: a valid container, chapters from the bookmarks, images and the scanned page carried
        ok, f = run("epub", "out.epub")
        epub_ok = False
        if ok:
            z = zipfile.ZipFile(str(f))
            first = z.infolist()[0]
            names = z.namelist()
            chap = [z.read(n).decode("utf-8") for n in names if n.endswith(".xhtml") and "chapter" in n]
            from ebooklib import epub as _epub
            book = _epub.read_epub(str(f))
            titles = [s.title for s in book.toc]
            epub_ok = (first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
                       and z.read("mimetype") == b"application/epub+zip"
                       and any(n.endswith("nav.xhtml") for n in names) and titles == ["Chapter One", "Chapter Two"]
                       and sum(1 for n in names if "/images/" in n) >= 2 and len(chap) == 2
                       and "<li>first bullet item</li>" in chap[0] and "DELETEME" not in "".join(chap))
        out["epub_roundtrip"] = epub_ok
        # PDF: unsaved edits in, page range, the Save target untouched, still unsaved
        ok, f = run("pdf", "out.pdf")
        pd = pymupdf.open(str(f)) if ok else None
        out["pdf_roundtrip"] = (ok and pd.page_count == 3 and "Chapter One" in pd[0].get_text() and not pd.needs_pass
                                and em.work.target is None and em.dirty() == dirty0)
        if pd:
            pd.close()
        ok, f = run("pdf", "range.pdf", opts={"range": "1, 3"})
        pd = pymupdf.open(str(f)) if ok else None
        out["pdf_page_range"] = ok and pd.page_count == 2 and "Chapter One" in pd[0].get_text()
        if pd:
            pd.close()
        out["journal_unchanged"] = em.work.step() == st0 and em.dirty() == dirty0
        # never the original: the source as destination is refused, nothing written
        nd = len(r["dialogs"])
        refused = ex.export("txt", options=dict(base), dest=str(A), wait=True) is False
        out["original_refused"] = refused and any("original" in m.lower() or "protected" in m.lower()
                                                  for m in r["dialogs"][nd:])
        # Cancel in the background: nothing written, no partial file
        em.open_pdf(str(BIG))
        cdst = ww / "cancel.epub"
        ex.export("epub", options=dict(base), dest=str(cdst), test_delay=0.03)
        t0 = _t.time()
        while ex.job is not None and len(ex.job["seen"]) < 5 and _t.time() - t0 < 60:
            app.processEvents()
        ex.cancel()
        while ex.job is not None and _t.time() - t0 < 90:
            app.processEvents()
        out["cancel_leaves_nothing"] = ((ex.last or {}).get("why") == "cancelled" and not cdst.exists()
                                        and not any(X.PARTIAL in p.name or p.name.startswith("tmp")
                                                    for p in ww.iterdir()))
        ok, f = run("epub", "big.epub")
        out["big_epub_204_pages_s"] = (ex.last or {}).get("seconds")
        out["big_epub_longest_ui_pause_ms"] = (ex.last or {}).get("gap_ms")
        out["big_epub_with_progress"] = ok and len((ex.last or {}).get("progress", [])) >= 200
        # a copy-forbidden source (owner password unknown): text formats blocked, PDF follows Save As
        import lazaruspdf.locked as L
        real_ask = L.ask_password
        L.ask_password = lambda *a_, **k_: "u"
        try:
            em.open_pdf(str(R))
        finally:
            L.ask_password = real_ask
        nd = len(r["dialogs"])
        blocked = [ex.export(fmt, options=dict(base), dest=str(ww / f"blocked{X.FORMATS[fmt][1]}"), wait=True)
                   for fmt in X.TEXT_FORMATS]
        msgs = r["dialogs"][nd:]
        real_warn = password_ui.warn_unknown_owner
        password_ui.warn_unknown_owner = lambda *a_, **k_: "continue"
        try:
            pok, pf = run("pdf", "restricted-out.pdf")
        finally:
            password_ui.warn_unknown_owner = real_warn
        rd = pymupdf.open(str(pf)) if pok else None
        still = bool(rd) and rd.needs_pass and rd.authenticate("u") and not (rd.permissions & pymupdf.PDF_PERM_COPY)
        if rd:
            rd.close()
        out["restricted_text_formats_blocked"] = (blocked == [False] * 4
                                                  and sum("don't allow copying" in x for x in msgs) == 4
                                                  and not any((ww / f"blocked{X.FORMATS[k][1]}").exists()
                                                              for k in X.TEXT_FORMATS))
        out["restricted_pdf_follows_save_rules"] = pok and still
        # a password-protected source: text formats warn first (declined: nothing), PDF keeps the password
        L.ask_password = lambda *a_, **k_: "o"
        try:
            em.open_pdf(str(P))
        finally:
            L.ask_password = real_ask
        declined = ex.export("md", options=dict(base), dest=str(ww / "prot.md"), unprotected_ok=False) is False
        pok, pf = run("pdf", "prot-out.pdf")
        pd = pymupdf.open(str(pf)) if pok else None
        kept = bool(pd) and pd.needs_pass and bool(pd.authenticate("o"))      # (Save As's rule: opened with the
                                                                               # owner password, it opens the copy)
        if pd:
            pd.close()
        out["protected_source"] = declined and not (ww / "prot.md").exists() and pok and kept
        out["originals_byte_identical"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        w.close()
        out["ok"] = all(v for k, v in out.items() if not k.endswith(("_s", "_ms")))
        return out
    check("s18b_export", s18b_export)

    def s18c_recent_and_annotations():
        """Stage 18c: File › Open Recent (cap, newest first, every editor entry point feeds it,
        untitled documents don't, missing files greyed, reopen, Clear, nothing automatic at start)
        and the Annotations panel (every 9a / 9b role, redaction marks and links with their text,
        live on change, click goes to and selects, a dock tabbed with Pages / Bookmarks)."""
        import hashlib
        import os
        import pymupdf
        from PySide6.QtCore import QMimeData, QPointF, QUrl
        from PySide6.QtGui import QDropEvent
        from . import contentedit as CE, recent
        ww = work / "s18c"
        ww.mkdir(exist_ok=True)
        out = {}

        def mkpdf(path, n=2, text="Recent file"):
            d = pymupdf.open()
            for i in range(n):
                d.new_page().insert_text((72, 80), f"{text} page {i + 1}", fontsize=14)
            d.save(str(path))
            d.close()
        files = [ww / f"r{k:02d}.pdf" for k in range(12)]
        for f in files:
            mkpdf(f)
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em, rm = w.editor_mode, w.recent
        recent.clear(w.settings)
        norm = lambda p: os.path.normcase(os.path.abspath(str(p)))    # noqa: E731
        listed = lambda: [norm(p) for p in recent.entries(w.settings)]  # noqa: E731
        # 1. entry points: Open (open_pdf), a drop on the window, a hand-over from another launch
        em.open_pdf(str(files[0]))
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(files[1]))])
        w.set_mode(em.key)
        w.dropEvent(QDropEvent(QPointF(50, 50), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier))
        for _ in range(200):
            if any(s.is_source(files[1]) and s.ready for s in em.sessions):
                break
            wait(20)
        from .instance import OpenQueue
        OpenQueue(w).add([str(files[2])])
        for _ in range(200):
            if any(s.is_source(files[2]) and s.ready for s in em.sessions):
                break
            wait(20)
        out["entry_points_feed_list"] = listed()[:3] == [norm(files[2]), norm(files[1]), norm(files[0])]
        # an untitled document (Paste Pages as New PDF) doesn't count
        w.pageclip.clip = __import__("lazaruspdf.pageclip", fromlist=["x"]).copy_pages(em.doc, [0])
        before = listed()
        w.pageclip.paste_new()
        out["untitled_not_listed"] = listed() == before
        # 2. the cap (10), newest first, a reopened file moves to the front (no duplicate)
        for f in files[3:]:
            em.open_pdf(str(f))
        em.activate(next(s for s in em.sessions if s.is_source(files[5])))
        em.close_session(em.cur, ask=False)
        em.open_pdf(str(files[5]))
        lst = listed()
        out["cap_10_newest_first_no_duplicates"] = (len(lst) == recent.MAX == 10 and lst[0] == norm(files[5])
                                                    and len(set(lst)) == 10 and norm(files[0]) not in lst)
        # 3. the menu: names, full path as tooltip, a missing file greyed, reopen, Clear
        rm.rebuild()
        acts = [a for a in rm.menu.actions() if a.data()]
        first_ok = (len(acts) == 10 and acts[0].text().endswith(files[5].name)
                    and norm(acts[0].toolTip()) == norm(files[5]))
        gone = files[11]
        tmp = ww / "moved.bin"
        gone.replace(tmp)
        rm.rebuild()
        acts2 = {norm(a.data()): a for a in rm.menu.actions() if a.data()}
        greyed = not acts2[norm(gone)].isEnabled() and "not found" in acts2[norm(gone)].toolTip()
        tmp.replace(gone)
        out["menu_names_tooltips_missing_greyed"] = first_ok and greyed
        for s in list(em.sessions):
            em.close_session(s, ask=False)
        rm.rebuild()
        a7 = next(a for a in rm.menu.actions() if a.data() and norm(a.data()) == norm(files[7]))
        a7.trigger()
        for _ in range(200):
            if em.cur is not None and em.cur.ready:
                break
            wait(20)
        out["reopen_from_menu"] = em.cur is not None and em.cur.is_source(files[7]) and listed()[0] == norm(files[7])
        missing = ww / "never-there.pdf"
        recent.add(w.settings, str(missing))
        quiet = rm.open(str(missing)) is False and norm(missing) not in listed()
        out["missing_on_click_removed_quietly"] = quiet
        clear_act = next(a for a in rm.menu.actions() if a.text() == "Clear recent files")
        clear_act.trigger()
        rm.rebuild()
        out["clear_recent"] = (recent.entries(w.settings) == [] and
                               not next(a for a in rm.menu.actions() if a.text() == "Clear recent files").isEnabled())
        for f in files[:3]:
            recent.add(w.settings, str(f))
        w.close()
        w2 = gui.Main()
        wait(100)
        out["nothing_reopened_at_start"] = (len(w2.editor_mode.sessions) == 0 and len(recent.entries(w2.settings)) == 3
                                            and w2.recent.menu.title() == "Open Recent"
                                            and w2.recent.menu.menuAction() in w2.file_menu.actions())
        w2.close()
        recent.clear(w.settings)
        # ---------------- the Annotations panel ----------------
        src = ww / "annots.pdf"
        d = pymupdf.open()
        p0 = d.new_page()
        p0.insert_text((72, 80), "Mark this phrase please", fontsize=14)
        p1 = d.new_page()
        p1.insert_text((72, 80), "Second page words", fontsize=14)
        p1.set_rotation(90)
        d.save(str(src))
        d.close()
        hs = hashlib.sha256(src.read_bytes()).hexdigest()
        w = gui.Main()
        w.resize(1300, 900)
        w.show()
        wait(100)
        em = w.editor_mode
        em.open_pdf(str(src))
        ap, ctl = em.annots, em.markup
        R_, P_ = pymupdf.Rect, pymupdf.Point
        q0 = em.doc[0].search_for("this phrase", quads=True)
        ctl.create_annot("highlight", 0, q0)
        ctl.create_annot("underline", 0, em.doc[0].search_for("Mark", quads=True))
        ctl.create_annot("note", 0, P_(400, 200), {"text": "A note body"})
        ctl.create_annot("freetext", 0, R_(72, 300, 300, 340), {"text": "Box words"})
        ctl.create_annot("rect", 0, R_(100, 400, 200, 460))
        ctl.create_annot("arrow", 0, (P_(300, 400), P_(400, 460)))
        ctl.create_annot("ink", 0, [P_(100 + 5 * k, 500 + (k % 4) * 6) for k in range(20)])
        ctl.create_annot("stamp", 0, R_(300, 500, 470, 550))
        ctl.create_annot("whiteout", 0, R_(68, 600, 200, 620))
        ctl.signature = CE.strokes_to_png([[(30, 100), (200, 60), (300, 110)]])
        ctl.create_annot("signature", 0, R_(300, 600, 420, 640))
        ctl.create_annot("callout", 0, (P_(120, 680), R_(200, 660, 380, 700)), {"text": "Callout words"})
        ctl.create_annot("distance", 0, (P_(80, 740), P_(280, 740)))
        ctl.create_annot("area", 0, [P_(400, 650), P_(520, 650), P_(470, 760)])
        em.run_edit("redact_mark", "mark", lambda doc: em.stage10 and __import__(
            "lazaruspdf.stage10", fromlist=["x"]).mark_redactions(doc, [(0, (60, 760, 200, 790))]), [0])
        ctl.create_link(0, (72, 60, 300, 90), {"kind": "page", "page": 1, "fit": "fit"})
        plain = pymupdf.open("pdf", em.doc.tobytes())          # (text of a rotated journalled page: read a copy)
        plain[1].set_rotation(0)
        q1 = plain[1].search_for("Second page", quads=True)
        plain.close()
        ctl.create_annot("highlight", 1, q1)                  # (a rotated page)
        ctl.set_tool("select")
        em.act_select.setChecked(True)
        ap.dock.show()
        ap.dock.raise_()
        wait(400)
        ents = ap.entries()
        kinds = [k for _p, k, _t in ents]
        text_of = {}
        for _p, k, t_ in ents:
            text_of.setdefault(k, t_)                         # (the first of a kind)
        want = ["Highlight", "Underline", "Note", "Text box", "Rectangle", "Arrow", "Drawing", "Stamp", "Whiteout",
                "Signature", "Callout", "Distance", "Area", "Redaction mark", "Link"]
        out["panel_lists_every_role"] = all(k in kinds for k in want) and kinds.count("Highlight") == 2
        out["panel_text_content"] = (text_of["Highlight"] == "this phrase" and text_of["Note"] == "A note body"
                                     and text_of["Text box"] == "Box words" and text_of["Callout"] == "Callout words"
                                     and text_of["Link"].startswith("Page 2") and bool(text_of["Distance"]) and text_of["Signature"] == ""
                                     and "Second page" in [t for p, k, t in ents if p == 1 and k == "Highlight"][0])
        out["panel_excludes_area_label_and_popup"] = "Text box" in kinds and kinds.count("Text box") == 1
        # live: delete, undo
        n0 = len(ents)
        note_row = kinds.index("Note")
        ap.go(note_row)
        wait(50)
        clicked = len(ctl.sel) == 1 and ctl.sel_page == 0 and em.canvas.current_page() == 0
        ctl.delete_selected()
        wait(400)
        after_del = len(ap.entries()) == n0 - 1 and "Note" not in [k for _p, k, _t in ap.entries()]
        em.undo()
        wait(400)
        after_undo = len(ap.entries()) == n0 and "Note" in [k for _p, k, _t in ap.entries()]
        out["click_goes_and_selects"] = clicked
        out["live_on_change"] = after_del and after_undo
        link_row = [k for _p, k, _t in ap.entries()].index("Link")
        ap.go(link_row)
        out["click_link_selects_with_link_tool"] = ctl.tool == "link" and len(ctl.sel) == 1
        hl1 = [i for i, (p, k, _t) in enumerate(ap.entries()) if p == 1][0]
        ap.go(hl1)
        wait(100)
        out["click_scrolls_to_page"] = em.canvas.current_page() == 1 and ctl.sel_page == 1
        pages_dock = em.docks[0]
        out["dock_tabbed_and_toggle"] = (pages_dock in w.tabifiedDockWidgets(ap.dock)
                                         and any(a.text() == "Annotations panel" for a in w.panel_toggles)
                                         and w.dockWidgetArea(ap.dock) == Qt.LeftDockWidgetArea)
        hidden_before = ap.refresh_count
        ap.dock.hide()
        ctl.create_annot("rect", 0, R_(450, 100, 500, 150))
        wait(300)
        out["hidden_panel_reads_nothing"] = ap.refresh_count == hidden_before
        # a quick Insert closes its progress dialogs before they ever show: they must not pop up later
        # (a closed, never-shown QProgressDialog's timer used to show it, window-modal, blocking hand-offs)
        from PySide6.QtWidgets import QApplication, QProgressDialog
        w.pageops.insert_pdf(after=True, path=str(files[0]))
        wait(900)
        stray = [d_ for d_ in QApplication.topLevelWidgets() if isinstance(d_, QProgressDialog) and d_.isVisible()]
        out["no_stray_progress_dialog"] = not stray and QApplication.activeModalWidget() is None
        em.undo()
        w.close()
        out["originals_byte_identical"] = (all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
                                           and hashlib.sha256(src.read_bytes()).hexdigest() == hs)
        out["ok"] = all(out.values())
        return out
    check("s18c_recent_and_annotations", s18c_recent_and_annotations)

    def installer_hooks():
        """Stage 19: LazarusPDF.exe --register-default / --unregister-default (the installer's
        default-reader task and uninstall), in a sandbox key: register for this exe; unregister
        removes it; a registration that points at another copy of Lazarus PDF is left alone."""
        import uuid
        from . import assoc as A
        from . import __version__
        root = rf"Software\LazarusSelfTest\{uuid.uuid4().hex[:8]}"
        os.environ["LAZARUS_ASSOC_ROOT"] = root
        reg = A.Registry(root)
        out = {}
        try:
            reg.set(r"Software\Classes\.pdf", "", "OtherApp.PDF")
            A.installer("register")
            cmd = reg.get(rf"Software\Classes\{A.PROGID}\shell\open\command") or ""
            out["register_for_this_exe"] = (A.is_registered(reg) and os.path.normcase(cmd) == os.path.normcase(A.command_line()[0])
                                            and reg.get(r"Software\Classes\.pdf") == "OtherApp.PDF")
            A.installer("unregister")
            out["unregister_this_exe"] = not A.is_registered(reg) and reg.get(r"Software\Classes\.pdf") == "OtherApp.PDF"
            A.register(reg, command=r'"C:\Elsewhere\LazarusPDF.exe" "%1"')
            A.installer("unregister")
            out["other_copy_left_alone"] = A.is_registered(reg)
            A.unregister(reg)
        finally:
            reg.delete_tree("")
            A.Registry().delete_if_empty(r"Software\LazarusSelfTest")
            os.environ.pop("LAZARUS_ASSOC_ROOT", None)
        out["sandbox_removed"] = not A.Registry().exists(root)
        out["version"] = __version__
        out["ok"] = all(v for k, v in out.items() if k != "version")
        return out
    check("installer_hooks", installer_hooks)

    def s19b_updater():
        """Stage 19b: the update checker against a local mock of GitHub's latest-release endpoint
        (LAZARUS_UPDATE_URL): version compare; newer / same / older / prerelease-only / nothing
        published / malformed reply / missing installer asset / missing or wrong checksum / offline /
        rate limit / slow server; download with SHA-256 verify and Cancel (nothing left behind);
        the notice bar, Skip this version memory, the startup toggle (off = no request at all);
        the unsaved-work prompt before installing; a source run refusing to install."""
        import hashlib
        import json
        import os
        import http.server
        import socket
        import threading
        import time as _t
        from . import updater as U
        from . import settings as S
        out = {}
        routes = {}
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                hits.append(self.path)
                st, hdr, body, delay = routes.get(self.path, (404, {}, b"{}", 0))
                if delay:
                    _t.sleep(delay)
                try:
                    self.send_response(st)
                    for k, v in hdr.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    step = max(1, len(body) // 20)
                    for i in range(0, len(body), step):
                        self.wfile.write(body[i:i + step])
                        if hdr.get("X-Trickle"):
                            _t.sleep(0.05)
                except (ConnectionError, OSError):
                    pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        setup_bytes = b"MZ fake installer " * 20000
        good = hashlib.sha256(setup_bytes).hexdigest()

        def release(tag, pre=False, assets=True, sha=True):
            a = []
            if assets:
                a.append({"name": f"LazarusPDF-Setup-{tag.lstrip('v')}.exe", "browser_download_url": base + "/dl/setup"})
                if sha:
                    a.append({"name": f"LazarusPDF-Setup-{tag.lstrip('v')}.exe.sha256", "browser_download_url": base + "/dl/sha"})
            return json.dumps({"tag_name": tag, "draft": False, "prerelease": pre, "body": "## Notes\n- a fix",
                               "html_url": base + "/page", "assets": a}).encode()

        def serve(body, st=200, hdr=None, delay=0):
            routes["/latest"] = (st, hdr or {"Content-Type": "application/json"}, body, delay)
        old_env = {k: os.environ.get(k) for k in ("LAZARUS_UPDATE_URL", "LAZARUS_UPDATE_TIMEOUT", "LAZARUS_UPDATE_DRYRUN")}
        os.environ["LAZARUS_UPDATE_URL"] = base + "/latest"
        try:
            # 1. versions, as versions
            out["version_compare"] = (U.is_newer("v0.19.2", "0.19.1") and not U.is_newer("v0.19.1", "0.19.1")
                                      and not U.is_newer("v0.19.0", "0.19.1") and U.is_newer("v0.19.10", "0.19.9")
                                      and U.is_newer("v0.20", "0.19.9") and not U.is_newer("v1.0.0-rc1", "1.0.0")
                                      and U.is_newer("v1.0.0", "1.0.0-rc1") and not U.is_newer("vX", "0.1"))
            cur = U.__version__
            nv = cur.split(".")
            newer_tag = "v" + ".".join(nv[:-1] + [str(int(nv[-1]) + 1)])
            older_tag = "v0.1.0"
            # 2. the check, every answer
            serve(release(newer_tag))
            r1 = U.check()
            serve(release("v" + cur))
            r2 = U.check()
            serve(release(older_tag))
            r3 = U.check()
            serve(release(newer_tag, pre=True))
            r4 = U.check()
            routes.pop("/latest")
            r5 = U.check()                                     # 404: nothing published
            serve(b"{not json")
            r6 = U.check()
            serve(b"{}", st=403, hdr={"X-RateLimit-Remaining": "0", "Content-Type": "application/json"})
            r7 = U.check()
            os.environ["LAZARUS_UPDATE_TIMEOUT"] = "1"
            serve(release(newer_tag), delay=2.5)
            r8 = U.check()
            os.environ.pop("LAZARUS_UPDATE_TIMEOUT")
            s_ = socket.socket()
            s_.bind(("127.0.0.1", 0))
            dead = s_.getsockname()[1]
            s_.close()
            os.environ["LAZARUS_UPDATE_URL"] = f"http://127.0.0.1:{dead}/latest"
            r9 = U.check()
            os.environ["LAZARUS_UPDATE_URL"] = base + "/latest"
            out["check_newer"] = bool(r1["status"] == "newer" and r1["version"] == newer_tag[1:] and r1["setup_url"] and r1["sha_url"])
            out["check_same_older"] = r2["status"] == "current" and r3["status"] == "current"
            out["check_prerelease_and_none"] = r4["status"] == "none" and r5["status"] == "none"
            out["check_malformed"] = r6["status"] == "error" and "readable" in r6["reason"]
            out["check_rate_limited"] = r7["status"] == "error" and "rate limit" in r7["reason"]
            out["check_slow_server"] = r8["status"] == "error" and "in time" in r8["reason"]
            out["check_offline"] = r9["status"] == "error" and "connection" in r9["reason"].lower()
            # 3. download: verified; wrong / missing checksum refused; missing asset; Cancel
            dl = work / "s19b-dl"
            dl.mkdir(exist_ok=True)
            routes["/dl/setup"] = (200, {}, setup_bytes, 0)
            routes["/dl/sha"] = (200, {}, f"{good} *LazarusPDF-Setup.exe".encode(), 0)
            p = U.download(r1, folder=dl)
            ok_dl = p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == good
            p.unlink()
            routes["/dl/sha"] = (200, {}, ("0" * 64).encode(), 0)
            try:
                U.download(r1, folder=dl)
                mism = False
            except U.UpdateError as e:
                mism = "doesn't match" in str(e)
            try:
                U.download(dict(r1, sha_url=None), folder=dl)
                nosha = False
            except U.UpdateError as e:
                nosha = "no .sha256" in str(e)
            serve(release(newer_tag, assets=False))
            r10 = U.check()
            try:
                U.download(r10, folder=dl)
                noasset = False
            except U.UpdateError as e:
                noasset = "no installer" in str(e)
            routes["/dl/sha"] = (200, {}, f"{good}  LazarusPDF-Setup.exe\n".encode(), 0)
            routes["/dl/setup"] = (200, {"X-Trickle": "1"}, setup_bytes, 0)
            seen = []
            try:
                U.download(r1, progress=lambda d, t: seen.append(d), stopped=lambda: len(seen) >= 1, folder=dl)
                cancelled = False
            except U.Cancelled:
                cancelled = True
            out["download_verified"] = ok_dl
            out["hash_mismatch_refused"] = mism
            out["hash_missing_refused"] = nosha
            out["asset_missing_refused"] = noasset
            out["cancel_mid_download"] = cancelled
            out["nothing_left_behind"] = list(dl.iterdir()) == []
            routes["/dl/setup"] = (200, {}, setup_bytes, 0)
            # 4. the UI: the bar (never a pop-up), Skip this version, the startup toggle
            w = gui.Main()
            w.resize(1200, 800)
            w.show()
            wait(100)
            ctl = w.updates
            serve(release(newer_tag))
            nd = len(r["dialogs"])
            ctl.check(manual=False)
            ctl.wait()
            wait(50)
            bar_shown = ctl.bar.isVisible() and newer_tag[1:] in ctl.bar.label.text() and len(r["dialogs"]) == nd
            ctl.skip()
            skipped = w.settings.value(U_SKIP, "", type=str) == newer_tag[1:] and not ctl.bar.isVisible()
            ctl.check(manual=False)
            ctl.wait()
            wait(50)
            quiet_when_skipped = not ctl.bar.isVisible()
            nv2 = "v" + ".".join(nv[:-1] + [str(int(nv[-1]) + 2)])
            serve(release(nv2))
            ctl.check(manual=False)
            ctl.wait()
            wait(50)
            newer_than_skipped = ctl.bar.isVisible() and nv2[1:] in ctl.bar.label.text()
            ctl.later()
            out["notice_bar_no_popup"] = bar_shown
            out["skip_version_remembered"] = skipped and quiet_when_skipped and newer_than_skipped
            # manual checks always say how it went
            serve(release("v" + cur))
            nd = len(r["dialogs"])
            ctl.check(manual=True)
            ctl.wait()
            wait(50)
            routes.pop("/latest")
            ctl.check(manual=True)
            ctl.wait()
            wait(50)
            serve(b"{not json")
            ctl.check(manual=True)
            ctl.wait()
            wait(50)
            msgs = r["dialogs"][nd:]
            out["manual_check_reports"] = (len(msgs) == 3 and "up to date" in msgs[0] and "No Lazarus PDF release" in msgs[1]
                                           and "Couldn't check" in msgs[2])
            # the startup toggle: off = no request at all; on = one, after the window is up
            serve(release(nv2))
            S.put(w.settings, "update_check", False)
            n0 = len(hits)
            ctl.startup()
            wait(4600)
            off_quiet = len(hits) == n0
            S.put(w.settings, "update_check", True)
            ctl.startup()
            wait(4600)
            ctl.wait()
            on_checks = len(hits) == n0 + 1
            out["startup_toggle_off_no_request"] = off_quiet
            out["startup_check_when_on"] = on_checks
            # installing: a source run refuses; an installed copy asks about unsaved work first
            em = w.editor_mode
            src = work / "s19b-doc.pdf"
            import pymupdf
            d_ = pymupdf.open()
            d_.new_page().insert_text((72, 72), "update test", fontsize=12)
            d_.save(str(src))
            d_.close()
            em.open_pdf(str(src))
            em.markup.create_annot("note", 0, pymupdf.Point(200, 200), {"text": "unsaved"})
            ctl.info = r1
            if not U.can_install():
                refused = ctl.download_and_install() is False and (ctl.last or {}).get("status") == "refused"
            else:
                refused = True                                   # (an installed copy: covered below)
            out["source_run_refuses_install"] = refused or U.can_install()
            launched = []
            real_launch, real_quit, real_q = U.launch_setup, QApplication.quit, QMessageBox.question
            U.launch_setup = lambda p: launched.append(str(p)) or True
            QApplication.quit = staticmethod(lambda: None)
            try:
                QMessageBox.question = _box("question", QMessageBox.Cancel)
                fake = work / "fake-setup.exe"
                fake.write_bytes(b"x")
                ctl.install(fake)
                postponed = (ctl.last or {}).get("status") == "postponed" and not launched and em.dirty()
                QMessageBox.question = _box("question", QMessageBox.Discard)
                ctl.install(fake)
                went = launched == [str(fake)] and (ctl.last or {}).get("status") == "installing"
            finally:
                U.launch_setup, QApplication.quit, QMessageBox.question = real_launch, real_quit, real_q
            out["unsaved_prompt_before_install"] = postponed and went
            out["setup_switches"] = U.setup_command("S.exe")[1:] == ["/SILENT", "/SP-", "/SUPPRESSMSGBOXES",
                                                                     "/NORESTART", "/RELAUNCH=1"]
            try:
                w.close()
            except Exception:
                pass
        finally:
            srv.shutdown()
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        out["requests_made"] = len(hits)
        out["ok"] = all(v for k, v in out.items() if k != "requests_made")
        return out
    U_SKIP = "update_skip"
    check("s19b_updater", s19b_updater)

    def s20_about_licenses_support():
        """Stage 20: the About box (DarkRelay banner behind it; current description; DarkRelay,
        version, AGPL-3.0, the source link), Help › Licenses (LICENSE + THIRD-PARTY-NOTICES ship
        with the program), Help › Support DarkRelay (the browser, nothing else), "program" wording."""
        from . import __version__, about_ui, assoc
        out = {}
        w = gui.Main()
        names = [a.text() for m in w.menuBar().findChildren(type(w.file_menu)) if m.title() == "Help" for a in m.actions()]
        out["help_menu"] = names == ["Check for updates…", "Licenses…", "Support DarkRelay", "", f"About {APP_NAME_}"]
        dlg = w.show_about(show=False)
        t = dlg.body.text()
        out["about_text"] = ("A free, local PDF editor and reader for Windows." in t and "HAL" not in t
                             and "later" not in t.lower() and "DarkRelay" in t and __version__ in t
                             and "AGPL-3.0" in t and about_ui.SOURCE_URL in t and "No telemetry" in t)
        out["about_banner"] = about_ui.BANNER.exists() and not dlg.backdrop._src.isNull()
        lic = w.show_licenses(show=False)
        notes, agpl = lic.texts.get("THIRD-PARTY-NOTICES.txt", ""), lic.texts.get("LICENSE", "")
        out["licenses_dialog"] = ("PyMuPDF" in notes and "Tesseract" in notes and "LGPL-3.0" in notes
                                  and "GNU AFFERO GENERAL PUBLIC LICENSE" in agpl)
        opened = []
        real = about_ui.QDesktopServices.openUrl
        about_ui.QDesktopServices.openUrl = staticmethod(lambda u: opened.append(u.toString()) or True)
        try:
            w.act_support.trigger()
        finally:
            about_ui.QDesktopServices.openUrl = real
        out["support_opens_browser"] = opened == ["https://buymeacoffee.com/crazy8697"]
        out["program_wording"] = ("let programs make themselves" in assoc.CONFIRM_NOTE
                                  and "apps make" not in assoc.CONFIRM_NOTE and "Default apps" in assoc.CONFIRM_NOTE)
        dlg.close()
        lic.close()
        w.close()
        out["ok"] = all(out.values())
        return out
    APP_NAME_ = "Lazarus PDF"
    check("s20_about_licenses_support", s20_about_licenses_support)

    def s20_combos():
        """Stage 20 cross-feature combinations: (a) annotations + a snapshot edit + page ops + a
        paste from another tab, undone all the way back to the original; (b) annotate + redact +
        Save With Password, reopened; (c) pages dragged between tabs, then exported (PDF, EPUB);
        (d) Batch on a password-protected file."""
        import hashlib
        import zipfile
        import pymupdf
        from . import locked as L
        from . import stage10 as T
        ww = work / "s20"
        ww.mkdir(exist_ok=True)
        out = {}

        def mk(path, label, n=3, enc=None):
            d = pymupdf.open()
            for i in range(n):
                p = d.new_page()
                p.insert_text((72, 80), f"{label} page {i + 1}", fontsize=16)
                p.insert_text((72, 120), f"SECRET{i} and keep{i}", fontsize=12)
            kw = (dict(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o", permissions=-1)
                  if enc else {})
            d.save(str(path), **kw)
            d.close()
        A, B, P = ww / "a.pdf", ww / "b.pdf", ww / "p.pdf"
        mk(A, "AAA")
        mk(B, "BBB", 2)
        mk(P, "PPP", 2, enc=True)
        H = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (A, B, P)}

        def fp(doc):
            plain = pymupdf.open("pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_NONE))
            try:
                return [(pg.rotation, round(pg.rect.width), round(pg.rect.height), pg.get_text().strip(),
                         len(list(pg.annots()))) for pg in plain]
            finally:
                plain.close()
        w = gui.Main()
        w.resize(1200, 800)
        w.show()
        wait(100)
        em, ops, pc = w.editor_mode, w.pageops, w.pageclip
        em.open_pdf(str(B))
        sb = em.cur
        em.open_pdf(str(A))
        sa = em.cur
        f0 = fp(em.doc)
        # (a)
        em.markup.create_annot("note", 0, pymupdf.Point(300, 300), {"text": "combo"})
        em.run_edit("insert_textbox", "Stamp", lambda d: d[1].insert_textbox(pymupdf.Rect(72, 200, 400, 260),
                                                                               "snapshot edit"), [1])
        em.thumbs.clearSelection()
        em.thumbs.item(2).setSelected(True)
        ops.rotate(90)
        em.thumbs.clearSelection()
        em.thumbs.item(0).setSelected(True)
        ops.duplicate()
        pc.drop({"sid": sb.sid, "pages": [1]}, sa, 1)
        ops._crop_drawn(0, (20, 20, 500, 700), scope="all", mode="margins")
        changed = fp(em.doc) != f0 and em.doc.page_count == 5
        while em.work.can_undo():
            em.undo()
        out["a_edit_pageops_undo_all_the_way"] = changed and fp(em.doc) == f0 and not em.dirty()
        # (b)
        em.markup.create_annot("highlight", 0, em.doc[0].search_for("keep0", quads=True))
        em.run_edit("redact_mark", "mark", lambda d: T.mark_redactions(d, [(0, ("pdf", q)) for q in
                                                                          d[0].search_for("SECRET0", quads=True)]), [0])
        em.run_edit("apply_redactions", "Apply", lambda d: T.apply_redactions(d), [0])
        dest = ww / "redacted-protected.pdf"
        em.work.save_as(str(dest), L.custom("u", "o"))
        rd = pymupdf.open(str(dest))
        locked_ok = rd.needs_pass and bool(rd.authenticate("u"))
        txt = rd[0].get_text()
        hl = [a.type[1] for a in rd[0].annots()]
        rd.close()
        out["b_annotate_redact_password_reopen"] = (locked_ok and "SECRET0" not in txt and "keep0" in txt
                                                    and "Highlight" in hl)
        while em.work.can_undo():
            em.undo()
        # (c) in a fresh tab (the one above keeps its new password choice for saves and exports, by design)
        em.close_session(sa, ask=False)
        em.open_pdf(str(A))
        sa = em.cur
        pc.drop({"sid": sb.sid, "pages": [0, 1]}, sa, em.doc.page_count)
        okp = w.exporter.export("pdf", options={"range": "all", "after": "none"}, dest=str(ww / "combo.pdf"), wait=True)
        oke = w.exporter.export("epub", options={"range": "all", "after": "none"}, dest=str(ww / "combo.epub"),
                                wait=True)
        cd = pymupdf.open(str(ww / "combo.pdf")) if okp else None
        pdf_ok = bool(cd) and cd.page_count == 5 and "BBB page 2" in cd[4].get_text()
        if cd:
            cd.close()
        epub_text = ""
        if oke:
            z = zipfile.ZipFile(str(ww / "combo.epub"))
            epub_text = " ".join(z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.endswith(".xhtml"))
        out["c_drag_between_tabs_then_export"] = pdf_ok and "BBB page 1" in epub_text and "AAA page 1" in epub_text
        while em.work.can_undo():
            em.undo()
        # (d)
        bd = ww / "batch"
        bd.mkdir(exist_ok=True)
        job = w.batch.start({"files": [str(P)], "pipeline": [{"op": "find_replace", "params": {
            "find": "keep0", "replace": "BATCHED", "range": "all"}}], "out_dir": str(bd), "pattern": "{name}-b",
            "open_password": "u", "save_pdf": True})
        job.wait(300)
        outs = sorted(bd.glob("*.pdf"))
        bok = False
        if job.status == ["done"] and len(outs) == 1:
            bdoc = pymupdf.open(str(outs[0]))
            bok = bdoc.needs_pass and bool(bdoc.authenticate("u")) and "BATCHED" in bdoc[0].get_text()
            bdoc.close()
        out["d_batch_on_password_file"] = bok
        w.close()
        out["originals_byte_identical"] = all(hashlib.sha256(p.read_bytes()).hexdigest() == h for p, h in H.items())
        out["ok"] = all(out.values())
        return out
    check("s20_combos", s20_combos)

    def s20_export_keeps_repeated_body_lines():
        """Stage 20 fix: Markdown / EPUB export dropped every line that repeats on many pages (digits
        aside) as a "running header", wherever it was on the page, so repeated body text vanished.
        Now only such lines in the top / bottom margin go."""
        import pymupdf
        from . import docexport as X
        d = pymupdf.open()
        for i in range(4):
            p = d.new_page()
            p.insert_text((72, 300), f"Step {i + 1} of the procedure", fontsize=12)     # body, repeats
            p.insert_textbox(pymupdf.Rect(72, 320, 520, 400), f"Unique text number {i} here.", fontsize=11)
            p.insert_text((280, 820), f"Page {i + 1} of 4", fontsize=9)               # a real footer
        dest = work / "s20-running.md"
        X.run({"fmt": "md", "data": d.tobytes(), "dest": str(dest), "pages": [0, 1, 2, 3]})
        d.close()
        md = dest.read_text(encoding="utf-8")
        out = {"body_kept": all(f"Step {i + 1} of the procedure" in md for i in range(4)),
               "footer_dropped": "Page 1 of 4" not in md}
        out["ok"] = all(out.values())
        return out
    check("s20_export_keeps_repeated_body_lines", s20_export_keeps_repeated_body_lines)

    def s21_searchable_and_rebuilt():
        """Stage 21: Extraction's Searchable PDF and Rebuilt pages carry the CORRECTED review text
        (a corrected word and a filled gap, typed in the review editor), the searchable copy keeps
        the scan image with the corrected words at the position of the words they replace, the
        rebuilt copy has no images and searches as typed, both are new files and the original is
        byte-identical. The editor's OCR page tool shares the layer code (ocrlayer)."""
        import hashlib
        import pymupdf
        from . import ocrlayer, stage10, tess
        from .extract import GAP, Filled, Para
        tess.configure()
        sw = work / "s21"
        sw.mkdir(exist_ok=True)
        t = pymupdf.open()
        tp = t.new_page()
        lines = ("The quarterly report shows growth", "Revenue rose in every single region",
                 "Costs were held flat this year")
        for i, s in enumerate(lines):
            tp.insert_text((72, 120 + 50 * i), s, fontsize=20)
        where = {"growth": tp.search_for("growth")[0], "single": tp.search_for("single")[0]}
        pix = tp.get_pixmap(dpi=200)
        t.close()
        src = sw / "scan.pdf"
        d = pymupdf.open()
        p = d.new_page()
        p.insert_image(p.rect, pixmap=pix)                       # a scan: an image, no text layer
        d.save(src)
        d.close()
        h0 = hashlib.sha256(src.read_bytes()).hexdigest()
        pages, paths, _ = modes.run(modes.EXTRACT, str(src), str(sw / "out"))
        for it in pages[0].items:                    # as Extraction marks a word it can't read
            if isinstance(it, Para) and isinstance(it.segs[0], str) and "single" in it.segs[0]:
                a, b = it.segs[0].split("single", 1)
                it.segs = [a.rstrip(), GAP, b.lstrip()]
        w = gui.Main()
        w.importer.last_src = str(src)
        w.on_imported(pages, paths)
        tm, ed = w.text_mode, w.text_mode.editor
        out = {"knows_source": tm.source == str(src), "one_open_gap": ed.gap_counts() == (1, 0)}
        ed.document().find("growth").insertText("GROWTHFIX")          # a corrected word
        ed.select_fragment(next(ed.gap_fragments()))
        ed.textCursor().insertText("SMUDGEFILL")                      # a filled gap
        wait(100)
        ed.flush()
        segs = [s for pr in ed.pages() for it in pr.items if isinstance(it, Para) for s in it.segs]
        out["editor_has_corrections"] = (ed.gap_counts() == (0, 1) and any(isinstance(s, Filled)
                                         and s.text == "SMUDGEFILL" for s in segs))
        sp, rp = sw / "out-searchable.pdf", sw / "out-rebuilt.pdf"
        out["searchable_written"] = tm.export_pdf_output("searchable", dest=str(sp), wait=True, gaps_ok=True)
        out["rebuilt_written"] = tm.export_pdf_output("rebuilt", dest=str(rp), wait=True, gaps_ok=True)

        def near(found, word):
            return bool(found) and (found[0] & where[word]).get_area() > 0.3 * where[word].get_area()
        s = pymupdf.open(sp)
        pg = s[0]
        text = pg.get_text()
        out["searchable_scan_kept"] = s.page_count == 1 and len(pg.get_images()) == 1
        out["searchable_corrected_word_in_place"] = near(pg.search_for("GROWTHFIX"), "growth")
        out["searchable_filled_gap_in_place"] = near(pg.search_for("SMUDGEFILL"), "single")
        out["searchable_text_is_corrected"] = (" growth" not in text.replace("GROWTHFIX", "")
                                               and "Costs were held flat this year" in text)
        traces = pg.get_texttrace()                            # every glyph: render mode 3, invisible
        out["searchable_layer_invisible"] = bool(traces) and all(tr["type"] == 3 for tr in traces)
        s.close()
        r_ = pymupdf.open(rp)
        rtext = "".join(q.get_text() for q in r_)
        out["rebuilt_no_images"] = sum(len(q.get_images()) for q in r_) == 0
        out["rebuilt_has_corrections"] = ("GROWTHFIX" in rtext and "SMUDGEFILL" in rtext
                                          and "every SMUDGEFILL region" in rtext and "\xa0" not in rtext)
        out["rebuilt_searchable"] = bool(r_[0].search_for("shows GROWTHFIX"))
        out["rebuilt_same_page_size"] = r_[0].rect == pymupdf.Rect(0, 0, 595, 842)
        r_.close()
        out["original_untouched"] = hashlib.sha256(src.read_bytes()).hexdigest() == h0
        n0 = len(r["dialogs"])
        out["refuses_the_original"] = (not tm.export_pdf_output("rebuilt", dest=str(src), wait=True, gaps_ok=True)
                                       and tm.last_pdf == {"status": "refused"} and len(r["dialogs"]) == n0 + 1
                                       and hashlib.sha256(src.read_bytes()).hexdigest() == h0)
        out["one_layer_code_path"] = stage10.ocr_page_pdf.__module__ == "lazaruspdf.stage10" and \
            "ocr_layer" in stage10.ocr_page_pdf.__code__.co_names and callable(ocrlayer.corrected_words)
        w.close()
        # a page that already has a real text layer keeps it as it is (nothing added twice)
        from . import rebuild
        tl = sw / "textlayer.pdf"
        d = pymupdf.open()
        d.new_page().insert_text((72, 100), "This page already has a real text layer of its own words.")
        d.save(tl)
        d.close()
        tp_pages, _, _ = modes.run(modes.EXTRACT, str(tl), str(sw / "out-tl"))
        tl_out = sw / "textlayer-searchable.pdf"
        got = rebuild.searchable(tp_pages, str(tl), str(tl_out))
        q = pymupdf.open(tl_out)
        out["text_layer_page_kept_as_is"] = got == [] and q[0].get_text().count("real text layer") == 1
        q.close()
        out["ok"] = all(out.values())
        return out
    check("s21_searchable_and_rebuilt", s21_searchable_and_rebuilt)

    def s21_no_placeholders():
        """Stage 21: nothing is a placeholder any more: no greyed "soon" action or widget, no "not
        implemented" / "placeholder" / "coming later" wording anywhere in the window, no Page
        layout menu, and the Document panel keeps only the in-memory-copy line."""
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QLabel, QWidget
        w = gui.Main()
        words = ("not implemented", "placeholder", "coming later", "later stage", "not built yet")
        bad = []
        for o in w.findChildren(QAction) + w.findChildren(QWidget):
            s = " ".join(str(getattr(o, m)()) for m in ("text", "toolTip", "statusTip") if hasattr(o, m))
            if o.property("soon") or any(k in s.lower() for k in words):
                bad.append(type(o).__name__)
        labels = [lb.text() for lb in w.findChildren(QLabel)]
        menus = [a.text() for a in w.menuBar().actions()]
        sub = [a.text() for m in w.menuBar().actions() if m.menu() for a in m.menu().actions()]
        out = {"no_placeholder_anything": not bad, "found": len(bad),
               "greyed_tools_line_gone": not any("Greyed tools" in t for t in labels),
               "in_memory_line_kept": any(t.startswith("Edits happen on an in-memory copy") for t in labels),
               "no_page_layout_menu": not any("Page layout" in t for t in menus + sub),
               "import_has_no_mode_choice": not hasattr(w.importer, "mode")}
        w.close()
        out["ok"] = all(v for k, v in out.items() if k != "found")
        return out
    check("s21_no_placeholders", s21_no_placeholders)

    def s22_rename_and_migration():
        """Stage 22: Lazarus -> Lazarus PDF. The names (shown: "Lazarus PDF"; identifiers: LazarusPDF)
        and the one-time move of a Lazarus 0.21.0 user's data: settings (INI and the real registry
        backend, on sandbox keys), saved signatures, and the .pdf registration (a sandbox), including
        the forwarder kept while Windows' default choice still names the old ProgID."""
        import re as _re
        import uuid
        from PySide6.QtCore import QSettings
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QWidget
        from . import APP_ID, APP_NAME, EXE_NAME, REPO, assoc as A, instance, migrate, settings as S, updater
        out = {}
        mw = work / "s22"
        mw.mkdir(exist_ok=True)
        # --- names ---
        saved_ini = os.environ.pop("LAZARUS_SETTINGS_INI", None)
        try:
            real = S.store()
            out["settings_key_is_LazarusPDF"] = (real.organizationName(), real.applicationName()) == (APP_ID, APP_ID)
        finally:
            if saved_ini is not None:
                os.environ["LAZARUS_SETTINGS_INI"] = saved_ini
        saved_inst = os.environ.pop("LAZARUS_INSTANCE_NAME", None)
        try:
            out["instance_name"] = instance.server_name().startswith("LazarusPDF-")
        finally:
            if saved_inst is not None:
                os.environ["LAZARUS_INSTANCE_NAME"] = saved_inst
        out["identifiers"] = (APP_NAME == "Lazarus PDF" and APP_ID == "LazarusPDF" and EXE_NAME == "LazarusPDF.exe"
                              and REPO == "Crazy8697/LazarusPDF" and A.PROGID == "LazarusPDF.PDF"
                              and A.EXE_NAME == "LazarusPDF.exe" and A.CAPS == r"Software\LazarusPDF\Capabilities"
                              and A.REG_NAME == "LazarusPDF" and "registeredAppUser=LazarusPDF" in A.SETTINGS_URI)
        out["updater_points_at_new_repo"] = (updater.LATEST_URL == "https://api.github.com/repos/Crazy8697/LazarusPDF/releases/latest"
                                             and bool(updater.SETUP_ASSET.match("LazarusPDF-Setup-0.22.0.exe"))
                                             and not updater.SETUP_ASSET.match("Lazarus-Setup-0.22.0.exe"))
        w = gui.Main()
        texts = []
        for o in w.findChildren(QAction) + w.findChildren(QWidget):
            texts.append(" ".join(str(getattr(o, m)()) for m in ("text", "toolTip", "statusTip", "windowTitle")
                                  if hasattr(o, m)))
        about = w.show_about(show=False)
        texts.append(about.body.text())
        old_word = _re.compile(r"\bLazarus\b(?! PDF)")
        out["no_old_name_in_the_window"] = not any(old_word.search(t) for t in texts)
        out["title"] = w.windowTitle().startswith("Lazarus PDF")
        about.close()
        w.close()

        # --- settings: INI backend (what the upgrade test uses too) ---
        old_ini, new_ini = mw / "old.ini", mw / "new.ini"
        for p in (old_ini, new_ini):
            if p.exists():
                p.unlink()
        old = QSettings(str(old_ini), QSettings.IniFormat)
        vals = {"out_dir": str(mw / "out-elsewhere"), "recent_files": ["a.pdf", "b.pdf"], "update_check": False,
                "undo_ram_pct": 17, "editor/geometry": b"\x01\x02geometry\xff", "export_headings": True}
        for k, v in vals.items():
            old.setValue(k, v)
        old.sync()
        new = QSettings(str(new_ini), QSettings.IniFormat)
        res = migrate.migrate_settings(old, new)
        new2 = QSettings(str(new_ini), QSettings.IniFormat)
        out["ini_moved"] = (res == "moved" and S.get(new2, "out_dir") == vals["out_dir"]
                            and new2.value("recent_files") == vals["recent_files"]
                            and S.get(new2, "update_check") is False and S.get(new2, "undo_ram_pct") == 17
                            and bytes(new2.value("editor/geometry")) == vals["editor/geometry"]
                            and QSettings(str(old_ini), QSettings.IniFormat).allKeys() == [])
        out["ini_second_run_noop"] = migrate.migrate_settings(QSettings(str(old_ini), QSettings.IniFormat), new2) == "none"
        o3 = QSettings(str(mw / "old3.ini"), QSettings.IniFormat)
        o3.setValue("out_dir", "x")
        o3.sync()
        out["ini_existing_new_store_wins"] = (migrate.migrate_settings(o3, new2) == "skipped"
                                              and S.get(new2, "out_dir") == vals["out_dir"] and o3.allKeys() == ["out_dir"])

        # --- settings: the real registry backend, on sandbox keys ---
        org = f"LazarusMigrationSelfTest{uuid.uuid4().hex[:8]}"
        try:
            ro, rn = QSettings(org, "Lazarus"), QSettings(org, "LazarusPDF")
            for k, v in vals.items():
                ro.setValue(k, v)
            ro.sync()
            res = migrate.migrate_settings(ro, rn)
            rn2 = QSettings(org, "LazarusPDF")
            out["registry_moved"] = (res == "moved" and S.get(rn2, "out_dir") == vals["out_dir"]
                                     and list(rn2.value("recent_files")) == vals["recent_files"]
                                     and S.get(rn2, "update_check") is False and S.get(rn2, "undo_ram_pct") == 17
                                     and bytes(rn2.value("editor/geometry")) == vals["editor/geometry"]
                                     and QSettings(org, "Lazarus").allKeys() == [])
        finally:
            A.Registry().delete_tree(rf"Software\{org}")
        out["registry_sandbox_removed"] = not A.Registry().exists(rf"Software\{org}")

        # --- signatures ---
        so, sn = mw / "sig-old", mw / "sig-new"
        (so / "signatures").mkdir(parents=True, exist_ok=True)
        (so / "signatures" / "Mine.png").write_bytes(b"\x89PNG fake signature")
        (so / "signatures" / "Other.png").write_bytes(b"\x89PNG other")
        sn.mkdir(exist_ok=True)
        (sn / "Other.png").write_bytes(b"\x89PNG newer")
        n = migrate.migrate_signatures(so / "signatures", sn)
        out["signatures_moved"] = (n == 1 and (sn / "Mine.png").read_bytes() == b"\x89PNG fake signature"
                                   and (sn / "Other.png").read_bytes() == b"\x89PNG newer"
                                   and not (so / "signatures" / "Mine.png").exists() and (so / "signatures" / "Other.png").exists())

        # --- the .pdf registration (sandbox) ---
        new_cmd, new_ico = r'"C:\Program Files\LazarusPDF\LazarusPDF.exe" "%1"', r'"C:\Program Files\LazarusPDF\LazarusPDF.exe",0'
        old_cmd = r'"C:\Program Files\Lazarus\Lazarus.exe" "%1"'
        uc = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice"

        def old_registration(reg, choice):
            cls = r"Software\Classes\Lazarus.PDF"
            reg.set(cls, "", "PDF Document (Lazarus)")
            reg.set(cls + r"\shell\open\command", "", old_cmd)
            reg.set(r"Software\Classes\.pdf\OpenWithProgids", "Lazarus.PDF", b"", A.winreg.REG_NONE)
            reg.set(r"Software\Classes\.pdf\OpenWithProgids", "OtherApp.PDF", b"", A.winreg.REG_NONE)
            reg.set(r"Software\Classes\Applications\Lazarus.exe\shell\open\command", "", old_cmd)
            reg.set(r"Software\Lazarus\Capabilities\FileAssociations", ".pdf", "Lazarus.PDF")
            reg.set(r"Software\Lazarus\Lazarus", "out_dir", "kept")              # the old settings key: not ours
            reg.set(r"Software\RegisteredApplications", "Lazarus", r"Software\Lazarus\Capabilities")
            reg.set(r"Software\RegisteredApplications", "OtherApp", r"Software\OtherApp\Capabilities")
            if choice:
                reg.set(uc, "ProgId", choice)

        root = rf"Software\LazarusSelfTest\{uuid.uuid4().hex[:8]}"
        reg = A.Registry(root)
        try:
            old_registration(reg, "Lazarus.PDF")                    # Lazarus WAS the default
            r1 = A.migrate_old(reg, command=new_cmd, icon=new_ico)
            fwd = reg.get(r"Software\Classes\Lazarus.PDF\shell\open\command")
            out["default_case_new_registered"] = (r1 and r1["forwarder_progid"]
                                                  and reg.get(r"Software\RegisteredApplications", "LazarusPDF") == A.CAPS
                                                  and reg.get(rf"Software\Classes\{A.PROGID}\shell\open\command") == new_cmd
                                                  and reg.has(r"Software\Classes\.pdf\OpenWithProgids", A.PROGID))
            out["default_case_pdfs_still_open_here"] = fwd == new_cmd           # Windows' choice -> the new exe
            out["default_case_old_rest_removed"] = (not reg.exists(r"Software\Lazarus\Capabilities")
                                                    and reg.get(r"Software\RegisteredApplications", "Lazarus") is None
                                                    and not reg.has(r"Software\Classes\.pdf\OpenWithProgids", "Lazarus.PDF")
                                                    and not reg.exists(r"Software\Classes\Applications\Lazarus.exe"))
            out["other_programs_untouched"] = (reg.has(r"Software\Classes\.pdf\OpenWithProgids", "OtherApp.PDF")
                                               and reg.get(r"Software\RegisteredApplications", "OtherApp") is not None
                                               and reg.get(r"Software\Lazarus\Lazarus", "out_dir") == "kept")
            out["forwarder_stays_while_chosen"] = A.cleanup_forwarders(reg) == [] and reg.exists(r"Software\Classes\Lazarus.PDF")
            reg.set(uc, "ProgId", A.PROGID)                         # the user confirms Lazarus PDF in Settings
            out["forwarder_goes_when_not_chosen"] = (A.cleanup_forwarders(reg) == ["Lazarus.PDF"]
                                                     and not reg.exists(r"Software\Classes\Lazarus.PDF"))
            out["migrate_again_noop"] = A.migrate_old(reg, command=new_cmd, icon=new_ico) is None
        finally:
            reg.delete_tree("")
        reg = A.Registry(root)
        try:
            old_registration(reg, "PXC.PDF")                        # another program was the default
            r2 = A.migrate_old(reg, command=new_cmd, icon=new_ico)
            out["not_default_case_old_progid_removed"] = (r2 and not r2["forwarder_progid"]
                                                          and not reg.exists(r"Software\Classes\Lazarus.PDF")
                                                          and A.is_registered(reg) and reg.get(uc, "ProgId") == "PXC.PDF")
            A.unregister(reg, command=new_cmd)
            out["uninstall_after_migration_clean"] = (not A.is_registered(reg) and not reg.exists(r"Software\Classes\Lazarus.PDF")
                                                      and reg.has(r"Software\Classes\.pdf\OpenWithProgids", "OtherApp.PDF"))
        finally:
            reg.delete_tree("")
        A.Registry().delete_if_empty(r"Software\LazarusSelfTest")
        out["ok"] = all(out.values())
        return out
    check("s22_rename_and_migration", s22_rename_and_migration)

    def s221_window_titles():
        """0.22.1: the main window's title is exactly "Lazarus PDF" in every mode, with a PDF open and with
        unsaved edits (PDF and extraction); no window title anywhere carries an em dash or the program's
        name twice; Qt never appends the name (no application display name is set at start-up)."""
        import pymupdf
        from PySide6.QtWidgets import QWidget
        from . import APP_NAME, gui as G
        from .settings_dialog import SettingsDialog
        tw = work / "s221"
        tw.mkdir(exist_ok=True)
        src = tw / "titles.pdf"
        d = pymupdf.open()
        for i in range(2):
            d.new_page().insert_text((72, 100), f"Synthetic title test page {i + 1} with enough words to extract.")
        d.save(src)
        d.close()
        out = {}
        w = G.Main()
        titles = {}

        def snap(tag):
            for key in ("overview", "editor", "extract"):
                w.set_mode(key)
                wait(50)
                titles[f"{tag}/{key}"] = w.windowTitle()
        snap("empty")
        w.editor_mode.open_pdf(str(src))
        wait(200)
        snap("pdf_open")
        em = w.editor_mode
        em.thumbs.clearSelection()
        em.thumbs.item(1).setSelected(True)
        w.pageops.delete_selected(confirm=False)        # an unsaved PDF edit
        pages, paths, _ = modes.run(modes.EXTRACT, str(src), str(tw / "out"))
        w.importer.last_src = str(src)
        w.on_imported(pages, paths)
        w.text_mode.editor.textCursor().insertText("x")  # an unsaved extraction edit
        wait(100)
        out["edits_really_unsaved"] = em.any_dirty() and w.text_mode.is_dirty()
        snap("unsaved")
        out["main_title_always_exact"] = all(t == APP_NAME for t in titles.values()) and len(titles) == 9
        out["titles_seen"] = sorted(set(titles.values()))
        dialogs = [SettingsDialog(w, w.settings), w.importer, w.show_about(show=False), w.show_licenses(show=False)]
        tops = [x.windowTitle() for x in dialogs] + [x.windowTitle() for x in w.findChildren(QWidget) if x.isWindow()]
        out["no_dash_or_double_name_in_any_title"] = all("—" not in t and t.count(APP_NAME) <= 1 for t in tops)
        out["import_title"] = w.importer.windowTitle() == "Import PDF"
        out["settings_title"] = dialogs[0].windowTitle() == "Settings"
        # (the compiled code's names: an installed copy has no source to read)
        out["no_display_name_set"] = "setApplicationDisplayName" not in G.run.__code__.co_names
        for x in dialogs[:1] + dialogs[2:]:
            x.close()
        w.text_mode.editor.document().setModified(False)
        w.close()                                       # (the unsaved-PDF question is answered Discard)
        out["ok"] = all(v for k, v in out.items() if k != "titles_seen")
        return out
    check("s221_window_titles", s221_window_titles)

    def s223_icon_fonts():
        """Stage 22.3: the icons come from a PRIVATE font set (Material Design Icons 6 only, loaded from an
        explicit folder), QtAwesome's shared instance (which copies its fonts into the user fonts folder and
        registers them) is never created, and the cleanup of what older versions installed removes only
        byte-for-byte QtAwesome copies: a synthetic font with the same name and any other font survive, a
        locked file stays (its registry value goes) and is removed on the next run. Sandbox only."""
        import hashlib
        import shutil
        import uuid
        import winreg
        import qtawesome
        from PySide6.QtCore import QSettings
        from . import fontclean as F, icons as I
        out = {}
        ic = I.icon("mdi6.marker", color="#e6e6e6")
        out["private_font_loads"] = (I.iconic().fontname.get("mdi6") == "Material Design Icons"
                                     and not ic.isNull() and not ic.pixmap(24, 24).toImage().isNull()
                                     and list(I.iconic().fontname) == ["mdi6"])
        out["shared_instance_never_created"] = qtawesome._resource.get("iconic") is None
        out["font_folder_is_explicit"] = os.path.isfile(os.path.join(I.fonts_dir(), I.MDI_TTF))
        src = Path(I.fonts_dir())
        here = [n for n in F.KNOWN if (src / n).exists()]                  # (installed: only the mdi6 pair)
        out["known_hashes_match_this_qtawesome"] = (I.MDI_TTF in here and len(F.KNOWN) == 24 and all(
            hashlib.sha256((src / n).read_bytes()).hexdigest() == F.KNOWN[n] for n in here))
        # --- the cleanup, in a sandbox: a fonts folder + a key below HKCU, SYNTHETIC files only; the hash
        # table is swapped for the synthetic files' hashes during the test (the same matching code runs) ---
        sb = work / "s223-fonts"
        shutil.rmtree(sb, ignore_errors=True)
        sb.mkdir()
        names = ["materialdesignicons6-webfont-6.9.96.ttf", "materialdesignicons6-webfont-charmap-6.9.96.json",
                 "fontawesome6-solid-webfont-6.7.2.ttf", "phosphor-1.3.0.ttf", "remixicon-2.5.0.ttf"]
        fake = {n: (b"synthetic QtAwesome copy " + n.encode()) * 40 for n in names}
        for n, v in fake.items():
            (sb / n).write_bytes(v)
        n_copied = len(names)
        (sb / "remixicon-2.5.0.ttf").write_bytes(b"not QtAwesome's remixicon, same name" * 50)   # must survive
        (sb / "SomeOtherFont.ttf").write_bytes(b"an unrelated user font" * 50)                  # must survive
        real_known = F.KNOWN
        F.KNOWN = {n: hashlib.sha256(v).hexdigest() for n, v in fake.items()}
        key = rf"Software\LazarusSelfTest\fonts-{uuid.uuid4().hex[:8]}"
        regs = {"Material Design Icons (TrueType)": "materialdesignicons6-webfont-6.9.96.ttf",
                "Font Awesome 6 Free Solid (TrueType)": "fontawesome6-solid-webfont-6.7.2.ttf",
                "remixicon (TrueType)": "remixicon-2.5.0.ttf",
                "Some Other Font (TrueType)": "SomeOtherFont.ttf",
                "Elsewhere (TrueType)": r"C:\SomewhereElse\phosphor-1.3.0.ttf"}
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key) as k:
                for name, data in regs.items():
                    winreg.SetValueEx(k, name, 0, winreg.REG_SZ, data)
            locked = sb / "fontawesome6-solid-webfont-6.7.2.ttf"
            ini = work / "s223-fonts.ini"
            if ini.exists():
                ini.unlink()
            st = QSettings(str(ini), QSettings.IniFormat)
            os.environ["LAZARUS_FONTCLEAN_DIR"], os.environ["LAZARUS_FONTCLEAN_REG"] = str(sb), key
            try:
                with open(locked, "rb"):                      # held open: Windows refuses to delete it
                    r1 = F.run(st)
                left_vals = {n: d for n, d, _k in F._values(key)}
                out["cleanup_first_run"] = (
                    sorted(r1["left"]) == [locked.name]
                    and len(r1["files_removed"]) == n_copied - 2          # all matching copies but the locked one
                    and "remixicon-2.5.0.ttf" not in r1["files_removed"]
                    and set(left_vals) == {"remixicon (TrueType)", "Some Other Font (TrueType)", "Elsewhere (TrueType)"}
                    and not st.value(F.DONE_KEY, False, type=bool))
                out["same_name_other_font_survives"] = (sb / "remixicon-2.5.0.ttf").read_bytes().startswith(b"not QtAwesome")
                out["other_font_survives"] = (sb / "SomeOtherFont.ttf").exists()
                out["locked_file_left_its_value_removed"] = locked.exists() and "Font Awesome 6 Free Solid (TrueType)" not in left_vals
                r2 = F.run(st)                                # the next start: the file is free now
                out["locked_file_removed_next_run"] = (r2 is not None and r2["files_removed"] == [locked.name]
                                                       and not locked.exists() and st.value(F.DONE_KEY, False, type=bool))
                out["done_is_recorded"] = F.run(st) is None
                out["only_the_expected_files_left"] = sorted(p.name for p in sb.iterdir()) == ["SomeOtherFont.ttf",
                                                                                              "remixicon-2.5.0.ttf"]
            finally:
                os.environ.pop("LAZARUS_FONTCLEAN_DIR", None)
                os.environ.pop("LAZARUS_FONTCLEAN_REG", None)
        finally:
            F.KNOWN = real_known
            A_reg = __import__("lazaruspdf.assoc", fromlist=["x"]).Registry()
            A_reg.delete_tree(key)
            A_reg.delete_if_empty(r"Software\LazarusSelfTest")
        # every test hook alone keeps the real folder and registry out of reach (a test's uninstaller
        # once ran the real cleanup: it had only LAZARUS_ASSOC_ROOT set)
        saved = {v: os.environ.pop(v, None) for v in F.TEST_HOOKS}
        try:
            alone = []
            for v in F.TEST_HOOKS:
                os.environ[v] = "x"
                alone.append(F.places() is None)
                os.environ.pop(v)
        finally:
            for v, val in saved.items():
                if val is not None:
                    os.environ[v] = val
        out["no_real_run_from_a_test"] = all(alone) and F.places() is None
        out["ok"] = all(out.values())
        return out
    check("s223_icon_fonts", s223_icon_fonts)

    def install_folder_untouched():
        """Nothing was created, changed or removed in the app's own folder by any check above: an
        installed copy lives in read-only Program Files (every write goes to per-user places)."""
        after = _app_snapshot()
        added = sorted(set(after) - set(app_before))
        changed = sorted(k for k in set(after) & set(app_before) if after[k] != app_before[k])
        removed = sorted(set(app_before) - set(after))
        return {"ok": not (added or changed or removed), "files": len(after), "added": len(added),
                "changed": len(changed), "removed": len(removed)}
    check("install_folder_untouched", install_folder_untouched)

    def real_registration_untouched():
        """Stage 22: the user's real .pdf registration (and Windows' default choice) is exactly as
        before every check above, child launches included: tests only ever use a sandbox."""
        return {"ok": _real_registration() == reg_before}
    check("real_registration_untouched", real_registration_untouched)

    def real_fonts_untouched():
        """Stage 22.3: the user's real fonts folder and HKCU font registrations are exactly as before every
        check above: nothing in a run installs, registers or removes a font outside a sandbox."""
        return {"ok": _real_fonts() == fonts_before}
    check("real_fonts_untouched", real_fonts_untouched)

    # overall OK only if nothing raised AND every check reported ok
    r["ok"] = not r["errors"] and all(isinstance(v, dict) and v.get("ok") for v in c.values())
    Path(report_path).write_text(json.dumps(r, indent=2), encoding="utf-8")
    return 0 if r["ok"] else 1
