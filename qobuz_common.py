"""Shared code for the Qobuz Discord Rich Presence synchronizers.

The platform scripts (qobuz.py, macOS.py, longserver.py) only implement how the
currently playing track is detected; everything else lives here.
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox

import requests
from packaging.version import parse as parse_version

# ActivityType requires pypresence >= 4.5
try:
    from pypresence import ActivityType, Presence
except ImportError:
    ActivityType = Presence = None
    RPC_AVAILABLE = False
    print("Warning: pypresence >= 4.5 is missing. RPC functionality disabled.")
else:
    RPC_AVAILABLE = True

# --- Configuration ---
CLIENT_ID = "928957672907227147"
VERSION_URL = "https://raw.githubusercontent.com/Seeyaflying/Qobuz-RPC/main/latest_version.txt"
DOWNLOAD_URL = "https://github.com/Seeyaflying/Qobuz-RPC/releases/latest"
ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
QOBUZ_IDLE_TITLE = "Qobuz"

POLL_INTERVAL = 1.0  # seconds between track checks while Qobuz is open
CLOSED_INTERVAL = 5.0  # seconds between checks while Qobuz is closed
RECONNECT_INTERVAL = 15.0  # seconds between Discord reconnect attempts

# Sentinel returned by poll() while Qobuz is open but nothing is playing
IDLE = "<idle>"


def make_headers(version, platform):
    return {'User-Agent': f'Qobuz-RPC-Sync/{version} ({platform})'}


# --- 1. UPDATE LOGIC ---

def fetch_latest_version(url, headers=None, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=5)
            response.raise_for_status()
            return response.text.strip()
        except requests.exceptions.RequestException:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return None


def check_for_updates_logic(local_version, headers=None):
    remote_version_str = fetch_latest_version(VERSION_URL, headers)
    if remote_version_str is None:
        return {"status": "error", "message": "Update check failed (Network Error)."}
    try:
        if parse_version(remote_version_str) > parse_version(local_version):
            message = (f"🚨 UPDATE AVAILABLE! 🚨\n\nYour version: v{local_version}\n"
                       f"Latest version: v{remote_version_str}\n\n"
                       f"Download here:\n{DOWNLOAD_URL}")
            return {"status": "update", "message": message, "remote_version": remote_version_str}
        return {"status": "ok", "message": f"Running latest version (v{local_version})."}
    except Exception:
        return {"status": "error", "message": "Update check failed (Parsing Error)."}


# --- 2. TRACK HELPERS ---

def parse_title(title):
    """Splits "Song Title - Artist Name" into (song, artist); artist is None if missing."""
    song, sep, artist = title.rpartition(' - ')
    if not sep:
        return title.strip(), None
    return song.strip(), artist.strip() or None


class ArtworkLookup:
    """Looks up album art and track duration via the iTunes search API, with a cache."""

    MAX_CACHE = 500

    def __init__(self, headers=None):
        self.headers = headers
        self._cache = {}

    @staticmethod
    def _key(song, artist):
        return song.casefold(), (artist or "").casefold()

    def is_cached(self, song, artist):
        return self._key(song, artist) in self._cache

    def lookup(self, song, artist):
        """Returns (art_url or None, duration_ms or None)."""
        key = self._key(song, artist)
        if key in self._cache:
            return self._cache[key]

        params = {"term": f"{song} {artist}" if artist else song, "entity": "song", "limit": 1}
        try:
            response = requests.get(ITUNES_SEARCH_URL, params=params, headers=self.headers, timeout=5)
            response.raise_for_status()
            results = response.json().get('results') or []
        except (requests.exceptions.RequestException, ValueError) as e:
            # Not cached, so the next play of this track tries again
            print(f"iTunes API Error: {e}")
            return None, None

        art_url = duration_ms = None
        if results:
            art_url = results[0].get('artworkUrl100')
            if art_url:
                art_url = art_url.replace('100x100bb', '512x512bb')
            duration_ms = results[0].get('trackTimeMillis')

        # Misses are cached too, so tracks without a match are not searched again
        if len(self._cache) >= self.MAX_CACHE:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (art_url, duration_ms)
        return art_url, duration_ms


# --- 3. DISCORD PRESENCE ---

class RPCDisconnected(Exception):
    """The Discord connection was lost; the synchronizer reconnects."""


def _fit(text):
    # Discord rejects activity strings shorter than 2 or longer than 128 characters
    return text[:128].ljust(2)


class PresenceController:
    """Wraps pypresence and shows the track as "Listening to" with a progress bar."""

    def __init__(self, client_id):
        self.client_id = client_id
        self._rpc = None
        self._track = None  # (song, artist) currently shown
        self._started_at = None  # unix time the shown track started
        self._paused = None  # (track, elapsed seconds) of the last paused track

    @property
    def connected(self):
        return self._rpc is not None

    def connect(self):
        # A closed Presence cannot reconnect (its event loop is closed), so always start fresh
        rpc = Presence(self.client_id)
        rpc.connect()
        self._rpc = rpc

    def show(self, song, artist, art_url=None, duration_ms=None):
        track = (song, artist)
        now = time.time()
        if track == self._track and self._started_at:
            start = self._started_at  # re-sent after a reconnect
        elif self._paused and self._paused[0] == track:
            start = now - self._paused[1]  # resumed: continue where it was paused
        else:
            start = now
        self._paused = None

        artist_text = artist or "Unknown Artist"
        activity = {
            "activity_type": ActivityType.LISTENING,
            "details": _fit(song),
            "state": _fit(f"by {artist_text}"),
            "large_image": art_url or "qobuz",
            "large_text": _fit(f"{song} - {artist_text}"),
            "small_image": "qobuz_icon",
            "small_text": "Qobuz Player",
            "start": int(start),
        }
        if duration_ms:
            activity["end"] = int(start + duration_ms / 1000)
        self._call("update", **activity)
        self._track, self._started_at = track, start

    def pause(self):
        if self._track and self._started_at:
            self._paused = (self._track, time.time() - self._started_at)
        self._track = self._started_at = None
        self._call("clear")

    def reset(self):
        self._track = self._started_at = self._paused = None
        self._call("clear")

    def close(self):
        if self._rpc:
            try:
                self._rpc.clear()
                self._rpc.close()
            except Exception as e:
                print(f"Error while closing RPC: {e}")
            self._rpc = None

    def _call(self, method, **kwargs):
        if self._rpc is None:
            raise RPCDisconnected()
        try:
            getattr(self._rpc, method)(**kwargs)
        except Exception as e:
            self._rpc = None
            raise RPCDisconnected(e) from e


# --- 4. RPC SYNCHRONIZER THREAD ---

class BaseSynchronizer(threading.Thread):
    """Polls Qobuz and mirrors the current track to Discord.

    Subclasses implement poll(), returning None (Qobuz closed), IDLE (open but
    paused/idle) or a "Song Title - Artist Name" string. Only this thread
    touches the Discord connection; the GUI is updated through app.set_status().
    """

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.presence = PresenceController(CLIENT_ID)
        self.artwork = ArtworkLookup(app.http_headers)
        self.last_state = None
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        self._needs_refresh = True

    def poll(self):
        raise NotImplementedError

    def stop(self):
        self._stop_event.set()
        self._wake.set()

    def wake(self):
        self._wake.set()

    def _wait(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def run(self):
        while not self._stop_event.is_set():
            if not self.presence.connected and not self._connect():
                self._wait(RECONNECT_INTERVAL)
                continue

            try:
                state = self.poll()
            except Exception as e:
                print(f"Track detection failed: {e}")
                state = None

            try:
                delay = self._apply(state)
            except RPCDisconnected as e:
                print(f"Discord connection lost: {e}")
                self.app.set_status("Discord connection lost. Reconnecting...", "fail")
                self._needs_refresh = True
                continue
            self._wait(delay)

        self.presence.close()
        self.app.set_status("Stopped")

    def _connect(self):
        self.app.set_status("Connecting to Discord...")
        try:
            self.presence.connect()
        except Exception as e:
            print(f"RPC connection failed: {e}")
            self.app.set_status("Discord not found. Retrying...", "fail")
            return False
        self.app.set_status("Connected. Waiting for Qobuz...", "ok")
        self._needs_refresh = True
        return True

    def _apply(self, state):
        """Pushes a changed state to Discord and returns the delay until the next poll."""
        changed = self._needs_refresh or state != self.last_state
        self.last_state = state
        self._needs_refresh = False

        if state is None:
            if changed:
                self.presence.reset()
                self.app.set_status("Qobuz closed. Waiting...")
            return CLOSED_INTERVAL

        if changed:
            if state == IDLE:
                self.presence.pause()
                self.app.set_status("Qobuz: Idle/Paused")
            else:
                self._show(state)
        return POLL_INTERVAL

    def _show(self, title):
        song, artist = parse_title(title)
        if not self.artwork.is_cached(song, artist):
            self.app.set_status(f"Searching art for '{song}'...")
        art_url, duration_ms = self.artwork.lookup(song, artist)
        self.presence.show(song, artist, art_url, duration_ms)
        self.app.set_status(f"Listening: {song}", "ok")


# --- 5. TKINTER GUI ---

class QobuzRPCApp:
    BG = '#36393F'
    COLORS = {'info': '#FFFFFF', 'ok': '#43B581', 'fail': '#F04747'}
    FONT_MAIN = ('Inter', 12)

    def __init__(self, master, synchronizer_cls, version, platform):
        self.master = master
        self.synchronizer_cls = synchronizer_cls
        self.version = version
        self.http_headers = make_headers(version, platform)
        self.rpc_thread = None
        self._events = queue.Queue()

        master.title(f"Qobuz Discord RPC (v{version})")
        master.geometry("550x450")
        master.resizable(False, False)
        master.configure(bg=self.BG)
        master.protocol("WM_DELETE_WINDOW", self.on_close)

        frame = tk.Frame(master, bg=self.BG, padx=20, pady=20)
        frame.pack(fill='both', expand=True)

        def label(text, fg='#C0C4CC', font=self.FONT_MAIN, pady=0, **kwargs):
            widget = tk.Label(frame, text=text, bg=self.BG, fg=fg, font=font, **kwargs)
            widget.pack(fill='x', pady=pady)
            return widget

        label("Qobuz Discord Rich Presence", fg='#FFFFFF', font=('Inter', 18, 'bold'), pady=(0, 5))
        label("Click Start RPC to show Qobuz as \"Listening to\" on Discord.", pady=(0, 20))
        label("Discord Requirement:", fg='#FFFFFF', pady=(5, 0))
        label("Must enable 'Display current activity as a status message' in Discord's "
              "User Settings > Activity Privacy.", font=('Inter', 9), wraplength=500, justify=tk.CENTER)
        label("Current Status:", fg='#FFFFFF', pady=(20, 5))

        self.status_var = tk.StringVar(value="Ready to Start")
        self.status_label = tk.Label(frame, textvariable=self.status_var, bg=self.BG,
                                     fg=self.COLORS['ok'], font=('Inter', 14, 'bold'))
        self.status_label.pack(fill='x')

        button_frame = tk.Frame(frame, bg=self.BG, pady=20)
        button_frame.pack()
        self.start_button = self._button(button_frame, "Start RPC", self.start_rpc, '#7289DA')
        self.stop_button = self._button(button_frame, "Stop RPC", self.stop_rpc, '#F04747', state=tk.DISABLED)

        self.version_var = tk.StringVar(value=f"Local Version: {version}")
        tk.Label(frame, textvariable=self.version_var, bg=self.BG, fg='#C0C4CC',
                 font=('Inter', 10)).pack(pady=(0, 5))
        tk.Button(frame, text="Check for Updates", command=self.check_for_updates, width=25,
                  bg='#5865F2', fg='#FFFFFF', font=('Inter', 11), relief='flat',
                  activebackground='#5865F2', activeforeground='#FFFFFF', cursor="hand2").pack()

        if not RPC_AVAILABLE:
            self._set_status("Missing Dependencies (pypresence >= 4.5)", "fail")
            self.start_button.config(state=tk.DISABLED)

        self._process_events()
        self.check_for_updates()

    def _button(self, parent, text, command, color, state=tk.NORMAL):
        button = tk.Button(parent, text=text, command=command, width=15, height=2, bg=color,
                           fg='#FFFFFF', font=self.FONT_MAIN, relief='flat', activebackground=color,
                           activeforeground='#FFFFFF', state=state, cursor="hand2")
        button.pack(side=tk.LEFT, padx=15)
        return button

    # --- Thread-safe bridge: worker threads queue GUI work, the Tk thread runs it ---

    def call_in_gui(self, func, *args):
        self._events.put((func, args))

    def set_status(self, message, kind="info"):
        self.call_in_gui(self._set_status, message, kind)

    def _set_status(self, message, kind="info"):
        self.status_var.set(message)
        self.status_label.config(fg=self.COLORS.get(kind, self.COLORS['info']))

    def _process_events(self):
        try:
            while True:
                func, args = self._events.get_nowait()
                func(*args)
        except queue.Empty:
            pass
        self.master.after(100, self._process_events)

    # --- Update check ---

    def check_for_updates(self):
        def worker():
            self.call_in_gui(self._handle_update_result, check_for_updates_logic(self.version, self.http_headers))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_update_result(self, info):
        if info["status"] == "update":
            self.version_var.set(f"Local Version: {self.version} · Update available: v{info['remote_version']}")
            messagebox.showinfo("Update Available", info["message"])
        elif info["status"] == "ok":
            self.version_var.set(f"Local Version: {self.version} · up to date")
        else:
            self.version_var.set(f"Local Version: {self.version} · {info['message']}")

    # --- RPC control ---

    @property
    def running(self):
        return self.rpc_thread is not None

    def start_rpc(self):
        if self.running or not RPC_AVAILABLE:
            return
        self.rpc_thread = self.synchronizer_cls(self)
        self.rpc_thread.start()
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)

    def stop_rpc(self):
        if not self.running:
            return
        self.rpc_thread.stop()
        # Give the worker time to clear the presence before the process may exit
        self.rpc_thread.join(timeout=3)
        self.rpc_thread = None
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self._set_status("Stopped")

    def on_close(self):
        self.stop_rpc()
        self.master.destroy()


def run_app(synchronizer_cls, version, platform, app_cls=QobuzRPCApp):
    root = tk.Tk()
    app_cls(root, synchronizer_cls, version, platform)
    root.mainloop()
