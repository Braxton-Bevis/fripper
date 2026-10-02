"""Capture README screenshots of the real FRipper window using demo data.

Uses an isolated temporary queue (never .local) with fictional music, so no
personal listening history is published. Windows only (screen capture).
Run: .venv\\Scripts\\python.exe docs\\design\\screenshots.py
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "media"
sys.path.insert(0, str(ROOT))
ctypes.windll.shcore.SetProcessDpiAwareness(1)
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("FRipper.Screenshots")

import gui  # noqa: E402
from manager import QueueManager  # noqa: E402

PLAYLIST = "https://tidal.com/browse/playlist/0f1e2d3c-aaaa-bbbb-cccc-111122223333"
TRACKS = [
    ("Nova Reyes", "Afterglow"), ("The Lantern Club", "Paper Moons"), ("Kito Vale", "Slow Burn"),
    ("Mira Sol", "Coastline"), ("Juno Park", "Neon Hours"), ("Oslo Drift", "Gravity"),
]


def demo_manager(data_dir: Path, filled: bool):
    (data_dir / "tidal").mkdir(parents=True, exist_ok=True)
    (data_dir / "tidal" / "session.bin").write_bytes(b"demo")  # Shows "TIDAL connected".
    manager = QueueManager(data_dir, command_builder=lambda job: [sys.executable, "-c", "pass"])
    if not filled:
        return manager
    running = manager.add("tidal", [PLAYLIST], {"service": "tidal"})[0]
    album = manager.add("albums", ["Nova Reyes - Golden Hour (Deluxe)"], {"service": "qobuz"})[0]
    sets = manager.add("soundcloud", ["https://soundcloud.com/warehouse/sets/sessions-vol-2"], {"service": "soundcloud"})[0]
    tracks = manager.add("tracks", ["Kito Vale - Slow Burn", "Mira Sol - Coastline", "Juno Park - Neon Hours"], {"service": "amazon"})[0]
    queued = manager.add("tidal", ["https://tidal.com/browse/album/123456789"], {"service": "tidal"})[0]
    lines = ["Started playlist", "TIDAL conservative mode: one transfer, 256 KiB/s, at least 5 seconds between track requests.",
             "TIDAL collection: Late Night Drive — 48 exact tracks"]
    for index, (artist, title) in enumerate(TRACKS * 6, 1):
        if index > 31:
            break
        prefix = f"[{index:03d}/48] {artist} - {title}"
        lines.append(f"{prefix} — requesting account audio")
        if index < 31:
            lines.append(f"{prefix} — saved FLAC (LOSSLESS)")
    with manager._lock:
        jobs = {job["id"]: job for job in manager._jobs}
        jobs[running].update(status="running", label="Late Night Drive", logs=lines)
        jobs[album].update(status="completed", label="Golden Hour (Deluxe)",
                           logs=["Summary: 14 downloaded, 0 already complete, 0 failed."])
        jobs[sets].update(status="completed", label="Warehouse Sessions Vol. 2",
                          logs=[f"[{i:03d}/9] saved" for i in range(1, 10)])
        jobs[tracks].update(status="completed", label="3 tracks")
        jobs[queued].update(label="Midnight Static")
        manager._active_id = running
    return manager, running


def capture(path: Path, filled: bool, size="1240x900"):
    data_dir = Path(tempfile.mkdtemp())
    result = demo_manager(data_dir, filled)
    manager, selected = result if filled else (result, None)
    real_snapshot = manager.snapshot

    def snapshot():
        state = real_snapshot()
        state.update(paused=False, running=bool(filled))  # Show a live queue without running jobs.
        return state

    with patch.object(gui, "QueueManager", return_value=manager), patch.object(manager, "snapshot", snapshot):
        app = gui.LucidaDesktop()
        app.geometry(f"{size}+30+30")
        app.attributes("-topmost", True)
        app.output.set(r"C:\Users\you\Music\FRipper")  # Never publish a real profile path.
        if filled:
            app.provider.set("TIDAL account")
            app.input.insert("1.0", "https://tidal.com/browse/playlist/7c1d…\nhttps://tidal.com/browse/album/2840…")
            app.after(400, lambda: app.tree.selection_set(selected))
        else:
            app.provider.set("SoundCloud")

        def shoot():
            app.update()
            x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
            script = (f"Add-Type -AssemblyName System.Drawing; $b=New-Object Drawing.Bitmap {w},{h};"
                      f"$g=[Drawing.Graphics]::FromImage($b); $g.CopyFromScreen({x},{y},0,0,$b.Size); $b.Save('{path}')")
            subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
            with manager._lock:
                manager._active_id = None
            app._closing = True
            manager.close()
            app.destroy()

        app.after(1800, shoot)
        app.mainloop()
    print("Wrote", path)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    capture(OUT / "screenshot-queue.png", filled=True)
    capture(OUT / "screenshot-welcome.png", filled=False)
