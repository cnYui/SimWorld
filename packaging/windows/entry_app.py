"""PyInstaller entry point for emcosim-app.exe: starts the local server and opens the panel in
the default browser. The console window stays open while emcosim runs; closing it quits."""
import multiprocessing
import sys

if __name__ == '__main__':
    multiprocessing.freeze_support()
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW('emcosim（关掉这个窗口就退出）')
        except Exception:
            pass
    from emcosim.app.server import serve
    print('emcosim 正在运行，浏览器里会打开面板。关掉这个窗口就退出。', flush=True)
    sys.exit(serve(sys.argv[1] if len(sys.argv) > 1 else None))
