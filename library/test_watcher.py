import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import watcher


AIR = datetime(2026, 10, 4, 13, 30, tzinfo=timezone.utc)
AIR_TS = AIR.timestamp()


def episode(episode_id=1, aired=AIR, **changes):
    ep = {"id": episode_id, "airDateUtc": aired.isoformat().replace("+00:00", "Z"),
          "hasFile": False, "monitored": True, "seasonNumber": 2,
          "episodeNumber": 5, "title": "Aired", "series": {"title": "Frieren",
                                                        "monitored": True}}
    ep.update(changes)
    return ep


class FakeSonarr:
    def __init__(self, episodes, queue=None):
        self.episodes = episodes
        self.queue = queue or []
        self.get_calls = []
        self.send_calls = []

    def get(self, path, **params):
        self.get_calls.append((path, params))
        if path == "/calendar":
            return self.episodes
        if path == "/queue":
            return {"records": self.queue}
        if path == "/history":
            return {"records": [{"eventType": "grabbed", "episodeId": 4},
                                {"eventType": "downloadFailed", "episodeId": 5}]}
        raise AssertionError(path)

    def send(self, method, path, body=None, **params):
        self.send_calls.append((method, path, body, params))
        return {"id": 99}


class FakeStack:
    def __init__(self, sonarr):
        self.sonarr = sonarr


class ParsingAndSchedule(unittest.TestCase):
    def test_parse_air(self):
        self.assertAlmostEqual(watcher.parse_air("2026-10-04T13:30:00Z"), AIR_TS)
        self.assertAlmostEqual(watcher.parse_air("2026-10-04T13:30:00.1234567Z"),
                               AIR_TS + 0.123456, places=5)
        self.assertIsNone(watcher.parse_air(None))
        self.assertIsNone(watcher.parse_air(""))
        self.assertIsNone(watcher.parse_air("not-a-date"))

    def test_schedule_and_gap(self):
        self.assertEqual(watcher.next_search(None, AIR_TS, AIR_TS + 4 * 60), AIR_TS + 5 * 60)
        self.assertEqual(watcher.next_search(None, AIR_TS, AIR_TS + 5 * 60), AIR_TS + 5 * 60)
        first = {"attempts": 1, "last": AIR_TS + 5 * 60}
        self.assertEqual(watcher.next_search(first, AIR_TS, AIR_TS + 19 * 60),
                         AIR_TS + 20 * 60)
        self.assertEqual(watcher.next_search(first, AIR_TS, AIR_TS + 20 * 60),
                         AIR_TS + 20 * 60)
        late = {"attempts": 1, "last": AIR_TS + 19 * 60}
        self.assertEqual(watcher.next_search(late, AIR_TS, AIR_TS + 20 * 60),
                         AIR_TS + 23 * 60)

    def test_repeat_and_give_up(self):
        last = AIR_TS + 720 * 60
        entry = {"attempts": len(watcher.SCHEDULE_MIN), "last": last}
        self.assertEqual(watcher.next_search(entry, AIR_TS, last + 719 * 60),
                         last + 720 * 60)
        self.assertEqual(watcher.next_search(entry, AIR_TS, last + 720 * 60),
                         last + 720 * 60)
        self.assertIsNone(watcher.next_search(None, AIR_TS,
                                              AIR_TS + watcher.GIVE_UP_DAYS * 86400 + 1))
        self.assertIsNotNone(watcher.next_search(None, AIR_TS,
                                                 AIR_TS + watcher.GIVE_UP_DAYS * 86400))

    def test_record_search_and_label(self):
        state = {}
        watcher.record_search(state, [1], AIR_TS)
        watcher.record_search(state, [1], AIR_TS + 60)
        self.assertEqual(state, {"1": {"attempts": 2, "last": AIR_TS + 60}})
        self.assertEqual(watcher.episode_label(episode()), "Frieren S02E05")
        self.assertEqual(watcher.episode_label(episode(series={})), "Episode 1")


