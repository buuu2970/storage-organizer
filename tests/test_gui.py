"""GUI の動作テスト。画面のない環境でも QT_QPA_PLATFORM=offscreen で実行できる。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.platform.startswith("linux"):
    # 音声デバイスのない Linux（CI など）で Qt の PipeWire 音声が落ちるのを避ける
    os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")

try:
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication
except ImportError:  # PySide6 未インストール時はスキップ
    QApplication = None

if QApplication is not None:
    from storage_organizer.file_table_model import COL_MTIME, COL_SIZE
    from storage_organizer.main_window import ScanPage


def wait_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@unittest.skipIf(QApplication is None, "PySide6 がインストールされていません")
class MainWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # 仕様変更: フォルダ指定の容量調査画面はメインウィンドウから外したため、部品として単独で確認する
        self._tmp = tempfile.TemporaryDirectory()
        self._data = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.window = ScanPage()
        self.window.show()
        self.page = self.window

    def tearDown(self) -> None:
        self.window.shutdown()
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self._tmp.cleanup()
        self._data.cleanup()

    def _make_files(self) -> None:
        sizes = {"small.txt": 10, "big.mp4": 9000, "mid/photo.png": 500, "mid/x/doc.pdf": 2000}
        for i, (rel, size) in enumerate(sizes.items()):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x" * size)
            # 更新日時をずらしておく（small.txt が最も新しい）
            ts = time.time() - (i + 1) * 86400
            os.utime(path, (ts, ts))
        os.utime(self.root / "small.txt", (time.time(), time.time()))

    def test_scan_and_sort(self) -> None:
        self._make_files()
        w = self.page
        self.assertFalse(w.scan_button.isEnabled())  # フォルダ未選択
        w.set_folder(str(self.root))
        self.assertTrue(w.scan_button.isEnabled())

        w.scan_button.click()
        self.assertTrue(wait_until(lambda: not w.is_scanning()))

        model = w.model
        self.assertEqual(model.rowCount(), 4)
        # 完了後は容量の大きい順
        self.assertEqual(model.record_at(0).name, "big.mp4")
        self.assertEqual(model.record_at(3).name, "small.txt")
        self.assertIn("調査済みファイル: 4 件", w.summary_label.text())
        self.assertIn("スキャン完了", w.current_dir_label.toolTip())
        self.assertFalse(w.progress_bar.isVisible())

        # 更新日時の新しい順
        w.table.sortByColumn(COL_MTIME, Qt.SortOrder.DescendingOrder)
        self.assertEqual(model.record_at(0).name, "small.txt")
        # 容量の小さい順
        w.table.sortByColumn(COL_SIZE, Qt.SortOrder.AscendingOrder)
        self.assertEqual(model.record_at(0).name, "small.txt")
        self.assertEqual(model.index(0, COL_SIZE).data(), "10 B")

    def test_cancel(self) -> None:
        for i in range(200):
            d = self.root / f"d{i:03}"
            d.mkdir()
            for j in range(150):
                (d / f"f{j}.txt").touch()
        w = self.page
        w.set_folder(str(self.root))
        w.scan_button.click()
        self.assertTrue(w.is_scanning())
        self.assertTrue(w.progress_bar.isVisible())
        self.assertEqual(w.progress_bar.maximum(), 0)  # パーセントを出さない不確定表示
        w.cancel_button.click()
        self.assertTrue(wait_until(lambda: not w.is_scanning()))
        self.assertTrue(w.scan_button.isEnabled())
        self.assertLess(w.model.rowCount(), 200 * 150)
        self.assertIn("キャンセル", w.current_dir_label.toolTip())

    def test_ui_stays_responsive_during_scan(self) -> None:
        """スキャン中もイベントループが回り続ける（画面が固まらない）ことを確認する。"""
        for i in range(300):
            d = self.root / f"d{i:03}"
            d.mkdir()
            for j in range(200):
                (d / f"f{j}.bin").touch()
        w = self.page
        w.set_folder(str(self.root))
        w.scan_button.click()
        max_gap = 0.0
        last = time.monotonic()
        while w.is_scanning():
            QCoreApplication.processEvents()
            now = time.monotonic()
            max_gap = max(max_gap, now - last)
            last = now
        self.assertEqual(w.model.rowCount(), 300 * 200)
        self.assertLess(max_gap, 0.5, f"イベント処理が {max_gap:.2f} 秒止まりました")


if __name__ == "__main__":
    unittest.main()


# RecSortGuiTest（動画AIの画面の操作テスト）は削除:
# 仕様変更で動画AI（試作）をアプリの画面から外したため。動画AIの処理自体の単体テストは tests/test_recsort.py に残す。
