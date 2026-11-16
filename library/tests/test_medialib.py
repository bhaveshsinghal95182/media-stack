import os
import tempfile
import unittest
import unittest.mock

from medialib import Kind, LibraryError
from medialib.details import library_details, lookup_details
from medialib.files import local_subs, local_videos, parse_sxxexx
from medialib.fmt import eta, human, safe_name
from medialib.following import refresh_series
from medialib.importer import ImportFolder
from medialib.orphans import claimed_hashes
from medialib.rescue import ImportRescuer
from medialib.seasons import get_season, season_rows
from medialib.watchlist import Watchlist, watchlist_send


def quiet(_message):
    pass


class Helpers(unittest.TestCase):
    def test_sxxexx(self):
        self.assertEqual(parse_sxxexx("Show.S01E02.720p.mkv"), (1, [2]))
        self.assertEqual(parse_sxxexx("Show - s02e03e04.mkv"), (2, [3, 4]))
        self.assertEqual(parse_sxxexx("Show.S10.E05.mkv"), (10, [5]))
        self.assertIsNone(parse_sxxexx("random_clip.mkv"))
        self.assertIsNone(parse_sxxexx("Show - 05.mkv"))

    def test_safe_name(self):
        self.assertEqual(safe_name('What If...? / "Ep": 1'), "What If... Ep 1")

    def test_eta(self):
        self.assertEqual(eta(8640000), "")
        self.assertEqual(eta(30), "1m")
        self.assertEqual(eta(5400), "1h 30m")
        self.assertEqual(eta(90000), "1d 1h")

    def test_human(self):
        self.assertEqual(human(512), "512 B")
        self.assertEqual(human(1536), "1.5 KB")


class LocalFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        for rel in ("Movie.2020/Movie.2020.mkv", "Movie.2020/Movie.2020.en.srt",
                    "Movie.2020/Subs/2_English.srt", "Movie.2020/Sample/movie-sample.mkv",
                    "Movie.2020/notes.txt"):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        self.dir = os.path.join(d, "Movie.2020")
        self.video = os.path.join(self.dir, "Movie.2020.mkv")

    def tearDown(self):
        self.tmp.cleanup()

    def test_videos_skip_samples(self):
        self.assertEqual([os.path.basename(v) for v in local_videos(self.dir)], ["Movie.2020.mkv"])

    def test_subs_whole_dir(self):
        subs = sorted(s for _, s in local_subs(self.dir, self.video, True))
        self.assertEqual(subs, [".2_English", ".en"])

    def test_subs_single_file(self):
        self.assertEqual([s for _, s in local_subs(self.video, self.video, False)], [".en"])


