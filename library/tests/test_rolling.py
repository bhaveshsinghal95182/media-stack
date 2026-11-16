import json
import os
import tempfile
import unittest
import unittest.mock

from medialib import Kind, LibraryError
from medialib import rolling
from medialib.rolling import (RollingStore, RollingWindow, apply, done_ids, episode_order,
                              find_start, monitor_changes, number_of, plan_window, preview,
                              search_due, start_window, status, stop_window)

NOW = 1_800_000_000.0
PAST = "2020-01-01T00:00:00Z"
FUTURE = "2030-01-01T00:00:00Z"


def ep(i, season=1, number=None, absolute=None, **kw):
    """Episode id i, season `season`, episode number `number` (default i)."""
    d = {"id": i, "seasonNumber": season, "episodeNumber": number or i, "title": f"Ep {i}",
         "monitored": False, "hasFile": False, "airDateUtc": PAST}
    if absolute is not None:
        d["absoluteEpisodeNumber"] = absolute
    d.update(kw)
    return d


def ids(eps):
    return [e["id"] for e in eps]


class FakeSonarr:
    """A tiny stateful Sonarr: PUTs change what the next GET returns."""

    def __init__(self, episodes, seasons=(1,), monitored=False, monitor_new="all"):
        self.episodes = episodes
        self.series = {"id": 7, "title": "One Piece", "year": 1999, "tvdbId": 81797,
                       "monitored": monitored, "monitorNewItems": monitor_new,
                       "seasons": [{"seasonNumber": n, "monitored": monitored} for n in seasons]}
        self.history, self.files, self._queue = [], [], []
        self.sent, self.commands = [], []

    def watch(self, episode_id):
        """What the janitor does: unmonitor, delete the file, leave a history event."""
        e = next(e for e in self.episodes if e["id"] == episode_id)
        e.update(monitored=False, hasFile=False)
        self.history.append({"eventType": "episodeFileDeleted", "episodeId": episode_id,
                             "data": {"reason": "Manual"}})

    def get(self, path, **params):
        if path == "/history/series":
            return self.history
        if path == "/episode":
            return json.loads(json.dumps(self.episodes))  # a fresh copy, as over HTTP
        if path == "/episodefile":
            return self.files
        if path == "/series":
            return [self.series]
        if path == f"/series/{self.series['id']}":
            return json.loads(json.dumps(self.series))
        raise KeyError(path)

    def queue(self):
        return self._queue

    def command(self, body):
        self.commands.append(body)
        return {}

    def send(self, method, path, body=None, **params):
        self.sent.append((method, path, body))
        if path.startswith("/series/"):
            self.series = json.loads(json.dumps(body))
            # Sonarr unmonitors a season's episodes when its flag flips
            for s in body["seasons"]:
                if not s["monitored"]:
                    for e in self.episodes:
                        if e["seasonNumber"] == s["seasonNumber"]:
                            e["monitored"] = False
        elif path == "/episode/monitor":
            for e in self.episodes:
                if e["id"] in body["episodeIds"]:
                    e["monitored"] = body["monitored"]
        return body


class FakeJellyfin:
    def __init__(self, paths=(), fail=False):
        self.paths, self.fail = list(paths), fail

    def get(self, path, **params):
        if self.fail:
            raise LibraryError("Jellyfin is down")
        if path == "/Users":
            return [{"Id": "u1", "Name": "me"}]
        return {"Items": [{"Path": p} for p in self.paths]}


class FakeStack:
    def __init__(self, sonarr, jellyfin=None):
        self.sonarr = sonarr
        self.radarr = None
        self.jellyfin = jellyfin or FakeJellyfin()

    def kind(self, kind):
        return Kind(self, kind)


def long_show(n=30):
    return [ep(i, absolute=i + 100) for i in range(1, n + 1)]


