import os
import tempfile
import unittest

from medialib import stalls as s
from medialib.errors import LibraryError
from medialib.stack import Kind

MIN = 60
STALL = 3600
MINP = 10 * s.MIB


def tor(h="aa", completed=0, state="downloading", progress=0.5, name="Rel", category="radarr"):
    return {"hash": h, "name": name, "progress": progress, "state": state,
            "completed": completed, "category": category}


def fresh():
    return {"lastCheck": None, "torrents": {}}


def run(state, torrents, now):
    return s.track(state, torrents, now, STALL, MINP)


class Track(unittest.TestCase):
    def test_new_torrent_starts_clock(self):
        st = fresh()
        self.assertEqual(run(st, [tor("AA", 100)], 1000), [])
        e = st["torrents"]["aa"]
        self.assertEqual((e["since"], e["completed"], e["name"]), (1000, 100, "Rel"))
        self.assertEqual(st["lastCheck"], 1000)

    def test_progress_resets_small_progress_stalls(self):
        st = fresh()
        run(st, [tor(completed=0)], 0)
        t = 500
        run(st, [tor(completed=MINP)], t)            # big gain: reset
        self.assertEqual(st["torrents"]["aa"]["since"], t)
        for now in range(t + 100, t + STALL, 100):
            self.assertEqual(run(st, [tor(completed=MINP + 1000)], now), [])
        self.assertEqual(st["torrents"]["aa"]["since"], t)  # small gain never resets
        self.assertEqual(run(st, [tor(completed=MINP + 1000)], t + STALL), ["aa"])

    def test_non_active_dropped_and_clock_restarts(self):
        st = fresh()
        run(st, [tor()], 0)
        for state, prog in [("queuedDL", 0.5), ("stoppedDL", 0.5), ("checkingDL", 0.5),
                            ("error", 0.5), ("downloading", 1.0), ("uploading", 1.0)]:
            run(st, [tor(state=state, progress=prog)], 100)
            self.assertEqual(st["torrents"], {}, state)
            run(st, [tor()], 100)
        run(st, [tor(state="stoppedDL")], 200)
        self.assertEqual(run(st, [tor()], 5000), [])  # restarted at 5000, not stalled
        self.assertEqual(st["torrents"]["aa"]["since"], 5000)

    def test_vanished_pruned(self):
        st = fresh()
        run(st, [tor("aa"), tor("bb")], 0)
        run(st, [tor("bb")], 60)
        self.assertEqual(list(st["torrents"]), ["bb"])

    def test_reset_gap_restarts_clocks(self):
        st = fresh()
        run(st, [tor()], 0)
        self.assertEqual(run(st, [tor()], s.RESET_GAP + 1), [])
        self.assertEqual(st["torrents"]["aa"]["since"], s.RESET_GAP + 1)
        # a gap of exactly RESET_GAP is not a reset
        st = fresh()
        run(st, [tor()], 0)
        run(st, [tor()], s.RESET_GAP)
        self.assertEqual(st["torrents"]["aa"]["since"], 0)

    def test_completed_going_down_resets(self):
        st = fresh()
        run(st, [tor(completed=5 * s.MIB)], 0)
        run(st, [tor(completed=s.MIB)], 100)
        e = st["torrents"]["aa"]
        self.assertEqual((e["since"], e["completed"]), (100, s.MIB))

    def test_oldest_first(self):
        st = fresh()
        run(st, [tor("cc")], 0)
        run(st, [tor("cc"), tor("aa")], 100)
        run(st, [tor("cc"), tor("aa"), tor("bb")], 200)
        for now in range(300, STALL + 200, s.TICK):
            run(st, [tor("bb"), tor("aa"), tor("cc")], now)
        self.assertEqual(run(st, [tor("bb"), tor("aa"), tor("cc")], STALL + 200),
                         ["cc", "aa", "bb"])


class FakeQB:
    def __init__(self, torrents):
        self.items = torrents

    def torrents(self):
        return self.items


