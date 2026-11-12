"""PyInstaller entry point for emcosim-app.exe (windowed)."""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from emcosim.app.desktop import main
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