class Helpers(unittest.TestCase):
    def test_order_drops_specials_and_sorts(self):
        eps = [ep(1, season=2, number=1), ep(2, season=0, number=1), ep(3, season=1, number=2),
               ep(4, season=1, number=1)]
        self.assertEqual(ids(episode_order(eps)), [4, 3, 1])

    def test_find_start_absolute(self):
        ordered = [ep(1, absolute=10), ep(2, absolute=11), ep(3, absolute=13)]
        self.assertEqual(find_start(ordered, 11), 1)
        self.assertEqual(find_start(ordered, 12), 2)  # the first episode at or after it
        self.assertEqual(number_of(ordered[2], 2), 13)

    def test_find_start_position_fallback(self):
        ordered = [ep(1), ep(2), ep(3)]
        self.assertEqual(number_of(ordered[1], 1), 2)
        self.assertEqual(find_start(ordered, 3), 2)

    def test_find_start_out_of_range(self):
        ordered = [ep(1), ep(2)]
        for bad in (0, -1, 3):
            with self.assertRaises(LibraryError):
                find_start(ordered, bad)
        with self.assertRaises(LibraryError):
            find_start([], 1)

    def test_plan_fresh_start(self):
        ordered = [ep(i) for i in range(1, 11)]
        frontier, window = plan_window(ordered, 2, set(), 3)
        self.assertEqual((frontier, ids(window)), (2, [3, 4, 5]))

    def test_plan_frontier_jumps_past_last_done(self):
        ordered = [ep(i) for i in range(1, 11)]
        frontier, window = plan_window(ordered, 0, {2, 5}, 3)  # 1, 3, 4 were skipped
        self.assertEqual((frontier, ids(window)), (5, [6, 7, 8]))

    def test_plan_ignores_done_before_start(self):
        ordered = [ep(i) for i in range(1, 11)]
        frontier, window = plan_window(ordered, 4, {1, 2}, 2)
        self.assertEqual((frontier, ids(window)), (4, [5, 6]))

    def test_plan_skips_done_inside_window(self):
        ordered = [ep(i) for i in range(1, 11)]
        frontier, window = plan_window(ordered, 0, {1}, 3)
        self.assertEqual((frontier, ids(window)), (1, [2, 3, 4]))

    def test_plan_short_near_the_end(self):
        ordered = [ep(i) for i in range(1, 6)]
        frontier, window = plan_window(ordered, 3, set(), 12)
        self.assertEqual(ids(window), [4, 5])
        frontier, window = plan_window(ordered, 0, {5}, 12)
        self.assertEqual((frontier, window), (5, []))

    def test_monitor_changes_leaves_files_and_queue(self):
        ordered = [ep(1, monitored=True, hasFile=True), ep(2, monitored=True),
                   ep(3, monitored=True), ep(4), ep(5, monitored=True)]
        to_mon, to_unmon = monitor_changes(ordered, {4, 5}, queued={3})
        self.assertEqual((to_mon, to_unmon), ([4], [2]))

    def test_search_due(self):
        window = [ep(1), ep(2, hasFile=True), ep(3), ep(4, airDateUtc=FUTURE), ep(5), ep(6)]
        searched = {"5": NOW - 60, "6": NOW - rolling.SEARCH_AGAIN_S}
        self.assertEqual(search_due(window, {3}, searched, NOW), [1, 6])


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sub", "rolling.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip(self):
        s = RollingStore(self.path)
        s.set(7, 892, 12)
        s.record_search(7, [1, 2], NOW)
        again = RollingStore(self.path)
        self.assertEqual(set(again.all()), {7})
        self.assertEqual((again.get(7)["start"], again.get(7)["ahead"]), (892, 12))
        self.assertEqual(again.searched(7), {"1": NOW, "2": NOW})
        with open(self.path) as f:
            self.assertIn("7", json.load(f)["series"])
        again.remove(7)
        self.assertEqual(RollingStore(self.path).all(), {})

    def test_set_keeps_since_and_searched(self):
        s = RollingStore(None)
        s.set(7, 10, 5)
        since = s.get(7)["since"]
        s.record_search(7, [3], NOW)
        s.set(7, 20, 8)
        self.assertEqual((s.get(7)["start"], s.get(7)["ahead"], s.get(7)["since"]), (20, 8, since))
        self.assertEqual(s.searched(7), {"3": NOW})

    def test_record_search_prunes_old(self):
        s = RollingStore(None)
        s.set(7, 1, 5)
        s.record_search(7, [1], NOW - 8 * 86400)
        s.record_search(7, [2], NOW - 6 * 86400)
        s.record_search(7, [3], NOW)
        self.assertEqual(set(s.searched(7)), {"2", "3"})

    def test_garbage_file(self):
        os.makedirs(os.path.dirname(self.path))
        for text in ("not json", "[1, 2]", '{"series": {"x": 1, "8": {"start": "a"}}}'):
            with open(self.path, "w") as f:
                f.write(text)
            self.assertEqual(RollingStore(self.path).all(), {})
        self.assertEqual(RollingStore(os.path.join(self.tmp.name, "missing.json")).all(), {})