class FakeArr:
    def __init__(self, name, records, gets=None):
        self.name, self.records, self.calls, self.queue_calls = name, records, [], 0
        self.fail_ids: set[int] = set()
        self.gets = gets or {}   # path -> answer
        self.searches, self.grabs = [], []

    def get(self, path, timeout=60, **params):
        if path == "/release":
            self.searches.append(params)
        return self.gets.get(path, {"records": []} if path == "/history" else [])

    def send(self, method, path, body=None, **params):
        assert (method, path) == ("POST", "/release")
        self.grabs.append(body)

    def queue(self):
        self.queue_calls += 1
        return self.records

    def remove_from_queue(self, ids, blocklist, search_again):
        if self.fail_ids & set(ids):
            raise LibraryError("boom")
        self.calls.append((list(ids), blocklist, search_again))


class FakeStack:
    def __init__(self, torrents, radarr=None, sonarr=None):
        self.qb = FakeQB(torrents)
        self.radarr = radarr or FakeArr("Radarr", [])
        self.sonarr = sonarr or FakeArr("Sonarr", [])

    def kind(self, kind):
        return Kind(self, kind)


def rotator(stack, path=None, **kw):
    return s.StallRotator(lambda: stack, path, stall_minutes=60, min_progress=MINP, **kw)


def stall(r, now=STALL):
    """Tick every TICK seconds (a longer gap would reset the clocks); the last tick's result."""
    for t in range(0, now, s.TICK):
        r.tick(t)
    return r.tick(now)


class Rotator(unittest.TestCase):
    def test_rotates_via_right_arr(self):
        stack = FakeStack(
            [tor("aa", name="Movie", category="radarr", progress=0.123),
             tor("bb", name="Show", category="tv-sonarr")],
            radarr=FakeArr("Radarr", [{"id": 7, "downloadId": "AA"}, {"id": 8, "downloadId": "zz"}]),
            sonarr=FakeArr("Sonarr", [{"id": 9, "downloadId": "BB"}]))
        r = rotator(stack)
        self.assertEqual(sorted(stall(r)), ["Movie", "Show"])
        self.assertEqual(stack.radarr.calls, [([7], True, True)])
        self.assertEqual(stack.sonarr.calls, [([9], True, True)])
        self.assertEqual(r.state["torrents"], {})
        self.assertEqual(stack.radarr.queue_calls, 1)
        msg = [x["msg"] for x in r.log if "Movie" in x["msg"]][0]
        self.assertEqual(msg, "Rotated Movie: no progress for 60 min at 12.3%; blocklisted, "
                              "Radarr is searching for another release")

    def test_queue_fetched_once_per_tick(self):
        stack = FakeStack([tor("aa", category="radarr"), tor("bb", category="radarr")],
                          radarr=FakeArr("Radarr", [{"id": 1, "downloadId": "aa"},
                                                    {"id": 2, "downloadId": "bb"}]))
        stall(rotator(stack))
        self.assertEqual(stack.radarr.queue_calls, 1)
        self.assertEqual(len(stack.radarr.calls), 2)

    def test_not_in_queue_logged_once_and_kept(self):
        stack = FakeStack([tor("aa", name="Orphan")])
        r = rotator(stack)
        stall(r)
        r.tick(STALL + 100)
        msgs = [x["msg"] for x in r.log]
        self.assertEqual(msgs, ["Orphan has stalled, but it isn't in Radarr's queue; left alone"])
        self.assertIn("aa", r.state["torrents"])
        self.assertEqual(stack.radarr.calls, [])

    def test_library_error_logged_others_rotate(self):
        stack = FakeStack(
            [tor("aa", name="Bad"), tor("bb", name="Good")],
            radarr=FakeArr("Radarr", [{"id": 1, "downloadId": "aa"}, {"id": 2, "downloadId": "bb"}]))
        stack.radarr.fail_ids = {1}
        r = rotator(stack)
        self.assertEqual(stall(r), ["Good"])
        self.assertIn("Couldn't rotate Bad: boom", [x["msg"] for x in r.log])
        self.assertIn("aa", r.state["torrents"])
        self.assertNotIn("bb", r.state["torrents"])

    def test_max_per_pass(self):
        n = s.MAX_PER_PASS + 3
        stack = FakeStack([tor(f"h{i:02d}", name=f"T{i}") for i in range(n)],
                          radarr=FakeArr("Radarr", [{"id": i, "downloadId": f"h{i:02d}"}
                                                    for i in range(n)]))
        r = rotator(stack)
        self.assertEqual(len(stall(r)), s.MAX_PER_PASS)
        self.assertEqual(len(r.state["torrents"]), 3)

    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "stalls.json")
            stack = FakeStack([tor("aa", completed=42)])
            r = rotator(stack, path)
            r.tick(100)
            self.assertTrue(os.path.exists(path))
            r2 = rotator(stack, path)
            self.assertEqual(r2.state, r.state)
            self.assertEqual(r2.state["torrents"]["aa"]["completed"], 42)
            with open(path, "w") as f:
                f.write('["not", "a dict"]')
            self.assertEqual(rotator(stack, path).state,
                             {"lastCheck": None, "torrents": {}, "fallback": {}})

    def test_status_lists_only_after_show_after(self):
        stack = FakeStack([tor("aa", name="Slow")])
        r = rotator(stack)
        r.tick(0)
        r.tick(s.SHOW_AFTER - 1)
        self.assertEqual(r.status(s.SHOW_AFTER - 1)["stalling"], [])
        r.tick(s.SHOW_AFTER)
        st = r.status(s.SHOW_AFTER)
        self.assertEqual(len(st["stalling"]), 1)
        row = st["stalling"][0]
        self.assertEqual((row["hash"], row["name"], row["since"], row["rotateAt"]),
                         ("aa", "Slow", 0, STALL))
        self.assertEqual((st["stallMinutes"], st["minProgress"], st["lastRun"], st["lastError"]),
                         (60, MINP, s.SHOW_AFTER, None))
        row["name"] = "changed"
        self.assertEqual(r.state["torrents"]["aa"]["name"], "Slow")

    def test_failed_sets_error(self):
        r = rotator(FakeStack([]))
        r._failed(RuntimeError("down"))
        st = r.status(0)
        self.assertEqual(st["lastError"], "down")
        self.assertEqual(st["log"][0]["msg"], "Error: down")


