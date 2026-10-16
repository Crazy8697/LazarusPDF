"""Entry point for the launcher and PyInstaller (no console window)."""
import multiprocessing
import sys

if __name__ == "__main__":                  # (a batch worker process imports this file too)
    multiprocessing.freeze_support()        # frozen exe: a batch worker process runs here and exits
    from lazarus.__main__ import main
    sys.exit(main())