class ImportFolderGuard(unittest.TestCase):
    """~/Downloads also holds ID scans and API keys: only videos may show, nothing outside."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.join(self.tmp.name, "Downloads")
        os.makedirs(os.path.join(root, "Show"))
        open(os.path.join(root, "Show", "a.S01E01.mkv"), "w").close()
        open(os.path.join(root, "passport.jpeg"), "w").close()
        os.makedirs(os.path.join(root, "fonts"))
        open(os.path.join(root, "fonts", "x.ttf"), "w").close()
        open(os.path.join(self.tmp.name, "secret.mkv"), "w").close()
        os.symlink(os.path.join(self.tmp.name, "secret.mkv"), os.path.join(root, "link.mkv"))
        self.folder = ImportFolder(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_traversal_rejected(self):
        for bad in ("../secret.mkv", "Show/../../secret.mkv", "link.mkv", "/../secret.mkv"):
            with self.assertRaises(LibraryError, msg=bad):
                self.folder.resolve(bad)

    def test_browse_shows_only_videos(self):
        names = {e["name"] for e in self.folder.browse("")["entries"]}
        self.assertEqual(names, {"Show"})  # no passport, no fonts folder, no escaping link target

    def test_display_hides_container_paths(self):
        path = os.path.join(self.folder.root, "Show", "a.S01E01.mkv")
        self.assertEqual(self.folder.display(f"can't read {path}"), "can't read Downloads/Show/a.S01E01.mkv")


class WatchlistSend(unittest.TestCase):
    def test_sent_titles_leave_failed_ones_stay(self):
        wl = Watchlist(None)
        wl.add("movie", 1, "Good")
        wl.add("show", 2, "Bad")

        def fake_add(_stack, kind, ext_id):
            if ext_id == 2:
                raise LibraryError("tvdb:2 not found")
            return {"label": f"{kind} {ext_id}", "message": "added"}

        with unittest.mock.patch("medialib.titles.add_title", fake_add):
            res = watchlist_send(None, wl, [("movie", 1), ("show", 2)], log=quiet)
        self.assertFalse(res["ok"])
        self.assertEqual(res["sent"], ["movie 1"])
        self.assertEqual([f["extId"] for f in res["failed"]], [2])
        self.assertEqual([(i["kind"], i["extId"]) for i in wl.items()], [("show", 2)])


class FakeArr:
    def __init__(self, name, routes, queue):
        self.name, self.routes, self._queue, self.commands, self.sent = name, routes, queue, [], []

    def get(self, path, **params):
        key = path + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                      if path in ("/history", "/manualimport") else "")
        return self.routes[key] if key in self.routes else self.routes[path]

    def queue(self):
        return self._queue

    def command(self, body):
        self.commands.append(body)
        return {}

    def send(self, method, path, body=None, **params):
        self.sent.append((method, path, body))
        return body


class FakeQB:
    def __init__(self, torrents):
        self.list = torrents

    def torrents(self):
        return self.list

    def torrents_by_hash(self):
        return {t["hash"].lower(): t for t in self.list}

    def file_paths(self, torrent):
        return [os.path.join(torrent["save_path"], torrent["hash"] + ".mkv")]


class FakeVolume:
    def __init__(self, links=None):
        self.links = links  # file path -> hardlink count; None = can't read the volume

    def link_counts(self, paths):
        return None if self.links is None else [self.links.get(p, 1) for p in paths]


class FakeStack:
    def __init__(self, sonarr, qb, radarr=None):
        self.sonarr, self.qb = sonarr, qb
        self.radarr = radarr or FakeArr("Radarr", {"/movie": []}, [])
        self.volume = FakeVolume()

    def kind(self, kind):
        return Kind(self, kind)


def torrent(h, tags, progress=1, added=0):
    return {"hash": h, "tags": tags, "progress": progress, "added_on": added, "name": h,
            "size": 1, "category": "tv-sonarr", "state": "stoppedUP", "save_path": "/d"}


def blocked(h, res):
    return {"downloadId": h.upper(), "trackedDownloadState": "importBlocked",
            "quality": {"quality": {"resolution": res}}, "customFormatScore": 0}


def grab(h, ep=714):
    return {"records": [{"eventType": "grabbed", "seriesId": 8, "episodeId": ep, "downloadId": h}]}


class Rescue(unittest.TestCase):
    def make(self, has_file=False):
        routes = {"/episode/714": {"hasFile": has_file, "seasonNumber": 3, "episodeNumber": 1},
                  "/episode/678": {"hasFile": False, "seasonNumber": 3, "episodeNumber": 1}}
        for h in ("aa", "bb", "cc"):
            routes[f"/history?downloadId={h.upper()}&page=1&pageSize=50"] = grab(h)
        routes["/manualimport?downloadId=BB&filterExistingFiles=false"] = [
            {"path": "/data/downloads/tv-sonarr/x.mkv", "episodes": [], "quality": {"q": 1},
             "languages": [{"id": 8}], "releaseGroup": "G"}]
        sonarr = FakeArr("Sonarr", routes, [blocked("aa", 720), blocked("bb", 1080),
                                             blocked("cc", 2160)])
        qb = FakeQB([torrent("aa", "subgate-ok"), torrent("bb", "subgate-ok"),
                     torrent("cc", "subgate-rejected")])
        return FakeStack(sonarr, qb)

    def setUp(self):
        self.rescue = ImportRescuer()

    def test_imports_best_passed_copy_once(self):
        st = self.make()
        self.assertEqual(self.rescue(st, log=quiet, now=1000), ["bb"])  # 1080p beats 720p; 2160p was rejected
        body, = st.sonarr.commands
        self.assertEqual(body["name"], "ManualImport")
        f, = body["files"]
        self.assertEqual((f["seriesId"], f["episodeIds"], f["downloadId"]), (8, [714], "BB"))
        self.assertEqual(self.rescue(st, log=quiet, now=1500), [])  # no retry yet

    def test_leaves_episodes_that_have_a_file(self):
        st = self.make(has_file=True)
        self.assertEqual(self.rescue(st, log=quiet, now=1000), [])
        self.assertEqual(st.sonarr.commands, [])

    def test_one_import_when_sonarr_holds_the_episode_twice(self):
        st = self.make()
        st.qb.list.append(torrent("dd", "subgate-ok"))
        st.sonarr.queue().append(blocked("dd", 1080))
        st.sonarr.routes["/history?downloadId=DD&page=1&pageSize=50"] = grab("dd", ep=678)
        st.sonarr.routes["/manualimport?downloadId=DD&filterExistingFiles=false"] = \
            st.sonarr.routes["/manualimport?downloadId=BB&filterExistingFiles=false"]
        self.assertEqual(len(self.rescue(st, log=quiet, now=1000)), 1)


class RefreshSeries(unittest.TestCase):
    class Arr:
        def __init__(self, polls):
            self.polls, self.ran = polls, []

        def get(self, path, **params):
            if path == "/command":
                return self.polls.pop(0) if self.polls else []
            return [{"id": 1}]                              # /episode: list loaded

        def run_command(self, body):
            self.ran.append(body)

    def test_new_series_waits_for_sonarrs_refresh_and_starts_none(self):
        running = [{"name": "RefreshSeries", "status": "started", "body": {"seriesIds": [8]}}]
        arr = self.Arr([running, running, []])
        with unittest.mock.patch("time.sleep"):
            refresh_series(arr, 8, new=True)
        self.assertEqual(arr.ran, [])
        self.assertEqual(arr.polls, [])                     # waited until it finished

    def test_existing_series_refreshes_after_others_finish(self):
        arr = self.Arr([[{"name": "RefreshSeries", "status": "queued", "body": {}}], []])
        with unittest.mock.patch("time.sleep"):
            refresh_series(arr, 8, new=False)
        self.assertEqual(arr.ran, [{"name": "RefreshSeries", "seriesId": 8}])


class Claimed(unittest.TestCase):
    def test_rejected_and_duplicates_are_leftovers(self):
        routes = {"/series": [{"id": 8, "title": "S"}], "/episode/714": {"hasFile": True},
                  "/episode/715": {"hasFile": False},
                  "/history/series": [
                      {"eventType": "grabbed", "downloadId": "OK", "episodeId": 714},
                      {"eventType": "downloadFolderImported", "downloadId": "OK", "episodeId": 714},
                      {"eventType": "grabbed", "downloadId": "DUP", "episodeId": 714},
                      {"eventType": "grabbed", "downloadId": "REJ", "episodeId": 714},
                      {"eventType": "grabbed", "downloadId": "OLD", "episodeId": 714},
                      {"eventType": "grabbed", "downloadId": "NEW", "episodeId": 715}]}
        queue = [blocked("dup", 1080), blocked("rej", 1080), blocked("lost", 1080),
                 {"downloadId": "NEW", "trackedDownloadState": "downloading"}]
        torrents = {h: torrent(h, t) for h, t in (("ok", "subgate-ok"), ("dup", "subgate-ok"),
                    ("rej", "subgate-rejected"), ("old", "subgate-ok"), ("new", ""),
                    ("lost", "subgate-ok"))}
        torrents["new"]["progress"] = 0.3
        st = FakeStack(FakeArr("Sonarr", routes, queue), FakeQB(list(torrents.values())))
        self.assertEqual(claimed_hashes(st, torrents), {"ok", "new"})

    def test_disk_links_beat_history(self):
        routes = {"/series": [{"id": 8, "title": "S"}], "/history/series": [
            {"eventType": "downloadFolderImported", "downloadId": "GONE", "episodeId": 1}]}
        torrents = {h: torrent(h, "subgate-ok") for h in ("gone", "lib", "plain")}
        st = FakeStack(FakeArr("Sonarr", routes, []), FakeQB(list(torrents.values())))
        st.volume = FakeVolume({"/d/lib.mkv": 2})
        # "gone": history says imported, but its library copy was deleted (1 link).
        # "lib": history was lost, but it's hardlinked into the library.
        self.assertEqual(claimed_hashes(st, torrents), {"lib"})
        st.volume = FakeVolume(None)  # can't see the disk: trust history
        self.assertEqual(claimed_hashes(st, torrents), {"gone"})


class Details(unittest.TestCase):
    movie = {"id": 5, "title": "Mirai", "year": 2018, "tmdbId": 9, "imdbId": "tt1", "runtime": 98,
             "studio": "Studio Chizu", "status": "released", "genres": ["Animation"],
             "ratings": {"imdb": {"value": 7}, "tmdb": {"value": 7.157}, "trakt": {"value": 7.3}},
             "originalLanguage": {"name": "Japanese"}, "path": "/data/media/movies/Mirai (2018)",
             "sizeOnDisk": 1536, "monitored": True,
             "images": [{"coverType": "poster", "remoteUrl": "p.jpg"}, {"coverType": "fanart", "remoteUrl": "f.jpg"}],
             "movieFile": {"relativePath": "Mirai.mkv", "quality": {"quality": {"name": "Bluray-1080p"}},
                           "mediaInfo": {"resolution": "1920x1036", "videoCodec": "x264",
                                         "audioLanguages": "jpn/und", "subtitles": "eng/eng"}}}

    def stack(self, routes):
        return FakeStack(FakeArr("Sonarr", routes, []), FakeQB([]),
                         radarr=FakeArr("Radarr", routes, []))

    def test_movie_in_the_library(self):
        d = library_details(self.stack({"/movie/5": self.movie}), "movie", 5)
        self.assertEqual((d["title"], d["runtime"], d["maker"], d["status"]),
                         ("Mirai", "98 min", "Studio Chizu", "Released"))
        self.assertEqual(d["scores"], ["IMDb 7.0", "TMDB 7.2"])  # trakt isn't shown
        self.assertEqual((d["poster"], d["fanart"], d["sizeHuman"]), ("p.jpg", "f.jpg", "1.5 KB"))
        self.assertEqual(d["file"], {"name": "Mirai.mkv", "quality": "Bluray-1080p", "video": "1920x1036 x264",
                                     "audio": "jpn, unknown", "subtitles": "eng"})
        self.assertEqual([name for name, _ in d["links"]], ["IMDb", "TMDB"])
        self.assertTrue(d["inLibrary"])

    def test_show_from_the_lookup(self):
        show = {"title": "Frieren", "tvdbId": 424536, "network": "Nippon TV", "status": "continuing",
                "ratings": {"value": 8.83}, "nextAiring": "1970-01-02T00:00:00Z"}
        d = lookup_details(self.stack({"/series/lookup": [show]}), "show", 424536)
        self.assertEqual((d["maker"], d["status"], d["scores"], d["nextAiring"]),
                         ("Nippon TV", "Still airing", ["8.8/10"], 86400))
        self.assertEqual(d["links"], [("TVDB", "https://thetvdb.com/dereferrer/series/424536")])
        self.assertFalse(d["inLibrary"])
        self.assertNotIn("id", d)  # the dialog offers library actions only for library titles


class AddBody(unittest.TestCase):
    """Titles added here get the profile and folder Seerr would pick, anime included."""

    def setUp(self):
        sonarr = FakeArr("Sonarr", {"/qualityprofile": [{"id": 6, "name": "HD"}, {"id": 7, "name": "Anime 1080p"}],
                                    "/rootfolder": [{"path": "/data/media/tv"}]}, [])
        self.stack = FakeStack(sonarr, FakeQB([]))
        self.stack.seerr_settings = {"sonarr": [{"isDefault": True, "activeProfileId": 6,
                                                 "activeDirectory": "/data/media/tv",
                                                 "activeAnimeProfileId": 7}]}

    def test_anime_gets_the_anime_profile(self):
        body = Kind(self.stack, "show").add_body({"title": "Frieren", "genres": ["Anime", "Drama"]}, True)
        self.assertEqual((body["qualityProfileId"], body["seriesType"]), (7, "anime"))
        self.assertEqual(body["rootFolderPath"], "/data/media/tv")  # no anime folder set: the usual one

    def test_other_shows_get_the_default(self):
        body = Kind(self.stack, "show").add_body({"title": "Severance", "genres": ["Drama"]}, True)
        self.assertEqual(body["qualityProfileId"], 6)
        self.assertNotIn("seriesType", body)


AIRED, LATER = "1970-01-02T00:00:00Z", "2999-01-01T00:00:00Z"


def ep(id_, season, has_file=False, air=AIRED):
    return {"id": id_, "seasonNumber": season, "hasFile": has_file, "airDateUtc": air, "monitored": False}


class Seasons(unittest.TestCase):
    """Getting a season downloads what's missing, never what was watched and removed."""

    def make(self, episodes, queue=(), deleted=()):
        series = {"id": 5, "title": "Frieren", "monitored": True,
                  "seasons": [{"seasonNumber": 0, "monitored": False}, {"seasonNumber": 1, "monitored": True},
                              {"seasonNumber": 2, "monitored": False}]}
        history = [{"eventType": "episodeFileDeleted", "episodeId": i, "data": {"reason": "Manual"}}
                   for i in deleted]
        history.append({"eventType": "episodeFileDeleted", "episodeId": 1, "data": {"reason": "Upgrade"}})
        self.sonarr = FakeArr("Sonarr", {"/series/5": series, "/episode": episodes,
                                         "/history/series": history},
                              [{"seriesId": 5, "episodeId": i} for i in queue])
        self.stack = FakeStack(self.sonarr, FakeQB([]))
        return series

    def test_rows_count_missing_removed_and_downloading(self):
        series = self.make([ep(1, 1, True), ep(2, 1), ep(3, 1), ep(4, 1), ep(5, 1, air=LATER),
                            ep(6, 2), ep(7, 0)], queue=[3], deleted=[4])
        rows = season_rows(Kind(self.stack, "show"), series)
        self.assertEqual([r["number"] for r in rows], [1, 2])  # specials only once some are on disk
        s1 = rows[0]
        self.assertEqual((s1["have"], s1["total"], s1["missing"], s1["downloading"], s1["upcoming"], s1["removed"]),
                         (1, 5, 1, 1, 1, 1))

    def test_get_partial_season_searches_only_missing(self):
        self.make([ep(1, 1, True), ep(2, 1), ep(4, 1), ep(5, 1, air=LATER), ep(6, 2)], deleted=[4])
        res = get_season(self.stack, 5, 1)
        put_series, unmonitor, monitor = self.sonarr.sent
        self.assertEqual(put_series[1], "/series/5")
        self.assertTrue(put_series[2]["monitored"])
        self.assertTrue(next(s for s in put_series[2]["seasons"] if s["seasonNumber"] == 1)["monitored"])
        self.assertEqual(unmonitor[2], {"episodeIds": [4], "monitored": False})  # watched & removed stays out
        self.assertEqual(monitor[2], {"episodeIds": [2, 5], "monitored": True})
        self.assertEqual(self.sonarr.commands, [{"name": "EpisodeSearch", "episodeIds": [2]}])
        self.assertIn("Skipped 1", res["message"])

    def test_get_whole_season_uses_season_search(self):
        self.make([ep(1, 1, True), ep(6, 2), ep(8, 2)])
        get_season(self.stack, 5, 2)
        self.assertEqual(self.sonarr.commands, [{"name": "SeasonSearch", "seriesId": 5, "seasonNumber": 2}])

    def test_nothing_to_get(self):
        self.make([ep(1, 1, True), ep(4, 1)], deleted=[4])
        with self.assertRaises(LibraryError):
            get_season(self.stack, 5, 1)
        self.assertEqual(self.sonarr.sent, [])


if __name__ == "__main__":
    unittest.main()
