"""Regional search recovery uses response fixtures, without live requests."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from lucidadl import api
import lucida_search


def response(data=None, status=200, text=None):
    if text is None:
        text = api._PD_START + json.dumps(data) + api._PD_END
    return SimpleNamespace(status_code=status, text=text)


def results(*tracks):
    return {"results": {"tracks": list(tracks), "albums": [], "artists": []}}


TRACK = {"url": "https://music.amazon.co.uk/tracks/B001", "title": "Song",
         "artists": [{"name": "Artist"}], "album": {"title": "Album"}}


class SearchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def client(self, responses):
        self.logs = []
        return SimpleNamespace(_get=AsyncMock(side_effect=responses), log=self.logs.append)

    async def test_server_errors_fall_back_to_gb_and_keep_search_results(self):
        original = AsyncMock()
        search = lucida_search.make_search(original)
        client = self.client([
            response({"error": "Cannot read properties of undefined (reading 'name')"}),
            response({"data": {"error": {"message": "US account unavailable"}}}),
            response(results(TRACK)),
        ])
        actual = await search(client, "Artist - Song", "amazon")
        self.assertEqual(actual, api._extract_search_results(results(TRACK)))
        self.assertEqual([call.kwargs["params"].get("country", "") for call in client._get.await_args_list],
                         ["", "US", "GB"])
        self.assertTrue(any("Cannot read properties" in line for line in self.logs))
        self.assertTrue(any("fallback region GB" in line for line in self.logs))
        original.assert_not_awaited()

    async def test_valid_empty_results_do_not_retry_other_regions(self):
        client = self.client([response(results())])
        actual = await lucida_search.make_search(AsyncMock())(client, "No such song", "amazon")
        self.assertEqual(actual, {"tracks": [], "albums": [], "artists": []})
        self.assertEqual(client._get.await_count, 1)
        self.assertEqual(getattr(client, lucida_search._CACHE_ATTR), "")

    async def test_working_country_is_cached_only_on_current_client(self):
        search = lucida_search.make_search(AsyncMock())
        first = self.client([response(status=500), response(status=500), response(results(TRACK)),
                             response(results(TRACK))])
        second = self.client([response(results())])
        await search(first, "First", "amazon")
        await search(first, "Second", "amazon")
        await search(second, "Third", "amazon")
        self.assertEqual(first._get.await_args_list[-1].kwargs["params"]["country"], "GB")
        self.assertNotIn("country", second._get.await_args.kwargs["params"])

    async def test_failed_cached_region_can_recover_in_another_region(self):
        client = self.client([response(status=500), response(results(TRACK))])
        setattr(client, lucida_search._CACHE_ATTR, "GB")
        await lucida_search.make_search(AsyncMock())(client, "Song", "amazon")
        self.assertEqual([call.kwargs["params"].get("country", "") for call in client._get.await_args_list],
                         ["GB", ""])
        self.assertEqual(getattr(client, lucida_search._CACHE_ATTR), "")

    async def test_other_providers_delegate_without_changes(self):
        for provider in ("qobuz", "grilledcheese", "tidal"):
            with self.subTest(provider=provider):
                expected = {"sentinel": object()}
                original = AsyncMock(return_value=expected)
                client = self.client([])
                result = await lucida_search.make_search(original)(client, "Exact Query", provider)
                self.assertIs(result, expected)
                original.assert_awaited_once_with(client, "Exact Query", provider)
                client._get.assert_not_awaited()

    async def test_http_errors_missing_blob_and_network_failure_retry(self):
        client = self.client([RuntimeError("network"), response(status=502),
                              response(text="<html>No data</html>"), response(results(TRACK))])
        actual = await lucida_search.make_search(AsyncMock())(client, "Song", "amazon_music")
        self.assertEqual(actual["tracks"][0]["url"], TRACK["url"])
        self.assertEqual(getattr(client, lucida_search._CACHE_ATTR), "JP")

    async def test_error_shaped_payload_does_not_count_as_empty_results(self):
        client = self.client([response({"message": "unexpected payload"}), response(results())])
        actual = await lucida_search.make_search(AsyncMock())(client, "Song", "amazon")
        self.assertFalse(actual["tracks"])
        self.assertEqual(client._get.await_count, 2)

    async def test_exhaustion_is_reported_as_service_failure(self):
        client = self.client([response({"error": "Service unavailable"}) for _ in range(4)])
        with self.assertRaisesRegex(api.LucidaError, "unavailable in Auto, US, GB, and JP"):
            await lucida_search.make_search(AsyncMock())(client, "Song", "amazon")
        self.assertEqual(client._get.await_count, 4)

    async def test_candidates_are_returned_without_blind_selection(self):
        second = dict(TRACK, title="Song (Live)", url="https://music.amazon.co.uk/tracks/B002")
        client = self.client([response(results(TRACK, second))])
        actual = await lucida_search.make_search(AsyncMock())(client, "Artist - Song", "amazon")
        self.assertEqual([item["url"] for item in actual["tracks"]], [TRACK["url"], second["url"]])

    def test_error_logs_redact_credentials(self):
        result = lucida_search._safe_error("Upstream failed token=private-value cf_clearance=private-cookie "
                                          "Bearer opaque-token " + "a" * 80)
        self.assertIn("Upstream failed", result)
        self.assertNotIn("private-value", result)
        self.assertNotIn("private-cookie", result)
        self.assertNotIn("opaque-token", result)
        self.assertNotIn("a" * 80, result)

    def test_install_is_idempotent(self):
        with patch.object(api.LucidaClient, "search", AsyncMock()) as original:
            lucida_search.install_patch()
            installed = api.LucidaClient.search
            lucida_search.install_patch()
            self.assertIs(api.LucidaClient.search, installed)
            self.assertIsNot(installed, original)


if __name__ == "__main__":
    unittest.main()
