"""Desktop integration checks with isolated state and no network access."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import gui
from manager import QueueManager


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = QueueManager(Path(self.temp.name), command_builder=lambda job: [sys.executable, "-c", "print('test')"])
        with patch.object(gui, "QueueManager", return_value=self.manager):
            self.app = gui.LucidaDesktop()
        self.app.attributes("-alpha", 0)
        self.app.update_idletasks()

    def tearDown(self):
        self.app._close()
        self.temp.cleanup()

    def test_startup_and_controls_fit_window(self):
        # This constructs the actual Tk subclass, catching collisions with Tk
        # internals as well as clipping at the supported minimum window size.
        for source, mode in (("Amazon Music", "TIDAL links"), ("SoundCloud", "SoundCloud links"), ("TIDAL account", "TIDAL links")):
            self.app.provider.set(source)
            self.app.mode.set(mode)
            for size in ("1200x820", "960x740"):
                with self.subTest(source=source, size=size):
                    self._assert_controls_fit(size)

    def _assert_controls_fit(self, size):
        self.app.geometry(size)
        self.app.update_idletasks()
        left, top = self.app.winfo_rootx(), self.app.winfo_rooty()
        right, bottom = left + self.app.winfo_width(), top + self.app.winfo_height()
        pending = list(self.app.winfo_children())
        while pending:
            widget = pending.pop()
            pending.extend(widget.winfo_children())
            if not widget.winfo_ismapped():
                continue
            self.assertGreaterEqual(widget.winfo_rootx(), left, str(widget))
            self.assertGreaterEqual(widget.winfo_rooty(), top, str(widget))
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right + 1, str(widget))
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), bottom + 1, str(widget))
            parent = widget.master
            self.assertGreaterEqual(widget.winfo_rootx(), parent.winfo_rootx(), str(widget))
            self.assertGreaterEqual(widget.winfo_rooty(), parent.winfo_rooty(), str(widget))
            self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), parent.winfo_rootx() + parent.winfo_width() + 1, str(widget))
            self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), parent.winfo_rooty() + parent.winfo_height() + 1, str(widget))

    def test_soundcloud_source_switching_and_set_queue_use_matching_backend(self):
        self.app.provider.set("TIDAL account")
        self.app.provider.set("SoundCloud")
        self.assertEqual(self.app.mode.get(), "SoundCloud links")
        self.assertEqual(str(self.app.preview_button.cget("state")), "normal")
        self.assertIn("SoundCloud tracks or sets", self.app.input_hint.cget("text"))

        # Choosing a TIDAL source mode while viewing SoundCloud must leave the
        # SoundCloud provider, otherwise valid pasted TIDAL links get rejected.
        self.app.mode.set("TIDAL links")
        self.assertEqual(self.app.provider.get(), "TIDAL account")
        self.app.provider.set("SoundCloud")
        url = "https://soundcloud.com/example-artist/sets/example-set"
        self.app.input.insert("1.0", url)
        with patch.object(gui.messagebox, "showerror") as error:
            self.app._add()
        error.assert_not_called()
        state = self.manager.snapshot()
        self.assertEqual(len(state["jobs"]), 1)
        job = state["jobs"][0]
        self.assertEqual(job["kind"], "playlist")
        self.assertEqual(job["options"]["service"], "soundcloud")
        self.assertEqual(job["inputs"], [url])
        self.assertEqual(job["status"], "queued")
        self.assertFalse(state["running"])
        command = self.manager.build_command(job)
        self.assertIn("soundcloud_direct", command)
        self.assertEqual(command[-1], url)

        self.app.provider.set("TIDAL account")
        self.assertEqual(self.app.mode.get(), "TIDAL links")
        self.assertEqual(self.manager.snapshot()["jobs"][0]["options"]["service"], "soundcloud")
        self.app.provider.set("SoundCloud")
        self.app.provider.set("Amazon Music")
        self.assertEqual(self.app.mode.get(), "Tracks")

    def test_start_and_resume_check_queued_source_after_controls_change(self):
        self.manager.add("soundcloud", ["https://soundcloud.com/example/sets/music"], {"service": "soundcloud"})
        self.app.provider.set("TIDAL account")
        state = self.manager.snapshot()
        state.update(tidal_ready=False, soundcloud_ready=True, client_ready=False)
        with patch.object(self.manager, "snapshot", return_value=state), \
                patch.object(self.manager, "start") as start, \
                patch.object(gui.messagebox, "showinfo") as info:
            self.app._start()
            self.app._snapshot = state
            self.app._pause()
        self.assertEqual(start.call_count, 2)
        info.assert_not_called()

    def test_add_batch_saves_without_starting(self):
        self.app.mode.set("Tracks")
        self.app.input.insert("1.0", "# sample list\nArtist - First\nArtist - Second")
        with patch.object(gui.messagebox, "showerror") as error:
            self.app._add()
            error.assert_not_called()
        state = self.manager.snapshot()
        self.assertEqual(state["jobs"][0]["inputs"], ["Artist - First", "Artist - Second"])
        self.assertEqual(state["jobs"][0]["status"], "queued")
        self.assertFalse(state["running"])
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertEqual(self.app.input.get("1.0", "end").strip(), "")

    def test_queue_order_and_persistence_notice_are_visible(self):
        track = self.manager.add("tracks", ["Artist - Title"])[0]
        self.app._render_snapshot()
        utility = self.manager.add("doctor")[0]
        self.app._render_snapshot()
        self.assertEqual(self.app.tree.get_children()[:2], (utility, track))
        state = self.manager.snapshot()
        state["notice"] = "Could not save queue state: disk full"
        with patch.object(self.manager, "snapshot", return_value=state):
            self.app._render_snapshot()
        self.assertIn("disk full", self.app.notice.get())

    def test_pasted_links_select_matching_link_type(self):
        self.app.provider.set("Amazon Music")
        self.app.mode.set("Tracks")
        self.app.input.insert("1.0", "https://soundcloud.com/artist/sets/mix\nhttps://on.soundcloud.com/AbCdE")
        self.app._detect_links()
        self.assertEqual(self.app.mode.get(), "SoundCloud links")
        self.assertEqual(self.app.provider.get(), "SoundCloud")
        self.assertIn("2 SoundCloud links", self.app.input_count.get())

        self.app.input.delete("1.0", "end")
        self.app.input.insert("1.0", "https://tidal.com/browse/track/123\nhttps://tidal.com/browse/album/456")
        self.app._detect_links()
        self.assertEqual(self.app.mode.get(), "TIDAL links")
        self.assertEqual(self.app.provider.get(), "TIDAL account")

        # Mixed or search text keeps the user's chosen mode.
        self.app.mode.set("Tracks")
        self.app.input.delete("1.0", "end")
        self.app.input.insert("1.0", "Artist - Title\nhttps://tidal.com/browse/track/123")
        self.app._detect_links()
        self.assertEqual(self.app.mode.get(), "Tracks")

    def test_running_job_progress_and_provider_status(self):
        job = {"id": "a" * 32, "label": "Mix", "status": "running", "kind": "playlist", "options": {"service": "tidal"},
               "logs": ["[001/77] A - B — saved M4A (HIGH)", "[024/77] C - D — requesting account audio"]}
        self.assertEqual(gui.LucidaDesktop._progress(job), (23, 77, "C - D — requesting account audio"))
        self.app._render_progress(job)
        self.app.update_idletasks()
        self.assertTrue(self.app.progress_frame.winfo_ismapped())
        self.assertEqual(float(self.app.progress_bar.cget("value")), 23)
        self.assertIn("23 of 77", self.app.progress_text.get())
        self.app._render_progress(None)
        self.app.update_idletasks()
        self.assertFalse(self.app.progress_frame.winfo_ismapped())

        state = self.manager.snapshot()
        state.update(tidal_ready=True, tidal_connected=True)
        self.app._render_status(state)
        self.assertIn("connected", self.app.status_pills["tidal"].cget("text"))
        self.assertEqual(self.app.tidal_button.cget("text"), "Reconnect TIDAL")
        self.assertEqual(self.app.title(), gui.APP_NAME)


if __name__ == "__main__":
    unittest.main()
