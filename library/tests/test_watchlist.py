import glob
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from medialib import watchlist


class WatchlistItems(unittest.TestCase):
    def test_add_normalises_all_item_fields(self):
        saved = watchlist.Watchlist(None)
        self.assertTrue(saved.add("movie", 1, "  The   Matrix  ", year="1999",
                                  poster="   ", overview=" x" * 400, now=10.0))
        self.assertEqual(saved.items(), [{
            "kind": "movie", "extId": 1, "title": "The Matrix", "year": 1999,
            "poster": None, "overview": ("x " * 400).strip()[:watchlist.MAX_OVERVIEW],
            "added": 10.0, "playlists": []}])

    def test_duplicate_updates_metadata_without_erasing_unsupplied_fields(self):
        saved = watchlist.Watchlist(None)
        saved.add("show", 4, "Original", year=2020, poster=" /old ",
                  overview="Old summary", now=1.0)
        self.assertFalse(saved.add("show", 4, " Updated ", year="2022",
                                   poster=" /new ", overview="New summary", now=2.0))
        self.assertEqual(saved.items()[0], {
            "kind": "show", "extId": 4, "title": "Updated", "year": 2022,
            "poster": "/new", "overview": "New summary", "added": 1.0, "playlists": []})
        self.assertFalse(saved.add("show", 4, "Final", now=3.0))
        self.assertEqual(saved.items()[0], {
            "kind": "show", "extId": 4, "title": "Final", "year": 2022,
            "poster": "/new", "overview": "New summary", "added": 1.0, "playlists": []})

    def test_items_are_newest_first_with_stable_ties_and_independent_copies(self):
        saved = watchlist.Watchlist(None)
        saved.add("movie", 1, "First", now=1.0)
        saved.add("movie", 2, "Second", now=2.0)
        saved.add("show", 3, "Third", now=2.0)
        result = saved.items()
        self.assertEqual([item["title"] for item in result], ["Second", "Third", "First"])
        result[0]["title"] = "Changed"
        result.clear()
        self.assertEqual([item["title"] for item in saved.items()],
                         ["Second", "Third", "First"])

    def test_remove_and_has(self):
        saved = watchlist.Watchlist(None)
        saved.add("movie", 123, "Example")
        self.assertTrue(saved.has("movie", "123"))
        self.assertTrue(saved.remove("movie", "123"))
        self.assertFalse(saved.has("movie", 123))
        self.assertFalse(saved.remove("movie", 123))
        self.assertFalse(saved.has("wrong", object()))
        self.assertFalse(saved.remove("wrong", object()))

    def test_validation_rejects_bad_identity_and_blank_title(self):
        saved = watchlist.Watchlist(None)
        for kind, ext_id, title in (("other", 1, "Title"), ("movie", 0, "Title"),
                                    ("movie", -1, "Title"), ("movie", True, "Title"),
                                    ("movie", "abc", "Title"), ("movie", 1, "  ")):
            with self.subTest(kind=kind, ext_id=ext_id, title=title):
                with self.assertRaises(ValueError):
                    saved.add(kind, ext_id, title)
        self.assertEqual(saved.items(), [])
        self.assertTrue(saved.add("movie", "123", "Valid"))
        self.assertEqual(saved.items()[0]["extId"], 123)

    def test_no_path_works_in_memory_without_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            before = os.listdir(directory)
            saved = watchlist.Watchlist(None)
            saved.add("movie", 1, "First")
            saved.add("show", 2, "Second")
            saved.remove("movie", 1)
            self.assertEqual([item["title"] for item in saved.items()], ["Second"])
            self.assertEqual(os.listdir(directory), before)

    def test_concurrent_adds_keep_every_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            saved = watchlist.Watchlist(path)
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertTrue(all(pool.map(
                    lambda number: saved.add("movie", number, f"Movie {number}",
                                             now=float(number)), range(1, 17))))
            self.assertEqual(len(saved.items()), 16)
            self.assertEqual(watchlist.Watchlist(path).items(), saved.items())