class Apply(unittest.TestCase):
    def make(self, n=30, start=105, ahead=3, **kw):
        sonarr = FakeSonarr(long_show(n), **kw)
        stack = FakeStack(sonarr)
        store = RollingStore(None)
        store.set(7, start, ahead)
        return sonarr, stack, store

    def test_first_apply_orders_puts_and_searches(self):
        sonarr, stack, store = self.make(monitored=True, monitor_new="all")
        result = apply(stack, store, 7, now=NOW, watched=set())
        kinds = [(m, p) for m, p, _ in sonarr.sent]
        self.assertEqual(kinds, [("PUT", "/series/7"), ("PUT", "/episode/monitor")])
        series_put = sonarr.sent[0][2]
        self.assertEqual((series_put["monitored"], series_put["monitorNewItems"]), (True, "none"))
        self.assertFalse(any(s["monitored"] for s in series_put["seasons"]))
        self.assertEqual(sonarr.sent[1][2], {"episodeIds": [5, 6, 7], "monitored": True})
        self.assertEqual(sonarr.commands, [{"name": "EpisodeSearch", "episodeIds": [5, 6, 7]}])
        self.assertEqual((result["monitored"], result["unmonitored"], result["searched"]), (3, 0, 3))
        self.assertEqual(result["position"]["number"], 105)
        self.assertEqual(result["counts"]["looking"], 3)

    def test_window_survives_the_season_flag_reset(self):
        # Already monitored (e.g. followed "all" before): the series PUT makes Sonarr drop
        # every episode's flag, so the window must be re-monitored from a fresh list.
        sonarr, stack, store = self.make(monitored=True, monitor_new="all")
        for e in sonarr.episodes:
            e["monitored"] = True
        apply(stack, store, 7, now=NOW, watched=set())
        self.assertEqual([e["id"] for e in sonarr.episodes if e["monitored"]], [5, 6, 7])

    def test_series_put_only_when_needed(self):
        sonarr, stack, store = self.make(monitored=True, monitor_new="none")
        sonarr.series["seasons"][0]["monitored"] = False
        for e in sonarr.episodes[:2]:  # stale monitoring outside the window
            e["monitored"] = True
        apply(stack, store, 7, now=NOW, watched=set())
        self.assertEqual([(p, b["monitored"]) for _, p, b in sonarr.sent],
                         [("/episode/monitor", False), ("/episode/monitor", True)])
        self.assertEqual(sonarr.sent[0][2]["episodeIds"], [1, 2])

    def test_second_apply_sends_nothing(self):
        sonarr, stack, store = self.make()
        apply(stack, store, 7, now=NOW, watched=set())
        sonarr.sent.clear()
        sonarr.commands.clear()
        again = apply(stack, store, 7, now=NOW + 300, watched=set())
        self.assertEqual((sonarr.sent, sonarr.commands), ([], []))
        self.assertEqual((again["monitored"], again["unmonitored"], again["searched"]), (0, 0, 0))

    def test_watching_advances_window_and_searches(self):
        sonarr, stack, store = self.make()
        apply(stack, store, 7, now=NOW, watched=set())
        for i in (5, 6, 7):  # downloaded, then watched and removed by the janitor
            sonarr.episodes[i - 1]["hasFile"] = True
        sonarr.watch(5)
        sonarr.sent.clear()
        sonarr.commands.clear()
        result = apply(stack, store, 7, now=NOW + 300, watched=set())
        self.assertEqual(result["position"]["number"], 106)
        self.assertEqual([e["id"] for e in sonarr.episodes if e["monitored"]], [6, 7, 8])
        self.assertEqual(sonarr.commands, [{"name": "EpisodeSearch", "episodeIds": [8]}])
        self.assertEqual(result["monitored"], 1)

    def test_jellyfin_played_file_counts_as_done(self):
        sonarr, stack, store = self.make()
        sonarr.files = [{"id": 90, "path": "/tv/ep5.mkv"}, {"id": 91, "path": "/tv/ep6.mkv"}]
        sonarr.episodes[4].update(hasFile=True, episodeFileId=90)
        sonarr.episodes[5].update(hasFile=True, episodeFileId=91)
        done = done_ids(Kind(stack, "show"), 7, sonarr.episodes, {"/tv/ep5.mkv"})
        self.assertEqual(done, {5})
        result = apply(stack, store, 7, now=NOW, watched={"/tv/ep5.mkv"})
        self.assertEqual(result["position"]["number"], 106)

    def test_jellyfin_down_falls_back_to_history(self):
        sonarr, stack, store = self.make()
        stack.jellyfin = FakeJellyfin(fail=True)
        sonarr.watch(5)
        self.assertEqual(apply(stack, store, 7, now=NOW)["position"]["number"], 106)

    def test_status_shape_and_states(self):
        sonarr, stack, store = self.make(n=8, start=105, ahead=4)
        sonarr.episodes[4]["hasFile"] = True
        sonarr._queue = [{"seriesId": 7, "episodeId": 6}]
        sonarr.episodes[6]["airDateUtc"] = FUTURE
        sonarr.episodes[7]["airDateUtc"] = FUTURE
        st = status(stack, store, 7, now=NOW)
        self.assertEqual([r["state"] for r in st["window"]],
                         ["on disk", "downloading", "to air", "to air"])
        self.assertEqual(st["counts"], {"on disk": 1, "downloading": 1, "looking": 0, "to air": 2})
        self.assertTrue(st["caughtUp"])
        self.assertEqual(st["window"][0]["code"], "S01E05")
        self.assertEqual(sonarr.sent, [])  # read-only
        self.assertIsNone(status(stack, RollingStore(None), 7))

    def test_not_caught_up_when_window_is_short_of_aired(self):
        sonarr, stack, store = self.make(ahead=3)
        self.assertFalse(status(stack, store, 7, now=NOW)["caughtUp"])

    def test_position_none_past_the_end(self):
        sonarr, stack, store = self.make(n=6, start=105, ahead=3)
        sonarr.watch(6)
        st = status(stack, store, 7, now=NOW)
        self.assertIsNone(st["position"])
        self.assertEqual(st["window"], [])

    def test_missing_config(self):
        _, stack, _ = self.make()
        with self.assertRaises(LibraryError):
            apply(stack, RollingStore(None), 7)


