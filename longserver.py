# --- Nuitka Build Configuration (Hints) ---
# nuitka-project: --standalone
# nuitka-project: --deployment
# nuitka-project: --msvc=latest
# nuitka-project: --windows-console-mode=disable
# nuitka-project: --enable-plugin=tk-inter
# nuitka-project: --include-package=pypresence
# nuitka-project: --include-package=requests
# nuitka-project: --include-package=psutil
# nuitka-project: --include-package=packaging
# nuitka-project: --include-package=flask
# nuitka-project: --include-package=werkzeug
# nuitka-project: --company-name="Seeyaflying"
# nuitka-project: --product-name="Qobuz-RPC"
# nuitka-project: --file-description="Discord Rich Presence for Qobuz"
# nuitka-project: --file-version=1.0.1
# nuitka-project: --output-dir=dist
# nuitka-project: --remove-output
# -------------------------------------------

import threading
from tkinter import messagebox

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from qobuz import WindowsSynchronizer
from qobuz_common import IDLE, QobuzRPCApp, run_app

LOCAL_VERSION = "1.0.1"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
ALLOWED_HOSTS = {f"127.0.0.1:{SERVER_PORT}", f"localhost:{SERVER_PORT}"}
EXTENSION_ORIGINS = ("chrome-extension://", "moz-extension://")

_NO_UPDATE = object()


class RemoteSynchronizer(WindowsSynchronizer):
    """Window title detection plus track titles pushed through the local HTTP server.

    A pushed title stays until the Qobuz window title changes again.
    """

    def __init__(self, app):
        super().__init__(app)
        self._lock = threading.Lock()
        self._remote = _NO_UPDATE
        self._last_window_state = _NO_UPDATE

    def submit(self, title):
        """Called from the server thread; an empty title clears the presence."""
        with self._lock:
            self._remote = title or IDLE
        self.wake()

    def poll(self):
        with self._lock:
            remote, self._remote = self._remote, _NO_UPDATE
        if remote is not _NO_UPDATE:
            return remote

        window_state = super().poll()
        if window_state == self._last_window_state:
            return self.last_state  # keep a pushed title until the window changes
        self._last_window_state = window_state
        return window_state


def _is_trusted_request():
    """Rejects requests a website could send from the browser (CSRF / DNS rebinding)."""
    if request.host not in ALLOWED_HOSTS or not request.is_json:
        return False
    origin = request.headers.get("Origin")
    return origin is None or origin.startswith(EXTENSION_ORIGINS)


class ServerApp(QobuzRPCApp):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._server = None

    def _create_flask_app(self):
        flask_app = Flask(__name__)

        @flask_app.before_request
        def reject_untrusted():
            if not _is_trusted_request():
                return jsonify({"status": "error", "message": "Forbidden"}), 403

        @flask_app.route('/update', methods=['POST'])
        def update_presence_route():
            data = request.get_json(silent=True)
            title = data.get('title') if isinstance(data, dict) else None
            if not isinstance(title, str):
                return jsonify({"status": "error", "message": "Missing 'title'"}), 400
            synchronizer = self.rpc_thread
            if synchronizer is None:
                return jsonify({"status": "error", "message": "RPC is not running"}), 503
            synchronizer.submit(title.strip())
            return jsonify({"status": "ok"}), 200

        @flask_app.route('/shutdown', methods=['POST'])
        def shutdown():
            self.call_in_gui(self.on_close)
            return jsonify({"status": "ok", "message": "Shutting down"}), 200

        return flask_app

    def start_server(self):
        # The server keeps running across Stop/Start, so it is only created once
        if self._server is not None:
            return
        try:
            self._server = make_server(SERVER_HOST, SERVER_PORT, self._create_flask_app(), threaded=True)
        except OSError as e:
            print(f"Could not start server: {e}")
            messagebox.showwarning("Server Error", f"Could not start the local server on port {SERVER_PORT} "
                                                   f"(already in use?). Window title detection still works.")
            return
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def start_rpc(self):
        super().start_rpc()
        if self.running:
            self.start_server()

    def on_close(self):
        if self._server is not None:
            # shutdown() blocks until serve_forever returns, so never call it from a request thread
            threading.Thread(target=self._server.shutdown, daemon=True).start()
        super().on_close()


if __name__ == '__main__':
    run_app(RemoteSynchronizer, LOCAL_VERSION, "Windows", app_cls=ServerApp)
