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


class FakeKind:
    """Just enough of medialib.stack.Kind for the watchlist list view."""

    def __init__(self, kind):
        self.kind = kind
        self.id_key = "tmdbId" if kind == "movie" else "tvdbId"

    def items(self):
        return []


class FakeStack:
    """Routes under test either don't touch the stack or have its callers patched."""

    def kind(self, kind):
        return FakeKind(kind)


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
        self.assertIn('id="save-movie-42"', response.text)  # swaps the button that opened the dialog
        self.assertIn('hx-swap-oob="outerHTML"', response.text)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertIn("watchlist-changed", trigger)
        self.assertIn("close-dialog", trigger)  # the save dialog closes itself
        self.assertEqual(trigger["toast"]["message"], "Heat saved.")
        self.assertTrue(self.services.watchlist.has("movie", 42))

        again = self.client.post("/watchlist/items", headers=HX, data=self.item)
        self.assertEqual(self.toast(again)["message"], "Heat was already on your watchlist.")

        gone = self.client.post("/watchlist/remove", headers=HX, data={"kind": "movie", "ext_id": 42})
        self.assertEqual(gone.status_code, 204)
        self.assertFalse(self.services.watchlist.has("movie", 42))

    def test_save_files_the_title_into_a_playlist(self):
        playlist = self.services.watchlist.create_playlist("Noir")["key"]
        response = self.client.post("/watchlist/items", headers=HX,
                                    data={**self.item, "playlist": playlist})
        self.assertEqual(self.toast(response)["message"], "Heat saved to Noir.")
        self.assertEqual(self.services.watchlist.get("movie", 42)["playlists"], [playlist])

    def test_save_with_a_new_playlist_creates_it(self):
        response = self.client.post("/watchlist/items", headers=HX,
                                    data={**self.item, "new_playlist": "Double feature"})
        self.assertIn("Double feature", self.toast(response)["message"])
        self.assertEqual([p["name"] for p in self.services.watchlist.playlists()],
                         ["Double feature"])
        self.assertEqual(self.services.watchlist.get("movie", 42)["playlists"],
                         ["double-feature"])

    def test_save_dialog_offers_playlists_and_checks_the_current_ones(self):
        playlist = self.services.watchlist.create_playlist("Noir")["key"]
        self.services.watchlist.add("movie", 42, "Heat", playlists=[playlist])
        response = self.client.get("/watchlist/save", headers=HX,
                                   params={"kind": "movie", "ext_id": 42})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Heat", response.text)
        self.assertIn('value="noir" class="sr-only" checked', response.text)
        self.assertIn("New playlist", response.text)

    def test_save_dialog_falls_back_to_what_was_saved(self):
        self.services.watchlist.add("movie", 42, "Heat", year=1995, overview="A heist.")
        response = self.client.get("/watchlist/save", headers=HX,
                                   params={"kind": "movie", "ext_id": 42})
        self.assertIn("Heat", response.text)
        self.assertIn("1995", response.text)

    def test_playlists_can_be_created_renamed_and_deleted(self):
        created = self.client.post("/watchlist/playlists", headers=HX, data={"name": "Noir"})
        self.assertEqual(created.status_code, 204)
        self.assertIn("Noir", self.toast(created)["message"])

        dialog = self.client.get("/watchlist/playlists/noir/rename", headers=HX)
        self.assertIn('value="Noir"', dialog.text)

        renamed = self.client.post("/watchlist/playlists/noir/rename", headers=HX,
                                   data={"name": "Film noir"})
        self.assertEqual(self.toast(renamed)["message"], "Playlist renamed.")
        self.assertEqual(self.services.watchlist.playlist("noir")["name"], "Film noir")

        self.services.watchlist.add("movie", 42, "Heat", playlists=["noir"])
        deleted = self.client.post("/watchlist/playlists/noir/delete", headers=HX)
        self.assertIn("stay on your watchlist", self.toast(deleted)["message"])
        self.assertEqual(self.services.watchlist.playlists(), [])
        self.assertTrue(self.services.watchlist.has("movie", 42))

    def test_assign_files_the_selection(self):
        playlist = self.services.watchlist.create_playlist("Noir")["key"]
        self.services.watchlist.add("movie", 1, "One")
        self.services.watchlist.add("show", 2, "Two")
        response = self.client.post("/watchlist/assign", headers=HX,
                                    data={"key": ["movie:1", "show:2"], "playlist": playlist})
        self.assertEqual(self.toast(response)["message"], "Filed 2 into Noir.")
        self.assertEqual(self.services.watchlist.get("movie", 1)["playlists"], [playlist])
        self.assertEqual(self.services.watchlist.get("show", 2)["playlists"], [playlist])

    def test_list_is_folder_sections(self):
        self.services.watchlist.add("movie", 1, "Loose")
        playlist = self.services.watchlist.create_playlist("Noir")["key"]
        self.services.watchlist.add("movie", 2, "Filed", playlists=[playlist])
        response = self.client.get("/watchlist/list", headers=HX)
        self.assertIn("Unfiled", response.text)
        self.assertIn("Noir", response.text)
        self.assertIn("Loose", response.text)
        self.assertIn("Filed", response.text)
        self.assertIn("/watchlist/playlists/noir/rename", response.text)

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
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["toast"]["message"], "Hidden from tv.")
        self.assertIn("library-changed", trigger)  # the row and the details dialog both show it
        self.assertIn('aria-label="Show on TV"', response.text)

    def test_details_dialog_keeps_its_labelled_button(self):
        result = {"hidden": False, "message": "Visible on tv again."}
        with patch("web.routes.library.set_tv_hidden", return_value=result):
            response = self.client.post("/library/shows/13/tv-access", headers=HX,
                                        data={"hidden": "false", "labelled": "true"})
        self.assertIn("Hide on TV</button>", response.text)


