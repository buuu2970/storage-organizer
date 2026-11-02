"""スキャナ（Qt 非依存部分）のテスト。 実行: python -m unittest discover -s tests"""

from __future__ import annotations

import builtins
import hashlib
import io
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage_organizer import scanner  # noqa: E402
from storage_organizer.file_types import categorize  # noqa: E402
from storage_organizer.formatting import format_size  # noqa: E402


def make_tree(root: Path) -> dict[str, int]:
    """テスト用のフォルダを作り、{相対パス: サイズ} を返す。"""
    files = {
        "movie.mp4": 5000,
        "photo.JPG": 1200,
        "notes.txt": 30,
        "empty.bin": 0,
        "sub/report.pdf": 800,
        "sub/deep/archive.zip": 2500,
        "sub/deep/deeper/setup.exe": 100,
    }
    for rel, size in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    (root / "empty_folder").mkdir()
    return files


def collect(root: str, **kwargs):
    records: list[scanner.FileRecord] = []
    event = kwargs.pop("cancel_event", threading.Event())
    result = scanner.scan(root, event, on_batch=records.extend, **kwargs)
    return records, result


class ScanBasicsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.files = make_tree(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_counts_and_total_size(self) -> None:
        records, result = collect(str(self.root))
        self.assertEqual(result.files, len(self.files))
        self.assertEqual(len(records), len(self.files))
        self.assertEqual(result.total_size, sum(self.files.values()))
        self.assertEqual(result.skipped, 0)
        self.assertFalse(result.cancelled)
        # root, sub, sub/deep, sub/deep/deeper, empty_folder
        self.assertEqual(result.dirs, 5)

    def test_record_fields(self) -> None:
        records, _ = collect(str(self.root))
        by_name = {r.name: r for r in records}
        movie = by_name["movie.mp4"]
        self.assertEqual(movie.size, 5000)
        self.assertEqual(movie.category, "動画")
        self.assertEqual(Path(movie.folder), self.root)
        self.assertAlmostEqual(movie.mtime, (self.root / "movie.mp4").stat().st_mtime)
        setup = by_name["setup.exe"]
        self.assertEqual(Path(setup.folder), self.root / "sub" / "deep" / "deeper")
        self.assertEqual(by_name["photo.JPG"].category, "画像")

    @unittest.skipIf(sys.platform == "win32", "Windows ではシンボリックリンク作成に権限が必要")
    def test_links_are_not_followed(self) -> None:
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (Path(outside.name) / "big_outside.mp4").write_bytes(b"x" * 9999)
        os.symlink(outside.name, self.root / "link_to_dir", target_is_directory=True)
        os.symlink(self.root / "movie.mp4", self.root / "link_to_file.mp4")
        os.symlink(self.root, self.root / "sub" / "loop")  # 無限ループになるリンク

        records, result = collect(str(self.root))
        names = {r.name for r in records}
        self.assertNotIn("big_outside.mp4", names)
        self.assertNotIn("link_to_file.mp4", names)
        self.assertEqual(result.links, 3)
        self.assertEqual(result.total_size, sum(self.files.values()))

    def test_permission_error_is_skipped(self) -> None:
        blocked = str(self.root / "sub" / "deep")
        real_scandir = os.scandir

        def fake_scandir(path):
            if os.path.normpath(path) == os.path.normpath(blocked):
                raise PermissionError(13, "Permission denied", path)
            return real_scandir(path)

        with mock.patch.object(scanner.os, "scandir", side_effect=fake_scandir):
            records, result = collect(str(self.root))

        names = {r.name for r in records}
        self.assertNotIn("archive.zip", names)
        self.assertNotIn("setup.exe", names)  # その下も調べない
        self.assertIn("report.pdf", names)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.skipped_details[0][0], blocked)

    def test_inaccessible_root(self) -> None:
        _, result = collect(str(self.root / "does_not_exist"))
        self.assertEqual(result.files, 0)
        self.assertEqual(result.skipped, 1)

    def test_cancel_before_start(self) -> None:
        event = threading.Event()
        event.set()
        records, result = collect(str(self.root), cancel_event=event)
        self.assertTrue(result.cancelled)
        self.assertEqual(records, [])

    def test_cancel_during_scan(self) -> None:
        event = threading.Event()
        records: list[scanner.FileRecord] = []

        def on_batch(batch):
            records.extend(batch)
            event.set()  # 最初のまとまりを受け取った時点でキャンセル

        result = scanner.scan(str(self.root), event, on_batch=on_batch, batch_size=1)
        self.assertTrue(result.cancelled)
        self.assertLess(result.files, len(self.files))
        self.assertEqual(result.files, len(records))

    def test_progress_reports_have_no_percentage(self) -> None:
        reports: list[scanner.ScanProgress] = []
        scanner.scan(str(self.root), threading.Event(), on_batch=lambda b: None,
                     on_progress=reports.append)
        self.assertTrue(reports)
        self.assertEqual(reports[-1].files, len(self.files))
        self.assertFalse(hasattr(reports[-1], "percent"))

    def test_does_not_open_or_modify_files(self) -> None:
        def snapshot():
            data = {}
            for path in sorted(self.root.rglob("*")):
                st = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                data[str(path)] = (st.st_size, st.st_mtime_ns, digest)
            return data

        before = snapshot()

        def forbidden(*args, **kwargs):
            raise AssertionError("スキャン中にファイルが開かれました")

        with mock.patch.object(builtins, "open", forbidden), mock.patch.object(io, "open", forbidden):
            collect(str(self.root))

        self.assertEqual(snapshot(), before)


class HelpersTest(unittest.TestCase):
    def test_categorize(self) -> None:
        self.assertEqual(categorize("a.MKV"), "動画")
        self.assertEqual(categorize("a.png"), "画像")
        self.assertEqual(categorize("a.docx"), "文書")
        self.assertEqual(categorize("noext"), "その他")
        self.assertEqual(categorize("a.unknown"), "その他")

    def test_format_size(self) -> None:
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(1023), "1023 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(10 * 1024 ** 3), "10.0 GB")


if __name__ == "__main__":
    unittest.main()
