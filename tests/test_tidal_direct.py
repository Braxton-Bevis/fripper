"""Direct account download tests with manifests, isolated files, and local audio."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shutil
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

try:
    import requests
    import mutagen
    from tidalapi.media import Stream
    import tidal_direct as direct
except ImportError:
    direct = None


PLAYLIST_ID = "17749d7f-99bc-415d-b37b-a85807902928"
CDN_URL = "https://audio.tidal.com/file.flac?signature=private-fixture"


def stream(data, mpd=False, track_id=100):
    raw = data if isinstance(data, str) else json.dumps(data)
    return Stream().parse({"trackId": track_id, "audioQuality": "LOSSLESS", "audioMode": "STEREO",
                           "manifestMimeType": "application/dash+xml" if mpd else "application/vnd.tidal.bts",
                           "manifest": base64.b64encode(raw.encode()).decode()})


def bts(**changes):
    data = {"encryptionType": "NONE", "codecs": "FLAC", "mimeType": "audio/flac", "urls": [CDN_URL]}
    data.update(changes)
    return stream(data)


def track(track_id=100):
    artist = SimpleNamespace(name="Example Artist")
    album = SimpleNamespace(name="Example Album", artist=artist, release_date=None)
    return SimpleNamespace(id=track_id, title="Example Song", full_name="Example Song", artists=[artist],
                           artist=artist, album=album, duration=1.0, available=True, track_num=2,
                           volume_num=1, isrc="XXTEST000001", get_stream=Mock(return_value=bts()))


class Response:
    def __init__(self, chunks, content_type="audio/flac", length=None, failure=None):
        self.status_code = 200
        self.url = CDN_URL
        self.headers = {"Content-Type": content_type}
        if length is not None:
            self.headers["Content-Length"] = str(length)
        self.chunks = chunks
        self.failure = failure

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_content(self, _size):
        yield from self.chunks
        if self.failure:
            raise self.failure


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@unittest.skipIf(direct is None, "Use the installed .venv interpreter")
class ManifestAndCatalogTests(unittest.TestCase):
    def test_manifest_starts_are_spaced_without_delaying_first_track(self):
        clock = FakeClock()
        pacer = direct.TrackPacer(clock=clock.clock, sleep=clock.sleep)
        pacer.wait()
        self.assertEqual(clock.now, 0)
        clock.now += 1
        pacer.wait()
        self.assertEqual(clock.now, 5)
        clock.now += 7
        pacer.wait()
        self.assertEqual(clock.now, 12)
        self.assertEqual(clock.sleeps, [4])

    def test_accepts_actual_bts_manifest_and_checks_track_identity(self):
        source = direct.source_from_stream(bts(), 100)
        self.assertEqual(source.extension, ".flac")
        self.assertEqual(source.urls, [CDN_URL])
        self.assertFalse(source.segmented)
        with self.assertRaisesRegex(direct.DirectError, "different track"):
            direct.source_from_stream(bts(), 101)

    def test_rejects_encrypted_and_ambiguous_bts(self):
        for changes in ({"encryptionType": "OLD_AES"}, {"keyId": "protected"}, {"encryptionType": None}):
            with self.subTest(changes=changes), self.assertRaisesRegex(direct.DirectError, "encrypted"):
                direct.source_from_stream(bts(**changes), 100)

    def test_dash_initialization_numbering_time_and_repeats(self):
        raw = '''<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period>
          <AdaptationSet contentType="audio" mimeType="audio/mp4">
            <Representation id="audio" bandwidth="1000000" codecs="flac">
              <BaseURL>https://audio.tidal.com/signed/</BaseURL>
              <SegmentTemplate initialization="init.mp4" media="s-$Number%03d$-$Time$.m4s" startNumber="5">
                <SegmentTimeline><S t="100" d="20" r="1"/><S d="10"/></SegmentTimeline>
              </SegmentTemplate>
            </Representation>
          </AdaptationSet></Period></MPD>'''
        source = direct.source_from_stream(stream(raw, mpd=True), 100)
        self.assertEqual(source.urls, ["https://audio.tidal.com/signed/init.mp4",
                                      "https://audio.tidal.com/signed/s-005-100.m4s",
                                      "https://audio.tidal.com/signed/s-006-120.m4s",
                                      "https://audio.tidal.com/signed/s-007-140.m4s"])
        self.assertEqual(source.extension, ".flac")

    def test_dash_protection_rejected_before_library_encryption_assumption(self):
        raw = '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet><ContentProtection schemeIdUri="urn:uuid:widevine"/></AdaptationSet></Period></MPD>'
        with self.assertRaisesRegex(direct.DirectError, "DRM-protected"):
            direct.source_from_stream(stream(raw, mpd=True), 100)

    def test_playlist_pagination_preserves_order_and_duplicate_occurrences(self):
        items = [track(index + 1) for index in range(205)]
        items[100] = items[0]
        playlist = SimpleNamespace(num_tracks=205, num_videos=0, items=Mock(side_effect=lambda limit, offset: items[offset:offset + limit]))
        result = direct._all_tracks(playlist, playlist=True)
        self.assertEqual([item.id for item in result], [item.id for item in items])
        self.assertIs(result[0], result[100])
        self.assertEqual([call.kwargs["offset"] for call in playlist.items.call_args_list], [0, 100, 200])

    def test_incomplete_catalog_does_not_return_partial_success(self):
        playlist = SimpleNamespace(num_tracks=101, num_videos=0, items=Mock(side_effect=[[track()] * 100, []]))
        with self.assertRaisesRegex(direct.DirectError, "100 of 101"):
            direct._all_tracks(playlist, playlist=True)
        playlist.num_videos = 1
        with self.assertRaisesRegex(direct.DirectError, "videos"):
            direct._all_tracks(playlist, playlist=True)

    def test_equal_length_playlist_edit_between_pages_is_rejected(self):
        items = [track(index + 1) for index in range(201)]
        playlist = SimpleNamespace(num_tracks=200, num_videos=0, _etag="revision-a")
        def page(limit, offset):
            if offset == 0:
                return items[:100]
            playlist._etag = "revision-b"
            # Removing the first track and appending one preserves total=200,
            # but offset 100 now starts at track 102 and would omit track 101.
            return items[101:201]
        playlist.items = page
        with self.assertRaisesRegex(direct.DirectError, "playlist changed"):
            direct._all_tracks(playlist, playlist=True)

    def test_exact_tidal_url_validation(self):
        self.assertEqual(direct.tidal_reference("https://listen.tidal.com/album/123/track/100", "track"), ("track", "100"))
        for value in ("Artist - Song", "https://tidal.com.evil.test/track/100", "https://user:pass@tidal.com/track/100", "https://tidal.com/album/100"):
            with self.subTest(value=value), self.assertRaises(direct.DirectError):
                direct.tidal_reference(value, "track")

    def test_windows_filename_safety_and_errors_never_include_signed_url(self):
        self.assertEqual(direct.safe_name("CON"), "_CON")
        self.assertNotRegex(direct.safe_name('bad:/\\*?"<>|\r\n.'), r'[<>:"/\\|?*\r\n]')
        error = requests.ConnectionError("failed: " + CDN_URL)
        self.assertNotIn("private-fixture", direct.safe_error(error))

    def test_manager_command_envelope(self):
        options = direct.parser().parse_args(["playlist", "--out", "downloads", "--jobs", "8", "--to", "mp3", "--bitrate", "320k", "--keep-original", "--flat", "--dry-run", "--", f"https://tidal.com/playlist/{PLAYLIST_ID}"])
        self.assertEqual(options.jobs, 8)
        self.assertTrue(options.dry_run)


@unittest.skipIf(direct is None, "Use the installed .venv interpreter")
class AudioPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.wav = self.root / "source.wav"
        with wave.open(str(self.wav), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(44100)
            output.writeframes(struct.pack("<h", 1200) * 44100 * 2)
        self.flac = self.root / "source.flac"
        direct._ffmpeg(["-i", self.wav, "-c:a", "flac", self.flac])
        self.metadata = direct.track_metadata(track())

    def tearDown(self):
        self.temporary.cleanup()

    def options(self, **changes):
        values = dict(out=str(self.root / "downloads"), to="original", jobs=3, bitrate="320k", keep_original=False, flat=True, dry_run=False)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_byte_limit_covers_all_chunks_and_segment_requests(self):
        clock = FakeClock()
        limiter = direct.BandwidthLimiter(clock=clock.clock, sleep=clock.sleep)
        chunk = b"x" * (64 * 1024)
        http = SimpleNamespace(headers={}, get=Mock(side_effect=[Response([chunk, chunk]), Response([chunk, chunk])]))
        destination = self.root / "stream.part"
        direct._download_urls([CDN_URL, CDN_URL], destination, http, limiter)
        self.assertEqual(destination.stat().st_size, 256 * 1024)
        self.assertEqual(clock.sleeps, [0.25, 0.25, 0.25, 0.25])
        self.assertEqual(clock.now, 1.0)
        self.assertEqual(http.get.call_count, 2)

    def test_cdn_429_stops_without_retrying_or_long_sleep(self):
        reply = Response([])
        reply.status_code = 429
        reply.headers["Retry-After"] = "3600"
        http = SimpleNamespace(headers={}, get=Mock(return_value=reply))
        destination = self.root / "stream.part"
        with patch.object(direct.time, "sleep") as sleep:
            with self.assertRaisesRegex(direct.RateLimitError, "3600 seconds"):
                direct._download_urls([CDN_URL, CDN_URL], destination, http)
        self.assertEqual(http.get.call_count, 1)
        sleep.assert_not_called()
        self.assertFalse(destination.exists())

    def test_collection_stops_before_remaining_tracks_on_429(self):
        options = self.options(jobs=8)
        item = track()
        complete = (self.root / "first.flac", direct.track_metadata(item), False)
        with patch.object(direct, "download_track", side_effect=[complete, direct._rate_limit_error(120), complete]) as download:
            with self.assertRaises(direct.RateLimitError) as error:
                direct.run_collection(direct.Collection("Test", [item, item, item]), options, threading.Lock())
        self.assertEqual(download.call_count, 2)
        self.assertEqual(error.exception.counts, (1, 0, 1))

    def test_rate_limit_exits_75_and_does_not_start_another_collection(self):
        session = SimpleNamespace(config=SimpleNamespace())
        with patch("tidal_account.load_session", return_value=session), \
                patch("tidal_account.save_session"), \
                patch.object(direct, "resolve_collections", return_value=[direct.Collection("First", []), direct.Collection("Second", [])]), \
                patch.object(direct, "run_collection", side_effect=direct._rate_limit_error(60)) as run:
            code = direct.main(["playlist", f"https://tidal.com/playlist/{PLAYLIST_ID}"])
        self.assertEqual(code, 75)
        run.assert_called_once()

    def test_zero_html_and_preview_are_rejected(self):
        for contents in (b"", b"<html>not audio</html>" * 5):
            bad = self.root / "bad.flac"
            bad.write_bytes(contents)
            with self.assertRaises(direct.DirectError):
                direct.validate_audio(bad)
        with self.assertRaisesRegex(direct.DirectError, "preview or incomplete"):
            direct.validate_audio(self.flac, expected_duration=200)

    def test_atomic_audio_is_tagged_and_playable(self):
        destination = self.root / "finished.flac"
        direct._atomic_audio(self.flac, destination, self.metadata, ".flac")
        audio = mutagen.File(destination)
        self.assertEqual(audio["title"], ["Example Song"])
        self.assertEqual(audio["artist"], ["Example Artist"])
        self.assertEqual(audio["tracknumber"], ["2"])
        self.assertEqual(audio["tidal_track_id"], ["100"])
        self.assertAlmostEqual(direct.validate_audio(destination, 1.0), 1.0, places=2)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_declared_full_duration_cannot_hide_incomplete_audio(self):
        # Keep a real one-second FLAC payload but declare ten seconds in STREAMINFO.
        # Header-only validation sees ten seconds; actual decoding must reject it.
        data = bytearray(self.flac.read_bytes())
        self.assertEqual(data[:4], b"fLaC")
        self.assertEqual(data[4] & 0x7f, 0)
        packed = int.from_bytes(data[18:26], "big")
        packed = (packed & ~((1 << 36) - 1)) | (44100 * 10)
        data[18:26] = packed.to_bytes(8, "big")
        incomplete = self.root / "incomplete.flac"
        incomplete.write_bytes(data)
        self.assertAlmostEqual(mutagen.File(incomplete).info.length, 10.0)
        with self.assertRaises(direct.DirectError):
            direct.validate_audio(incomplete, 10.0)

    def test_supported_conversions_produce_tagged_playable_audio(self):
        for name, extension in (("mp3", ".mp3"), ("aac", ".m4a"), ("opus", ".opus"), ("ogg", ".ogg"), ("wav", ".wav")):
            with self.subTest(format=name):
                codec, _mux = direct._OUTPUTS[name]
                target = self.root / ("converted" + extension)
                direct._atomic_audio(self.flac, target, self.metadata, extension, codec, "320k")
                self.assertTrue(mutagen.File(target).tags)
                self.assertGreater(direct.validate_audio(target, 1.0), 0.9)

    def test_bad_conversion_does_not_replace_existing_file_or_leave_partials(self):
        destination = self.root / "finished.flac"
        shutil.copyfile(self.flac, destination)
        before = destination.read_bytes()
        def bad_ffmpeg(arguments):
            Path(arguments[-1]).write_bytes(b"<html>error response</html>" * 5)
        with patch.object(direct, "_ffmpeg", side_effect=bad_ffmpeg):
            with self.assertRaises(direct.DirectError):
                direct._atomic_audio(self.flac, destination, self.metadata, ".flac")
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_interrupted_http_transfer_cleans_partial_and_does_not_leak_url(self):
        partial = self.root / "audio.part"
        failure = requests.ChunkedEncodingError if hasattr(requests, "ChunkedEncodingError") else requests.exceptions.ChunkedEncodingError
        http = SimpleNamespace(headers={}, get=Mock(side_effect=lambda *_args, **_kwargs: Response([b"fLaCpartial"], failure=failure("secret " + CDN_URL))))
        with patch.object(direct.time, "sleep"):
            with self.assertRaises(direct.DirectError) as error:
                direct._download_urls([CDN_URL], partial, http)
        self.assertFalse(partial.exists())
        self.assertEqual(http.get.call_count, 3)
        self.assertNotIn("private-fixture", str(error.exception))

    def test_html_or_short_http_response_never_becomes_audio(self):
        for reply in (Response([b"<html>error</html>"], content_type="text/html"), Response([b"fLaC"], length=100)):
            path = self.root / "audio.part"
            http = SimpleNamespace(headers={}, get=Mock(return_value=reply))
            with self.assertRaises(direct.DirectError):
                direct._download_urls([CDN_URL], path, http)
            self.assertFalse(path.exists())

    def test_http_redirect_is_rejected_before_following_it(self):
        redirect = SimpleNamespace(status_code=302, headers={"Location": "http://audio.tidal.com/file.flac"}, close=Mock())
        http = SimpleNamespace(get=Mock(return_value=redirect))
        with self.assertRaisesRegex(direct.DirectError, "unsupported audio URL"):
            direct._stream_response(http, CDN_URL)
        http.get.assert_called_once()
        self.assertFalse(http.get.call_args.kwargs["allow_redirects"])
        redirect.close.assert_called_once()

    def test_success_then_skip_validated_existing_file(self):
        item = track()
        options = self.options()
        def download(_urls, path):
            shutil.copyfile(self.flac, path)
        with patch.object(direct, "_download_urls", side_effect=download), patch.object(direct.time, "sleep"):
            first = direct.download_track(item, options.out, 1, 1, options, threading.Lock())
            second = direct.download_track(item, options.out, 1, 1, options, threading.Lock())
        self.assertFalse(first[2])
        self.assertTrue(second[2])
        self.assertEqual(item.get_stream.call_count, 1)
        self.assertEqual(list(Path(options.out).glob("*.part")), [])

    def test_conversion_failure_keeps_validated_original(self):
        options = self.options(to="mp3")
        actual = direct._atomic_audio
        def convert(source, target, metadata, extension, *args):
            if extension == ".mp3":
                raise direct.DirectError("test conversion failure")
            return actual(source, target, metadata, extension, *args)
        with patch.object(direct, "_download_urls", side_effect=lambda _urls, path: shutil.copyfile(self.flac, path)), \
                patch.object(direct, "_atomic_audio", side_effect=convert), patch.object(direct.time, "sleep"):
            with self.assertRaisesRegex(direct.DirectError, "conversion failure"):
                direct.download_track(track(), options.out, 1, 1, options, threading.Lock())
        originals = list(Path(options.out).glob("*.flac"))
        self.assertEqual(len(originals), 1)
        direct.validate_audio(originals[0], 1.0)
        self.assertEqual(list(Path(options.out).glob("*.mp3")), [])
        self.assertEqual(list(Path(options.out).glob("*.part")), [])

    def test_original_request_does_not_reuse_a_converted_mp3(self):
        item = track()
        converted_options = self.options(to="mp3", keep_original=False)
        with patch.object(direct, "_download_urls", side_effect=lambda _urls, path: shutil.copyfile(self.flac, path)) as download, \
                patch.object(direct.time, "sleep"):
            converted = direct.download_track(item, converted_options.out, 1, 1, converted_options, threading.Lock())
            self.assertEqual(converted[0].suffix, ".mp3")
            self.assertEqual(list(Path(converted_options.out).glob("*.flac")), [])
            original = direct.download_track(item, converted_options.out, 1, 1, self.options(), threading.Lock())
        self.assertEqual(download.call_count, 2)
        self.assertEqual(item.get_stream.call_count, 2)
        self.assertEqual(original[0].suffix, ".flac")
        self.assertFalse(original[2])
        record = json.loads(direct._provenance_path(original[0]).read_text())
        self.assertTrue(record["is_source"])
        self.assertEqual(record["source"]["quality"], "LOSSLESS")
        self.assertNotIn("private-fixture", json.dumps(record))

    def test_changed_bitrate_reconverts_but_unchanged_request_can_skip(self):
        item = track()
        high = self.options(to="mp3", bitrate="320k")
        low = self.options(to="mp3", bitrate="128k")
        with patch.object(direct, "_download_urls", side_effect=lambda _urls, path: shutil.copyfile(self.flac, path)) as download, \
                patch.object(direct.time, "sleep"):
            first = direct.download_track(item, high.out, 1, 1, high, threading.Lock())
            first_rate = mutagen.File(first[0]).info.bitrate
            second = direct.download_track(item, low.out, 1, 1, low, threading.Lock())
            second_rate = mutagen.File(second[0]).info.bitrate
            third = direct.download_track(item, low.out, 1, 1, low, threading.Lock())
        self.assertFalse(second[2])
        self.assertTrue(third[2])
        self.assertEqual(download.call_count, 2)
        self.assertLess(second_rate, first_rate)
        self.assertEqual(json.loads(direct._provenance_path(second[0]).read_text())["request"]["bitrate"], "128k")

    def test_enabling_keep_original_restores_missing_source_before_skipping(self):
        item = track()
        initial = self.options(to="mp3", keep_original=False)
        keep = self.options(to="mp3", keep_original=True)
        with patch.object(direct, "_download_urls", side_effect=lambda _urls, path: shutil.copyfile(self.flac, path)) as download, \
                patch.object(direct.time, "sleep"):
            direct.download_track(item, initial.out, 1, 1, initial, threading.Lock())
            self.assertEqual(list(Path(initial.out).glob("*.flac")), [])
            second = direct.download_track(item, keep.out, 1, 1, keep, threading.Lock())
            third = direct.download_track(item, keep.out, 1, 1, keep, threading.Lock())
        self.assertFalse(second[2])
        self.assertTrue(third[2])
        self.assertEqual(download.call_count, 2)
        self.assertEqual(len(list(Path(keep.out).glob("*.flac"))), 1)

    def test_playlist_order_and_duplicates_survive_parallel_completion(self):
        options = self.options()
        def download(item, directory, position, total, _options, _lock):
            return Path(directory) / f"{position:03d}.flac", direct.track_metadata(item), False
        item = track()
        collection = direct.Collection("Test", [item, track(101), item], PLAYLIST_ID, True)
        with patch.object(direct, "download_track", side_effect=download):
            counts = direct.run_collection(collection, options, threading.Lock())
        self.assertEqual(counts, (3, 0, 0))
        entries = [line for line in (Path(options.out) / "Test.m3u8").read_text().splitlines() if not line.startswith("#")]
        self.assertEqual(entries, ["001.flac", "002.flac", "003.flac"])

    def test_partial_failure_does_not_replace_complete_playlist(self):
        options = self.options()
        directory = Path(options.out)
        directory.mkdir()
        complete = directory / "Test.m3u8"
        complete.write_text("old complete playlist", encoding="utf-8")
        def download(item, path, position, total, _options, _lock):
            if position == 2:
                raise direct.DirectError("unavailable")
            return Path(path) / "001.flac", direct.track_metadata(item), False
        with patch.object(direct, "download_track", side_effect=download):
            counts = direct.run_collection(direct.Collection("Test", [track(), track()], PLAYLIST_ID, True), options, threading.Lock())
        self.assertEqual(counts, (1, 0, 1))
        self.assertEqual(complete.read_text(), "old complete playlist")
        self.assertTrue((directory / "Test.partial.m3u8").is_file())


if __name__ == "__main__":
    unittest.main()
