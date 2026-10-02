"""FRipper: native desktop interface for the local download queue manager."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from manager import QueueManager, ROOT


APP_NAME = "FRipper"
APP_ID = "FRipper.Desktop"
# Window title used before the rename; still matched so an older open window gets focused.
LEGACY_TITLE = "Lucida Desktop"
ASSETS = ROOT / "assets"

BG = "#0b0b0d"
PANEL = "#17171a"
RAISED = "#222226"
FIELD = "#0e0e10"
BORDER = "#343439"
TEXT = "#f5f5f7"
MUTED = "#aaaab2"
FAINT = "#707079"
ACCENT = "#ffffff"
ACCENT_DARK = "#101012"
SELECT = "#36363d"
SUCCESS = "#8fdcb4"  # Job state only; identity stays black and white.
DANGER = "#f4a5a0"
WARN = "#e6c488"
INFO = "#a5d8f0"

_PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")


def _open_path(path):
    """Show a folder in Explorer, Finder or the Linux file manager."""
    if os.name == "nt":
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class LucidaDesktop(tk.Tk):
    MODES = {"TIDAL links": "tidal", "SoundCloud links": "soundcloud", "Tracks": "tracks", "Albums": "albums", "Playlists": "playlist"}
    PROVIDERS = {"TIDAL account": "tidal", "SoundCloud": "soundcloud", "Amazon Music": "amazon", "Qobuz": "qobuz", "GrilledCheese": "grilledcheese"}
    FORMATS = {"Original quality": "original", "MP3": "mp3", "FLAC": "flac", "AAC": "aac",
               "Opus": "opus", "OGG": "ogg", "WAV": "wav"}
    SOURCE_NAMES = {"tidal": "TIDAL", "soundcloud": "SoundCloud", "amazon": "Amazon", "qobuz": "Qobuz", "grilledcheese": "GrilledCheese"}
    KIND_NAMES = {"tracks": "Track", "albums": "Album", "playlist": "Playlist", "setup": "Setup", "doctor": "Check", "tidal_login": "Sign-in"}

    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        # Tk scales fonts by DPI; pixel paddings and sizes are scaled by hand.
        self._scale = max(1.0, self.winfo_fpixels("1i") / 96)
        width, height = self._fit_screen(1200, 820)
        self.geometry(f"{width}x{height}")
        self.minsize(self._s(960), self._s(740))
        self.configure(bg=BG)
        self._set_icon()
        self.manager = QueueManager()
        initial = self.manager.snapshot()
        settings = initial.get("settings", {})
        self._snapshot = initial
        self._jobs = {}
        self._log_key = None
        self._save_after = None
        self._poll_after = None
        self._detect_after = None
        self._closing = False
        self._backend_notice = ""
        self._setup_style()

        self.mode = tk.StringVar(value="SoundCloud links" if settings.get("service") == "soundcloud" else "TIDAL links")
        self.provider = tk.StringVar(value=self._display(self.PROVIDERS, settings.get("service", "amazon")))
        self.audio_format = tk.StringVar(value=self._display(self.FORMATS, settings.get("format", "original")))
        self.bitrate = tk.StringVar(value=settings.get("bitrate", "320k"))
        self.parallel = tk.StringVar(value=str(settings.get("jobs", 3)))
        self.output = tk.StringVar(value=str(settings.get("output") or ROOT / "downloads"))
        # Fresh installs live in an app folder; put their music somewhere people look.
        music = Path.home() / "Music"
        if (not initial.get("jobs") and not (ROOT / "downloads").exists() and music.is_dir()
                and Path(self.output.get()).resolve() == (ROOT / "downloads").resolve()):
            self.output.set(str(music / "FRipper"))
        self.keep_original = tk.BooleanVar(value=bool(settings.get("keep_original", False)))
        self.flat = tk.BooleanVar(value=bool(settings.get("flat", False)))
        self.notice = tk.StringVar(value="Ready when you are. Paste links to build your queue.")
        self.queue_summary = tk.StringVar(value="No jobs yet")
        self.activity = tk.StringVar(value="READY")
        self.log_title = tk.StringVar(value="Job details")
        self.progress_text = tk.StringVar(value="")
        self.input_count = tk.StringVar(value="Ctrl + Enter to add")
        self._build()
        if not initial.get("client_ready", True):
            self.notice.set("First run: open Install client.cmd in this folder, then reopen " + APP_NAME + ".")
        for variable in (self.provider, self.audio_format, self.bitrate, self.parallel,
                         self.output, self.keep_original, self.flat):
            variable.trace_add("write", self._schedule_save)
        self.mode.trace_add("write", self._mode_changed)
        self.provider.trace_add("write", self._source_changed)
        self._mode_changed()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Control-Return>", lambda _event: self._add())
        self._refresh()

    # ----- helpers -------------------------------------------------------

    def _s(self, value):
        return int(round(value * self._scale))

    def _fit_screen(self, width, height):
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        return min(self._s(width), int(screen_w * 0.92)), min(self._s(height), int(screen_h * 0.88))

    def _set_icon(self):
        try:
            if os.name == "nt" and (ASSETS / "fripper.ico").is_file():
                self.iconbitmap(default=str(ASSETS / "fripper.ico"))
            elif (ASSETS / "fripper.png").is_file():
                self._icon_image = tk.PhotoImage(file=str(ASSETS / "fripper.png"))
                self.iconphoto(True, self._icon_image)
        except tk.TclError:
            pass  # A missing or unreadable icon must never block startup.

    @staticmethod
    def _display(mapping, value):
        return next((label for label, internal in mapping.items() if internal == value), next(iter(mapping)))

    @staticmethod
    def _status_label(job):
        status = str(job.get("status", "queued"))
        if status == "failed":
            for line in reversed(job.get("logs") or []):
                summary = re.search(r"Summary: (\d+) downloaded, (\d+) already complete, (\d+) failed", str(line))
                if summary:
                    downloaded, existing, failures = map(int, summary.groups())
                    complete = downloaded + existing
                    if complete and failures:
                        return f"Partial {complete}/{complete + failures}"
                    break
        return status.capitalize()

    @staticmethod
    def _progress(job):
        """Return (finished, total, current line) parsed from "[007/77]" log lines."""
        for line in reversed(job.get("logs") or []):
            match = _PROGRESS.search(str(line))
            if match:
                index, total = int(match.group(1)), int(match.group(2))
                finished = index - 1 if "requesting" in str(line) else index
                current = str(line)[match.end():].strip()
                return max(0, min(finished, total)), total, current
        return None

    @staticmethod
    def _classify_links(lines):
        lines = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
        if not lines:
            return None, 0
        hosts = []
        for line in lines:
            match = re.match(r"https?://([^/\s]+)", line, re.IGNORECASE)
            hosts.append(match.group(1).lower() if match else "")
        if all(host.endswith("tidal.com") for host in hosts):
            return "tidal", len(lines)
        if all(host.endswith("soundcloud.com") for host in hosts):
            return "soundcloud", len(lines)
        return None, len(lines)

    # ----- style and layout ---------------------------------------------

    def _setup_style(self):
        s = self._s
        base = "Segoe UI" if "Segoe UI" in tkfont.families(self) else tkfont.nametofont("TkDefaultFont").actual("family")
        strong = "Segoe UI Semibold" if "Segoe UI Semibold" in tkfont.families(self) else base
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family=base, size=10)
        self.fonts = {
            "title": (strong, 17), "section": (strong, 11), "small": (base, 9), "tiny": (base, 8),
            "strong": (strong, 10), "mono": (next((f for f in ("Cascadia Mono", "Consolas", "Menlo") if f in tkfont.families(self)), "Courier"), 9),
        }
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", SELECT)
        self.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        self.option_add("*TCombobox*Listbox.font", (base, 10))
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, font=(base, 10), bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, troughcolor=PANEL, focuscolor=ACCENT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=PANEL)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Faint.TLabel", background=PANEL, foreground=FAINT, font=self.fonts["tiny"])
        style.configure("Field.TLabel", background=PANEL, foreground=MUTED, font=self.fonts["small"])
        style.configure("Title.TLabel", font=self.fonts["title"])
        style.configure("Section.TLabel", background=PANEL, font=self.fonts["section"])
        style.configure("Status.TLabel", background=SELECT, foreground=ACCENT, padding=(s(10), s(4)),
                        font=(strong, 8))
        style.configure("Progress.TLabel", background=BG, foreground=TEXT, font=self.fonts["small"])
        style.configure("TButton", background=RAISED, foreground=TEXT, borderwidth=0, relief="flat",
                        focusthickness=1, focuscolor=ACCENT, padding=(s(12), s(7)))
        style.map("TButton", background=[("pressed", BORDER), ("active", "#2c2c31"), ("disabled", PANEL)],
                  foreground=[("disabled", FAINT)])
        style.configure("Card.TButton", background=RAISED)
        style.configure("Accent.TButton", background=ACCENT, foreground=ACCENT_DARK, font=self.fonts["strong"])
        style.map("Accent.TButton", background=[("pressed", "#bcbcc4"), ("active", "#e1e1e6"), ("disabled", "#3a3a42")],
                  foreground=[("disabled", MUTED)])
        style.configure("Ghost.TButton", background=BG, foreground=MUTED, padding=(s(10), s(6)))
        style.map("Ghost.TButton", background=[("active", RAISED)], foreground=[("active", TEXT)])
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, padding=s(6))
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        style.configure("TCombobox", fieldbackground=FIELD, background=RAISED, foreground=TEXT,
                        arrowcolor=MUTED, padding=s(5), arrowsize=s(13))
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)], arrowcolor=[("active", TEXT)], background=[("active", BORDER)])
        style.configure("TSpinbox", fieldbackground=FIELD, background=RAISED, foreground=TEXT,
                        arrowcolor=MUTED, padding=s(5), arrowsize=s(11))
        style.map("TSpinbox", fieldbackground=[("disabled", PANEL)], foreground=[("disabled", FAINT)])
        style.configure("TCheckbutton", background=PANEL, foreground=MUTED, indicatorbackground=FIELD,
                        indicatorforeground=ACCENT, indicatorsize=s(13), indicatormargin=(0, 0, s(7), 0),
                        padding=(0, s(4)))
        style.map("TCheckbutton", background=[("active", PANEL)], foreground=[("active", TEXT)],
                  indicatorbackground=[("selected", SELECT)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        borderwidth=0, rowheight=s(32), font=(base, 10))
        style.configure("Treeview.Heading", background=PANEL, foreground=FAINT, relief="flat",
                        font=(strong, 8), borderwidth=0, padding=(s(6), s(6)))
        style.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
        style.map("Treeview.Heading", background=[("active", PANEL)])
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        for orient in ("Vertical", "Horizontal"):
            style.configure(f"{orient}.TScrollbar", background=RAISED, troughcolor=PANEL, bordercolor=PANEL,
                            lightcolor=RAISED, darkcolor=RAISED, arrowcolor=MUTED, gripcount=0)
            style.map(f"{orient}.TScrollbar", background=[("active", BORDER)])
        style.configure("Accent.Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                        lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=FIELD, thickness=s(6))
        style.configure("TPanedwindow", background=BG)
        style.configure("Sash", sashthickness=s(10), background=BG, gripcount=0)

    def _card(self, parent, **grid):
        card = ttk.Frame(parent, style="Card.TFrame", padding=self._s(16))
        card.grid(**grid)
        return card

    def _pill(self, parent):
        return tk.Label(parent, text="", bg=RAISED, fg=MUTED, font=self.fonts["tiny"],
                        padx=self._s(9), pady=self._s(3), bd=0)

    def _build(self):
        s = self._s
        shell = ttk.Frame(self, padding=(s(22), s(16), s(22), s(10)))
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(0, weight=1)
        shell.rowconfigure(3, weight=1)

        # Header: identity on the left, account/provider status on the right.
        header = ttk.Frame(shell)
        header.grid(row=0, column=0, sticky="ew", pady=(0, s(14)))
        header.columnconfigure(1, weight=1)
        # Wordmark: the FR monogram followed by "ipper" (assets/build_fripper_wordmark.ps1).
        self._logo = None
        sizes = sorted((int(m.group(1)), path) for path in ASSETS.glob("fripper-wordmark-*.png")
                       if (m := re.fullmatch(r"fripper-wordmark-(\d+)\.png", path.name)))
        if sizes:
            try:
                _, wordmark = min(sizes, key=lambda item: abs(item[0] - s(38)))
                self._logo = tk.PhotoImage(file=str(wordmark))
                tk.Label(header, image=self._logo, bg=BG, bd=0).grid(row=0, column=0, columnspan=2, sticky="sw")
            except tk.TclError:
                self._logo = None
        if self._logo is None:
            ttk.Label(header, text=APP_NAME, style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="sw")
        ttk.Label(header, text="FUND RAVERS / Your music, one queue.", style="Muted.TLabel", font=self.fonts["small"]).grid(row=1, column=0, columnspan=2, sticky="nw", pady=(s(4), 0))
        pills = ttk.Frame(header)
        pills.grid(row=0, column=2, sticky="e")
        self.status_pills = {}
        for key in ("tidal", "soundcloud", "lucida"):
            pill = self._pill(pills)
            pill.pack(side="left", padx=(s(6), 0))
            self.status_pills[key] = pill
        ttk.Label(pills, textvariable=self.activity, style="Status.TLabel").pack(side="left", padx=(s(10), 0))
        actions = ttk.Frame(header)
        actions.grid(row=1, column=2, sticky="e", pady=(s(8), 0))
        self.tidal_button = ttk.Button(actions, text="Connect TIDAL", command=lambda: self._utility("tidal_login"))
        self.tidal_button.pack(side="left")
        ttk.Button(actions, text="Set up Lucida", command=lambda: self._utility("setup")).pack(side="left", padx=(s(6), 0))
        ttk.Button(actions, text="Diagnostics", command=lambda: self._utility("doctor")).pack(side="left", padx=(s(6), 0))

        # Compose row: paste box and download settings.
        compose = ttk.Frame(shell)
        compose.grid(row=1, column=0, sticky="ew")
        compose.columnconfigure(0, weight=3, uniform="compose")
        compose.columnconfigure(1, weight=2, uniform="compose")
        add_card = self._card(compose, row=0, column=0, sticky="nsew", padx=(0, s(12)))
        add_card.columnconfigure(0, weight=1)
        add_header = ttk.Frame(add_card, style="Card.TFrame")
        add_header.grid(row=0, column=0, sticky="ew")
        add_header.columnconfigure(0, weight=1)
        ttk.Label(add_header, text="Add music", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(add_header, textvariable=self.mode, values=list(self.MODES), state="readonly", width=15).grid(row=0, column=1, padx=(s(12), s(6)))
        ttk.Button(add_header, text="Import .txt", command=self._import_text).grid(row=0, column=2)
        self.input_hint = ttk.Label(add_card, text="", style="Field.TLabel")
        self.input_hint.grid(row=1, column=0, sticky="w", pady=(s(8), s(6)))
        input_frame = ttk.Frame(add_card, style="Card.TFrame")
        input_frame.grid(row=2, column=0, sticky="nsew")
        input_frame.columnconfigure(0, weight=1)
        self.input = tk.Text(input_frame, height=5, width=34, wrap="word", bg=FIELD, fg=TEXT,
                             insertbackground=ACCENT, selectbackground=SELECT, selectforeground=TEXT,
                             relief="flat", highlightthickness=1, highlightbackground=BORDER,
                             highlightcolor=ACCENT, padx=s(10), pady=s(8), undo=True, font=self.fonts["small"])
        self.input.grid(row=0, column=0, sticky="nsew")
        input_scroll = ttk.Scrollbar(input_frame, command=self.input.yview)
        input_scroll.grid(row=0, column=1, sticky="ns")
        self.input.configure(yscrollcommand=input_scroll.set)
        self.input.bind("<<Modified>>", self._input_modified)
        input_actions = ttk.Frame(add_card, style="Card.TFrame")
        input_actions.grid(row=3, column=0, sticky="ew", pady=(s(10), 0))
        ttk.Button(input_actions, text="Add to queue", style="Accent.TButton", command=self._add).pack(side="left")
        self.preview_button = ttk.Button(input_actions, text="Preview playlist", command=lambda: self._add(preview=True))
        self.preview_button.pack(side="left", padx=(s(6), 0))
        ttk.Label(input_actions, textvariable=self.input_count, style="Faint.TLabel").pack(side="right")

        options = self._card(compose, row=0, column=1, sticky="nsew")
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)
        ttk.Label(options, text="Download settings", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, s(8)))
        ttk.Label(options, text="Audio source", style="Field.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(options, text="Format", style="Field.TLabel").grid(row=1, column=1, sticky="w", padx=(s(10), 0))
        ttk.Combobox(options, textvariable=self.provider, values=list(self.PROVIDERS), state="readonly", width=14).grid(row=2, column=0, sticky="ew", pady=(s(4), s(8)))
        ttk.Combobox(options, textvariable=self.audio_format, values=list(self.FORMATS), state="readonly", width=14).grid(row=2, column=1, sticky="ew", padx=(s(10), 0), pady=(s(4), s(8)))
        settings_row = ttk.Frame(options, style="Card.TFrame")
        settings_row.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Label(settings_row, text="Parallel", style="Field.TLabel").pack(side="left")
        self.parallel_control = ttk.Spinbox(settings_row, from_=1, to=8, width=3, textvariable=self.parallel)
        self.parallel_control.pack(side="left", padx=(s(8), s(16)))
        ttk.Label(settings_row, text="Bitrate", style="Field.TLabel").pack(side="left")
        self.bitrate_control = ttk.Combobox(settings_row, textvariable=self.bitrate, values=("320k", "256k", "192k", "128k"), state="readonly", width=6)
        self.bitrate_control.pack(side="left", padx=(s(8), 0))
        ttk.Label(options, text="Save music to", style="Field.TLabel").grid(row=4, column=0, columnspan=2, sticky="w", pady=(s(10), s(4)))
        destination = ttk.Frame(options, style="Card.TFrame")
        destination.grid(row=5, column=0, columnspan=2, sticky="ew")
        destination.columnconfigure(0, weight=1)
        ttk.Entry(destination, textvariable=self.output, width=18).grid(row=0, column=0, sticky="ew")
        ttk.Button(destination, text="Browse", command=self._browse).grid(row=0, column=1, padx=(s(6), 0))
        ttk.Button(destination, text="Open", command=self._open_output).grid(row=0, column=2, padx=(s(6), 0))
        switches = ttk.Frame(options, style="Card.TFrame")
        switches.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(s(6), 0))
        ttk.Checkbutton(switches, text="Keep original after converting", variable=self.keep_original).pack(side="left")
        ttk.Checkbutton(switches, text="Flat folders", variable=self.flat).pack(side="right")

        # Queue controls with live progress for the running job.
        toolbar = ttk.Frame(shell)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(s(14), s(10)))
        toolbar.columnconfigure(4, weight=1)
        self.start_button = ttk.Button(toolbar, text="▶  Start queue", style="Accent.TButton", command=self._start)
        self.start_button.grid(row=0, column=0)
        self.pause_button = ttk.Button(toolbar, text="Pause after current", command=self._pause)
        self.pause_button.grid(row=0, column=1, padx=(s(6), 0))
        self.stop_button = ttk.Button(toolbar, text="Stop current", command=self._stop)
        self.stop_button.grid(row=0, column=2, padx=(s(6), 0))
        self.retry_button = ttk.Button(toolbar, text="Retry failed", command=self._retry)
        self.retry_button.grid(row=0, column=3, padx=(s(6), 0))
        self.progress_frame = ttk.Frame(toolbar)
        self.progress_frame.grid(row=0, column=4, sticky="ew", padx=(s(18), 0))
        self.progress_frame.columnconfigure(0, weight=1)
        self.progress_label = ttk.Label(self.progress_frame, textvariable=self.progress_text, style="Progress.TLabel", anchor="w")
        self.progress_label.grid(row=0, column=0, sticky="ew")
        self.progress_bar = ttk.Progressbar(self.progress_frame, style="Accent.Horizontal.TProgressbar", mode="determinate", maximum=1)
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(s(4), 0))
        self.progress_frame.grid_remove()

        split = ttk.Panedwindow(shell, orient="horizontal")
        split.grid(row=3, column=0, sticky="nsew")
        queue_card = ttk.Frame(split, style="Card.TFrame", padding=(s(14), s(12), s(10), s(8)))
        queue_card.columnconfigure(0, weight=1)
        queue_card.rowconfigure(1, weight=1)
        queue_header = ttk.Frame(queue_card, style="Card.TFrame")
        queue_header.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, s(8)))
        queue_header.columnconfigure(1, weight=1)
        ttk.Label(queue_header, text="Your queue", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(queue_header, textvariable=self.queue_summary, style="Field.TLabel").grid(row=0, column=1, sticky="w", padx=(s(10), 0))
        self.remove_button = ttk.Button(queue_header, text="Remove", command=self._remove)
        self.remove_button.grid(row=0, column=2, padx=(s(6), 0))
        self.clear_button = ttk.Button(queue_header, text="Clear completed", command=self._clear_completed)
        self.clear_button.grid(row=0, column=3, padx=(s(6), 0))
        self.tree = ttk.Treeview(queue_card, columns=("label", "source", "kind", "progress", "status"), show="headings", selectmode="extended", height=7)
        for column, heading, width, stretch in (("label", "MUSIC / TASK", 240, True), ("source", "SOURCE", 84, False),
                                                ("kind", "TYPE", 66, False), ("progress", "PROGRESS", 74, False),
                                                ("status", "STATUS", 96, False)):
            self.tree.heading(column, text=heading, anchor="w")
            self.tree.column(column, width=s(width), minwidth=s(min(width, 60)), stretch=stretch)
        self.tree.grid(row=1, column=0, sticky="nsew")
        queue_scroll = ttk.Scrollbar(queue_card, command=self.tree.yview)
        queue_scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=queue_scroll.set)
        self.tree.tag_configure("completed", foreground=SUCCESS)
        self.tree.tag_configure("failed", foreground=DANGER)
        self.tree.tag_configure("running", foreground=INFO)
        self.tree.tag_configure("cancelled", foreground=MUTED)
        self.tree.tag_configure("interrupted", foreground=WARN)
        self.tree.tag_configure("odd", background="#1b1b1e")
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)
        self.tree.bind("<Delete>", lambda _event: self._remove())
        self.tree.bind("<Double-1>", self._open_job_folder)
        self.tree.bind("<Button-3>", self._show_menu)
        self.empty_label = ttk.Label(self.tree, text="", style="CardMuted.TLabel", justify="center", anchor="center")
        self.menu = tk.Menu(self, tearoff=False, bg=RAISED, fg=TEXT, activebackground=SELECT, activeforeground=TEXT,
                            bd=0, relief="flat", font=self.fonts["small"])
        self.menu.add_command(label="Open download folder", command=self._open_job_folder)
        self.menu.add_command(label="Copy links", command=self._copy_links)
        self.menu.add_separator()
        self.menu.add_command(label="Retry failed jobs", command=self._retry)
        self.menu.add_command(label="Remove from queue", command=self._remove)

        log_card = ttk.Frame(split, style="Card.TFrame", padding=(s(14), s(12), s(10), s(10)))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        ttk.Label(log_card, textvariable=self.log_title, style="Section.TLabel", anchor="w").grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, s(8)))
        self.log = tk.Text(log_card, width=32, height=9, wrap="word", bg=FIELD, fg=MUTED,
                           insertbackground=TEXT, selectbackground=SELECT, selectforeground=TEXT,
                           relief="flat", borderwidth=0, padx=s(10), pady=s(10), state="disabled",
                           font=self.fonts["mono"], spacing1=s(1), spacing3=s(1))
        self.log.grid(row=1, column=0, sticky="nsew")
        self.log.tag_configure("head", foreground=TEXT, font=self.fonts["strong"])
        self.log.tag_configure("ok", foreground=SUCCESS)
        self.log.tag_configure("err", foreground=DANGER)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("info", foreground=INFO)
        log_scroll = ttk.Scrollbar(log_card, command=self.log.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)
        split.add(queue_card, weight=3)
        split.add(log_card, weight=2)

        footer = ttk.Frame(shell)
        footer.grid(row=4, column=0, sticky="ew", pady=(s(10), 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.notice, style="Muted.TLabel", wraplength=s(660), font=self.fonts["small"]).grid(row=0, column=0, sticky="w")
        self.source_caption = ttk.Label(footer, text="", style="Muted.TLabel", font=self.fonts["tiny"])
        self.source_caption.grid(row=0, column=1, sticky="e", padx=(s(14), 0))

    # ----- source and mode coupling --------------------------------------

    def _mode_changed(self, *_args):
        if self.mode.get() == "SoundCloud links" and self.provider.get() != "SoundCloud":
            self.provider.set("SoundCloud")
        elif self.mode.get() == "TIDAL links" and self.provider.get() == "SoundCloud":
            self.provider.set("TIDAL account")
        direct = self.provider.get() == "TIDAL account"
        soundcloud = self.provider.get() == "SoundCloud"
        limited = direct or soundcloud
        if limited and self.parallel.get() != "1":
            self.parallel.set("1")
        self.parallel_control.configure(state="disabled" if limited else "normal")
        converting = self.FORMATS.get(self.audio_format.get()) not in ("original", "flac", "wav")
        self.bitrate_control.configure(state="readonly" if converting else "disabled")
        limits = "One at a time, 256 KB/s cap, 5s between tracks."
        detail = limits if direct else "Audio is matched on the selected source."
        hints = {"TIDAL links": "TIDAL tracks, albums, or playlists; one link per line.\n" + detail,
                 "SoundCloud links": "SoundCloud tracks or sets; one link per line.\n" + limits,
                 "Tracks": "One track URL or Artist - Title per line.",
                 "Albums": "One album URL or Artist - Album per line.",
                 "Playlists": "One playlist URL per line. Preview lists its tracks."}
        if direct:
            hints.update({"Tracks": "One TIDAL track link per line.\n" + limits,
                          "Albums": "One TIDAL album link per line.\n" + limits,
                          "Playlists": "One TIDAL playlist link per line.\n" + limits})
        elif soundcloud:
            hints.update({"Tracks": "One SoundCloud track link per line.\n" + limits,
                          "Albums": "One SoundCloud set or album link per line.\n" + limits,
                          "Playlists": "One SoundCloud set link per line.\n" + limits})
        self.input_hint.configure(text=hints[self.mode.get()])
        self.source_caption.configure(text="Direct TIDAL" if direct else "Direct SoundCloud" if soundcloud else "Via lucida.to")
        self.preview_button.configure(state="normal" if self.mode.get() in ("Playlists", "TIDAL links", "SoundCloud links") else "disabled")

    def _source_changed(self, *_args):
        if self.provider.get() == "SoundCloud":
            self.mode.set("SoundCloud links")
        elif self.mode.get() == "SoundCloud links":
            self.mode.set("TIDAL links" if self.provider.get() == "TIDAL account" else "Tracks")
        self._mode_changed()

    def _input_modified(self, _event=None):
        self.input.edit_modified(False)
        if self._detect_after is not None:
            self.after_cancel(self._detect_after)
        self._detect_after = self.after(250, self._detect_links)

    def _detect_links(self):
        """Pick the matching link type for a pasted batch so it is not rejected."""
        self._detect_after = None
        service, count = self._classify_links(self.input.get("1.0", "end").splitlines())
        if not count:
            self.input_count.set("Ctrl + Enter to add")
            return
        noun = {"tidal": "TIDAL link", "soundcloud": "SoundCloud link"}.get(service, "item")
        self.input_count.set(f"{count} {noun}{'s' if count != 1 else ''}  ·  Ctrl + Enter")
        target = {"tidal": "TIDAL links", "soundcloud": "SoundCloud links"}.get(service)
        if target and self.mode.get() != target:
            self.mode.set(target)
            route = "your TIDAL account" if self.provider.get() == "TIDAL account" else self.provider.get()
            self.notice.set(f"Detected {noun}s. Link type set to {target}; audio comes from {route}.")

    def _download_options(self):
        try:
            count = int(self.parallel.get())
        except ValueError:
            raise ValueError("Choose a parallel download count from 1 to 8.") from None
        if not 1 <= count <= 8:
            raise ValueError("Choose a parallel download count from 1 to 8.")
        destination = self.output.get().strip()
        if not destination:
            raise ValueError("Choose a folder for your downloaded music.")
        return {"service": self.PROVIDERS[self.provider.get()], "format": self.FORMATS[self.audio_format.get()],
                "bitrate": self.bitrate.get(), "jobs": count, "output": str(Path(destination).expanduser().resolve()),
                "keep_original": self.keep_original.get(), "flat": self.flat.get()}

    def _schedule_save(self, *_args):
        if self._save_after is not None:
            self.after_cancel(self._save_after)
        self._save_after = self.after(600, self._save_settings)
        if hasattr(self, "bitrate_control"):
            converting = self.FORMATS.get(self.audio_format.get()) not in ("original", "flac", "wav")
            self.bitrate_control.configure(state="readonly" if converting else "disabled")

    def _save_settings(self):
        self._save_after = None
        try:
            self.manager.update_settings(self._download_options())
        except ValueError:
            pass  # A field can be temporarily empty while the user edits it.
        except Exception as exc:
            self.notice.set(f"Could not save settings: {exc}")

    # ----- actions --------------------------------------------------------

    def _perform(self, action):
        try:
            action()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)

    def _add(self, preview=False):
        def action():
            if self._detect_after is not None:
                self.after_cancel(self._detect_after)
                self._detect_links()
            inputs = [line.strip() for line in self.input.get("1.0", "end").splitlines() if line.strip()]
            if not inputs:
                self.input.focus_set()
                raise ValueError("Paste at least one link or search into Add music first.")
            options = self._download_options()
            self.manager.update_settings(options)
            kind = self.MODES[self.mode.get()]
            ids = self.manager.add(kind, inputs, options, preview=preview)
            if not preview:
                self.input.delete("1.0", "end")
                self.input_count.set("Ctrl + Enter to add")
            self.notice.set(f"Added {len(ids)} {'preview' if preview else 'job'}{'s' if len(ids) != 1 else ''}. Select Start queue when ready.")
            self._render_snapshot()
            if ids:
                self.tree.selection_set(ids[0])
                self.tree.see(ids[0])
        self._perform(action)

    def _utility(self, kind):
        def action():
            if kind == "tidal_login":
                self.provider.set("TIDAL account")
            service = "tidal" if kind == "tidal_login" else "amazon" if kind == "setup" else None
            if not self._require_client(service):
                return
            ids = self.manager.add(kind, [], self._download_options())
            self.manager.start()
            task = "TIDAL sign-in" if kind == "tidal_login" else "Lucida setup" if kind == "setup" else "Diagnostics"
            self.notice.set(f"{task} will run next; the queue pauses afterward. Follow Job details.")
            self._render_snapshot()
            if ids:
                self.tree.selection_set(ids[0])
                self.tree.see(ids[0])
        self._perform(action)

    def _start(self):
        def action():
            if not self._require_client(self._next_queued_service()):
                return
            self.manager.update_settings(self._download_options())
            first = next((job for job in self.manager.snapshot().get("jobs", []) if job.get("status") == "queued"), None)
            self.manager.start()
            if first and self.tree.exists(first["id"]):
                self.tree.selection_set(first["id"])
                self.tree.see(first["id"])
            self.notice.set("Queue started. Select a job to follow its progress.")
        self._perform(action)

    def _next_queued_service(self):
        job = next((job for job in self.manager.snapshot().get("jobs", [])
                    if job.get("status") == "queued"), None)
        if job is None:
            return None
        if job.get("kind") == "tidal_login":
            return "tidal"
        if job.get("kind") == "setup":
            return "amazon"
        return job.get("options", {}).get("service")

    def _require_client(self, service=None):
        service = service or self.PROVIDERS[self.provider.get()]
        key = "tidal_ready" if service == "tidal" else "soundcloud_ready" if service == "soundcloud" else "client_ready"
        if self.manager.snapshot().get(key, True):
            return True
        self.notice.set("Client installation is needed. Open Install client.cmd, then reopen this app.")
        messagebox.showinfo("Install download support", "Open Install client.cmd in the " + APP_NAME + " folder.\n\nWhen installation finishes, reopen this app. Your queued music will be saved.", parent=self)
        return False

    def _pause(self):
        def action():
            if self._snapshot.get("paused"):
                if not self._require_client(self._next_queued_service()):
                    return
                self.manager.start()
                self.notice.set("Queue resumed.")
            else:
                self.manager.pause()
                self.notice.set("The current job will finish, then the queue will pause.")
        self._perform(action)

    def _stop(self):
        def action():
            self.manager.cancel_active()
            self.notice.set("Stopping the current job and pausing the queue. Select Start queue to continue.")
        self._perform(action)

    def _retry(self):
        def action():
            self.manager.retry_failed()
            self.notice.set("Failed, stopped, and interrupted jobs requeued. Select Start queue to run them.")
        self._perform(action)

    def _remove(self):
        selected = list(self.tree.selection())
        if not selected:
            return
        active = self._snapshot.get("active_id")
        eligible = [job_id for job_id in selected if job_id != active]
        if not eligible:
            messagebox.showinfo("Job is running", "Stop the current job before removing it.", parent=self)
            return
        def action():
            self.manager.remove(eligible)
            suffix = " The running job was kept." if active in selected else ""
            self.notice.set(f"Removed {len(eligible)} job{'s' if len(eligible) != 1 else ''}.{suffix}")
            self._render_snapshot()
        self._perform(action)

    def _clear_completed(self):
        def action():
            self.manager.clear_completed()
            self.notice.set("Completed jobs cleared from the list. Your music files are kept.")
            self._render_snapshot()
        self._perform(action)

    def _import_text(self):
        filename = filedialog.askopenfilename(parent=self, title="Import music links", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if filename:
            def action():
                content = Path(filename).read_text(encoding="utf-8-sig")
                if self.input.get("1.0", "end-1c").strip():
                    self.input.insert("end", "\n")
                self.input.insert("end", content)
                self.notice.set(f"Imported {Path(filename).name}. Check the link type, then add to queue.")
            self._perform(action)

    def _browse(self):
        existing = Path(self.output.get()).expanduser()
        initial = existing if existing.is_dir() else ROOT
        folder = filedialog.askdirectory(parent=self, title="Choose download folder", initialdir=str(initial))
        if folder:
            self.output.set(folder)

    def _open_output(self):
        def action():
            folder = Path(self._download_options()["output"])
            if not folder.is_dir():
                raise ValueError("This folder has not been created yet. Start a download or choose an existing folder.")
            _open_path(folder)
        self._perform(action)

    def _selected_job(self):
        selected = self.tree.selection()
        return self._jobs.get(selected[0]) if selected else None

    def _open_job_folder(self, event=None):
        if event is not None and self.tree.identify_region(event.x, event.y) != "cell":
            return
        job = self._selected_job()
        if job is None or not job.get("options", {}).get("output"):
            self._open_output()
            return
        def action():
            folder = Path(job["options"]["output"])
            if not folder.is_dir():
                raise ValueError("Nothing has been saved for this job yet.")
            _open_path(folder)
        self._perform(action)

    def _copy_links(self):
        jobs = [self._jobs[job_id] for job_id in self.tree.selection() if job_id in self._jobs]
        links = [str(item) for job in jobs for item in job.get("inputs") or []]
        if links:
            self.clipboard_clear()
            self.clipboard_append("\n".join(links))
            self.notice.set(f"Copied {len(links)} link{'s' if len(links) != 1 else ''} to the clipboard.")

    def _show_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
        has_job = bool(self.tree.selection())
        for index in (0, 1, 4):
            self.menu.entryconfigure(index, state="normal" if has_job else "disabled")
        self.menu.entryconfigure(3, state="normal" if self.retry_button.instate(["!disabled"]) else "disabled")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ----- rendering -------------------------------------------------------

    def _selection_changed(self, _event=None):
        self.remove_button.configure(state="normal" if self.tree.selection() else "disabled")
        self._render_log()

    @staticmethod
    def _log_tag(line):
        if re.search(r"FAILED|RATE LIMIT|[Ee]rror|Could not|could not|not available|blocked", line):
            return "err"
        if re.search(r"Partial|partial|Stopped|interrupted|unattempted|[Ww]arning", line):
            return "warn"
        if re.search(r"saved |Saved |already complete|connected|Summary: |complete;|Preview complete", line):
            return "ok"
        if line.startswith(("Started", "TIDAL collection", "TIDAL conservative", "SoundCloud")):
            return "info"
        return None

    def _render_log(self):
        job = self._selected_job()
        if job:
            label = str(job.get("label", "Job"))
            self.log_title.set(label[:48] + ("…" if len(label) > 48 else ""))
            lines = [str(line) for line in job.get("logs") or []]
            if not lines:
                lines = (["Waiting in the queue.", "", "Select Start queue to begin."] if job.get("status") == "queued"
                         else ["No output has been recorded yet."])
            head = self._status_label(job).upper()
            key = (job.get("id"), head, tuple(lines))
        else:
            self.log_title.set("Job details")
            if self._snapshot.get("client_ready", True):
                first_run = ["SoundCloud: paste a track or set link.", "TIDAL: select Connect TIDAL and sign in.", "Lucida sources: select Set up Lucida."]
            else:
                first_run = ["Open Install client.cmd in this folder, then reopen " + APP_NAME + "."]
            head = "Select a job to see progress, results, and any errors."
            lines = ["", "First time here?", *first_run, "", "Tip: double-click a job to open its folder;", "right-click for more options."]
            key = (None, head, tuple(lines))
        if key != self._log_key:
            follow = self._log_key is None or key[0] != self._log_key[0] or self.log.yview()[1] >= 0.98
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", head + "\n\n", "head" if job else ())
            for line in lines:
                self.log.insert("end", line + "\n", self._log_tag(line) or ())
            self.log.configure(state="disabled")
            if follow and job:
                self.log.see("end")  # Follow live output; help text reads from the top.
            self._log_key = key

    def _render_status(self, snapshot):
        def paint(key, text, state):
            color = {"ok": SUCCESS, "warn": WARN, "off": FAINT}[state]
            pill = self.status_pills[key]
            if pill.cget("text") != text:
                pill.configure(text=text, fg=color if state != "ok" else TEXT)
        if os.name != "nt":
            paint("tidal", "○  TIDAL  Windows only", "off")
        elif not snapshot.get("tidal_ready", True):
            paint("tidal", "●  TIDAL  not installed", "warn")
        elif snapshot.get("tidal_connected"):
            paint("tidal", "●  TIDAL  connected", "ok")
        else:
            paint("tidal", "○  TIDAL  not connected", "off")
        paint("soundcloud", "●  SoundCloud  ready" if snapshot.get("soundcloud_ready", True) else "●  SoundCloud  not installed",
              "ok" if snapshot.get("soundcloud_ready", True) else "warn")
        paint("lucida", "●  Lucida  installed" if snapshot.get("client_ready", True) else "●  Lucida  not installed",
              "ok" if snapshot.get("client_ready", True) else "warn")
        label = "Reconnect TIDAL" if snapshot.get("tidal_connected") else "Connect TIDAL"
        if self.tidal_button.cget("text") != label:
            self.tidal_button.configure(text=label)

    def _render_snapshot(self):
        snapshot = self.manager.snapshot()
        self._snapshot = snapshot
        backend_notice = snapshot.get("notice", "")
        if backend_notice and backend_notice != self._backend_notice:
            self.notice.set(backend_notice)
        self._backend_notice = backend_notice
        jobs = snapshot.get("jobs", [])
        self._jobs = {str(job["id"]): job for job in jobs}
        existing = set(self.tree.get_children())
        for position, job in enumerate(jobs):
            job_id = str(job["id"])
            kind = self.KIND_NAMES.get(job.get("kind"), str(job.get("kind", "Job")))
            status = str(job.get("status", "queued"))
            source = "—" if job.get("kind") in ("setup", "doctor", "tidal_login") else self.SOURCE_NAMES.get(job.get("options", {}).get("service"), "—")
            progress = self._progress(job)
            progress_text = f"{progress[0]}/{progress[1]}" if progress else ""
            values = (job.get("label", "Untitled"), source, kind, progress_text, self._status_label(job))
            tags = (status, "odd") if position % 2 else (status,)
            if job_id in existing:
                if tuple(self.tree.item(job_id, "values")) != values or tuple(self.tree.item(job_id, "tags")) != tags:
                    self.tree.item(job_id, values=values, tags=tags)
                existing.remove(job_id)
            else:
                self.tree.insert("", "end", iid=job_id, values=values, tags=tags)
            self.tree.move(job_id, "", position)
        for job_id in existing:
            self.tree.delete(job_id)
        if jobs:
            self.empty_label.place_forget()
        else:
            if self.provider.get() == "TIDAL account":
                steps = "1. Connect TIDAL and sign in using your browser\n2. Paste TIDAL track, album, or playlist links\n3. Add to queue, then select Start queue"
            elif self.provider.get() == "SoundCloud":
                steps = "1. Paste SoundCloud track or set links above\n2. Keep Original quality for the best source\n3. Add to queue, then select Start queue"
            else:
                steps = "1. Set up Lucida\n2. Paste track, album, or playlist links above\n3. Add to queue, then select Start queue" if snapshot.get("client_ready", True) else "1. Run Install client.cmd in this folder\n2. Reopen the app and set up Lucida\n3. Paste music, add to queue, then start"
            self.empty_label.configure(text=f"Build your first queue\n\n{steps}")
            self.empty_label.place(relx=0.5, rely=0.5, anchor="center")
        queued = sum(job.get("status") == "queued" for job in jobs)
        done = sum(job.get("status") == "completed" for job in jobs)
        failed = sum(job.get("status") in ("failed", "interrupted", "cancelled") for job in jobs)
        self.queue_summary.set(f"{queued} queued  ·  {done} complete" + (f"  ·  {failed} need attention" if failed else "") if jobs else "No jobs yet")
        active = bool(snapshot.get("active_id"))
        paused = bool(snapshot.get("paused"))
        current_job = self._jobs.get(str(snapshot.get("active_id")), {})
        working = "WORKING" if current_job.get("kind") in ("setup", "doctor", "tidal_login") else "DOWNLOADING"
        self.activity.set("PAUSING" if active and paused else working if active else "PAUSED" if paused and queued else "READY")
        self._render_progress(current_job if active else None)
        self._render_status(snapshot)
        self.start_button.configure(state="normal" if queued and (not snapshot.get("running") or paused) else "disabled")
        self.pause_button.configure(text="Resume queue" if paused and (queued or active) else "Pause after current",
                                    state="normal" if paused and (queued or active) or active or (snapshot.get("running") and not paused) else "disabled")
        self.stop_button.configure(state="normal" if active else "disabled")
        self.retry_button.configure(state="normal" if failed else "disabled")
        self.clear_button.configure(state="normal" if done else "disabled")
        self.remove_button.configure(state="normal" if self.tree.selection() else "disabled")
        self._render_log()

    def _render_progress(self, job):
        if not job:
            self.progress_frame.grid_remove()
            return
        progress = self._progress(job)
        name = str(job.get("label", "Current job"))
        name = name[:40] + ("…" if len(name) > 40 else "")
        if progress:
            finished, total, current = progress
            current = re.sub(r"\s+—\s+(requesting account audio|saved .*|already complete.*)$", "", current)
            text = f"{name}  ·  {finished} of {total}" + (f"  ·  {current[:48]}" if current else "")
            if str(self.progress_bar.cget("mode")) != "determinate":
                self.progress_bar.stop()  # stop() also resets the value, so it must come first.
            self.progress_bar.configure(mode="determinate", maximum=max(total, 1), value=finished)
        else:
            text = f"{name}  ·  working…"
            if str(self.progress_bar.cget("mode")) != "indeterminate":
                self.progress_bar.configure(mode="indeterminate", maximum=100)
                self.progress_bar.start(18)
        if self.progress_text.get() != text:
            self.progress_text.set(text)
        self.progress_frame.grid()

    def _refresh(self):
        if self._closing:
            return
        try:
            self._render_snapshot()
        except Exception as exc:
            self.notice.set(f"Could not refresh queue: {exc}")
        self._poll_after = self.after(500, self._refresh)

    def _close(self):
        snapshot = self.manager.snapshot()
        if snapshot.get("active_id"):
            if not messagebox.askyesno(f"Close {APP_NAME}?", "A job is still running. Closing will stop it.\n\nYour queue will be saved so you can resume later.", parent=self):
                return
        self._closing = True
        for pending in (self._poll_after, self._save_after, self._detect_after):
            if pending is not None:
                self.after_cancel(pending)
        self._save_after = None
        self._save_settings()
        try:
            self.manager.close()
        except Exception as exc:
            self._closing = False
            self._refresh()
            messagebox.showerror("Could not close safely", str(exc), parent=self)
            return
        self.destroy()


def _windows_integration():
    """Sharp text on scaled displays, and one taskbar identity for pinning."""
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _focus_existing_window():
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = user32.FindWindowW("TkTopLevel", APP_NAME) or user32.FindWindowW("TkTopLevel", LEGACY_TITLE)
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def _single_instance():
    """Keep a Windows mutex open for the lifetime of this workspace's GUI."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    identity = hashlib.sha256(str(ROOT.resolve()).lower().encode("utf-8")).hexdigest()[:24]
    handle = kernel.CreateMutexW(None, False, f"Local\\LucidaDesktop_{identity}")
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel.CloseHandle(handle)
        if not _focus_existing_window():
            dialog_root = tk.Tk()
            dialog_root.withdraw()
            messagebox.showinfo(f"{APP_NAME} is already open", f"{APP_NAME} is already running for this folder.\n\nSwitch to its existing window to manage your queue.", parent=dialog_root)
            dialog_root.destroy()
        return False
    return kernel, handle


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} music queue")
    parser.add_argument("--start-queue", action="store_true", help="Start saved queued jobs when this launch opens.")
    arguments = parser.parse_args()
    _windows_integration()
    instance = _single_instance()
    if instance is False:
        return
    try:
        app = LucidaDesktop()
        if arguments.start_queue:
            app.after(500, app._start)
        app.mainloop()
    except Exception as exc:
        messagebox.showerror(f"{APP_NAME} could not start", str(exc))
        raise
    finally:
        if instance:
            kernel, handle = instance
            kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