def rel(guid, quality="WEBDL-1080p", res=1080, seeders=10, rejections=(), eps=(1,),
        series=5, movie=None, score=0):
    r = {"guid": guid, "indexerId": 1, "title": guid, "seeders": seeders,
         "quality": {"quality": {"name": quality, "resolution": res}},
         "rejections": list(rejections), "rejected": bool(rejections), "customFormatScore": score}
    if movie is not None:
        r["mappedMovieId"] = movie
    else:
        r.update(mappedSeriesId=series, mappedEpisodeInfo=[{"id": e} for e in eps])
    return r


LOW = "HDTV-1080p is not wanted in profile"


class PickFallback(unittest.TestCase):
    def pick(self, releases, wanted=frozenset({1})):
        found = s.pick_fallback(releases, "show", 5, set(wanted))
        return found and found["guid"]

    def test_soft_rejections_are_the_live_texts(self):
        for text in (LOW, "Custom Formats Anime LQ Groups have score -10000 below Series "
                          "profile minimum 0",
                     "100.5 MB is smaller than minimum allowed 200 MB (for 24min)",
                     "9 GB is larger than maximum allowed 5 GB"):
            self.assertTrue(s.is_soft(text), text)
        for text in ("Release is blocklisted", "Not enough seeders: 0. Minimum seeders: 1",
                     "Episode wasn't requested: 1x02", "Unknown Series",
                     "Release in queue already meets cutoff: WEBDL-1080p v1",
                     "Language Hindi is not wanted in profile"):
            self.assertFalse(s.is_soft(text), text)

    def test_allowed_beats_low_quality(self):
        self.assertEqual(self.pick([rel("low", rejections=[LOW], seeders=90),
                                    rel("ok", seeders=5)]), "ok")

    def test_low_quality_when_nothing_else(self):
        self.assertEqual(self.pick([rel("bl", rejections=["Release is blocklisted"]),
                                    rel("low", rejections=[LOW, "Release is blocklisted"]),
                                    rel("low2", quality="HDTV-720p", res=720,
                                        rejections=[LOW])]), "low2")

    def test_never_cam_unknown_or_unseeded(self):
        self.assertIsNone(self.pick([rel("cam", quality="CAM", res=480, rejections=[LOW]),
                                     rel("unk", quality="Unknown", res=0),
                                     rel("dead", seeders=0)]))

    def test_well_seeded_then_coverage_then_resolution(self):
        self.assertEqual(self.pick([rel("1080", seeders=1), rel("720", res=720, seeders=3)]),
                         "720")
        self.assertEqual(self.pick([rel("single"), rel("pack", res=720, eps=(1, 2))],
                                   wanted={1, 2}), "pack")
        self.assertEqual(self.pick([rel("720", res=720), rel("1080")]), "1080")

    def test_must_be_this_title(self):
        self.assertIsNone(self.pick([rel("other", series=6), rel("noep", eps=(9,))]))
        found = s.pick_fallback([rel("m", movie=3), rel("m2", movie=4)], "movie", 4, set())
        self.assertEqual(found["guid"], "m2")