class Selection(unittest.TestCase):
    def test_skip_conditions_and_purity(self):
        now = AIR_TS + 60 * 60
        candidates = [episode(1), episode(2, hasFile=True), episode(3, monitored=False),
                      episode(4, series={"title": "Frieren", "monitored": False}),
                      episode(5), episode(6, aired=AIR + timedelta(days=1)),
                      episode(7, aired=AIR - timedelta(days=8)),
                      episode(8, airDateUtc="not-a-date"), episode(9, airDateUtc=None),
                      episode(10, series={})]
        missing_air = episode(11)
        del missing_air["airDateUtc"]
        candidates.append(missing_air)
        missing_series = episode(12)
        del missing_series["series"]
        candidates.append(missing_series)
        original = copy.deepcopy(candidates)
        queued = {5}
        state = {}
        self.assertEqual(watcher.due_episodes(candidates, queued, state, now), [1, 10, 12])
        self.assertEqual(queued, {5})
        self.assertEqual(state, {})
        self.assertEqual(candidates, original)

    def test_recent_first_and_cap(self):
        now = AIR_TS + 2 * 60 * 60
        candidates = [episode(i, aired=AIR - timedelta(minutes=i))
                      for i in range(watcher.MAX_PER_CYCLE + 5, 0, -1)]
        result = watcher.due_episodes(candidates, set(), {}, now)
        self.assertEqual(len(result), watcher.MAX_PER_CYCLE)
        self.assertEqual(result, list(range(1, watcher.MAX_PER_CYCLE + 1)))

    def test_schedule_controls_selection(self):
        ep = episode()
        self.assertEqual(watcher.due_episodes([ep], set(), {}, AIR_TS + 4 * 60), [])
        self.assertEqual(watcher.due_episodes([ep], set(), {}, AIR_TS + 5 * 60), [1])
        state = {"1": {"attempts": 1, "last": AIR_TS + 5 * 60}}
        self.assertEqual(watcher.due_episodes([ep], set(), state, AIR_TS + 19 * 60), [])
        self.assertEqual(watcher.due_episodes([ep], set(), state, AIR_TS + 20 * 60), [1])
        state["1"]["last"] = AIR_TS + 19 * 60
        self.assertEqual(watcher.due_episodes([ep], set(), state, AIR_TS + 20 * 60), [])
        self.assertEqual(watcher.due_episodes([ep], set(), state, AIR_TS + 23 * 60), [1])


class WatcherCycle(unittest.TestCase):
    def test_search_cleanup_log_and_state_round_trip(self):
        now = AIR_TS + 20 * 60
        sonarr = FakeSonarr([episode(1), episode(2, hasFile=True), episode(3), episode(4)],
                            queue=[{"episodeId": 3}, {}, {"downloadId": "ABC"}])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "watcher.json")
            w = watcher.AiringWatcher(lambda: FakeStack(sonarr), path)
            w.state = {"2": {"attempts": 1, "last": AIR_TS},
                       "99": {"attempts": 1, "last": AIR_TS}}
            self.assertEqual(w.run_once(now), [1])
            self.assertEqual(sonarr.get_calls, [
                ("/calendar", {"start": "2026-09-27T13:50:00Z",
                               "end": "2026-10-04T13:50:00Z",
                               "includeSeries": "true", "unmonitored": "false"}),
                ("/queue", {"page": 1, "pageSize": 1000, "includeUnknownSeriesItems": "true"}),
                ("/history", {"page": 1, "pageSize": 50, "downloadId": "ABC"})])
            self.assertEqual(sonarr.send_calls, [
                ("POST", "/command", {"name": "EpisodeSearch", "episodeIds": [1]}, {})])
            self.assertEqual(w.state, {"1": {"attempts": 1, "last": now}})
            self.assertEqual([item["msg"] for item in w.status()["log"]], [
                "Searching Frieren S02E05 (aired 20 min ago, try 1)",
                "Got Frieren S02E05"])
            self.assertEqual(w.status()["lastRun"], now)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), w.state)
            reloaded = watcher.AiringWatcher(lambda: FakeStack(sonarr), path)
            self.assertEqual(reloaded.state, w.state)
            self.assertEqual(reloaded.episode_info(1, AIR_TS, now),
                             {"attempts": 1, "last": now, "next": AIR_TS + 24 * 60})

    def test_corrupt_state_and_no_file_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watcher.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not JSON")
            w = watcher.AiringWatcher(lambda: FakeStack(FakeSonarr([])), path)
            self.assertEqual(w.state, {})
            with open(path, "w", encoding="utf-8") as f:
                f.write("[]")
            self.assertEqual(watcher.AiringWatcher(lambda: None, path).state, {})
            memory_only = watcher.AiringWatcher(lambda: FakeStack(FakeSonarr([])), None)
            self.assertEqual(memory_only.run_once(AIR_TS), [])
            self.assertIsNone(memory_only.episode_info(1, None, AIR_TS))

    def test_status_newest_first_and_bounded(self):
        now = AIR_TS + 6 * 60
        sonarr = FakeSonarr([episode()])
        w = watcher.AiringWatcher(lambda: FakeStack(sonarr), None, log_size=2)
        for minute in (6, 20, 45):
            w.run_once(AIR_TS + minute * 60)
        messages = [item["msg"] for item in w.status()["log"]]
        self.assertEqual(len(w.log), 2)
        self.assertEqual(len(messages), 2)
        self.assertIn("try 3", messages[0])
        self.assertIn("try 2", messages[1])
        status = w.status()
        status["log"][0]["msg"] = "changed"
        self.assertNotEqual(w.status()["log"][0]["msg"], "changed")

    def test_send_failure_does_not_record_search(self):
        class FailingSonarr(FakeSonarr):
            def send(self, method, path, body=None, **params):
                raise RuntimeError("Sonarr down")

        w = watcher.AiringWatcher(lambda: FakeStack(FailingSonarr([episode()])), None)
        with self.assertRaisesRegex(RuntimeError, "Sonarr down"):
            w.run_once(AIR_TS + 6 * 60)
        self.assertEqual(w.state, {})
        self.assertIsNone(w.last_run)


if __name__ == "__main__":
    unittest.main()
