import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import soundcloud_direct as sc


MIX = "https://soundcloud.com/discover/sets/personalized-tracks::thebrax2000:2335709492?si=shared"
TRACK = "https://soundcloud.com/artist/song"


def track_info(identifier="123", formats=None):
    return {"id": identifier, "title": "Song", "uploader": "Artist", "duration": 1.0,
            "webpage_url": TRACK, "formats": formats or [{"format_id": "download", "ext": "wav", "url": "https://media.example/source", "vcodec": "none"}]}


class FakeFactory:
    def __init__(self, responses=None, audio=b""):
        self.responses = responses or {}
        self.audio = audio
        self.options = []
        self.downloads = []
        self.download_error = None

    def __call__(self, options):
        self.options.append(options)
        owner = self

        class FakeYDL:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def extract_info(self, value, download=False):
                assert download is False
                return copy.deepcopy(owner.responses[value])

            def process_ie_result(self, info, download=True):
                assert download is True
                selected = info["formats"][0]
                owner.downloads.append(selected["format_id"])
                if owner.download_error is not None:
                    raise owner.download_error
                target = Path(options["outtmpl"].replace("%(ext)s", selected["ext"]))
                target.write_bytes(owner.audio)
                return info

        return FakeYDL()


class SoundCloudTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.fixture = self.directory / "fixture.wav"
        with wave.open(str(self.fixture), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x00\x00" * 8000)
        self.options = SimpleNamespace(out=str(self.directory / "output"), jobs=3, to="original",
                                       bitrate="192k", flat=True, keep_original=False, dry_run=False)

    def tearDown(self):
        self.temporary.cleanup()

    def test_public_links_and_mix_validation(self):
        self.assertEqual(sc.soundcloud_kind(MIX), "playlist")
        self.assertEqual(sc.soundcloud_kind(TRACK), "tracks")
        self.assertEqual(sc.soundcloud_kind("https://on.soundcloud.com/Abc123"), "unknown")
        for value in ("https://soundcloud.com/artist", "https://soundcloud.com.evil.test/a/b", "http://soundcloud.com/a/b", "https://user:secret@soundcloud.com/a/b", "https://soundcloud.com/you/likes"):
            with self.subTest(url=value), self.assertRaises(sc.SoundCloudError):
                sc.soundcloud_kind(value)

    def test_original_preference_and_preview_rejection(self):
        formats = [{"format_id": "hls_mp3_preview", "url": "x", "preference": -10},
                   {"format_id": "hls_mp3", "url": "x"}, {"format_id": "hls_aac_160k", "url": "x"},
                   {"format_id": "download", "url": "x", "quality": 10}]
        self.assertEqual([item["format_id"] for item in sc.select_formats({"formats": formats})], ["download", "hls_aac_160k"])
        with self.assertRaisesRegex(sc.SoundCloudError, "Preview-only"):
            sc.select_formats({"formats": formats[:1]})

    def test_resolve_preserves_playlist_duplicates_and_order(self):
        entries = [{"id": "1", "url": "https://api-v2.soundcloud.com/tracks/1"},
                   {"id": "2", "url": "https://api-v2.soundcloud.com/tracks/2"},
                   {"id": "1", "url": "https://api-v2.soundcloud.com/tracks/1"}]
        factory = FakeFactory({MIX: {"_type": "playlist", "id": "mix", "title": "Mix", "entries": entries}})
        collections = sc.resolve_collections("playlist", [MIX], factory)
        self.assertEqual([entry["id"] for entry in collections[0].entries], ["1", "2", "1"])
        self.assertTrue(collections[0].playlist)
        self.assertIsNone(factory.options[0]["cookiesfrombrowser"])
        self.assertIsNone(factory.options[0]["cookiefile"])
        self.assertFalse(factory.options[0]["usenetrc"])

    def test_short_share_collection_resolves_without_wrong_track_mode(self):
        short = "https://on.soundcloud.com/abc123"
        factory = FakeFactory({short: {"id": "set", "title": "Set", "entries": [track_info()]}})
        self.assertTrue(sc.resolve_collections("tracks", [short], factory)[0].playlist)

    def test_incomplete_playlist_rejected(self):
        factory = FakeFactory({MIX: {"id": "mix", "entries": [track_info(), None]}})
        with self.assertRaisesRegex(sc.SoundCloudError, "incomplete"):
            sc.resolve_collections("playlist", [MIX], factory)

    def test_audio_download_validates_and_commits_atomically(self):
        factory = FakeFactory(audio=self.fixture.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            target, metadata, skipped = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), self.fixture.read_bytes())
        self.assertEqual(metadata["id"], "123")
        self.assertFalse(skipped)
        self.assertEqual(factory.downloads, ["download"])
        self.assertEqual(list(target.parent.glob(".soundcloud-*")), [])

    def test_invalid_response_never_becomes_audio_file(self):
        factory = FakeFactory(audio=b"<html>access denied</html>" * 5)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(sc.SoundCloudError):
            sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
        self.assertEqual(list((self.directory / "output").glob("*.wav")), [])

    def test_forged_flac_duration_is_rejected_by_actual_decode(self):
        original = self.directory / "one-second.flac"
        sc._ffmpeg(["-i", self.fixture, "-c:a", "flac", original])
        contents = bytearray(original.read_bytes())
        self.assertEqual(contents[:4], b"fLaC")
        # STREAMINFO total_samples occupies the low 36 bits at bytes 18..25.
        packed = int.from_bytes(contents[18:26], "big")
        contents[18:26] = ((packed & ~((1 << 36) - 1)) | 80000).to_bytes(8, "big")
        original.write_bytes(contents)
        with self.assertRaisesRegex(sc.SoundCloudError, "decoder did not reach"):
            sc.validate_audio(original, expected_duration=10)

    def test_mp3_conversion_and_original_retention(self):
        self.options.to, self.options.keep_original = "mp3", True
        factory = FakeFactory(audio=self.fixture.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            target, _, _ = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
        self.assertEqual(target.suffix, ".mp3")
        self.assertGreater(sc.validate_audio(target, 1), 0)
        self.assertEqual(len(list(target.parent.glob("*.original.wav"))), 1)

    def test_matching_provenance_reuses_only_identical_source_and_options(self):
        factory = FakeFactory(audio=self.fixture.read_bytes())
        info = track_info()
        with contextlib.redirect_stdout(io.StringIO()):
            first, _, _ = sc.download_track(info, self.directory / "output", 1, 1, self.options, factory)
            second, _, skipped = sc.download_track(info, self.directory / "output", 1, 1, self.options, factory)
        self.assertEqual(first, second)
        self.assertTrue(skipped)
        self.assertEqual(factory.downloads, ["download"])
        record = json.loads(first.with_suffix(".wav.source.json").read_text(encoding="utf-8"))
        self.assertEqual(record["output_format"], "original")
        self.assertNotIn("https://", json.dumps(record))

    def test_original_mode_does_not_reuse_converted_same_extension(self):
        factory = FakeFactory(audio=self.fixture.read_bytes())
        self.options.to = "wav"
        with contextlib.redirect_stdout(io.StringIO()):
            converted, _, _ = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
            converted_bytes = converted.read_bytes()
            self.options.to = "original"
            original, _, skipped = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
        self.assertFalse(skipped)
        self.assertNotEqual(converted, original)
        self.assertEqual(converted.read_bytes(), converted_bytes)
        self.assertEqual(original.read_bytes(), self.fixture.read_bytes())

    def test_changed_bitrate_and_missing_retained_original_are_not_skipped(self):
        factory = FakeFactory(audio=self.fixture.read_bytes())
        self.options.to = "mp3"
        with contextlib.redirect_stdout(io.StringIO()):
            first, _, _ = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
            self.options.bitrate = "320k"
            second, _, skipped = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
            self.assertFalse(skipped)
            self.assertNotEqual(first, second)
            self.options.keep_original = True
            retained_target, _, skipped = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
            self.assertFalse(skipped)
            sidecar = json.loads(retained_target.with_suffix(".mp3.source.json").read_text(encoding="utf-8"))
            (retained_target.parent / sidecar["retained_original"]).unlink()
            _, _, skipped = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
        self.assertFalse(skipped)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_later_conversion_does_not_delete_previously_retained_original(self):
        factory = FakeFactory(audio=self.fixture.read_bytes())
        self.options.to, self.options.keep_original = "mp3", True
        with contextlib.redirect_stdout(io.StringIO()):
            target, _, _ = sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
            original = next(target.parent.glob("*.original.wav"))
            source_bytes = original.read_bytes()
            self.options.keep_original, self.options.bitrate = False, "320k"
            sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
        self.assertEqual(original.read_bytes(), source_bytes)

    def test_stream_normalization_tags_audio_without_ffprobe(self):
        aac = self.directory / "fixture.m4a"
        sc._ffmpeg(["-i", self.fixture, "-c:a", "aac", aac])
        selected = {"format_id": "hls_aac_160k", "ext": "m4a", "url": "https://media.example/signed", "vcodec": "none", "abr": 160}
        factory = FakeFactory(audio=aac.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            target, _, _ = sc.download_track(track_info(formats=[selected]), self.directory / "output", 1, 1, self.options, factory)
        self.assertEqual(factory.options[0]["fixup"], "never")
        tags = sc.mutagen.File(target, easy=True)
        self.assertEqual(tags["title"], ["Song"])
        self.assertEqual(tags["artist"], ["Artist"])
        self.assertEqual(tags["tracknumber"], ["1"])

    def test_atomic_existing_file_tagging_preserves_decoded_audio(self):
        aac = self.directory / "existing.m4a"
        sc._ffmpeg(["-i", self.fixture, "-c:a", "aac", aac])
        decode = lambda: sc._ffmpeg(["-i", aac, "-map", "0:a:0", "-f", "md5", "pipe:1"], capture=True)
        before = decode()
        sc.tag_audio(aac, {"id": "123", "title": "Tagged", "artist": "Artist", "duration": 1, "position": 3})
        self.assertEqual(decode(), before)
        self.assertEqual(sc.mutagen.File(aac, easy=True)["title"], ["Tagged"])

    def test_conversion_failure_keeps_validated_original(self):
        self.options.to = "mp3"
        factory = FakeFactory(audio=self.fixture.read_bytes())
        real_ffmpeg = sc._ffmpeg

        def fail_conversion(arguments, **kwargs):
            if "-c:a" in arguments:
                raise sc.SoundCloudError("Conversion failed")
            return real_ffmpeg(arguments, **kwargs)

        with patch.object(sc, "_ffmpeg", side_effect=fail_conversion), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(sc.SoundCloudError):
            sc.download_track(track_info(), self.directory / "output", 1, 1, self.options, factory)
        self.assertEqual(len(list((self.directory / "output").glob("*.original.wav"))), 1)
        self.assertEqual(list((self.directory / "output").glob("*.mp3")), [])

    def test_partial_playlist_does_not_replace_complete_playlist(self):
        output = Path(self.options.out)
        output.mkdir()
        complete = output / "Mix.m3u8"
        complete.write_text("existing-complete-playlist", encoding="utf-8")
        collection = sc.Collection("Mix", [track_info(), track_info(), track_info()], "mix", True)

        def download(entry, directory, position, total, options):
            if position == 2:
                raise sc.SoundCloudError("Unavailable")
            return Path(directory) / f"{position:03d}.wav", {"artist": "A", "title": "B", "duration": 1}, False

        with patch.object(sc, "download_track", side_effect=download), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sc.run_collection(collection, self.options), (2, 0, 1))
        self.assertEqual(complete.read_text(encoding="utf-8"), "existing-complete-playlist")
        partial = (output / "Mix.partial.m3u8").read_text(encoding="utf-8")
        self.assertLess(partial.index("001.wav"), partial.index("003.wav"))

    def test_error_log_omits_signed_urls(self):
        message = sc.safe_error(RuntimeError("HTTP 403 at https://media.example/audio?signature=private"))
        self.assertIn("403", message)
        self.assertNotIn("signature", message)
        self.assertIn("Browser cookies were not read", message)

    def test_original_login_warning_is_readable_without_cli_flags(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            sc.SafeLogger().warning("Original download format is only available for registered users. Use --cookies-from-browser.")
        self.assertIn("using the best available public stream", output.getvalue())
        self.assertNotIn("--cookies", output.getvalue())
        self.assertIn("cannot download protected streams", sc.safe_error(RuntimeError("This video is DRM protected")))

    def test_parser_supports_queue_envelope(self):
        args = sc.parser().parse_args(["playlist", "--jobs", "3", "--to", "mp3", "--bitrate", "320k", "--dry-run", "--", MIX])
        self.assertEqual(args.url, MIX)
        self.assertTrue(args.dry_run)

    def test_sdk_enforces_bandwidth_and_download_delay_without_real_sleep(self):
        from yt_dlp.downloader.common import FileDownloader

        class OfflineDownloader(FileDownloader):
            def real_download(self, filename, info_dict):
                return True

        options = sc.ydl_options()
        self.assertEqual(options["sleep_interval_requests"], 1)
        self.assertEqual(options["concurrent_fragment_downloads"], 1)
        self.assertEqual(options["extractor_retries"], 0)
        with sc.YoutubeDL(options) as ydl, patch("yt_dlp.downloader.common.time.sleep") as sleep:
            downloader = OfflineDownloader(ydl, options)
            self.assertEqual(downloader.download(str(self.directory / "not-created.wav"), {"url": "https://media.example/file"}), (True, True))
            sleep.assert_called_once_with(5.0)
            sleep.reset_mock()
            downloader.slow_down(0, 1, 512 * 1024)
            sleep.assert_called_once_with(1.0)

    def test_rate_limit_stops_later_tracks_and_keeps_partial_playlist(self):
        self.options.jobs = 8  # Caller preference cannot enable parallel direct requests.
        collection = sc.Collection("Mix", [track_info(), track_info(), track_info()], "mix", True)
        calls = []

        def download(entry, directory, position, total, options):
            calls.append(position)
            if position == 2:
                raise RuntimeError("HTTP Error 429: Too Many Requests")
            return Path(directory) / f"{position:03d}.wav", {"artist": "A", "title": "B", "duration": 1}, False

        with patch.object(sc, "download_track", side_effect=download), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(sc.RateLimitedError):
            sc.run_collection(collection, self.options)
        self.assertEqual(calls, [1, 2])
        partial = (Path(self.options.out) / "Mix.partial.m3u8").read_text(encoding="utf-8")
        self.assertIn("001.wav", partial)
        self.assertNotIn("003.wav", partial)

    def test_rate_limit_does_not_fallback_to_another_audio_request(self):
        factory = FakeFactory(audio=self.fixture.read_bytes())
        factory.download_error = RuntimeError("HTTP Error 429")
        formats = [{"format_id": "download", "ext": "wav", "url": "https://media.example/original"},
                   {"format_id": "hls_audio", "ext": "wav", "url": "https://media.example/stream"}]
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(sc.RateLimitedError):
            sc.download_track(track_info(formats=formats), self.directory / "output", 1, 1, self.options, factory)
        self.assertEqual(factory.downloads, ["download"])

    def test_main_rate_limit_exit_requests_queue_pause(self):
        with patch.object(sc, "run_downloads", side_effect=RuntimeError("HTTP Error 429")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sc.main(["playlist", MIX]), 75)


if __name__ == "__main__":
    unittest.main()
