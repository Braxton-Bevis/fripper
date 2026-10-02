"""Queue tests use tiny local subprocesses; no network or client is required."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from manager import LOG_LIMIT, ROOT, QueueManager, _direct_python


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        self.temporary.cleanup()

    def manager(self, code="print('ready')"):
        manager = QueueManager(self.data, python_executable=sys.executable,
                               command_builder=lambda job: [sys.executable, "-u", "-c", code])
        self.managers.append(manager)
        return manager

    def wait_for(self, manager, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = manager.snapshot()
            if predicate(state):
                return state
            time.sleep(0.02)
        self.fail(f"Queue condition timed out: {manager.snapshot()}")

    def test_persistence_and_no_automatic_start(self):
        manager = self.manager()
        ids = manager.add("tracks", ["Artist - Song", "Another - Song"], {"jobs": 2})
        manager.update_settings({"service": "qobuz"})
        manager.close()
        restored = self.manager()
        state = restored.snapshot()
        self.assertTrue(state["paused"])
        self.assertFalse(state["running"])
        self.assertEqual(state["jobs"][0]["id"], ids[0])
        self.assertEqual(state["jobs"][0]["status"], "queued")
        self.assertEqual(state["settings"]["service"], "qobuz")
        state["jobs"][0]["logs"].append("mutation")
        self.assertNotIn("mutation", restored.snapshot()["jobs"][0]["logs"])

    def test_real_process_success_environment_and_ansi(self):
        manager = self.manager("import os; print('\\x1b[32mhello\\x1b[0m'); print(os.environ['LUCIDADL_HOME']); print(os.environ['PYTHONUTF8'])")
        manager.add("tracks", ["Artist - Song"])
        manager.start()
        state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "completed")
        self.assertIn("hello", state["jobs"][0]["logs"])
        self.assertIn(str(self.data / "client"), state["jobs"][0]["logs"])
        self.assertEqual(state["jobs"][0]["returncode"], 0)
        self.assertTrue((self.data / "logs" / f"{state['jobs'][0]['id']}.log").is_file())

    def test_nonzero_exit_is_failed_and_retry_is_queued(self):
        manager = self.manager("import sys; print('provider error'); sys.exit(7)")
        ids = manager.add("doctor")
        manager.start()
        state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "failed")
        self.assertEqual(state["jobs"][0]["returncode"], 7)
        manager.pause()
        self.assertEqual(manager.retry_failed(), ids)
        self.assertEqual(manager.snapshot()["jobs"][0]["status"], "queued")

    def test_rate_limit_exit_pauses_all_later_jobs(self):
        manager = self.manager("import sys; print('Rate limit; stopped'); sys.exit(75)")
        manager.add("tracks", ["First song"])
        manager.add("tracks", ["Second song"])
        manager.start()
        state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "failed")
        self.assertTrue(state["paused"])
        self.assertFalse(state["running"])
        self.assertEqual(state["jobs"][1]["status"], "queued")
        self.assertIn("rate-limited", state["notice"])

    def test_utilities_run_first_and_do_not_start_pending_downloads(self):
        for kind in ("setup", "doctor", "tidal_login"):
            with self.subTest(kind=kind):
                manager = self.manager()
                manager.add("tracks", ["Queued song"])
                ids = manager.add(kind)
                manager.start()
                state = self.wait_for(manager, lambda state: any(job["id"] in ids and job["status"] == "completed" for job in state["jobs"]))
                self.assertTrue(state["paused"])
                self.assertTrue(all(job["status"] == "queued" for job in state["jobs"] if job["kind"] == "tracks"))
                manager.close()

    def test_persistence_failure_pauses_without_launching_subprocess(self):
        manager = self.manager()
        manager.add("tracks", ["Queued song"])
        with patch.object(manager, "_save_locked", side_effect=OSError("disk full")), patch("manager.subprocess.Popen") as popen:
            manager.start()
            state = self.wait_for(manager, lambda state: "disk full" in state["notice"])
            self.assertTrue(state["paused"])
            self.assertIsNone(state["active_id"])
            self.assertEqual(state["jobs"][0]["status"], "queued")
            popen.assert_not_called()

    def test_pause_allows_current_to_finish_but_preserves_next(self):
        manager = self.manager("import time; print('ready'); time.sleep(0.5)")
        manager.add("tracks", ["First"])
        manager.add("albums", ["Second"])
        manager.start()
        self.wait_for(manager, lambda state: "ready" in state["jobs"][0]["logs"])
        manager.pause()
        state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "completed")
        self.assertEqual(state["jobs"][1]["status"], "queued")
        self.assertTrue(state["paused"])
        self.assertFalse(state["running"])
        manager.start()
        self.wait_for(manager, lambda state: state["jobs"][1]["status"] == "completed")

    def test_cancel_active_stops_process_and_pauses_queue(self):
        manager = self.manager("import time; print('ready'); time.sleep(60)")
        ids = manager.add("tracks", ["First"])
        manager.add("albums", ["Second"])
        manager.start()
        self.wait_for(manager, lambda state: "ready" in state["jobs"][0]["logs"])
        manager.remove(ids)
        self.assertEqual(len(manager.snapshot()["jobs"]), 2)
        manager.cancel_active()
        state = self.wait_for(manager, lambda state: state["active_id"] is None)
        self.assertEqual(state["jobs"][0]["status"], "cancelled")
        self.assertEqual(state["jobs"][1]["status"], "queued")
        self.assertTrue(state["paused"])
        self.assertEqual(manager.retry_failed(), ids)

    def test_cancel_stops_child_after_parent_exits_with_stdout_pipe_open(self):
        # The child inherits stdout and survives its parent's normal exit. Killing
        # just the parent cannot unblock the worker's pipe reader in this case.
        child_code = "import time; print('child ready', flush=True); time.sleep(60)"
        code = f"import subprocess, sys; subprocess.Popen([sys.executable, '-u', '-c', {child_code!r}])"
        manager = self.manager(code)
        manager.add("tracks", ["Song"])
        manager.start()
        self.wait_for(manager, lambda state: "child ready" in state["jobs"][0]["logs"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and manager._process.poll() is None:
            time.sleep(0.02)
        self.assertEqual(manager._process.poll(), 0)
        manager.cancel_active()
        state = self.wait_for(manager, lambda state: state["active_id"] is None, timeout=5)
        self.assertEqual(state["jobs"][0]["status"], "cancelled")

    def test_direct_interpreter_launch_preserves_installed_dependencies(self):
        environment = {}
        direct = _direct_python([sys.executable], environment)
        redirected = direct[0] != sys.executable
        code = "import sys; print('executable=' + sys.executable)"
        if redirected:
            code += "; import lucidadl, playwright; print('client=' + lucidadl.__file__); print('browser=' + playwright.__file__)"
        manager = self.manager(code)
        manager.add("doctor")
        real_popen = subprocess.Popen
        with patch("manager.subprocess.Popen", wraps=real_popen) as popen:
            manager.start()
            state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "completed")
            self.assertEqual(popen.call_args.args[0][0], direct[0])
        reported = next(line.removeprefix("executable=") for line in state["jobs"][0]["logs"] if line.startswith("executable="))
        self.assertIn(Path(reported), {Path(direct[0]), Path(getattr(sys, "_base_executable", sys.executable))})
        if redirected:
            site_packages = str(ROOT / ".venv" / "Lib" / "site-packages")
            self.assertTrue(any(line.startswith("client=" + site_packages) for line in state["jobs"][0]["logs"]))
            self.assertTrue(any(line.startswith("browser=" + site_packages) for line in state["jobs"][0]["logs"]))
            self.assertEqual(environment["PYTHONNOUSERSITE"], "1")

    def test_recovery_changes_running_to_interrupted(self):
        manager = self.manager()
        manager.add("setup")
        manager.close()
        saved = json.loads(manager.state_path.read_text(encoding="utf-8"))
        saved["jobs"][0]["status"] = "running"
        manager.state_path.write_text(json.dumps(saved), encoding="utf-8")
        restored = self.manager()
        self.assertEqual(restored.snapshot()["jobs"][0]["status"], "interrupted")
        self.assertTrue(restored.snapshot()["paused"])
        restored.retry_failed()
        self.assertEqual(restored.snapshot()["jobs"][0]["status"], "queued")

    def test_batch_arguments_keep_untrusted_names_in_file(self):
        manager = self.manager()
        title = 'Artist - Song & echo injected; $(calc) "quote"'
        manager.add("tracks", [title, "日本語 - Café"], {"format": "mp3", "flat": True, "keep_original": True})
        job = manager.snapshot()["jobs"][0]
        command = manager.build_command(job)
        self.assertEqual(command[:5], [sys.executable, "-u", "-m", "client_bridge", "tracks"])
        self.assertNotIn(title, command)
        file_path = Path(command[command.index("--file") + 1])
        self.assertEqual(file_path.read_text(encoding="utf-8").splitlines(), [title, "日本語 - Café"])
        self.assertIn("--flat", command)
        self.assertIn("--keep-original", command)
        self.assertEqual(command[command.index("--to") + 1], "mp3")

    def test_playlist_jobs_and_preview_arguments(self):
        manager = self.manager()
        urls = ["https://open.spotify.com/playlist/first?si=a&x=1", "https://www.deezer.com/playlist/123"]
        ids = manager.add("playlist", urls, preview=True)
        self.assertEqual(len(ids), 2)
        for job, url in zip(manager.snapshot()["jobs"], urls):
            command = manager.build_command(job)
            self.assertEqual(command[-2:], ["--", url])
            self.assertIn("--dry-run", command)
        with self.assertRaisesRegex(ValueError, "playlists only"):
            manager.add("tracks", ["Song"], preview=True)

    def test_tidal_routes_mixed_inputs_and_preserves_order(self):
        manager = self.manager()
        urls = ["https://tidal.com/browse/track/123?u=sharing",
                "https://listen.tidal.com/track/456",
                "https://www.tidal.com/browse/album/789",
                "https://embed.tidal.com/albums/987/",
                "https://tidal.com/browse/playlist/12345678-abcd-abcd-abcd-123456789abc",
                "https://tidal.com/playlist/abcdef12-abcd-abcd-abcd-123456789abc",
                "https://tidal.com/tracks/321"]
        ids = manager.add("tidal", urls, {"service": "qobuz"})
        jobs = manager.snapshot()["jobs"]
        self.assertEqual(len(ids), 5)
        self.assertEqual([job["kind"] for job in jobs], ["tracks", "albums", "playlist", "playlist", "tracks"])
        self.assertEqual([value for job in jobs for value in job["inputs"]], urls)
        self.assertTrue(all(job["options"]["service"] == "qobuz" for job in jobs))
        self.assertTrue(all(job["status"] == "queued" for job in jobs))
        manager.close()
        restored = self.manager()
        self.assertEqual([job["kind"] for job in restored.snapshot()["jobs"]], [job["kind"] for job in jobs])

    def test_tidal_invalid_later_input_rejects_whole_paste(self):
        manager = self.manager()
        valid = "https://tidal.com/browse/track/123"
        bad_urls = ["https://open.spotify.com/track/123", "http://tidal.com/track/123",
                    "https://tidal.com.evil.test/track/123", "https://tidal.com/artist/123",
                    "https://tidal.com/track/not-numeric", "https://tidal.com/playlist/incomplete",
                    "https://user:secret@tidal.com/track/123", "https://tidal.com:999/track/123",
                    "https://browse.tidal.com/track/123", "https://tidal.com/track/123456789012345678901"]
        for bad in bad_urls:
            with self.subTest(url=bad), self.assertRaises(ValueError):
                manager.add("tidal", [valid, bad])
            self.assertEqual(manager.snapshot()["jobs"], [])
            self.assertEqual(list(manager.inputs_dir.glob("*.txt")), [])

    def test_tidal_preview_requires_only_playlists(self):
        manager = self.manager()
        playlist = "https://tidal.com/browse/playlist/12345678-abcd-abcd-abcd-123456789abc"
        for other in ("https://tidal.com/track/123", "https://tidal.com/album/123"):
            with self.assertRaisesRegex(ValueError, "playlists only"):
                manager.add("tidal", [playlist, other], preview=True)
            self.assertEqual(manager.snapshot()["jobs"], [])
        manager.add("tidal", [playlist], preview=True)
        job = manager.snapshot()["jobs"][0]
        self.assertEqual(job["kind"], "playlist")
        self.assertIn("--dry-run", manager.build_command(job))

    def test_tidal_batch_file_failure_does_not_enqueue_partial_paste(self):
        manager = self.manager()
        original = manager._write_inputs
        calls = []

        def write(job):
            calls.append(job["id"])
            if len(calls) == 2:
                raise OSError("disk full")
            return original(job)

        with patch.object(manager, "_write_inputs", side_effect=write), self.assertRaises(OSError):
            manager.add("tidal", ["https://tidal.com/track/123", "https://tidal.com/album/456"])
        self.assertEqual(manager.snapshot()["jobs"], [])

    def test_tidal_account_commands_use_direct_source(self):
        manager = self.manager()
        playlist = "https://tidal.com/playlist/12345678-abcd-abcd-abcd-123456789abc"
        manager.add("tidal", ["https://tidal.com/track/123", "https://tidal.com/album/456", playlist],
                    {"service": "tidal", "format": "flac", "flat": True, "keep_original": True})
        for job in manager.snapshot()["jobs"]:
            command = manager.build_command(job)
            self.assertEqual(command[2:4], ["-m", "tidal_direct"])
            self.assertNotIn("--service", command)
            self.assertIn("--jobs", command)
            self.assertIn("--out", command)
            self.assertIn("--flat", command)
            self.assertIn("--keep-original", command)
            self.assertEqual(command[command.index("--to") + 1], "flac")
        self.assertEqual(manager.build_command(manager.snapshot()["jobs"][-1])[-2:], ["--", playlist])

    def test_tidal_account_requires_matching_links_in_each_mode(self):
        manager = self.manager()
        track = "https://tidal.com/track/123"
        album = "https://tidal.com/album/456"
        playlist = "https://tidal.com/playlist/12345678-abcd-abcd-abcd-123456789abc"
        for kind, invalid in (("tracks", [track, "Artist - Song"]), ("tracks", [album]),
                              ("albums", [track]), ("playlist", [track]),
                              ("playlist", ["https://open.spotify.com/playlist/123"])):
            with self.subTest(kind=kind, inputs=invalid), self.assertRaises(ValueError):
                manager.add(kind, invalid, {"service": "tidal"})
            self.assertEqual(manager.snapshot()["jobs"], [])
        for kind, value in (("tracks", track), ("albums", album), ("playlist", playlist)):
            manager.add(kind, [value], {"service": "tidal"})
        self.assertEqual(len(manager.snapshot()["jobs"]), 3)

    def test_tidal_login_command_readiness_and_saved_account_presence(self):
        manager = self.manager("import os; print(os.environ['TIDAL_DESKTOP_HOME'])")
        manager.add("tidal_login", options={"service": "tidal"})
        job = manager.snapshot()["jobs"][0]
        self.assertEqual(manager.build_command(job), [sys.executable, "-u", "-m", "tidal_account", "login"])
        with patch.object(manager, "_tidal_ready", return_value=True):
            self.assertTrue(manager.snapshot()["tidal_ready"])
        self.assertFalse(manager.snapshot()["tidal_connected"])
        saved_account = self.data / "tidal" / "session.bin"
        saved_account.parent.mkdir()
        saved_account.write_bytes(b"test-placeholder-not-a-token")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Manager must not decrypt or read tokens")):
            self.assertTrue(manager.snapshot()["tidal_connected"])
        manager.start()
        state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "completed")
        self.assertIn(str(self.data / "tidal"), state["jobs"][0]["logs"])
        self.assertTrue(state["paused"])

    def test_tidal_diagnostics_use_account_status_and_setup_stays_lucida(self):
        manager = self.manager()
        for kind in ("doctor", "setup"):
            ids = manager.add(kind, options={"service": "tidal"})
            job = next(job for job in manager.snapshot()["jobs"] if job["id"] == ids[0])
            expected = ["tidal_account", "status"] if kind == "doctor" else ["client_bridge", "setup"]
            self.assertEqual(manager.build_command(job)[3:], expected)

    def test_tidal_download_failure_logs_omit_signed_urls(self):
        manager = self.manager("import sys; print('Download failed: HTTP 403 https://media.example/audio?signature=private-value'); sys.exit(1)")
        manager.add("tracks", ["https://tidal.com/track/123"], {"service": "tidal"})
        manager.start()
        state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "failed")
        logs = "\n".join(state["jobs"][0]["logs"])
        self.assertIn("HTTP 403", logs)
        self.assertIn("[URL omitted]", logs)
        self.assertNotIn("private-value", logs)
        saved = (manager.logs_dir / f"{state['jobs'][0]['id']}.log").read_text(encoding="utf-8")
        self.assertNotIn("private-value", saved)

    def test_tidal_source_setting_does_not_change_existing_provider_jobs(self):
        manager = self.manager()
        manager.add("tracks", ["Artist - Song"], {"service": "amazon"})
        manager.update_settings({"service": "tidal"})
        manager.add("tracks", ["https://tidal.com/track/123"])
        manager.close()
        restored = self.manager()
        jobs = restored.snapshot()["jobs"]
        self.assertEqual([job["options"]["service"] for job in jobs], ["amazon", "tidal"])
        self.assertEqual(restored.build_command(jobs[0])[3], "client_bridge")
        self.assertEqual(restored.build_command(jobs[1])[3], "tidal_direct")

    def test_soundcloud_links_route_tracks_sets_and_unknown_shares(self):
        manager = self.manager()
        mix = "https://soundcloud.com/discover/sets/personalized-tracks::thebrax2000:2335709492?si=share"
        urls = ["https://soundcloud.com/artist/song", "https://on.soundcloud.com/abc123", mix,
                "https://soundcloud.com/artist/sets/album", "https://soundcloud.com/artist/another"]
        manager.add("soundcloud", urls, {"service": "soundcloud"})
        jobs = manager.snapshot()["jobs"]
        self.assertEqual([job["kind"] for job in jobs], ["tracks", "playlist", "playlist", "tracks"])
        self.assertEqual([value for job in jobs for value in job["inputs"]], urls)
        for job in jobs:
            command = manager.build_command(job)
            self.assertEqual(command[3], "soundcloud_direct")
            self.assertNotIn("--service", command)
        self.assertEqual(manager.build_command(jobs[1])[-2:], ["--", mix])

    def test_soundcloud_validation_is_atomic_and_source_specific(self):
        manager = self.manager()
        track = "https://soundcloud.com/artist/song"
        for bad in ("https://soundcloud.com/artist", "https://soundcloud.com.evil.test/a/b", "https://tidal.com/track/123", "Artist - Song"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                manager.add("soundcloud", [track, bad], {"service": "soundcloud"})
            self.assertEqual(manager.snapshot()["jobs"], [])
        with self.assertRaises(ValueError):
            manager.add("soundcloud", [track], {"service": "amazon"})
        with self.assertRaises(ValueError):
            manager.add("tracks", ["https://soundcloud.com/a/sets/b"], {"service": "soundcloud"})

    def test_soundcloud_shortlink_preview_and_status(self):
        manager = self.manager()
        manager.add("soundcloud", ["https://on.soundcloud.com/abc123"], {"service": "soundcloud"}, preview=True)
        command = manager.build_command(manager.snapshot()["jobs"][0])
        self.assertIn("--dry-run", command)
        manager.add("doctor", options={"service": "soundcloud"})
        job = next(job for job in manager.snapshot()["jobs"] if job["kind"] == "doctor")
        self.assertEqual(manager.build_command(job)[3:], ["soundcloud_direct", "status"])
        with patch.object(manager, "_soundcloud_ready", return_value=True):
            self.assertTrue(manager.snapshot()["soundcloud_ready"])

    def test_input_validation(self):
        manager = self.manager()
        for bad in ("file:///secret", "https://127.0.0.1/playlist/1", "https://open.spotify.com.evil.test/playlist/1", "https://user:secret@open.spotify.com/playlist/1"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                manager.add("playlist", [bad])
        for bad in (0, 9, "3.5", True):
            with self.subTest(jobs=bad), self.assertRaises(ValueError):
                manager.add("tracks", ["Song"], {"jobs": bad})
        with self.assertRaises(ValueError):
            manager.add("tracks", ["One\nTwo"])
        with self.assertRaises(ValueError):
            manager.add("tracks", [])
        with self.assertRaises(ValueError):
            manager.add("tracks", ["# comment", "  # another comment"])
        manager.add("tracks", ["# ignored comment", "Actual track"])
        self.assertEqual(manager.snapshot()["jobs"][0]["inputs"], ["Actual track"])
        manager.add("playlist", ["https://www.youtube.com/playlist?list=PLabc", "https://soundcloud.com/artist/sets/album"])

    def test_no_shell_is_used_and_log_tail_is_bounded(self):
        manager = self.manager(f"for i in range({LOG_LIMIT + 20}): print(i)")
        real_popen = subprocess.Popen
        with patch("manager.subprocess.Popen", wraps=real_popen) as popen:
            manager.add("doctor")
            manager.start()
            state = self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "completed")
            self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(len(state["jobs"][0]["logs"]), LOG_LIMIT)
        manager.clear_completed()
        self.assertEqual(manager.snapshot()["jobs"], [])

    def test_close_terminates_active_job_and_rejects_new_work(self):
        manager = self.manager("import time; print('ready'); time.sleep(60)")
        manager.add("doctor")
        manager.start()
        self.wait_for(manager, lambda state: "ready" in state["jobs"][0]["logs"])
        manager.close()
        self.assertFalse(manager._thread.is_alive())
        self.assertEqual(manager.snapshot()["jobs"][0]["status"], "cancelled")
        with self.assertRaises(RuntimeError):
            manager.add("doctor")

    def test_corrupt_state_is_preserved(self):
        (self.data / "queue.json").write_text("not json", encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.snapshot()["jobs"], [])
        self.assertTrue(manager.snapshot()["notice"])
        self.assertEqual(len(list(self.data.glob("queue.corrupt-*.json"))), 1)

    def test_non_object_state_is_preserved(self):
        (self.data / "queue.json").write_text("[]", encoding="utf-8")
        manager = self.manager()
        self.assertEqual(manager.snapshot()["jobs"], [])
        self.assertIn("must contain an object", manager.snapshot()["notice"])
        self.assertEqual(len(list(self.data.glob("queue.corrupt-*.json"))), 1)

    def test_loading_valid_state_with_disk_error_does_not_mark_it_corrupt(self):
        manager = self.manager()
        ids = manager.add("tracks", ["Saved track"])
        manager.close()
        with patch.object(QueueManager, "_save_locked", side_effect=OSError("read-only disk")):
            restored = self.manager()
        self.assertEqual(restored.snapshot()["jobs"][0]["id"], ids[0])
        self.assertIn("read-only disk", restored.snapshot()["notice"])
        self.assertTrue(restored.state_path.exists())
        self.assertEqual(list(self.data.glob("queue.corrupt-*.json")), [])

    def test_failed_writes_roll_back_changes_visible_to_user(self):
        manager = self.manager()
        ids = manager.add("tracks", ["Saved track"])
        original_settings = manager.snapshot()["settings"]
        with patch.object(manager, "_save_locked", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.add("tracks", ["Not added"])
            with self.assertRaises(OSError):
                manager.update_settings({"service": "qobuz"})
            with self.assertRaises(OSError):
                manager.remove(ids)
        state = manager.snapshot()
        self.assertEqual([job["id"] for job in state["jobs"]], ids)
        self.assertEqual(state["settings"], original_settings)
        manager.start()
        self.wait_for(manager, lambda state: state["jobs"][0]["status"] == "completed")
        with patch.object(manager, "_save_locked", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.clear_completed()
        self.assertEqual(manager.snapshot()["jobs"][0]["status"], "completed")

    def test_close_timeout_is_reported(self):
        manager = self.manager()
        with patch.object(manager._thread, "join"), patch.object(manager._thread, "is_alive", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "has not stopped"):
                manager.close()

    def test_failed_final_save_is_retried_on_next_close(self):
        manager = self.manager()
        manager.add("tracks", ["Saved track"])
        with patch.object(manager, "_save_locked", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                manager.close()
        self.assertFalse(manager._closed)
        with patch.object(manager, "_save_locked", wraps=manager._save_locked) as save:
            manager.close()
            save.assert_called_once()
        self.assertTrue(manager._closed)


if __name__ == "__main__":
    unittest.main()