class Fallback(unittest.TestCase):
    def setUp(self):
        self.sonarr = FakeArr("Sonarr", [{"id": 9, "downloadId": "bb", "seriesId": 5,
                                          "episodeId": 1}],
                              gets={"/episode": [{"id": 1, "seasonNumber": 1, "hasFile": False,
                                                  "monitored": True}],
                                    "/series/5": {"title": "Show"},
                                    "/release": [rel("low", rejections=[LOW])]})
        self.stack = FakeStack([tor("bb", name="Dead.S01E01", category="tv-sonarr")],
                               sonarr=self.sonarr)
        self.r = rotator(self.stack)
        stall(self.r)
        self.sonarr.records = []          # removed from the queue; the Arr found nothing
        self.stack.qb.items = []

    def test_grabs_a_lower_quality_after_the_arr_found_nothing(self):
        self.r.tick(STALL + s.FALLBACK_AFTER - 1)
        self.assertEqual(self.sonarr.searches, [])      # the Arr gets its turn first
        self.r.tick(STALL + s.FALLBACK_AFTER)
        self.assertEqual(self.sonarr.searches, [{"episodeId": 1}])
        self.assertEqual(self.sonarr.grabs, [{"guid": "low", "indexerId": 1}])
        self.assertIn("below the profile: " + LOW, self.r.log[-1]["msg"])
        self.r.tick(STALL + s.FALLBACK_AFTER + s.TICK)
        self.assertEqual(len(self.sonarr.searches), 1)  # one try only

    def test_nothing_when_the_arr_grabbed_one_itself(self):
        self.sonarr.records = [{"id": 10, "downloadId": "cc", "seriesId": 5, "episodeId": 1}]
        self.r.tick(STALL + s.FALLBACK_AFTER)
        self.assertEqual((self.sonarr.searches, self.sonarr.grabs), ([], []))

    def test_no_release_is_logged(self):
        self.sonarr.gets["/release"] = [rel("bl", rejections=["Release is blocklisted"])]
        self.r.tick(STALL + s.FALLBACK_AFTER)
        self.assertEqual(self.sonarr.grabs, [])
        self.assertIn("No release at all for 1 episode(s) of Show", self.r.log[-1]["msg"])

    def test_movie_with_a_file_is_left_alone(self):
        radarr = FakeArr("Radarr", [{"id": 7, "downloadId": "aa", "movieId": 3}],
                         gets={"/movie/3": {"title": "Film", "hasFile": True, "monitored": True}})
        stack = FakeStack([tor("aa")], radarr=radarr)
        r = rotator(stack)
        stall(r)
        radarr.records, stack.qb.items = [], []
        r.tick(STALL + s.FALLBACK_AFTER)
        self.assertEqual(radarr.searches, [])


if __name__ == "__main__":
    unittest.main()