class StartStop(unittest.TestCase):
    def test_bad_start_stores_nothing(self):
        sonarr = FakeSonarr(long_show(10))
        stack, store = FakeStack(sonarr), RollingStore(None)
        stack.sonarr.get = sonarr.get
        with unittest.mock.patch.object(Kind, "in_library", lambda self, t: sonarr.series):
            with self.assertRaises(LibraryError):
                start_window(stack, store, 81797, 5000, 12)
            for start, ahead in ((0, 12), (5, 0), (5, rolling.MAX_AHEAD + 1)):
                with self.assertRaises(LibraryError):
                    start_window(stack, store, 81797, start, ahead)
        self.assertEqual(store.all(), {})
        self.assertEqual((sonarr.sent, sonarr.commands), ([], []))

    def test_start_existing_series(self):
        sonarr = FakeSonarr(long_show(30))
        stack, store = FakeStack(sonarr), RollingStore(None)
        with unittest.mock.patch.object(Kind, "in_library", lambda self, t: sonarr.series):
            res = start_window(stack, store, 81797, 105, 3)
        self.assertFalse(res["added"])
        self.assertEqual(store.get(7)["start"], 105)
        self.assertIn('One Piece from episode 105 "Ep 5" (S01E05): keeping the next 3 episodes '
                      "on disk; searching 3 now.", res["message"])

    def test_start_adds_missing_series_without_search(self):
        sonarr = FakeSonarr(long_show(10))
        stack, store = FakeStack(sonarr), RollingStore(None)
        posted = []

        def send(method, path, body=None, **params):
            if method == "POST":
                posted.append(body)
                return dict(sonarr.series, monitored=True)
            return FakeSonarr.send(sonarr, method, path, body)

        sonarr.send = send
        lookup = {"title": "One Piece", "tvdbId": 81797, "genres": ["Anime"]}
        with unittest.mock.patch.object(Kind, "in_library", lambda self, t: None), \
                unittest.mock.patch.object(Kind, "lookup_id", lambda self, t: lookup), \
                unittest.mock.patch.object(Kind, "profile_and_root", lambda self, a=False: (1, "/tv")), \
                unittest.mock.patch("medialib.rolling.refresh_series") as refresh:
            res = start_window(stack, store, 81797, 1, 3)
        self.assertTrue(res["added"])
        self.assertEqual(posted[0]["monitorNewItems"], "none")
        self.assertEqual(posted[0]["addOptions"], {"monitor": "none",
                                                   "searchForMissingEpisodes": False})
        refresh.assert_called_once_with(sonarr, 7, new=True)

    def test_stop_unmonitors_only_waiting_episodes(self):
        sonarr = FakeSonarr(long_show(6), monitored=True)
        for e in sonarr.episodes:
            e["monitored"] = True
        sonarr.episodes[0]["hasFile"] = True
        sonarr._queue = [{"seriesId": 7, "episodeId": 2}]
        stack, store = FakeStack(sonarr), RollingStore(None)
        store.set(7, 1, 3)
        res = stop_window(stack, store, 7)
        self.assertIsNone(store.get(7))
        self.assertEqual(sonarr.sent, [("PUT", "/episode/monitor",
                                        {"episodeIds": [3, 4, 5, 6], "monitored": False})])
        self.assertIn("One Piece: rolling window off.", res["message"])
        self.assertTrue(sonarr.series["monitored"])

    def test_preview_uses_skyhook_and_caches(self):
        rolling._skyhook.clear()
        show = {"title": "One Piece", "episodes": [
            {"seasonNumber": 0, "episodeNumber": 1, "title": "Special"},
            {"seasonNumber": 1, "episodeNumber": 1, "absoluteEpisodeNumber": 1, "title": "A",
             "airDateUtc": PAST},
            {"seasonNumber": 1, "episodeNumber": 2, "absoluteEpisodeNumber": 2, "title": "B"}]}
        stack = FakeStack(FakeSonarr([]))
        with unittest.mock.patch.object(Kind, "in_library", lambda self, t: None), \
                unittest.mock.patch("medialib.rolling.fetch_public_json", return_value=show) as f:
            got = preview(stack, 81797, 2)
            self.assertEqual((got["title"], got["code"], got["last"]), ("B", "S01E02", 2))
            with self.assertRaises(LibraryError) as cm:
                preview(stack, 81797, 3)
            self.assertIn("One Piece has episodes 1–2", str(cm.exception))
            self.assertEqual(f.call_count, 1)
        rolling._skyhook.clear()