class DetailsDialogs(WebCase):
    facts = {"kind": "show", "extId": 7, "title": "Frieren", "year": 2023, "overview": "An elf mage.",
             "genres": ["Anime"], "scores": ["8.8/10"], "links": [], "status": "Still airing"}

    def test_library_title_has_library_actions(self):
        details = dict(self.facts, id=13, inLibrary=True, seasons=[], hiddenFromTv=True, monitored=True)
        with patch("web.routes.library.library_details", return_value=details):
            response = self.client.get("/library/show/13", headers=HX)
        self.assertEqual(response.status_code, 200)
        for text in ("An elf mage.", "/releases/show/13", "/library/show/13/delete", "Hidden on TV"):
            self.assertIn(text, response.text)

    def test_watchlist_title_offers_download_and_remove(self):
        self.services.watchlist.add("show", 7, "Frieren")
        with patch("web.routes.watchlist.lookup_details", return_value=dict(self.facts, inLibrary=False)):
            response = self.client.get("/watchlist/show/7", headers=HX)
        self.assertIn("8.8/10", response.text)
        # A show asks what to get (the follow dialog) instead of sending every episode.
        self.assertIn("/following/follow?title=Frieren&amp;tvdb_id=7&amp;origin=watchlist", response.text)
        self.assertNotIn("/watchlist/send", response.text)
        self.assertNotIn("/delete", response.text)

    def test_watchlist_movie_downloads_straight_away(self):
        self.services.watchlist.add("movie", 42, "Heat")
        movie = dict(self.facts, kind="movie", extId=42, title="Heat", inLibrary=False)
        with patch("web.routes.watchlist.lookup_details", return_value=movie):
            response = self.client.get("/watchlist/movie/42", headers=HX)
        self.assertIn("/watchlist/send", response.text)

    def test_watchlist_falls_back_to_what_was_saved(self):
        self.services.watchlist.add("movie", 42, "Heat", overview="A heist.")
        with patch("web.routes.watchlist.lookup_details", side_effect=LibraryError("Radarr is down")):
            response = self.client.get("/watchlist/movie/42", headers=HX)
            self.assertIn("A heist.", response.text)
            missing = self.client.get("/watchlist/movie/43", headers=HX)  # not saved: nothing to show
        self.assertIn("Radarr is down", missing.text)

    def test_season_with_missing_episodes_can_be_fetched(self):
        season = {"number": 2, "label": "Season 2", "have": 3, "total": 12, "missing": 4, "downloading": 0,
                  "upcoming": 5, "removed": 0, "sizeHuman": "", "monitored": False}
        details = dict(self.facts, id=13, inLibrary=True, seasons=[season], hiddenFromTv=False, monitored=True)
        with patch("web.routes.library.library_details", return_value=details):
            dialog = self.client.get("/library/show/13", headers=HX)
            self.assertIn("/library/shows/13/seasons/2/get", dialog.text)
            self.assertIn("Get 4", dialog.text)
            with patch("web.routes.library.get_season", return_value={"message": "Frieren season 2: go"}) as get:
                response = self.client.post("/library/shows/13/seasons/2/get", headers=HX)
        get.assert_called_once()
        self.assertEqual(get.call_args.args[1:], (13, 2))
        self.assertIn("Frieren season 2: go", response.headers["HX-Trigger"])
        self.assertIn("library-changed", response.headers["HX-Trigger"])


