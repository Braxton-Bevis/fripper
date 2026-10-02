"""Web server checks with isolated state, a local socket and no downloads."""
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import zipfile

import web
from manager import QueueManager


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.music = base / "music"
        self.manager = QueueManager(base / "state", command_builder=lambda job: [sys.executable, "-c", "pass"])
        self.manager.update_settings({"output": str(self.music)})
        self.app = web.FRipperWeb(self.manager, base / "state")
        self.app.set_pin("2468")
        self.server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, header=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        headers = {"Content-Type": "application/json"}
        if header:
            headers["X-FRipper"] = "1"
        if self.cookie:
            headers["Cookie"] = self.cookie
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        connection.close()
        return response.status, data, response

    def sign_in(self):
        status, _, _ = self.request("POST", "/api/login", {"pin": "2468"})
        self.assertEqual(status, 200)

    def test_pin_required_and_wrong_pin_rejected(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"pin": "0000"})[0], 403)
        self.assertEqual(self.request("GET", "/")[0], 200)  # The sign-in page itself is public.
        self.sign_in()
        status, data, _ = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("jobs", json.loads(data))

    def test_repeated_wrong_pins_lock_out(self):
        for _ in range(5):
            self.request("POST", "/api/login", {"pin": "1111"})
        status, data, _ = self.request("POST", "/api/login", {"pin": "2468"})
        self.assertEqual(status, 403)
        self.assertIn("Too many attempts", json.loads(data)["error"])

    def test_posts_need_custom_header(self):
        self.sign_in()
        status, _, _ = self.request("POST", "/api/start", {}, header=False)
        self.assertEqual(status, 403)

    def test_add_detects_service_and_queues_without_starting(self):
        self.sign_in()
        status, data, _ = self.request("POST", "/api/add", {
            "text": "https://soundcloud.com/artist/sets/mix", "mode": "tracks", "service": "amazon", "format": "original"})
        self.assertEqual(status, 200, data)
        result = json.loads(data)
        self.assertEqual((result["mode"], result["service"]), ("soundcloud", "soundcloud"))
        state = json.loads(self.request("GET", "/api/state?job=" + result["ids"][0])[1])
        self.assertEqual(state["jobs"][0]["status"], "queued")
        self.assertIn("logs", state["jobs"][0])
        self.assertFalse(state["running"])
        self.assertEqual(self.request("POST", "/api/add", {"text": "", "mode": "tidal"})[0], 400)

    def test_library_listing_downloads_and_path_safety(self):
        album = self.music / "Artist" / "Album"
        album.mkdir(parents=True)
        (album / "01 Song.flac").write_bytes(b"fLaC" + b"x" * 100)
        (album / "02 Song.flac").write_bytes(b"fLaC" + b"y" * 100)
        self.sign_in()
        listing = json.loads(self.request("GET", "/api/library?path=Artist")[1])
        self.assertEqual(listing["entries"][0]["name"], "Album")
        self.assertEqual(listing["entries"][0]["tracks"], 2)
        status, data, response = self.request("GET", "/download?path=Artist/Album/01%20Song.flac")
        self.assertEqual(status, 200)
        self.assertTrue(data.startswith(b"fLaC"))
        self.assertIn("attachment", response.getheader("Content-Disposition"))
        status, data, _ = self.request("GET", "/download?path=Artist/Album")
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertEqual(sorted(archive.namelist()), ["Album/01 Song.flac", "Album/02 Song.flac"])
            self.assertTrue(archive.read("Album/02 Song.flac").startswith(b"fLaC"))
        for bad in ("../state/web.json", "Artist/../../state", "C:/Windows", "..%2Fstate"):
            self.assertIn(self.request("GET", "/download?path=" + bad)[0], (400, 403, 404), bad)
        self.assertEqual(self.request("POST", "/api/library/delete", {"path": ""})[0], 403)
        self.assertEqual(self.request("POST", "/api/library/delete", {"path": "Artist/Album"})[0], 200)
        self.assertFalse(album.exists())

    def test_static_assets_are_whitelisted(self):
        self.assertEqual(self.request("GET", "/assets/fripper.png")[0], 200)
        self.assertEqual(self.request("GET", "/assets/BRANDING.txt")[0], 404)
        self.assertEqual(self.request("GET", "/assets/..%2Fweb.py")[0], 404)


if __name__ == "__main__":
    unittest.main()
