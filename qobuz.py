# --- Nuitka Build Configuration (Hints) ---
# nuitka-project: --standalone
# nuitka-project: --msvc=latest
# nuitka-project: --windows-console-mode=disable
# nuitka-project: --enable-plugin=tk-inter
# nuitka-project: --include-package=pypresence
# nuitka-project: --include-package=requests
# nuitka-project: --include-package=psutil
# nuitka-project: --include-package=packaging
# nuitka-project: --company-name="Seeyaflying"
# nuitka-project: --product-name="Qobuz-RPC"
# nuitka-project: --file-description="Discord Rich Presence for Qobuz"
# nuitka-project: --file-version=1.0.0
# nuitka-project: --output-dir=dist
# nuitka-project: --remove-output
# -------------------------------------------

from qobuz_common import IDLE, QOBUZ_IDLE_TITLE, BaseSynchronizer, run_app

LOCAL_VERSION = "1.0.1"
QOBUZ_PROCESS_NAME = "Qobuz.exe"

try:
    import psutil
    import win32gui
    import win32process
except ImportError:
    psutil = win32gui = win32process = None
    print("Warning: psutil/pywin32 missing. Qobuz window detection disabled.")


class WindowsSynchronizer(BaseSynchronizer):
    """Reads the current track from the Qobuz window title ("Song Title - Artist Name")."""

    def __init__(self, app):
        super().__init__(app)
        self._pid = None
        self._hwnd = None

    def _is_qobuz_window(self, hwnd, pid):
        return (win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd)
                and win32process.GetWindowThreadProcessId(hwnd)[1] == pid)

    def _find_window(self):
        pids = {proc.info['pid'] for proc in psutil.process_iter(['name', 'pid'])
                if proc.info['name'] == QOBUZ_PROCESS_NAME}
        if not pids:
            return None, None

        found = []

        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                pid = win32process.GetWindowThreadProcessId(hwnd)[1]
                if pid in pids:
                    found.append((hwnd, pid))
                    return False  # stop enumerating
            return True

        try:
            win32gui.EnumWindows(callback, None)
        except win32gui.error:
            pass  # EnumWindows reports an error when the callback stops it early
        return found[0] if found else (None, None)

    def poll(self):
        if psutil is None:
            return None

        # Reuse the window found last time instead of scanning all processes every second
        if not (self._hwnd and self._is_qobuz_window(self._hwnd, self._pid)):
            self._hwnd, self._pid = self._find_window()
            if not self._hwnd:
                return None

        title = win32gui.GetWindowText(self._hwnd).strip()
        if not title:
            return self.last_state  # title briefly empty while Qobuz redraws
        return IDLE if title == QOBUZ_IDLE_TITLE else title


if __name__ == '__main__':
    run_app(WindowsSynchronizer, LOCAL_VERSION, "Windows")