class AddingShows(WebCase):
    """Adding a show never downloads everything without asking what to get."""

    class Kind:
        def lookup(self, _query):
            return ["show", "movie"]

    def results(self, kind):
        def summarize(_k, r):
            return {"kind": r, "extId": 7, "title": "Black Clover", "year": 2017, "overview": "",
                    "poster": None, "inLibrary": False}
        with patch.object(FakeStack, "kind", lambda _self, _k: self.Kind(), create=True), \
             patch("web.routes.add.summarize_lookup", side_effect=summarize):
            return self.client.get(f"/add/results?kind={kind}&q=black", headers=HX).text

    def test_add_opens_the_choice_for_a_show_but_not_a_movie(self):
        text = self.results("show")
        self.assertIn("/following/follow?title=Black+Clover&amp;tvdb_id=7&amp;origin=add", text)
        self.assertEqual(text.count('hx-post="/add"'), 1)  # the movie result only

    def test_dialog_says_add_when_adding(self):
        candidate = {"tvdbId": 7, "title": "Black Clover", "year": 2017, "network": "", "status": "",
                     "poster": None, "seasons": [1, 2], "inLibrary": False, "following": False}
        with patch("web.routes.following.fol.follow_candidates", return_value=[candidate]):
            text = self.client.get("/following/follow?title=Black+Clover&tvdb_id=7&origin=add", headers=HX).text
        self.assertIn("Add Black Clover", text)
        self.assertIn("Add &amp; download", text)
        self.assertIn('name="origin" value="add"', text)
        self.assertIn('value="from"', text)  # the rolling window is one of the choices

    def wait(self, response, release):
        job_id = re.search(r'hx-get="/jobs/(\w+)"', response.text)[1]  # a running job polls
        release.set()
        for _ in range(250):
            if self.services.jobs.get(job_id).finished:
                break
            time.sleep(0.02)
        return self.client.get(f"/jobs/{job_id}", headers=HX)

    def test_from_the_watchlist_it_leaves_the_list(self):
        self.services.watchlist.add("show", 7, "Black Clover")
        release = threading.Event()

        def start_window(*_args):
            release.wait(5)
            return {"message": "Black Clover from episode 120"}

        with patch("web.routes.following.rolling.start_window", side_effect=start_window) as start:
            poll = self.wait(self.client.post("/following/follow", headers=HX, data={
                "tvdb_id": 7, "mode": "from", "start": 120, "ahead": 12, "origin": "watchlist"}), release)
        self.assertEqual(start.call_args.args[2:], (7, 120, 12))
        self.assertFalse(self.services.watchlist.has("show", 7))
        trigger = json.loads(poll.headers["HX-Trigger"])
        for event in ("library-changed", "watchlist-changed", "following-changed"):
            self.assertIn(event, trigger)

    def test_bulk_send_skips_shows(self):
        with patch("web.routes.watchlist.watchlist_send") as send:
            response = self.client.post("/watchlist/send", headers=HX, data={"key": "show:7"})
        send.assert_not_called()
        self.assertIn("own Download button", self.toast(response)["message"])


