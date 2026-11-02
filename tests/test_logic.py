"""ffmpeg 不要の単体テスト"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from storage_organizer.files import find_duplicates, find_files
from storage_organizer.video_analyzer import (CAT_AUDIO, CAT_DUP, CAT_ERROR, CAT_LONG, CAT_SHORT, CAT_VIDEO,
                                              VIDEO_EXTS, fmt_time, parse_fps, sample_points)
from storage_organizer.video_cleanup import priority


class TestHelpers(unittest.TestCase):
    def test_fmt_time(self):
        self.assertEqual(fmt_time(5), "0:05")
        self.assertEqual(fmt_time(65), "1:05")
        self.assertEqual(fmt_time(5400), "1:30:00")

    def test_parse_fps(self):
        self.assertAlmostEqual(parse_fps("30000/1001"), 29.97, places=2)
        self.assertEqual(parse_fps("0/0"), 0.0)
        self.assertEqual(parse_fps("abc"), 0.0)

    def test_sample_points_short_video_uses_whole(self):
        self.assertEqual(sample_points(30, 5, 20), [0.0])

    def test_sample_points_spread(self):
        pts = sample_points(1000, 5, 20)
        self.assertEqual(len(pts), 5)
        self.assertTrue(all(0 <= p <= 980 for p in pts))
        self.assertEqual(pts, sorted(pts))


class TestPriority(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(priority([]), 0)
        self.assertEqual(priority([CAT_LONG]), 1)
        self.assertEqual(priority([CAT_VIDEO]), 2)
        self.assertEqual(priority([CAT_AUDIO]), 2)
        self.assertEqual(priority([CAT_VIDEO, CAT_AUDIO]), 3)
        self.assertEqual(priority([CAT_SHORT]), 3)
        self.assertEqual(priority([CAT_DUP]), 3)
        self.assertEqual(priority([CAT_ERROR]), 3)
        self.assertEqual(priority([CAT_LONG, CAT_VIDEO]), 2)


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, data=b"x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_find_files_filters_and_skips(self):
        a = self._write("a.mp4")
        b = self._write("sub/b.MKV")
        self._write("note.txt")
        self._write("AppData/c.mp4")      # システム系フォルダは除外
        self._write(".hidden/d.mp4")      # 隠しフォルダは除外
        self.assertEqual(find_files([self.root], VIDEO_EXTS), sorted([a, b]))
        self.assertEqual(find_files([self.root], VIDEO_EXTS, recursive=False), [a])

    def test_find_files_exclude_and_no_double_count(self):
        a = self._write("a.mp4")
        self._write("out/x.mp4")
        found = find_files([self.root, self.root], VIDEO_EXTS, exclude=self.root / "out")
        self.assertEqual(found, [a])

    def test_duplicates_keep_oldest(self):
        data = os.urandom(3 << 20)  # 3MB (先頭・末尾の1MBハッシュ範囲を超える)
        orig = self._write("orig.mp4", data)
        copy = self._write("sub/copy.mp4", data)
        old = time.time() - 1000
        os.utime(orig, (old, old))
        self._write("other.mp4", data[:-1] + b"\0")  # 同サイズだが末尾が違う → 重複ではない
        self._write("small.mp4", b"abc")
        self.assertEqual(find_duplicates(find_files([self.root], VIDEO_EXTS)), {copy: orig})


if __name__ == "__main__":
    unittest.main()
