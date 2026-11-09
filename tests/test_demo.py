"""デモ（main.py --demo）のテスト。一時フォルダだけを使い、本物のビデオ・デスクトップなどには作らない。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage_organizer import autoscan, demo, media  # noqa: E402
from storage_organizer.store import Store  # noqa: E402

FFMPEG = media.find_ffmpeg()


class DemoTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.places = []
        for label, name in (("ビデオ", "Videos"), ("デスクトップ", "Desktop"), ("ダウンロード", "Downloads"),
                            ("ドキュメント", "Documents")):
            (self.base / name).mkdir()
            self.places.append((label, str(self.base / name)))
        self.patch = mock.patch.object(autoscan, "known_folders", lambda: list(self.places))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._tmp.cleanup()

    def test_demo_scans_only_demo_folders(self):
        db = Store(self.base / "data")
        try:
            roots = [str(p) for p in demo.demo_roots().values()]
            for r in roots:
                Path(r).mkdir()
            db.set_json_list("demo_locations", roots)
            with mock.patch.object(autoscan, "local_drives",
                                   lambda: [{"root": str(self.base), "kind": "internal"}]):
                locations, not_scanned = autoscan.build_locations(db)
            # 内蔵ドライブや、ふつうのビデオ・デスクトップは調べない（デモのフォルダだけ）
            self.assertEqual(sorted(loc.path for loc in locations), sorted(roots))
            self.assertEqual(not_scanned, [])
        finally:
            db.close()

    def test_remove_deletes_only_files_made_by_demo(self):
        root = demo.demo_roots()["ビデオ"]
        root.mkdir()
        (root / "デモ.mp4").write_bytes(b"x")
        (root / "自分の録画.mp4").write_bytes(b"y")  # デモで作っていないファイル
        (root / demo.MARKER).write_text(json.dumps({"files": ["デモ.mp4", "../Videos"]}), encoding="utf-8")
        self.assertEqual(demo.remove(report=lambda m: None), 1)
        self.assertFalse((root / "デモ.mp4").exists())
        self.assertTrue((root / "自分の録画.mp4").exists())  # 一覧にないファイルは消さない
        self.assertTrue(root.parent.is_dir())

    def test_existing_folder_not_made_by_demo_is_not_used(self):
        demo.demo_roots()["ビデオ"].mkdir()  # 目印のない同名フォルダ
        with self.assertRaises(RuntimeError):
            demo.make(media.FFmpegTools("ffmpeg", "ffprobe"), report=lambda m: None)

    def test_make_requires_ffmpeg(self):
        with self.assertRaises(RuntimeError):
            demo.make(media.FFmpegTools(None, None), report=lambda m: None)

    @unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
    def test_make_creates_playable_videos_in_each_place(self):
        files = demo.make(FFMPEG, report=lambda m: None)
        self.assertGreaterEqual(len(files), 10)
        self.assertEqual({p.parent.parent.name for p in files}, {"Videos", "Desktop", "Downloads", "Documents"})
        info = media.probe(FFMPEG, str(demo.demo_roots()["ビデオ"] / "ゲーム実況_ボス戦_第1回.mp4"))
        self.assertTrue(info["has_video"])
        self.assertTrue(demo.exists())
        self.assertGreaterEqual(demo.remove(report=lambda m: None), 10)
        self.assertFalse(demo.exists())


@unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
class TransportStreamTest(unittest.TestCase):
    """テレビ録画などの .ts で、位置へ飛んで読む方法では1コマも取れない場合がある（11/09 に発見）。"""

    def test_frames_volume_and_signature_from_ts(self):
        import subprocess
        from storage_organizer import similar
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "rec.ts")
            subprocess.run([FFMPEG.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30",
                            "-f", "lavfi", "-i", "sine=f=440", "-t", "8", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-shortest", "-f", "mpegts", path], check=True)
            info = media.probe(FFMPEG, path)
            self.assertEqual(len(media.extract_frames(FFMPEG, path, info["duration"], 3)), 3)
            self.assertTrue(media.measure_max_volume(FFMPEG, path, info["duration"]))
            sig = similar.signature(FFMPEG, path, info["duration"])
            self.assertEqual(sum(f is not None for f in sig["frames"]), similar.N_POINTS)


if __name__ == "__main__":
    unittest.main()
