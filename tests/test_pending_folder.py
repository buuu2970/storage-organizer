"""削除予定フォルダへの移動のテスト (ffmpeg 不要)"""

import tempfile
import unittest
from pathlib import Path

import storage_organizer
from storage_organizer.pending_folder import PENDING_DIR_NAME, desktop_dir, move_to_pending, pending_dir


class TestPendingFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dest = self.root / "desktop" / PENDING_DIR_NAME

    def tearDown(self):
        self.tmp.cleanup()

    def _video(self, rel, data=b"video"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_desktop_location(self):
        self.assertTrue(desktop_dir().is_dir())
        self.assertEqual(pending_dir(), desktop_dir() / "削除予定")

    def test_creates_folder_and_moves(self):
        src = self._video("rec/a.mp4")
        dest = move_to_pending(src, self.dest)
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(dest, self.dest / "a.mp4")
        self.assertFalse(src.exists())
        self.assertEqual(dest.read_bytes(), b"video")

    def test_never_overwrites_same_name(self):
        first = move_to_pending(self._video("x/a.mp4", b"1"), self.dest)
        second = move_to_pending(self._video("y/a.mp4", b"2"), self.dest)
        self.assertEqual(second, self.dest / "a_1.mp4")
        self.assertEqual(first.read_bytes(), b"1")
        self.assertEqual(second.read_bytes(), b"2")

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            move_to_pending(self.root / "none.mp4", self.dest)

    def test_already_in_pending(self):
        p = move_to_pending(self._video("a.mp4"), self.dest)
        with self.assertRaises(ValueError):
            move_to_pending(p, self.dest)

    def test_app_has_no_delete_feature(self):
        """削除・ごみ箱へ移す処理をアプリのコードに持たせていないこと。"""
        pkg = Path(storage_organizer.__file__).parent
        forbidden = ["SHFileOperation", "send2trash", "os.remove", "os.unlink", ".unlink(", "shutil.rmtree",
                     "FOF_ALLOWUNDO", "/api/delete"]
        for f in list(pkg.rglob("*.py")) + list(pkg.rglob("*.js")):
            text = f.read_text(encoding="utf-8")
            for word in forbidden:
                self.assertNotIn(word, text, f"{f.name} に {word} が含まれています")


if __name__ == "__main__":
    unittest.main()
