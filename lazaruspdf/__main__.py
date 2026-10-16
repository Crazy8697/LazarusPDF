"""python -m lazaruspdf            -> GUI
python -m lazaruspdf --cli IN.pdf OUTDIR   -> headless extract (prints counts only, never names)"""
import sys


def main():
    if len(sys.argv) >= 5 and sys.argv[1] == "--selftest":
        from .selftest import run
        return run(sys.argv[2], sys.argv[3], sys.argv[4])
    if len(sys.argv) == 2 and sys.argv[1] in ("--register-default", "--unregister-default"):
        from . import assoc                   # the installer's hooks (stage 19); no window
        return assoc.installer(sys.argv[1][2:].split("-")[0])
    if len(sys.argv) == 2 and sys.argv[1] == "--migrate":
        from . import migrate                 # Setup runs this after an upgrade (stage 22); no window
        migrate.run()
        return 0
    if len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        from . import modes, tess
        tess.configure()
        src, out = sys.argv[2], sys.argv[3]

        def prog(done, total, status, gaps, figs):
            print(f"\r{done}/{total} pages · gaps {gaps} · figures {figs}   ", end="", flush=True)

        _, _, (gaps, figs) = modes.run(modes.EXTRACT, src, out, prog)
        print(f"\ndone: {gaps} gap(s), {figs} figure(s)")
        return 0
    # a Lazarus PDF is running already: it opens the PDFs as tabs, this launch ends here (stage 12)
    from . import startup  # noqa: F401  (starts the start-up clock's marks)
    from .instance import hand_off, pdf_args
    paths = pdf_args(sys.argv[1:])
    if hand_off(paths):
        # done: end now. A normal exit can hang on Windows tearing down the named-pipe socket
        # (seen with several launches at once), and this launch has nothing to clean up.
        import os
        os._exit(0)
    from .gui import run
    return run(paths)


if __name__ == "__main__":
    sys.exit(main())
