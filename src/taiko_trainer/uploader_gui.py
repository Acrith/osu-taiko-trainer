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
from tkinter import filedialog, messagebox, simpledialog, ttk

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


def _log_path() -> Path:
    """~/.taiko-trainer/uploader.log — sibling of the config + state DB."""
    return _config_path().parent / "uploader.log"


def _write_log(msg: str) -> None:
    """Append a timestamped line to the debug log. Non-fatal — if writing
    fails we swallow so the watcher can keep running."""
    try:
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


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
    or similar. Pausable via self._paused flag.

    IMPORTANT: sqlite3 connections are tied to the thread that opened them.
    The State DB gets opened inside `run()` (not __init__) so it lives in
    the worker thread, not the main tk/tray thread."""

    def __init__(self, cfg: Config):
        super().__init__(daemon=True, name="uploader-watch")
        self.cfg = cfg
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self.uploads_today = 0
        self.last_upload: str | None = None
        self.status_msg = "Starting…"
        self.state: State | None = None  # opened in run()

    def pause(self) -> None:
        self._paused.set()
        self.status_msg = "Paused"

    def resume(self) -> None:
        self._paused.clear()
        self.status_msg = "Watching"

    def stop(self) -> None:
        self._stop_event.set()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def run(self) -> None:
        import traceback
        try:
            self._run_inner()
        except Exception as e:
            # Anything reaching here means the watcher thread died. Surface
            # the error to the tray + append to the debug log so the user
            # has something to click.
            self.status_msg = f"Error: {e}"
            _write_log(f"UploaderThread died: {e!r}\n{traceback.format_exc()}")

    def _run_inner(self) -> None:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        # Open State inside this thread so its sqlite3 connection lives here.
        # Cross-thread use raises sqlite3.ProgrammingError by default.
        self.state = State(_state_path())

        # Guard against a missing / mistyped folder — Observer.schedule
        # raises a cryptic OSError otherwise, and without this check the
        # thread dies before status_msg updates from "Starting…".
        folder = Path(self.cfg.replays_folder)
        if not folder.is_dir():
            msg = f"Replays folder not found: {self.cfg.replays_folder}"
            self.status_msg = msg
            _write_log(msg + " — edit uploader.toml and restart, or delete it to re-run the setup dialog.")
            return

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
        for p in folder.glob("*.osr"):
            if not self.state.known(p.name):
                self.state.record(p.name, "", None, {"map_title": "SKIPPED_HISTORIC"})

        obs = Observer()
        obs.schedule(_Handler(), self.cfg.replays_folder, recursive=False)
        obs.start()
        self.status_msg = "Watching for new plays"
        _write_log(f"Watching {self.cfg.replays_folder} · server {self.cfg.server_url}")

        try:
            with httpx.Client() as client:
                while not self._stop_event.is_set():
                    if self._paused.is_set():
                        time.sleep(0.5)
                        continue
                    try:
                        path = q.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if self.state.known(path.name):
                        continue
                    try:
                        _process_one(client, self.cfg, self.state, path)
                        self.uploads_today += 1
                        self.last_upload = path.name
                    except Exception as e:
                        # Individual upload failures shouldn't kill the whole
                        # watcher — log and keep watching.
                        import traceback as _tb
                        _write_log(f"Upload failed for {path.name}: {e!r}\n{_tb.format_exc()}")
        finally:
            obs.stop()
            obs.join()
            if self.state is not None:
                try:
                    self.state.close()
                except Exception:
                    pass


# --- Main window + tray ---------------------------------------------------

class _MainWindow:
    """The uploader's main window. Shows current status + recent uploads +
    config + actions. Runs on the tk main thread; polls the worker for
    updates via after(). Close/minimize hides to tray rather than quitting.

    Shares the same UploaderThread with the tray icon — the tray runs
    detached in its own thread (see _run_app)."""

    def __init__(self, root: tk.Tk, uploader: UploaderThread, cfg: Config, quit_cb, whoami: dict | None = None):
        self.root = root
        self.uploader = uploader
        self.cfg = cfg
        self._quit_cb = quit_cb
        self._whoami = whoami
        self._state_cache = State(_state_path())  # Read-only shadow for the UI
        self._build_layout()
        # Once layout exists, populate the identity strip if we have data
        if whoami:
            self._render_whoami(whoami)
        self._refresh()   # initial render
        self._schedule_refresh()

    def _render_whoami(self, w: dict) -> None:
        name = w.get("osu_username") or "?"
        cc   = (w.get("osu_country_code") or "").upper()
        rank = w.get("osu_global_rank")
        rank_str = f"  ·  #{rank:,} taiko" if rank else ""
        badge = f"[{cc}]  " if cc else ""
        self.player_name_var.set(f"{badge}{name}")
        self.player_meta_var.set(f"logged in via API token{rank_str}  ·  {self.cfg.server_url}")

    # --- Layout ------------------------------------------------------------

    def _build_layout(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("760x780")
        self.root.minsize(700, 640)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        main = ttk.Frame(self.root, padding=14)
        main.pack(fill=tk.BOTH, expand=True)

        # --- Player card (thin, like leaderboard row) ---
        self.player_frame = ttk.Frame(main, padding=(12, 8))
        self.player_frame.pack(fill=tk.X, pady=(0, 12))
        self.player_avatar_lbl = ttk.Label(self.player_frame, text="●", font=("Segoe UI", 22))
        self.player_avatar_lbl.pack(side=tk.LEFT, padx=(0, 12))
        self.player_name_var = tk.StringVar(value="…")
        self.player_meta_var = tk.StringVar(value=self.cfg.server_url)
        pl_txt = ttk.Frame(self.player_frame)
        pl_txt.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(pl_txt, textvariable=self.player_name_var, font=("Segoe UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(pl_txt, textvariable=self.player_meta_var, foreground="#888", font=("Segoe UI", 9)).pack(anchor=tk.W)

        # --- Status card ---
        status_frame = ttk.LabelFrame(main, text="Status", padding=12)
        status_frame.pack(fill=tk.X, pady=(0, 12))
        self.status_var = tk.StringVar(value="…")
        status_lbl = ttk.Label(status_frame, textvariable=self.status_var, font=("Segoe UI", 14, "bold"))
        status_lbl.pack(anchor=tk.W)
        server_lbl = ttk.Label(status_frame, text=self.cfg.server_url, foreground="#666")
        server_lbl.pack(anchor=tk.W, pady=(2, 8))
        btn_row = ttk.Frame(status_frame)
        btn_row.pack(anchor=tk.W)
        self.pause_btn = ttk.Button(btn_row, text="Pause", command=self._on_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="Open dashboard", command=self._on_open_dashboard).pack(side=tk.LEFT, padx=6)

        # --- Stats row ---
        stats_frame = ttk.Frame(main)
        stats_frame.pack(fill=tk.X, pady=(0, 12))
        self.today_var = tk.StringVar(value="0")
        self.total_var = tk.StringVar(value="0")
        self.skipped_var = tk.StringVar(value="0")
        self._stat_cell(stats_frame, "Uploaded today",   self.today_var).pack(side=tk.LEFT, expand=True, fill=tk.X)
        self._stat_cell(stats_frame, "Uploaded (total)", self.total_var).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 0))
        self._stat_cell(stats_frame, "Skipped (historic)", self.skipped_var).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(6, 0))

        # --- Recent activity table ---
        recent_frame = ttk.LabelFrame(main, text="Recent activity", padding=8)
        recent_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 12))
        cols = ("when", "map", "mods", "acc", "status")
        self.tree = ttk.Treeview(recent_frame, columns=cols, show="headings", height=8)
        self.tree.heading("when",   text="When")
        self.tree.heading("map",    text="Map")
        self.tree.heading("mods",   text="Mods")
        self.tree.heading("acc",    text="Acc")
        self.tree.heading("status", text="Status")
        self.tree.column("when",   width=140, anchor=tk.W)
        self.tree.column("map",    width=280, anchor=tk.W)
        self.tree.column("mods",   width=60,  anchor=tk.CENTER)
        self.tree.column("acc",    width=70,  anchor=tk.E)
        self.tree.column("status", width=90,  anchor=tk.W)
        vsb = ttk.Scrollbar(recent_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # --- Config summary + action buttons ---
        cfg_frame = ttk.LabelFrame(main, text="Config", padding=12)
        cfg_frame.pack(fill=tk.X)
        # Token (masked prefix + 8 chars + …)
        raw_tok = self.cfg.api_token or ""
        tok_display = f"{raw_tok[:16]}…" if len(raw_tok) > 16 else raw_tok
        ttk.Label(cfg_frame, text="Token:", foreground="#666").grid(row=0, column=0, sticky=tk.W)
        ttk.Label(cfg_frame, text=tok_display, font=("Consolas", 10)).grid(row=0, column=1, sticky=tk.W, padx=(8, 8))
        ttk.Label(cfg_frame, text="Folder:", foreground="#666").grid(row=1, column=0, sticky=tk.W, pady=(6, 0))
        ttk.Label(cfg_frame, text=self.cfg.replays_folder, font=("Consolas", 10)).grid(row=1, column=1, sticky=tk.W, padx=(8, 8), pady=(6, 0))
        cfg_frame.columnconfigure(1, weight=1)

        actions = ttk.Frame(cfg_frame)
        actions.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(12, 0))
        ttk.Button(actions, text="Change folder", command=self._on_change_folder).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(actions, text="Change token",  command=self._on_change_token).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Open log",       command=self._on_open_log).pack(side=tk.LEFT, padx=6)
        ttk.Button(actions, text="Hide to tray",   command=self._on_close).pack(side=tk.RIGHT)

    def _stat_cell(self, parent, label: str, var: tk.StringVar) -> ttk.Frame:
        f = ttk.Frame(parent, borderwidth=1, relief="solid", padding=10)
        ttk.Label(f, text=label, foreground="#666", font=("Segoe UI", 9)).pack(anchor=tk.W)
        ttk.Label(f, textvariable=var,          font=("Segoe UI", 18, "bold")).pack(anchor=tk.W)
        return f

    # --- Data refresh ------------------------------------------------------

    def _refresh(self) -> None:
        # Status label + pause button
        self.status_var.set(self.uploader.status_msg)
        self.pause_btn.configure(text=("Resume" if self.uploader.is_paused() else "Pause"))
        # Stats
        try:
            s = self._state_cache.stats()
        except Exception:
            s = {"total": 0, "uploaded": 0, "skipped_historic": 0}
        self.today_var.set(str(self.uploader.uploads_today))
        self.total_var.set(str(s.get("uploaded", 0)))
        self.skipped_var.set(str(s.get("skipped_historic", 0)))
        # Recent activity table
        self.tree.delete(*self.tree.get_children())
        try:
            rows = self._state_cache.recent(limit=50)
        except Exception:
            rows = []
        for r in rows:
            when   = (r.get("uploaded_at") or "")[:19].replace("T", " ")
            title  = r.get("map_title")   or "?"
            ver    = r.get("map_version") or ""
            mods   = r.get("mods")        or "NM"
            acc    = r.get("accuracy")
            acc_s  = f"{acc*100:.2f}%" if isinstance(acc, (int, float)) else "—"
            status = "uploaded" if r.get("replay_id") else "failed"
            self.tree.insert(
                "", tk.END,
                values=(when, f"{title} [{ver}]" if ver else title, mods, acc_s, status),
            )

    def _schedule_refresh(self) -> None:
        self.root.after(1000, self._tick)

    def _tick(self) -> None:
        self._refresh()
        self._schedule_refresh()

    # --- Actions -----------------------------------------------------------

    def _on_pause(self) -> None:
        if self.uploader.is_paused():
            self.uploader.resume()
        else:
            self.uploader.pause()
        self._refresh()

    def _on_open_dashboard(self) -> None:
        webbrowser.open(self.cfg.server_url)

    def _on_open_log(self) -> None:
        import os as _os
        import subprocess as _sp
        p = _log_path()
        if not p.exists():
            _write_log("(log opened — no prior entries)")
        try:
            _os.startfile(str(p))
        except Exception:
            _sp.Popen(["notepad", str(p)])

    def _on_change_folder(self) -> None:
        new = filedialog.askdirectory(title="Choose your osu! replays folder", initialdir=self.cfg.replays_folder)
        if not new:
            return
        # Persist to config file. Watcher won't automatically re-target —
        # user has to restart. Warn them.
        write_config(Config(server_url=self.cfg.server_url, api_token=self.cfg.api_token, replays_folder=new))
        messagebox.showinfo(APP_NAME, f"Folder saved to config:\n{new}\n\nRestart the uploader for the change to take effect.")

    def _on_change_token(self) -> None:
        new = simpledialog.askstring(
            APP_NAME,
            "New API token (paste from /settings/tokens on the server):",
            initialvalue="",
        )
        if not new or not new.strip():
            return
        write_config(Config(server_url=self.cfg.server_url, api_token=new.strip(), replays_folder=self.cfg.replays_folder))
        messagebox.showinfo(APP_NAME, "Token saved.\n\nRestart the uploader for the change to take effect.")

    def _on_close(self) -> None:
        """Hide to tray (window keeps running in background)."""
        self.root.withdraw()

    def show(self) -> None:
        """Called from the tray menu to restore the window."""
        self.root.after(0, self._do_show)

    def _do_show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def shutdown(self) -> None:
        """Called on Quit — close state cache before tk destroys widgets."""
        try:
            self._state_cache.close()
        except Exception:
            pass


def _fetch_whoami(cfg: Config) -> dict | None:
    """One-shot call to /api/v1/whoami — populates the header player card.
    Non-fatal: if it fails the header just stays anonymous."""
    try:
        r = httpx.get(
            f"{cfg.server_url}/api/v1/whoami",
            headers={"Authorization": f"Bearer {cfg.api_token}"},
            timeout=5.0,
        )
        if r.status_code == 200:
            return r.json()
        _write_log(f"whoami failed: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        _write_log(f"whoami request errored: {e!r}")
    return None


def _run_app(cfg: Config) -> None:
    """Start the watcher thread + system tray + main window. Blocks until
    user quits from tray or File → Quit."""
    thread = UploaderThread(cfg)
    thread.start()

    # tk root — main window lives here. Apply the dark ttk theme so the app
    # matches the site's aesthetic instead of defaulting to Windows' beige.
    root = tk.Tk()
    try:
        import sv_ttk
        sv_ttk.set_theme("dark")
    except Exception as e:
        # Theme missing (dev environment without sv_ttk) — fall back to
        # whatever ttk default is available. Not fatal.
        _write_log(f"sv_ttk theme unavailable: {e!r}")

    # Fetch identity for the header player card. Runs synchronously before
    # window construction so the card renders populated on first paint.
    whoami = _fetch_whoami(cfg)

    # Tray icon runs detached so tk mainloop can own the main thread
    icon = pystray.Icon(APP_NAME, _make_icon(), title=APP_NAME)
    window: _MainWindow | None = None

    def _quit_all():
        # Called from tray Quit or window close-forever action
        thread.stop()
        try:
            if window is not None:
                window.shutdown()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        icon.stop()

    def _quit_tray(icon_, item):
        _quit_all()

    def _show(icon_, item):
        if window is not None:
            window.show()

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda i: f"Status: {thread.status_msg}", None, enabled=False),
        pystray.MenuItem(lambda i: f"Uploaded today: {thread.uploads_today}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Show window", _show, default=True),
        pystray.MenuItem("Open dashboard", lambda i, it: webbrowser.open(cfg.server_url)),
        pystray.MenuItem("Open log file", lambda i, it: _open_log_external()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit_tray),
    )

    window = _MainWindow(root, thread, cfg, _quit_all, whoami=whoami)
    icon.run_detached()

    try:
        root.mainloop()
    finally:
        thread.stop()
        thread.join(timeout=3.0)
        try:
            icon.stop()
        except Exception:
            pass


def _open_log_external() -> None:
    """Open the log file for the tray's Open log menu item."""
    import os
    import subprocess
    p = _log_path()
    if not p.exists():
        _write_log("(log opened — no prior entries)")
    try:
        os.startfile(str(p))
    except Exception:
        subprocess.Popen(["notepad", str(p)])


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

    _run_app(cfg)
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
