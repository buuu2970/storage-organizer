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
    from storage_organizer.db import Database
    from storage_organizer.main_window import MainWindow


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
        self._tmp = tempfile.TemporaryDirectory()
        self._data = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.window = MainWindow(Database(self._data.name), start_autoscan=False)
        self.window.ask_initial_learning = False
        self.window.show()
        self.window.show_page(self.window.scan_page)
        self.page = self.window.scan_page

    def tearDown(self) -> None:
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


# =============================================================================
# RecSort AI の追加画面
# =============================================================================
import hashlib  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
if QApplication is not None:
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from storage_organizer import media
    from storage_organizer.db import CURATE_ALL_SCOPE as CURATE_ALL


def _snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


@unittest.skipIf(QApplication is None, "PySide6 がインストールされていません")
@unittest.skipUnless(QApplication is not None and media.find_ffmpeg().available, "FFmpeg がありません")
class RecSortGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_sample_videos
        cls.app = QApplication.instance() or QApplication([])
        cls._videos = tempfile.TemporaryDirectory()
        cls.video_root = Path(cls._videos.name) / "videos"
        make_sample_videos.create(cls.video_root, media.find_ffmpeg().ffmpeg)
        cls.before = _snapshot(cls.video_root)

    @classmethod
    def tearDownClass(cls):
        cls._videos.cleanup()

    def setUp(self):
        self._data = tempfile.TemporaryDirectory()
        self.messages = []
        answer = lambda *a, **k: (self.messages.append(a[1:3]) or QMessageBox.StandardButton.Yes)  # noqa: E731
        self._patches = [mock.patch.object(QMessageBox, name, staticmethod(answer))
                         for name in ("information", "warning", "question")]
        for p in self._patches:
            p.start()
        self.window = self._new_window()

    def tearDown(self):
        self._close(self.window)
        for p in self._patches:
            p.stop()
        self._data.cleanup()
        self.assertEqual(_snapshot(self.video_root), self.before, "元の動画が変更されました")

    def _new_window(self):
        w = MainWindow(Database(self._data.name), start_autoscan=False)
        w.ask_initial_learning = False
        w.show()
        return w

    def _close(self, w):
        w.close()
        w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _scan(self):
        w = self.window
        w.show_page(w.scan_page)
        w.scan_page.set_folder(str(self.video_root))
        w.scan_page.scan_button.click()
        self.assertTrue(wait_until(lambda: not w.scan_page.is_scanning(), 30))
        QCoreApplication.processEvents()

    def _wait_task(self, timeout=120):
        self.assertTrue(wait_until(lambda: not self.window.ctx.is_busy(), timeout), "処理が終わりません")
        QCoreApplication.processEvents()

    def _ids(self):
        return {r["name"]: r["id"] for r in self.window.ctx.db.videos()}

    def test_full_flow(self):
        w, db = self.window, self.window.ctx.db
        self._scan()
        self.assertEqual(db.stats()["videos"], 11)
        self.assertIn("動画 11 本を登録しました", w.scan_page.current_dir_label.toolTip())

        # 動画一覧: 検索・並び替え
        w.show_page(w.video_list)
        vl = w.video_list
        self.assertEqual(vl.model.rowCount(), 11)
        vl.search.setText("song")
        self.assertEqual(vl.model.rowCount(), 2)
        vl.search.clear()
        vl.table.sortByColumn(2, Qt.SortOrder.DescendingOrder)  # サイズ
        sizes = [r["size"] for r in vl.model.rows]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

        # 初期学習: 解析 → 5本選択 → ジャンル付け → 学習
        w.show_page(w.initial)
        w.initial.start()
        self._wait_task()
        self.assertEqual(len(w.initial.candidates), 5)
        self.assertTrue(w.initial.step.isVisible())
        g = {x["name"]: x["id"] for x in db.genres()}
        rule = {"game": "ゲーム", "song": "音楽・歌唱", "lecture": "授業・学習", "camera": "個人撮影"}
        for i, vid in enumerate(w.initial.candidates):
            name = db.video(vid)["name"]
            genre = next((v for k, v in rule.items() if name.startswith(k)), "その他・不明")
            w.initial.genres.set_checked({g[genre]})
            if i < len(w.initial.candidates) - 1:
                w.initial.next_button.click()
        w.initial.finish_button.click()
        self._wait_task()
        self.assertEqual(db.get_setting("initial_state"), "done")
        self.assertIsNotNone(db.active_model())
        self.assertEqual(db.stats()["manual"], 5)
        self.assertTrue(any(r["ai_state"] in ("分類済み", "未分類") for r in db.videos()
                            if r["id"] not in db.all_user_genres()))

        # 動画詳細: ジャンル保存・お気に入り・保護・サムネイル
        ids = self._ids()
        w.open_video(ids["lecture_02.mp4"])
        d = w.detail
        self.assertIs(w.stack.currentWidget(), d)
        self.assertIsNotNone(d.thumb_labels[0].pixmap())
        self.assertFalse(d.thumb_labels[0].pixmap().isNull())
        d.genres.set_checked({g["授業・学習"], g["会議・仕事"]})
        d.save_genres_button.click()
        self.assertEqual(db.user_genres(ids["lecture_02.mp4"]), {g["授業・学習"], g["会議・仕事"]})
        self.assertGreaterEqual(db.pending_training_count(), 1)
        d.favorite_box.setChecked(True)
        d.protect_box.setChecked(True)
        self.assertTrue(db.video(ids["lecture_02.mp4"])["protected"])
        self.assertFalse(d.compress_button.isEnabled())

        # 未分類一覧 → 確定 → 再学習
        w.show_page(w.unclassified)
        un = w.unclassified
        if un.model.rowCount():
            vid = un.model.video_id(0)
            un.table.selectRow(0)
            un.genres.set_checked({g["その他・不明"]})
            un.confirm_button.click()
            self.assertEqual(db.user_genres(vid), {g["その他・不明"]})
        version = db.active_model()["version"]
        un.retrain_button.click()
        self._wait_task()
        self.assertGreater(db.active_model()["version"], version)
        self.assertEqual(db.pending_training_count(), 0)

        # 厳選: ジャンル別（OR）と保護動画の扱い
        w.show_page(w.curation)
        cu = w.curation
        cu.genre_mode.setChecked(True)
        cu.genres.set_checked({g["授業・学習"], g["音楽・歌唱"]})
        cu.refresh()
        names = [r["name"] for r in cu.model.rows]
        self.assertIn("lecture_02.mp4", names)
        self.assertEqual(len(names), len(set(names)))  # 複数ジャンルでも1本
        cu.table.selectAll()
        cu.choice_buttons["整理候補"].click()
        self.assertNotIn(ids["lecture_02.mp4"], {k[0] for k in db.effective_labels("genre")})  # 保護中は対象外
        self.assertTrue(any("保護中" in str(m) for m in self.messages))
        cu.all_mode.setChecked(True)
        self.assertEqual(cu.model.rowCount(), 11)

        # 重複グループ
        w.show_page(w.duplicates)
        w.duplicates.run_button.click()
        self._wait_task()
        self.assertEqual(w.duplicates.tree.topLevelItemCount(), 1)
        self.assertEqual(w.duplicates.tree.topLevelItem(0).childCount(), 2)

        # 圧縮: 保護中は不可、別ファイルに出力
        w.open_compress(ids["lecture_02.mp4"])
        self.assertFalse(w.compress.start_button.isEnabled())
        out = Path(self._data.name) / "compressed"
        out.mkdir()
        w.compress.select_video(ids["old_format.avi"])
        w.compress.out_dir.setText(str(out))
        self.assertTrue(w.compress.start_button.isEnabled())
        w.compress.start_button.click()
        self._wait_task()
        self.assertIn("完了", w.compress.result_label.text())
        self.assertEqual(len(list(out.glob("*.mp4"))), 1)

        # 再起動後もデータが残る
        self._close(w)
        self.window = w = self._new_window()
        db = w.ctx.db
        self.assertEqual(db.user_genres(ids["lecture_02.mp4"]), {g["授業・学習"], g["会議・仕事"]})
        self.assertTrue(db.video(ids["lecture_02.mp4"])["favorite"])
        self.assertTrue(db.video(ids["lecture_02.mp4"])["protected"])
        w.show_page(w.dashboard)
        self.assertIn("採用中のモデル: v", w.dashboard.ai_label.text())

    def test_initial_learning_prompt_skip(self):
        w = self.window
        w.ask_initial_learning = True
        with mock.patch.object(QMessageBox, "exec", lambda self: 0):  # 「今はスキップ」相当
            self._scan()
        self.assertEqual(w.ctx.db.get_setting("initial_state"), "skipped")
        w.show_page(w.video_list)
        self.assertEqual(w.video_list.model.rowCount(), 11)  # 通常の動画管理は使える
        w.show_page(w.initial)
        self.assertIn("スキップ", w.initial.status_label.text())

    def test_curation_flow(self):
        from storage_organizer import pages as pg
        w, db = self.window, self.window.ctx.db
        self._scan()
        ids = self._ids()
        g = {x["name"]: x["id"] for x in db.genres()}
        g1, g2 = g["授業・学習"], g["ゲーム"]
        for name, vid in ids.items():
            db.set_user_genres(vid, {g1, g2} if name.startswith("game") else {g1}, source="修正")
        w.show_page(w.curation)
        cu = w.curation
        # ジャンル別: 共通回答 → 両方のジャンルに記録、全動画には記録しない
        cu.genre_mode.setChecked(True)
        cu.genres.set_checked({g1, g2})
        cu.genre_apply.click()
        self.assertEqual(cu.model.rowCount(), 11)
        row = next(i for i, r in enumerate(cu.model.rows) if r["name"] == "game_01.mp4")
        cu.table.selectRow(row)
        cu.choice_buttons["残す"].click()
        vid = ids["game_01.mp4"]
        eff = db.effective_labels("genre")
        self.assertEqual((eff[(vid, g1)]["label"], eff[(vid, g2)]["label"]), ("残す", "残す"))
        self.assertEqual(db.effective_labels("all"), {})
        # 個別回答（ゲームだけ整理候補）が共通回答より優先される
        row = next(i for i, r in enumerate(cu.model.rows) if r["name"] == "game_01.mp4")
        cu.table.selectRow(row)
        self.assertEqual(set(cu._combos), {g1, g2})
        cu._combos[g2].setCurrentText("整理候補")
        cu.individual_save.click()
        row = next(i for i, r in enumerate(cu.model.rows) if r["name"] == "game_01.mp4")
        cu.table.selectRow(row)
        cu.choice_buttons["保留"].click()
        eff = db.effective_labels("genre")
        self.assertEqual((eff[(vid, g1)]["label"], eff[(vid, g2)]["label"]), ("保留", "整理候補"))
        self.assertIn("整理候補（個別）", next(r for r in cu.model.rows if r["id"] == vid)["answer"])
        # ジャンル変更 → 再確認待ち → 確認して戻す
        w.open_video(vid)
        w.detail.genres.set_checked({g1})
        w.detail.save_genres_button.click()
        w.show_page(w.review)
        self.assertEqual(w.review.tree.topLevelItemCount(), 1)
        self.assertEqual(w.review.tree.topLevelItem(0).text(3), "属していない")
        w.review.tree.setCurrentItem(w.review.tree.topLevelItem(0))
        w.review.confirm_button.click()
        self.assertEqual(w.review.tree.topLevelItemCount(), 0)
        # 全動画: 回答して再学習 → AI提案が表示される
        w.show_page(w.curation)
        cu.all_mode.setChecked(True)
        for name, vid2 in ids.items():
            if name in ("game_01.mp4", "game_02.mp4", "camera_01.mp4", "old_format.avi"):
                db.answer(vid2, "all", None, "残す", "all")
            elif name in ("song_01.mp4", "silent_gray.mp4", "broken.mp4", "lecture_01.mp4"):
                db.answer(vid2, "all", None, "整理候補", "all")
        cu.retrain_button.click()
        self._wait_task()
        self.assertIn(CURATE_ALL, db.active_models())
        cu.refresh()
        self.assertTrue(all(r["suggest"] for r in cu.model.rows))
        # 学習リセット: 全動画厳選AIだけを初期化
        self.assertTrue(pg.open_reset_dialog(w.ctx, w, choose=lambda: [CURATE_ALL]))
        self.assertNotIn(CURATE_ALL, db.active_models())
        self.assertTrue(any("初期化するモデル" in str(m) for m in self.messages))
        self.assertEqual(len(db.training_labels("all")), 8)

    def test_genre_management(self):
        w, db = self.window, self.window.ctx.db
        w.show_page(w.genre_page)
        with mock.patch.object(QInputDialog, "getText", staticmethod(lambda *a, **k: ("料理", True))):
            w.genre_page._add("genre")
        self.assertIn("料理", [x["name"] for x in db.genres()])
        lst = w.genre_page.genre_list
        lst.setCurrentRow(lst.count() - 1)
        with mock.patch.object(QInputDialog, "getText", staticmethod(lambda *a, **k: ("お料理", True))):
            w.genre_page._rename("genre")
        self.assertIn("お料理", [x["name"] for x in db.genres()])
        lst.setCurrentRow(lst.count() - 1)
        w.genre_page._delete("genre")  # 確認ダイアログは「はい」で自動応答
        self.assertNotIn("お料理", [x["name"] for x in db.genres()])
        self.assertTrue(any("削除の確認" in str(m) for m in self.messages))

    def test_video_preview_plays(self):
        w = self.window
        self._scan()
        ids = self._ids()
        w.open_video(ids["game_01.mp4"])
        player = w.detail.player
        if player.player is None:
            self.skipTest("Qt Multimedia を利用できません")
        player.player.play()
        ok = wait_until(lambda: player.player.position() > 300 or player.error_label.text(), 15)
        self.assertTrue(ok)
        self.assertEqual(player.error_label.text(), "")
        player.slider.sliderMoved.emit(4000)  # シーク
        self.assertTrue(wait_until(lambda: player.player.position() >= 3900, 5))
        player.stop_button.click()
        # 壊れたファイルは再生エラーを表示する
        w.open_video(ids["broken.mp4"])
        w.detail.player.player.play()
        self.assertTrue(wait_until(lambda: bool(w.detail.player.error_label.text()), 10))