class RollingWindowRoutes(WebCase):
    facts = DetailsDialogs.facts
    window = {"start": 1157, "ahead": 2, "since": 0, "caughtUp": False,
              "position": {"id": 5, "number": 1157, "code": "S23E02", "title": "Nami in a Fix!", "airAt": 0},
              "window": [{"id": 5, "number": 1157, "code": "S23E02", "title": "Nami in a Fix!",
                          "airAt": 0, "state": "on disk"},
                         {"id": 6, "number": 1158, "code": "S23E03", "title": "A Quest", "airAt": 0,
                          "state": "looking"}],
              "counts": {"on disk": 1, "downloading": 0, "looking": 1, "to air": 0}}

    def details(self):
        return dict(self.facts, id=13, inLibrary=True, seasons=[], hiddenFromTv=False, monitored=True)

    def test_dialog_offers_a_window_when_there_is_none(self):
        with patch("web.routes.library.library_details", return_value=self.details()):
            response = self.client.get("/library/show/13", headers=HX)
        self.assertIn("/library/shows/13/rolling", response.text)
        self.assertIn('name="start"', response.text)
        self.assertNotIn("/rolling/stop", response.text)

    def test_a_long_gap_opens_the_window_form_and_warns(self):
        season = {"number": 1, "label": "Season 1", "have": 0, "total": 170, "missing": 170,
                  "downloading": 0, "upcoming": 0, "removed": 0, "sizeHuman": "", "monitored": False}
        details = dict(self.details(), seasons=[season])
        with patch("web.routes.library.library_details", return_value=details):
            response = self.client.get("/library/show/13", headers=HX)
        self.assertRegex(response.text, r'<details class="mt-2" open>')
        self.assertIn("That&#39;s a lot", response.text)
        with patch("web.routes.library.library_details", return_value=details), \
             patch("web.routes.library.rolling.status", return_value=self.window):
            windowed = self.client.get("/library/show/13", headers=HX)
        self.assertNotIn("/seasons/1/get", windowed.text)  # the window decides what downloads

    def test_dialog_shows_the_window(self):
        with patch("web.routes.library.library_details", return_value=self.details()), \
             patch("web.routes.library.rolling.status", return_value=self.window):
            response = self.client.get("/library/show/13", headers=HX)
        for text in ("Up to episode 1157", "Nami in a Fix!", "1 looking", "/library/shows/13/rolling/stop",
                     'value="1157"'):
            self.assertIn(text, response.text)

    def test_follow_from_an_episode_needs_the_number(self):
        response = self.client.post("/following/follow", headers=HX, data={"tvdb_id": 81797, "mode": "from"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("episode number", self.toast(response)["message"])

    def test_preview_shows_the_episode_or_the_problem(self):
        ep = {"id": None, "number": 1157, "code": "S23E02", "title": "Nami in a Fix!", "airAt": 0, "last": 1180}
        with patch("web.routes.following.rolling.preview", return_value=ep):
            ok = self.client.get("/following/rolling/preview?tvdb_id=81797&start=1157", headers=HX)
        self.assertIn("S23E02", ok.text)
        self.assertIn("of 1180", ok.text)
        with patch("web.routes.following.rolling.preview", side_effect=LibraryError("One Piece has episodes 1–1180")):
            bad = self.client.get("/following/rolling/preview?tvdb_id=81797&start=2000", headers=HX)
        self.assertIn("has episodes 1–1180", bad.text)


if __name__ == "__main__":
    unittest.main()
