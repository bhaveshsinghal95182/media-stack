import os
import tempfile
import unittest

import core


class Helpers(unittest.TestCase):
    def test_sxxexx(self):
        self.assertEqual(core.parse_sxxexx("Show.S01E02.720p.mkv"), (1, [2]))
        self.assertEqual(core.parse_sxxexx("Show - s02e03e04.mkv"), (2, [3, 4]))
        self.assertEqual(core.parse_sxxexx("Show.S10.E05.mkv"), (10, [5]))
        self.assertIsNone(core.parse_sxxexx("random_clip.mkv"))
        self.assertIsNone(core.parse_sxxexx("Show - 05.mkv"))

    def test_safe_name(self):
        self.assertEqual(core.safe_name('What If...? / "Ep": 1'), "What If... Ep 1")

    def test_eta(self):
        self.assertEqual(core._eta(8640000), "")
        self.assertEqual(core._eta(30), "1m")
        self.assertEqual(core._eta(5400), "1h 30m")
        self.assertEqual(core._eta(90000), "1d 1h")

    def test_human(self):
        self.assertEqual(core.human(512), "512 B")
        self.assertEqual(core.human(1536), "1.5 KB")


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

    def tearDown(self):
        self.tmp.cleanup()

    def test_videos_skip_samples(self):
        self.assertEqual([os.path.basename(v) for v in core.local_videos(self.dir)],
                         ["Movie.2020.mkv"])

    def test_subs_whole_dir(self):
        video = os.path.join(self.dir, "Movie.2020.mkv")
        subs = sorted(s for _, s in core.local_subs(self.dir, video, True))
        self.assertEqual(subs, [".2_English", ".en"])

    def test_subs_single_file(self):
        video = os.path.join(self.dir, "Movie.2020.mkv")
        self.assertEqual([s for _, s in core.local_subs(video, video, False)], [".en"])


class ImportGuard(unittest.TestCase):
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
        os.environ["LIBRARY_PASSWORD"] = "x"
        import web
        self.web = web
        web.IMPORT_ROOT = os.path.realpath(root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_traversal_rejected(self):
        for bad in ("../secret.mkv", "Show/../../secret.mkv", "link.mkv"):
            with self.assertRaises(core.LibraryError, msg=bad):
                self.web.resolve_import_path(bad)

    def test_browse_shows_only_videos(self):
        names = {e["name"] for e in self.web.browse("")["entries"]}
        self.assertEqual(names, {"Show"})  # no passport, no fonts folder, no escaping link target


if __name__ == "__main__":
    unittest.main()