class Service(unittest.TestCase):
    def test_empty_store_makes_no_stack(self):
        made = []
        svc = RollingWindow(lambda: made.append(1), RollingStore(None))
        svc.cycle(NOW)
        self.assertEqual(made, [])

    def test_cycle_applies_and_logs_changes_once(self):
        sonarr = FakeSonarr(long_show(30), monitored=True, monitor_new="none")
        store = RollingStore(None)
        store.set(7, 105, 3)
        svc = RollingWindow(lambda: FakeStack(sonarr), store)
        svc.cycle(NOW)
        svc.cycle(NOW + 300)
        st = svc.status()
        self.assertEqual(len(st["log"]), 1)
        self.assertEqual(st["log"][0]["msg"],
                         "One Piece: now at episode 105; 3 more wanted, searching 3")
        self.assertEqual((st["lastRun"], st["lastError"], st["series"]), (NOW + 300, None, 1))
        self.assertEqual(svc.series_ids(), {7})

    def test_cycle_drops_deleted_and_unmonitored(self):
        sonarr = FakeSonarr(long_show(5), monitored=False)
        store = RollingStore(None)
        store.set(7, 1, 3)
        store.set(99, 1, 3)  # not in Sonarr
        svc = RollingWindow(lambda: FakeStack(sonarr), store)
        svc.cycle(NOW)
        self.assertEqual(store.all(), {})
        self.assertEqual(sonarr.sent, [])  # the unfollowed series is left alone
        logs = " | ".join(e["msg"] for e in svc.status()["log"])
        self.assertIn("deleted from Sonarr: rolling window removed", logs)
        self.assertIn("One Piece is no longer followed", logs)

    def test_cycle_survives_one_bad_series(self):
        sonarr = FakeSonarr(long_show(5), monitored=True)
        store = RollingStore(None)
        store.set(7, 999, 3)  # past the end
        svc = RollingWindow(lambda: FakeStack(sonarr), store)
        svc.cycle(NOW)
        self.assertIn("Error:", svc.status()["log"][0]["msg"])
        self.assertIsNotNone(store.get(7))

    def test_log_is_capped(self):
        svc = RollingWindow(lambda: None, RollingStore(None), log_size=2)
        for i in range(5):
            svc._log(NOW, str(i))
        self.assertEqual([e["msg"] for e in svc.status()["log"]], ["4", "3"])


if __name__ == "__main__":
    unittest.main()
