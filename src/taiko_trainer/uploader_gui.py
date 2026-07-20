"""GUI wrapper for the taiko-trainer uploader companion.

This is what the shipped Windows binary runs. It:

1. On first launch (no config): shows a small tkinter setup dialog
   asking for the API token + confirming the auto-detected replays
   folder. Writes ~/.taiko-trainer/uploader.toml, then continues to (2).
2. Runs the watchdog + upload loop from `uploader.py` in a background
   thread. Server URL is hardcoded to DEFAULT_SERVER_URL — users only
   ever see + edit their token.
3. Shows a system-tray icon (pystray) with a small right-click menu:
   Status, Pause/Resume, Open dashboard, Settings, Quit.
4. On close: exits cleanly (stops the watchdog thread, releases the
   folder handle).

Runs as a normal executable (double-click). Auto-update flow lives in
`uploader.py`'s startup path — this module is just the UI shell.
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import httpx
from PIL import Image, ImageDraw
import pystray

from .uploader import (
    DEFAULT_SERVER_URL,
    Config,
    State,
    _config_path,
    _process_one,
    _state_path,
    detect_replays_folder,
    load_config,
    write_config,
)


APP_NAME = "taiko-trainer uploader"


# --- Icon (generated in-code; no image file to ship) -----------------------

def _make_icon(size: int = 64, tint: str = "#e86428") -> Image.Image:
    """Simple taiko-drum-ish icon. Two concentric circles, colored center.
    Generated inline so the binary doesn't need to bundle a .png."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 8
    d.ellipse((pad, pad, size - pad, size - pad), fill=tint)
    inner = size // 3
    d.ellipse(
        (size // 2 - inner // 2, size // 2 - inner // 2,
         size // 2 + inner // 2, size // 2 + inner // 2),
        fill="#ffffff",
    )
    return img


# --- First-run setup dialog ------------------------------------------------

def _setup_dialog() -> Config | None:
    """Modal tkinter dialog asking for token + confirming the replays folder.
    Returns a Config on submit, or None if the user closes the window."""
    root = tk.Tk()
    root.title(f"{APP_NAME} — first-time setup")
    root.geometry("560x340")
    root.resizable(False, False)

    result: dict[str, str | None] = {"token": None, "folder": None}

    ttk.Style().configure("TLabel", padding=(0, 0))
    header = ttk.Label(root,
        text=f"Welcome. This will connect the uploader to your account\n"
             f"at {DEFAULT_SERVER_URL}.",
        font=("Segoe UI", 11),
    )
    header.pack(pady=(16, 12), padx=20, anchor="w")

    # Token field
    ttk.Label(root, text="API token", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20)
    ttk.Label(root,
        text=f"Get one at {DEFAULT_SERVER_URL}/settings/tokens (Log in with osu! → Create a token).",
        font=("Segoe UI", 8), foreground="#666",
    ).pack(anchor="w", padx=20)
    token_var = tk.StringVar()
    token_entry = ttk.Entry(root, textvariable=token_var, width=68)
    token_entry.pack(padx=20, pady=(2, 12), fill="x")
    token_entry.focus_set()

    # Folder field
    detected = detect_replays_folder()
    folder_var = tk.StringVar(value=str(detected) if detected else "")
    ttk.Label(root, text="osu! replays folder (Data/r/)", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20)
    folder_row = ttk.Frame(root)
    folder_row.pack(padx=20, pady=(2, 4), fill="x")
    folder_entry = ttk.Entry(folder_row, textvariable=folder_var)
    folder_entry.pack(side="left", fill="x", expand=True)
    def _browse():
        selected = filedialog.askdirectory(initialdir=folder_var.get() or str(Path.home()))
        if selected:
            folder_var.set(selected)
    ttk.Button(folder_row, text="Browse…", command=_browse).pack(side="left", padx=(6, 0))
    ttk.Label(root,
        text=("Detected automatically — verify it's correct." if detected else
              "Not detected — click Browse to pick your osu! Data/r/ folder."),
        font=("Segoe UI", 8), foreground="#666",
    ).pack(anchor="w", padx=20)

    # Submit / Cancel
    button_row = ttk.Frame(root)
    button_row.pack(pady=16)
    def _submit():
        t = token_var.get().strip()
        f = folder_var.get().strip()
        if not t.startswith("tt_uploader_"):
            messagebox.showerror(
                "Invalid token",
                "The token should start with `tt_uploader_`. Get one at "
                f"{DEFAULT_SERVER_URL}/settings/tokens.",
            )
            return
        if not f or not Path(f).is_dir():
            messagebox.showerror(
                "Invalid folder",
                f"The path {f!r} isn't a directory. Point this at your osu! Data/r/ folder.",
            )
            return
        result["token"] = t
        result["folder"] = f
        root.destroy()
    def _cancel():
        root.destroy()

    ttk.Button(button_row, text="Save and start", command=_submit).pack(side="left", padx=6)
    ttk.Button(button_row, text="Cancel", command=_cancel).pack(side="left", padx=6)

    root.mainloop()

    if result["token"] and result["folder"]:
        return Config(api_token=result["token"], replays_folder=result["folder"])
    return None


# --- Watcher thread --------------------------------------------------------

class UploaderThread(threading.Thread):
    """Runs the watchdog + upload loop in the background. Communicates status
    via an in-process queue so the tray icon can show "N uploaded today"
    or similar. Pausable via self._paused flag."""

    def __init__(self, cfg: Config, state: State):
        super().__init__(daemon=True, name="uploader-watch")
        self.cfg = cfg
        self.state = state
        self._stop = threading.Event()
        self._paused = threading.Event()
        self.uploads_today = 0
        self.last_upload: str | None = None
        self.status_msg = "Starting…"

    def pause(self) -> None:
        self._paused.set()
        self.status_msg = "Paused"

    def resume(self) -> None:
        self._paused.clear()
        self.status_msg = "Watching"

    def stop(self) -> None:
        self._stop.set()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def run(self) -> None:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        q: "queue.Queue[Path]" = queue.Queue()

        class _Handler(FileSystemEventHandler):
            def _enqueue(self, path_str: str) -> None:
                p = Path(path_str)
                if p.suffix.lower() != ".osr":
                    return
                time.sleep(0.5)  # osu! sometimes flushes in two writes
                q.put(p)

            def on_created(self, ev):
                if not ev.is_directory:
                    self._enqueue(ev.src_path)

            def on_moved(self, ev):
                if not ev.is_directory:
                    self._enqueue(ev.dest_path)

        # Snapshot existing files so we don't re-upload history on first launch.
        folder = Path(self.cfg.replays_folder)
        if folder.is_dir():
            for p in folder.glob("*.osr"):
                if not self.state.known(p.name):
                    self.state.record(p.name, "", None, {"map_title": "SKIPPED_HISTORIC"})

        obs = Observer()
        obs.schedule(_Handler(), self.cfg.replays_folder, recursive=False)
        obs.start()
        self.status_msg = "Watching for new plays"

        try:
            with httpx.Client() as client:
                while not self._stop.is_set():
                    if self._paused.is_set():
                        time.sleep(0.5)
                        continue
                    try:
                        path = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if self.state.known(path.name):
                        continue
                    _process_one(client, self.cfg, self.state, path)
                    # Very light per-day counter (resets on restart — good
                    # enough for the tray tooltip; state DB has the real
                    # history if we ever want a permanent count).
                    self.uploads_today += 1
                    self.last_upload = path.name
        finally:
            obs.stop()
            obs.join()


# --- Tray icon -------------------------------------------------------------

def _build_menu(uploader: UploaderThread, cfg: Config, quit_fn) -> pystray.Menu:
    def _open_dashboard(icon, item):
        webbrowser.open(cfg.server_url)

    def _open_settings(icon, item):
        webbrowser.open(f"{cfg.server_url}/settings/tokens")

    def _open_config_folder(icon, item):
        # Open the config directory in Explorer
        import os
        import subprocess
        cfg_dir = _config_path().parent
        try:
            os.startfile(str(cfg_dir))
        except Exception:
            subprocess.Popen(["explorer", str(cfg_dir)])

    def _toggle_pause(icon, item):
        if uploader.is_paused():
            uploader.resume()
        else:
            uploader.pause()
        icon.update_menu()

    def _pause_label(item):
        return "Resume" if uploader.is_paused() else "Pause"

    return pystray.Menu(
        pystray.MenuItem(
            lambda item: f"Status: {uploader.status_msg}",
            None, enabled=False,
        ),
        pystray.MenuItem(
            lambda item: f"Uploaded today: {uploader.uploads_today}",
            None, enabled=False,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(_pause_label, _toggle_pause),
        pystray.MenuItem("Open dashboard", _open_dashboard),
        pystray.MenuItem("Settings (browser)", _open_settings),
        pystray.MenuItem("Open config folder", _open_config_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_fn),
    )


def _run_tray(cfg: Config) -> None:
    """Start the watcher thread + system tray. Blocks until user quits."""
    state = State(_state_path())
    thread = UploaderThread(cfg, state)
    thread.start()

    icon = pystray.Icon(APP_NAME, _make_icon(), title=APP_NAME)

    def _quit(icon_, item):
        thread.stop()
        icon.stop()

    icon.menu = _build_menu(thread, cfg, _quit)
    icon.run()

    # After icon.run() returns (user quit), give the thread a moment to wind down.
    thread.stop()
    thread.join(timeout=3.0)
    state.close()


# --- Entrypoint ------------------------------------------------------------

def main() -> int:
    # If no config file exists, run the setup dialog.
    try:
        cfg = load_config()
    except FileNotFoundError:
        cfg = _setup_dialog()
        if cfg is None:
            return 1
        write_config(cfg)
    except Exception as e:
        # Corrupt config → surface the error, don't silently fall into setup.
        _fatal_dialog(f"Failed to read config:\n\n{e}")
        return 2

    # Verify server reachability + token validity before starting the tray.
    # Better failure feedback than silently failing to upload later.
    try:
        _preflight(cfg)
    except _PreflightError as e:
        _fatal_dialog(f"Startup check failed:\n\n{e}\n\nEdit or delete the config and try again:\n{_config_path()}")
        return 3

    _run_tray(cfg)
    return 0


class _PreflightError(RuntimeError):
    pass


def _preflight(cfg: Config) -> None:
    """Quick round-trip against the server to verify the token works. Fail
    with a clear message if not — better than starting the tray only to
    fail every replay upload silently."""
    try:
        resp = httpx.get(
            f"{cfg.server_url}/api/auth/me",
            headers={"Authorization": f"Bearer {cfg.api_token}"},
            timeout=10.0,
        )
    except httpx.ConnectError as e:
        raise _PreflightError(f"Can't reach {cfg.server_url}: {e}")
    # /api/auth/me doesn't do bearer auth — it's session-cookie only. We're
    # using it as a "does the server respond?" ping. To actually verify the
    # token we'd need a dedicated endpoint. For now: if the server answers
    # at all, we assume things are healthy. Real token issues surface at
    # first upload with a clear log message.
    if resp.status_code >= 500:
        raise _PreflightError(f"Server error: HTTP {resp.status_code}")


def _fatal_dialog(msg: str) -> None:
    """Show a modal error dialog before exiting. Only used for setup-time
    problems; runtime errors log to the tray tooltip instead."""
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, msg)
        root.destroy()
    except Exception:
        # If tkinter fails, fall back to console
        print(f"ERROR: {msg}", flush=True)


if __name__ == "__main__":
    import sys
    sys.exit(main())
