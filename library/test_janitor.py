import json
import os
import tempfile
import unittest

import janitor as j

DAY = 86400


def jf_item(path, typ="Episode", played=None, fav=False, **kw):
    return dict({"Path": path, "Type": typ, "Id": kw.pop("Id", path), "Name": "Ep",
                 "SeriesName": "Show", "ParentIndexNumber": 1, "IndexNumber": 2,
                 "UserData": {"LastPlayedDate": played, "IsFavorite": fav}}, **kw)


class Helpers(unittest.TestCase):
    def test_parse_jf_date(self):
        self.assertEqual(j.parse_jf_date("1970-01-02T00:00:00.0000000Z"), DAY)
        self.assertIsNone(j.parse_jf_date(None))
        self.assertIsNone(j.parse_jf_date("yesterday"))

    def test_merge_latest_play_wins_and_favourites(self):
        p = "/data/media/tv/Show/Season 01/e2.mkv"
        seen = {"/data/media/tv/gone.mkv": 5}
        out = j.merge_watched([
            ("a", [jf_item(p, played="1970-01-02T00:00:00Z"),
                   jf_item("/elsewhere/x.mkv", played="1970-01-02T00:00:00Z")]),
            ("b", [jf_item(p, played="1970-01-03T00:00:00Z")]),
        ], set(), seen, now=10 * DAY)
        self.assertEqual(list(out), [p])
        self.assertEqual(out[p]["watchedAt"], 2 * DAY)
        self.assertEqual(out[p]["users"], ["a", "b"])
        self.assertFalse(out[p]["favourite"])
        self.assertNotIn("/data/media/tv/gone.mkv", seen)  # pruned
        fav = j.merge_watched([("a", [jf_item(p, SeriesId="S1")])], {"S1"}, {}, now=0)
        self.assertTrue(fav[p]["favourite"])

    def test_marked_watched_without_date_counts_from_first_seen(self):
        p = "/data/media/movies/M (2020)/M.mkv"
        seen = {}
        first = j.merge_watched([("a", [jf_item(p, "Movie")])], set(), seen, now=100)
        later = j.merge_watched([("a", [jf_item(p, "Movie")])], set(), seen, now=999)
        self.assertEqual(first[p]["watchedAt"], 100)
        self.assertEqual(later[p]["watchedAt"], 100)

    def test_is_due(self):
        e = {"favourite": False, "watchedAt": 0}
        self.assertFalse(j.is_due(e, 2 * DAY, 3 * DAY, low_space=False))
        self.assertTrue(j.is_due(e, 3 * DAY, 3 * DAY, low_space=False))
        self.assertTrue(j.is_due(e, 1, 3 * DAY, low_space=True))
        self.assertFalse(j.is_due(dict(e, favourite=True), 9 * DAY, 3 * DAY, low_space=True))

    def test_guard_action(self):
        self.assertEqual(j.guard_action(10, False, 20, 25), "pause")
        self.assertEqual(j.guard_action(10, True, 20, 25), "pause")
        self.assertIsNone(j.guard_action(22, True, 20, 25))   # between: stay paused
        self.assertEqual(j.guard_action(30, True, 20, 25), "resume")
        self.assertIsNone(j.guard_action(30, False, 20, 25))

    def test_releasable(self):
        self.assertFalse(j.releasable([]))
        self.assertTrue(j.releasable([1, 1]))
        self.assertFalse(j.releasable([1, 2]))


class FakeSonarr:
    def __init__(self, files):
        self.files, self.calls = files, []

    def get(self, path, **params):
        if path == "/episodefile":
            return [{"id": i, "path": p} for i, p in self.files.items()]
        if path == "/episode":
            return [{"id": i * 10, "episodeFileId": i} for i in self.files]
        raise AssertionError(path)

    def send(self, method, path, body=None, **params):
        self.calls.append((method, path, body))
        if method == "DELETE":
            os.remove(self.files.pop(int(path.rsplit("/", 1)[1])))


class FakeQB:
    def __init__(self, save_path, names):
        self.save_path, self.names, self.deleted = save_path, names, []

    def torrents(self):
        return [] if self.deleted else [{"hash": "h1", "progress": 1, "name": "Pack",
                                         "save_path": self.save_path, "size": 10}]

    def call(self, path, data=None):
        assert path == "torrents/files"
        return json.dumps([{"name": n} for n in self.names]).encode()

    def delete(self, hashes):
        self.deleted += hashes


class FakeStack:
    def __init__(self, sonarr, qb):
        self.sonarr, self.qb = sonarr, qb


class EpisodeRemoval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        dl, self.show = os.path.join(root, "dl"), os.path.join(root, "media/tv/Show")
        os.makedirs(os.path.join(dl, "Pack"))
        os.makedirs(os.path.join(self.show, "Season 01"))
        self.lib = {}
        for n in (1, 2):  # a two-episode season pack, both imported as hardlinks
            src = os.path.join(dl, "Pack", f"e{n}.mkv")
            with open(src, "w") as f:
                f.write("x")
            self.lib[n] = os.path.join(self.show, "Season 01", f"e{n}.mkv")
            os.link(src, self.lib[n])
        self.sonarr = FakeSonarr(dict(self.lib))
        self.qb = FakeQB(dl, ["Pack/e1.mkv", "Pack/e2.mkv"])
        self.stack = FakeStack(self.sonarr, self.qb)
        self.jan = j.Janitor(lambda: self.stack, None)
        self.series = [{"id": 1, "path": self.show}]

    def tearDown(self):
        self.tmp.cleanup()

    def entry(self, n):
        return {"path": self.lib[n], "kind": "episode", "series": "Show", "season": 1,
                "episode": n, "users": ["a"], "title": "", "watchedAt": 0, "favourite": False}

    def test_unmonitors_first_and_keeps_pack_until_all_watched(self):
        self.jan._inodes = None
        self.jan._remove_episode(self.stack, self.entry(1), self.series)
        self.assertEqual(self.sonarr.calls[0],
                         ("PUT", "/episode/monitor", {"episodeIds": [10], "monitored": False}))
        self.assertEqual(self.sonarr.calls[1][:2], ("DELETE", "/episodefile/1"))
        self.assertEqual(self.jan.state["pending"], ["h1"])
        self.jan._release_torrents(self.stack)
        self.assertEqual(self.qb.deleted, [])          # e2 is still in the library
        self.assertEqual(self.jan.state["pending"], ["h1"])

        self.jan._inodes = None
        self.jan._remove_episode(self.stack, self.entry(2), self.series)
        self.jan._release_torrents(self.stack)
        self.assertEqual(self.qb.deleted, ["h1"])
        self.assertEqual(self.jan.state["pending"], [])

    def test_unknown_series_is_left_alone(self):
        self.jan._remove_episode(self.stack, self.entry(1), [{"id": 9, "path": "/other"}])
        self.assertEqual(self.sonarr.calls, [])
        self.assertTrue(os.path.exists(self.lib[1]))


if __name__ == "__main__":
    unittest.main()


class ForceClean(unittest.TestCase):
    def test_force_skips_grace_and_refuses_while_a_check_runs(self):
        jan = j.Janitor(lambda: None, None)
        calls = []
        jan.clean = lambda stack, now, low: calls.append(low)
        self.assertTrue(jan.force_clean())
        self.assertEqual(calls, [True])                 # low=True: grace period ignored
        jan.tick_lock.acquire()
        self.assertFalse(jan.force_clean())
        self.assertEqual(calls, [True])
