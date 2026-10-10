"""The web layer: password, CSRF guard, error shapes, and a few routes over a fake stack.

Needs FastAPI (requirements-dev.txt); skipped by the host's plain Python.
"""

import json
import os
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ImportError:
    raise unittest.SkipTest("FastAPI is not installed") from None

from medialib import LibraryError
from web.app import create_app
from web.config import WebConfig
from web.services import Services

AUTH = ("me", "secret")
HX = {"HX-Request": "true"}


class FakeStack:
    """Routes under test either don't touch the stack or have its callers patched."""


class WebCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.downloads = os.path.join(self.tmp.name, "Downloads")
        os.makedirs(os.path.join(self.downloads, "Show"))
        open(os.path.join(self.downloads, "Show", "Show.S01E01.mkv"), "w").close()
        open(os.path.join(self.downloads, "passport.jpeg"), "w").close()
        open(os.path.join(self.downloads, "keys.csv"), "w").close()
        config = WebConfig(password=AUTH[1], username=AUTH[0], port=0,
                           import_root=self.downloads,
                           watchlist_state=os.path.join(self.tmp.name, "watchlist.json"),
                           watcher_state=None, janitor_state=None)
        self.services = Services(config, make_stack=FakeStack)
        self.client = TestClient(create_app(config, self.services, background=False))
        self.client.auth = AUTH  # every request logs in unless it passes auth=None

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def toast(self, response):
        return json.loads(response.headers["HX-Trigger"])["toast"]


class Security(WebCase):
    def test_pages_need_the_password(self):
        response = self.client.get("/", auth=None)
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers["WWW-Authenticate"])
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_wrong_logins_lock_out(self):
        with patch("web.security.time.sleep"):
            for _ in range(10):
                self.assertEqual(self.client.get("/", auth=("me", "nope")).status_code, 401)
            self.assertEqual(self.client.get("/", auth=("me", "nope")).status_code, 429)
            self.assertEqual(self.client.get("/").status_code, 429)  # even the right one, for a while

    def test_username_is_checked(self):
        with patch("web.security.time.sleep"):
            self.assertEqual(self.client.get("/", auth=("you", AUTH[1])).status_code, 401)

    def test_static_files_skip_the_password(self):
        self.assertEqual(self.client.get("/static/app.js", auth=None).status_code, 200)

    def test_state_changes_need_htmx(self):
        response = self.client.post("/watchlist/remove", data={"kind": "movie", "ext_id": 1})
        self.assertEqual(response.status_code, 403)

    def test_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertNotIn("no-store", self.client.get("/static/app.js").headers.get("Cache-Control", ""))


class Errors(WebCase):
    def failing_list(self):
        return patch("web.routes.library.titles.overview", side_effect=LibraryError("Radarr is down"))

    def test_page_request_gets_an_error_page(self):
        with self.failing_list():
            response = self.client.get("/library/list")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Radarr is down", response.text)
        self.assertIn("<nav", response.text)  # the whole page, nav included

    def test_htmx_part_shows_the_error_in_place(self):
        with self.failing_list():
            response = self.client.get("/library/list", headers=HX)
        self.assertEqual(response.status_code, 200)  # or htmx wouldn't swap it in
        self.assertIn("Radarr is down", response.text)
        self.assertNotIn("<nav", response.text)

    def test_actions_get_a_toast(self):
        response = self.client.post("/watchlist/send", headers=HX)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.toast(response), {"message": "nothing selected", "level": "error"})

    def test_bad_input_is_a_422_toast(self):
        response = self.client.post("/watchlist/remove", headers=HX, data={"kind": "book", "ext_id": 1})
        self.assertEqual(response.status_code, 422)
        self.assertIn("kind", self.toast(response)["message"])

    def test_container_paths_are_hidden_in_messages(self):
        path = os.path.join(os.path.realpath(self.downloads), "Show", "x.mkv")
        with patch("web.routes.library.titles.overview", side_effect=LibraryError(f"can't read {path}")):
            response = self.client.get("/library/list", headers=HX)
        self.assertIn("Downloads/Show/x.mkv", response.text)
        self.assertNotIn(self.tmp.name, response.text)


class ImportBrowser(WebCase):
    def test_lists_only_videos(self):
        response = self.client.get("/import")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Show", response.text)
        self.assertNotIn("passport", response.text)
        self.assertNotIn("keys.csv", response.text)

    def test_cannot_leave_the_folder(self):
        for bad in ("..", "../..", "Show/../../"):
            response = self.client.get("/import", params={"path": bad})
            self.assertEqual(response.status_code, 400, bad)
            self.assertIn("outside the import folder", response.text)


class WatchlistRoutes(WebCase):
    item = {"kind": "movie", "ext_id": "42", "title": "Heat", "year": "1995"}

    def test_save_then_remove(self):
        response = self.client.post("/watchlist/items", headers=HX, data=self.item)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Saved", response.text)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertIn("watchlist-changed", trigger)
        self.assertEqual(trigger["toast"]["message"], "Heat saved to your watchlist.")
        self.assertTrue(self.services.watchlist.has("movie", 42))

        again = self.client.post("/watchlist/items", headers=HX, data=self.item)
        self.assertEqual(self.toast(again)["message"], "Heat was already on your watchlist.")

        gone = self.client.post("/watchlist/remove", headers=HX, data={"kind": "movie", "ext_id": 42})
        self.assertEqual(gone.status_code, 204)
        self.assertFalse(self.services.watchlist.has("movie", 42))

    def test_nav_badge_counts_the_list(self):
        self.client.post("/watchlist/items", headers=HX, data=self.item)
        self.assertIn(">1<", self.client.get("/watchlist/count").text)

    def test_send_runs_a_job_until_done(self):
        self.client.post("/watchlist/items", headers=HX, data=self.item)
        release = threading.Event()

        def slow_send(_stack, _watchlist, keys, _log):
            release.wait(5)
            return {"ok": True, "sent": ["Heat"], "failed": [], "message": "Sent 1 to Radarr/Sonarr."}

        with patch("web.routes.watchlist.watchlist_send", side_effect=slow_send) as send:
            response = self.client.post("/watchlist/send", headers=HX, data={"key": "movie:42"})
            self.assertEqual(response.status_code, 200)
            job_id = re.search(r'hx-get="/jobs/(\w+)"', response.text)[1]  # a running job polls
            release.set()
            for _ in range(250):
                if self.services.jobs.get(job_id).finished:
                    break
                time.sleep(0.02)
        self.assertEqual(send.call_args.args[2], [("movie", 42)])
        poll = self.client.get(f"/jobs/{job_id}", headers=HX)
        self.assertIn("Sent 1 to Radarr/Sonarr.", poll.text)
        self.assertIn("watchlist-changed", poll.headers["HX-Trigger"])

    def test_unknown_job(self):
        response = self.client.get("/jobs/nope", headers=HX)
        self.assertEqual(response.status_code, 200)
        self.assertIn("class=\"empty text-danger\"", response.text)


class TvAccess(WebCase):
    def test_toggle_renders_the_new_state(self):
        result = {"hidden": True, "message": "Hidden from tv."}
        with patch("web.routes.library.set_tv_hidden", return_value=result) as hide:
            response = self.client.post("/library/shows/13/tv-access", headers=HX, data={"hidden": "true"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(hide.call_args.args[1:], (13, True))
        self.assertEqual(self.toast(response)["message"], "Hidden from tv.")


if __name__ == "__main__":
    unittest.main()
