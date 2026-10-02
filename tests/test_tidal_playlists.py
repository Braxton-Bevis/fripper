"""Public playlist fixtures; no network, saved browser profile, or login."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import httpx
    from lucidadl import api
    import tidal_playlists as adapter
except ImportError:
    adapter = None


PLAYLIST_ID = "17749d7f-99bc-415d-b37b-a85807902928"
URL = f"https://tidal.com/playlist/{PLAYLIST_ID}"
ENDPOINT = f"https://tidal.com/v1/playlists/{PLAYLIST_ID}/items"


def fixture(offset, count, total=77):
    rows = [{"type": "track", "item": {"id": 1000 + index, "title": f"Song {index}",
                                      "artists": [{"name": "Artist"}]}}
            for index in range(offset, offset + count)]
    return {"offset": offset, "limit": 50, "totalNumberOfItems": total, "items": rows}


def response(data, status=200, url=ENDPOINT):
    return SimpleNamespace(
        status=status, url=url, json=AsyncMock(return_value=data),
        request=SimpleNamespace(all_headers=AsyncMock(return_value={"x-tidal-token": "fixture-public-token"})))


class PendingResponse:
    def __init__(self, value):
        self.value = asyncio.get_running_loop().create_future()
        self.value.set_result(value)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


@unittest.skipIf(adapter is None, "Installed community client required; use .venv Python")
class TidalPlaylistTests(unittest.IsolatedAsyncioTestCase):
    def test_public_html_title_and_count(self):
        self.assertEqual(adapter._public_metadata(
            '<meta property="og:title" content="Summer &amp; friends">'
            '<meta name="description" content="Playlist - Summer &amp; friends - 77 items">'),
            ("Summer & friends", 77))
        with self.assertRaisesRegex(api.LucidaError, "Make the playlist public"):
            adapter._public_metadata('<meta property="og:title" content="TIDAL">')

    def test_validates_public_playlist_urls(self):
        self.assertEqual(adapter._playlist_id(URL), PLAYLIST_ID)
        self.assertEqual(adapter._playlist_id(f"https://listen.tidal.com/browse/playlist/{PLAYLIST_ID}"), PLAYLIST_ID)
        for value in (URL.replace("tidal.com", "tidal.com.evil.test"),
                      URL.replace("tidal.com", "user:secret@tidal.com"),
                      URL.replace("tidal.com", "tidal.com:8888"),
                      "https://tidal.com/playlist/not-an-id"):
            with self.subTest(url=value), self.assertRaises(api.LucidaError):
                adapter._playlist_id(value)

    async def test_browser_fixture_reads_all_77_positions_in_order_including_duplicates(self):
        first_data = fixture(0, 50)
        second_data = fixture(50, 27)
        second_data["items"][0] = deepcopy(first_data["items"][0])
        first = response(first_data)
        request = SimpleNamespace(get=AsyncMock(return_value=response(second_data)))
        page = SimpleNamespace(goto=AsyncMock(), expect_response=Mock())

        def expect(predicate, **_kwargs):
            self.assertTrue(predicate(first))
            self.assertFalse(predicate(response(first_data, url="https://evil.test/v1/playlists/x/items")))
            return PendingResponse(first)

        page.expect_response.side_effect = expect
        context = SimpleNamespace(request=request, new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        lifecycle = AsyncMock()
        lifecycle.__aenter__.return_value = playwright
        logs = []
        with patch("playwright.async_api.async_playwright", return_value=lifecycle):
            tracks = await adapter._browser_tracklist(PLAYLIST_ID, 77, logs.append)

        self.assertEqual(len(tracks), 77)
        self.assertEqual(tracks[0], tracks[50])
        self.assertEqual(tracks[49]["title"], "Song 49")
        self.assertEqual(tracks[51]["title"], "Song 51")
        self.assertEqual(tracks[-1]["title"], "Song 76")
        self.assertEqual(tracks[0]["url"], "https://tidal.com/track/1000")
        self.assertEqual(request.get.call_args.args[0], ENDPOINT)
        self.assertEqual(request.get.call_args.kwargs["params"]["offset"], 50)
        self.assertEqual(request.get.call_args.kwargs["max_redirects"], 0)
        self.assertNotIn("fixture-public-token", " ".join(logs))
        browser.new_context.assert_awaited_once_with(locale="en-US")
        browser.close.assert_awaited_once()

    async def test_browser_is_closed_after_public_metadata_failure(self):
        first = response(fixture(0, 50))
        page = SimpleNamespace(goto=AsyncMock(), expect_response=Mock(return_value=PendingResponse(first)))
        context = SimpleNamespace(request=Mock(), new_page=AsyncMock(return_value=page))
        browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
        lifecycle = AsyncMock()
        lifecycle.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
        with patch("playwright.async_api.async_playwright", return_value=lifecycle), \
                patch.object(adapter, "_read_pages", side_effect=api.LucidaError("unavailable")):
            with self.assertRaisesRegex(api.LucidaError, "unavailable"):
                await adapter._browser_tracklist(PLAYLIST_ID, 77, Mock())
        browser.close.assert_awaited_once()

    async def test_does_not_return_partial_page(self):
        request = SimpleNamespace(get=AsyncMock(return_value=response(fixture(50, 0))))
        with self.assertRaisesRegex(api.LucidaError, "only 50 of 77"):
            await adapter._read_pages(request, response(fixture(0, 50)), PLAYLIST_ID, 77, Mock())

    async def test_rejects_shifted_position_or_changed_total(self):
        for second in (fixture(0, 27), fixture(50, 27, 78)):
            with self.subTest(page=second["offset"]):
                request = SimpleNamespace(get=AsyncMock(return_value=response(second)))
                with self.assertRaisesRegex(api.LucidaError, "changed while reading"):
                    await adapter._read_pages(request, response(fixture(0, 50)), PLAYLIST_ID, 77, Mock())

    async def test_rejects_non_music_and_missing_credits(self):
        data = fixture(0, 1, 1)
        data["items"][0]["type"] = "video"
        with self.assertRaisesRegex(api.LucidaError, "non-music"):
            adapter._page_tracks(data, 0, 1)
        data = fixture(0, 1, 1)
        data["items"][0]["item"]["artists"] = []
        with self.assertRaisesRegex(api.LucidaError, "without title or artist"):
            adapter._page_tracks(data, 0, 1)

    async def test_does_not_send_public_session_to_unexpected_endpoint(self):
        request = SimpleNamespace(get=AsyncMock())
        with self.assertRaisesRegex(api.LucidaError, "unexpected endpoint"):
            await adapter._read_pages(request, response(fixture(0, 50), url=ENDPOINT.replace("tidal.com", "evil.test")), PLAYLIST_ID, 77, Mock())
        request.get.assert_not_awaited()

    async def test_fast_embed_stays_fast(self):
        original = AsyncMock(return_value=("Small playlist", [{"title": "A", "artist": "B"}]))
        with patch.object(adapter, "_fallback", new_callable=AsyncMock) as fallback:
            result = await adapter.make_tracklist(original)(URL, Mock())
        self.assertEqual(result[0], "Small playlist")
        fallback.assert_not_awaited()

    async def test_404_or_embed_window_uses_fallback(self):
        error = httpx.HTTPStatusError("not found", request=httpx.Request("GET", URL),
                                    response=httpx.Response(404))
        for exception in (error, api.TidalPlaylistWindow("Summer adds")):
            original = AsyncMock(side_effect=exception)
            with patch.object(adapter, "_fallback", new_callable=AsyncMock, return_value=("Summer adds", ["complete"])) as fallback:
                result = await adapter.make_tracklist(original)(URL, Mock())
            self.assertEqual(result, ("Summer adds", ["complete"]))
            fallback.assert_awaited_once()

    async def test_unexpected_failure_is_not_hidden(self):
        original = AsyncMock(side_effect=TypeError("programming error"))
        with self.assertRaisesRegex(TypeError, "programming error"):
            await adapter.make_tracklist(original)(URL, Mock())


if __name__ == "__main__":
    unittest.main()
