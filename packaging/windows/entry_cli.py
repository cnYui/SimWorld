"""PyInstaller entry point for emcosim.exe (console: CLI and the MCP server)."""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    from emcosim.cli import main
    sys.exit(main())