class WatchlistPersistence(unittest.TestCase):
    def test_round_trip_and_versioned_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "nested", "watchlist.json")
            saved = watchlist.Watchlist(path)
            saved.add("movie", 1, "Movie", now=1.0)
            saved.add("show", 2, "Show", now=2.0)
            loaded = watchlist.Watchlist(path)
            self.assertEqual(loaded.items(), saved.items())
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.assertEqual(raw, {"version": 2, "playlists": {}, "items": [
                saved.items()[1], saved.items()[0]]})

    def test_corrupt_json_is_renamed_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("not JSON")
            self.assertEqual(watchlist.Watchlist(path).items(), [])
            renamed = glob.glob(f"{path}.corrupt-*")
            self.assertEqual(len(renamed), 1)
            with open(renamed[0], encoding="utf-8") as f:
                self.assertEqual(f.read(), "not JSON")
            self.assertFalse(os.path.exists(path))

    def test_invalid_top_level_is_renamed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": [], "extra": True}, f)
            self.assertEqual(watchlist.Watchlist(path).items(), [])
            self.assertEqual(len(glob.glob(f"{path}.corrupt-*")), 1)

    def test_failed_corrupt_rename_does_not_allow_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("original garbage")
            with patch.object(watchlist.os, "replace", side_effect=OSError("rename failed")):
                saved = watchlist.Watchlist(path)
            with self.assertRaises(OSError):
                saved.add("movie", 1, "Movie")
            self.assertEqual(saved.items(), [])
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "original garbage")

    def test_invalid_entries_are_skipped_and_first_duplicate_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            entries = [
                {"kind": "movie", "extId": "123", "title": "  First   title ",
                 "year": "2001", "added": 1},
                {"kind": "bad", "extId": 2, "title": "Bad", "added": 2},
                {"kind": "movie", "extId": 0, "title": "Bad", "added": 3},
                {"kind": "movie", "extId": True, "title": "Bad", "added": 4},
                {"kind": "movie", "extId": 5, "title": " ", "added": 5},
                "not a dict",
                {"kind": "movie", "extId": 123, "title": "Duplicate", "added": 6},
                {"kind": "show", "extId": 8, "title": "Other", "added": 7},
            ]
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": entries}, f)
            loaded = watchlist.Watchlist(path).items()
            self.assertEqual(len(loaded), 2)
            self.assertEqual([item["title"] for item in loaded], ["Other", "First title"])
            self.assertEqual(loaded[1]["year"], 2001)
            self.assertEqual(loaded[1]["added"], 1.0)
            self.assertEqual(set(loaded[1]),
                             {"kind", "extId", "title", "year", "poster", "overview", "added",
                              "playlists"})

    def test_save_failure_rolls_back_add_and_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            saved = watchlist.Watchlist(path)
            saved.add("movie", 1, "Existing", now=1.0)
            original = saved.items()
            with patch.object(watchlist.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    saved.add("show", 2, "New", now=2.0)
                self.assertEqual(saved.items(), original)
                with self.assertRaisesRegex(OSError, "disk failure"):
                    saved.remove("movie", 1)
                self.assertEqual(saved.items(), original)
            self.assertEqual(watchlist.Watchlist(path).items(), original)


class WatchlistPlaylists(unittest.TestCase):
    def test_create_slugs_the_name_and_reuses_it_case_insensitively(self):
        saved = watchlist.Watchlist(None)
        playlist = saved.create_playlist("  The   Hunger  Games ")
        self.assertEqual(playlist["name"], "The Hunger Games")
        self.assertEqual(playlist["key"], "the-hunger-games")
        self.assertEqual(saved.create_playlist("the hunger games")["key"], playlist["key"])
        self.assertEqual(len(saved.playlists()), 1)

    def test_blank_names_are_rejected(self):
        saved = watchlist.Watchlist(None)
        with self.assertRaises(ValueError):
            saved.create_playlist("   ")
        with self.assertRaises(ValueError):
            saved.rename_playlist("anything", "  ")

    def test_membership_is_replaced_and_unknown_keys_are_ignored(self):
        saved = watchlist.Watchlist(None)
        first = saved.create_playlist("First")["key"]
        second = saved.create_playlist("Second")["key"]
        saved.add("movie", 1, "One", playlists=[first, "not-a-real-playlist"])
        self.assertEqual(saved.get("movie", 1)["playlists"], [first])
        saved.add("movie", 1, "One", playlists=[second, first])
        self.assertEqual(saved.get("movie", 1)["playlists"], [second, first])
        self.assertTrue(saved.set_playlists("movie", 1, [first]))
        self.assertFalse(saved.set_playlists("movie", 1, [first]))
        self.assertEqual(saved.get("movie", 1)["playlists"], [first])
        self.assertFalse(saved.set_playlists("movie", 99, [first]))

    def test_deleting_a_playlist_unfiles_but_keeps_its_titles(self):
        saved = watchlist.Watchlist(None)
        playlist = saved.create_playlist("Halloween")["key"]
        saved.add("movie", 1, "One", playlists=[playlist])
        saved.add("movie", 2, "Two")
        self.assertTrue(saved.delete_playlist(playlist))
        self.assertTrue(saved.has("movie", 1))
        self.assertEqual(saved.get("movie", 1)["playlists"], [])
        self.assertEqual(saved.playlists(), [])
        self.assertFalse(saved.delete_playlist(playlist))

    def test_rename_keeps_the_key_and_refuses_a_clash(self):
        saved = watchlist.Watchlist(None)
        first = saved.create_playlist("First")["key"]
        saved.create_playlist("Second")
        saved.add("movie", 1, "One", playlists=[first])
        self.assertTrue(saved.rename_playlist(first, "Better name"))
        self.assertEqual(saved.playlist(first)["name"], "Better name")
        self.assertEqual(saved.get("movie", 1)["playlists"], [first])
        with self.assertRaises(ValueError):
            saved.rename_playlist(first, "second")
        self.assertFalse(saved.rename_playlist(first, "Better name"))
        self.assertFalse(saved.rename_playlist("missing", "Any"))

    def test_bulk_add_is_additive(self):
        saved = watchlist.Watchlist(None)
        playlist = saved.create_playlist("Keepers")["key"]
        saved.add("movie", 1, "One", playlists=[playlist])
        saved.add("movie", 2, "Two")
        self.assertEqual(saved.add_to_playlists([("movie", 1), ("movie", 2), ("movie", 9)], playlist), 1)
        self.assertEqual(saved.add_to_playlists([("movie", 1), ("movie", 2)], playlist), 0)
        self.assertEqual(saved.playlists()[0]["count"], 2)
        with self.assertRaises(ValueError):
            saved.add_to_playlists([("movie", 1)], "missing")

    def test_v1_loads_unfiled_and_the_next_write_is_v2(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": [
                    {"kind": "movie", "extId": 1, "title": "Old", "added": 1}]}, f)
            saved = watchlist.Watchlist(path)
            self.assertEqual(saved.items()[0]["playlists"], [])
            playlist = saved.create_playlist("Later")
            saved.add("movie", 1, "Old", playlists=[playlist["key"]])
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.assertEqual(raw["version"], 2)
            self.assertEqual(raw["playlists"][playlist["key"]]["name"], "Later")
            self.assertEqual(raw["items"][0]["playlists"], [playlist["key"]])

    def test_bad_playlist_entries_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "playlists": {
                    "good": {"name": "Good", "created": 1},
                    "BAD KEY": {"name": "Bad key", "created": 2},
                    "nameless": {"created": 3},
                }, "items": [
                    {"kind": "movie", "extId": 1, "title": "One", "added": 1,
                     "playlists": ["good", "missing"]},
                ]}, f)
            saved = watchlist.Watchlist(path)
            self.assertEqual([p["key"] for p in saved.playlists()], ["good"])
            self.assertEqual(saved.get("movie", 1)["playlists"], ["good"])

    def test_playlists_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "watchlist.json")
            saved = watchlist.Watchlist(path)
            playlist = saved.create_playlist("Round trip")["key"]
            saved.add("show", 2, "Show", playlists=[playlist], now=1.0)
            loaded = watchlist.Watchlist(path)
            self.assertEqual(loaded.items(), saved.items())
            self.assertEqual(loaded.playlists(), saved.playlists())


if __name__ == "__main__":
    unittest.main()
