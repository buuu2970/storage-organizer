"""初期12本によるジャンル分類学習のテスト。

FFmpeg がない環境では、解析・学習・GUI のテストをスキップする。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from storage_organizer import ai, media, samples, scanner  # noqa: E402
from storage_organizer.db import ANSWERED, SKIPPED, UNANSWERED, Database  # noqa: E402
from storage_organizer.profiles import ProfileManager  # noqa: E402
from storage_organizer.samples import SampleStore  # noqa: E402

FFMPEG = media.find_ffmpeg()
# make_sample_videos.INITIAL_SAMPLES の順に付けるジャンル
RULE = ["ゲーム", "ゲーム", "授業・学習", "授業・学習", "音楽・歌唱", "音楽・歌唱", "個人撮影",
        "会議・仕事", "アニメ・テレビ", "その他・不明", "配信・実況", "音声・ラジオ"]

try:
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
except ImportError:
    QApplication = None


def snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


def fake_videos(folder: Path, names) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        (folder / name).write_bytes(f"video {i} {name}".encode() * 50)


def genre_ids(db: Database) -> dict[str, int]:
    return {g["name"]: g["id"] for g in db.genres()}


# =============================================================================
# サンプル動画の登録（FFmpeg 不要）
# =============================================================================
class SampleStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.folder = self.tmp / "samples_src"
        fake_videos(self.folder, [f"sample_{i:02d}.mp4" for i in range(1, 13)] + ["memo.txt"])
        self.store = SampleStore(self.tmp / "shared")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_register_twelve_in_fixed_order(self):  # 1
        before = snapshot(self.folder)
        result = self.store.register_folder(str(self.folder))
        self.assertEqual(result.count, 12)
        self.assertEqual(result.warnings, [])
        self.assertEqual([r["sample_id"] for r in self.store.samples()],
                         [f"sample_{i:02d}" for i in range(1, 13)])
        self.assertTrue(all(r["content_hash"] for r in self.store.samples()))
        self.assertEqual(snapshot(self.folder), before)  # 19: 元動画を変更しない
        # 2回目の登録でも順番と ID は変わらない
        self.store.register_folder(str(self.folder))
        self.assertEqual(len(self.store.samples()), 12)

    def test_any_extension_natural_order_and_limit(self):
        other = self.tmp / "mixed"
        fake_videos(other, ["clip10.mkv", "clip2.mov", "clip1.webm"] + [f"x{i}.avi" for i in range(11)])
        result = self.store.register_folder(str(other))
        self.assertEqual(result.count, 12)
        self.assertTrue(any("先頭の 12 本" in w for w in result.warnings))
        ids = [r["sample_id"] for r in self.store.samples()]
        self.assertEqual(ids[:3], ["clip1", "clip2", "clip10"])

    def test_manifest_gives_stable_ids(self):
        (self.folder / samples.MANIFEST).write_text(json.dumps(
            {"samples": [{"id": "game", "file": "sample_05.mp4"}, {"id": "talk", "file": "sample_01.mp4"}]}),
            encoding="utf-8")
        self.store.register_folder(str(self.folder))
        self.assertEqual([r["sample_id"] for r in self.store.samples()], ["game", "talk"])

    def test_missing_folder_and_file_do_not_crash(self):
        with self.assertRaises(FileNotFoundError):
            self.store.register_folder(str(self.tmp / "nothing"))
        self.store.register_folder(str(self.folder))
        (self.folder / "sample_03.mp4").unlink()
        self.store.refresh()
        self.assertEqual(self.store.sample("sample_03")["status"], "missing")
        self.assertEqual(self.store.sample("sample_04")["status"], "ok")

    def test_changed_sample_is_detected_and_analysis_invalidated(self):  # 18
        self.store.register_folder(str(self.folder))
        old_hash = self.store.sample("sample_02")["content_hash"]
        self.store.update_analysis("sample_02", analysis_state="完了", meta={"duration": 1},
                                   features={"video": None, "audio": None}, analyzed_hash=old_hash)
        path = self.folder / "sample_02.mp4"
        path.write_bytes(b"replaced content" * 99)
        result = self.store.refresh()
        self.assertEqual(result.changed, ["sample_02"])
        row = self.store.sample("sample_02")
        self.assertNotEqual(row["content_hash"], old_hash)
        self.assertEqual(row["analysis_state"], "未解析")
        self.assertIsNone(row["features"])

    def test_links_are_not_followed(self):
        link = self.folder / "linked.mp4"
        try:
            os.symlink(self.folder / "sample_01.mp4", link)
        except (OSError, NotImplementedError):
            self.skipTest("シンボリックリンクを作成できない環境です")
        files, warnings = samples.list_sample_files(str(self.folder))
        self.assertNotIn("linked", [sid for sid, _ in files])
        self.assertTrue(any("リンク" in w for w in warnings))

    def test_sample_path_check(self):  # 14 の前提
        self.store.register_folder(str(self.folder))
        self.assertTrue(self.store.is_sample_path(str(self.folder / "sample_01.mp4")))
        self.assertFalse(self.store.is_sample_path(str(self.tmp / "other.mp4")))


# =============================================================================
# 回答の保存・ジャンル・プロファイル（FFmpeg 不要）
# =============================================================================
class AnswerAndGenreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(self._tmp.name)
        self.g = genre_ids(self.db)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_presets_without_duplicates(self):  # 4
        names = [g["name"] for g in self.db.genres()]
        for name in ("ゲーム", "授業・学習", "会議・仕事", "アニメ・テレビ", "配信・実況", "音声・ラジオ", "個人撮影"):
            self.assertIn(name, names)
        # 「音楽・歌」「その他」は既存の同等ジャンル（音楽・歌唱／その他・不明）を使い、重複作成しない
        self.assertIn("音楽・歌唱", names)
        self.assertIn("その他・不明", names)
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("未分類", names)

    def test_multiple_genres_and_history(self):  # 3, 10
        sid = "sample_01"
        self.assertTrue(self.db.save_sample_answer(sid, ANSWERED, {self.g["ゲーム"], self.g["配信・実況"]}, "h1"))
        self.assertEqual(self.db.sample_answer(sid)["genres"], {self.g["ゲーム"], self.g["配信・実況"]})
        self.assertFalse(self.db.save_sample_answer(sid, ANSWERED, {self.g["ゲーム"], self.g["配信・実況"]}, "h1"))
        self.db.save_sample_answer(sid, ANSWERED, {self.g["音楽・歌唱"]}, "h1")  # 修正
        self.assertEqual(self.db.sample_answer(sid)["genres"], {self.g["音楽・歌唱"]})
        self.assertEqual(len(self.db.sample_answer_history(sid)), 2)
        with self.assertRaises(ValueError):
            self.db.save_sample_answer(sid, ANSWERED, set(), "h1")

    def test_skip_and_unanswer(self):  # 7
        self.db.save_sample_answer("sample_02", SKIPPED, {self.g["ゲーム"]}, "h")
        ans = self.db.sample_answer("sample_02")
        self.assertEqual(ans["status"], SKIPPED)
        self.assertEqual(ans["genres"], set())  # スキップにジャンルは付けない
        self.assertEqual(self.db.pending_training_count(), 0)  # 教師データにしない
        self.db.save_sample_answer("sample_02", UNANSWERED)
        self.assertIsNone(self.db.sample_answer("sample_02"))

    def test_add_and_rename_genre_keeps_id(self):  # 5, 6
        gid = self.db.add_genre("料理")
        self.db.save_sample_answer("sample_03", ANSWERED, {gid}, "h")
        self.db.rename_genre(gid, "料理・グルメ")
        self.assertEqual(genre_ids(self.db)["料理・グルメ"], gid)
        self.assertEqual(self.db.sample_answer("sample_03")["genres"], {gid})
        # 実質的な重複・未分類は追加できない
        for bad in ("ゲーム", " ｹﾞｰﾑ ", "授業 学習", "料理グルメ", "未分類", ""):
            with self.assertRaises(ValueError, msg=bad):
                self.db.add_genre(bad)
        with self.assertRaises(ValueError):
            self.db.rename_genre(gid, "ゲーム")

    def test_answers_persist_after_reopen(self):  # 9
        self.db.save_sample_answer("sample_01", ANSWERED, {self.g["ゲーム"]}, "h")
        self.db.set_setting("initial_state", "in_progress")
        self.db.set_setting("initial_index", "4")
        self.db.close()
        self.db = Database(self._tmp.name)
        self.assertEqual(self.db.sample_answer("sample_01")["genres"], {self.g["ゲーム"]})
        self.assertEqual(self.db.initial_state(), "in_progress")
        self.assertEqual(self.db.get_setting("initial_index"), "4")

    def test_initial_answers_do_not_enter_curation(self):  # 17
        self.db.save_sample_answer("sample_01", ANSWERED, {self.g["ゲーム"]}, "h")
        self.assertEqual(self.db.effective_labels("all"), {})
        self.assertEqual(self.db.effective_labels("genre"), {})
        self.assertEqual(self.db.labeled_genres(), set())
        self.assertEqual(ai.curation_scopes_with_data(self.db), [])


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_profiles_are_separated(self):  # 11
        pm = ProfileManager(self.root)
        p1 = pm.current()
        p2 = pm.add()
        self.assertEqual([p["name"] for p in pm.profiles()], ["ユーザー1", "ユーザー2"])
        self.assertNotEqual(pm.data_dir(p1["id"]), pm.data_dir(p2["id"]))
        db1, db2 = Database(pm.data_dir(p1["id"])), Database(pm.data_dir(p2["id"]))
        try:
            g1 = db1.add_genre("料理")
            db1.save_sample_answer("sample_01", ANSWERED, {g1}, "h")
            self.assertNotIn("料理", genre_ids(db2))  # 別プロファイルにジャンルを追加しない
            self.assertEqual(db2.sample_answers(), {})
            self.assertEqual(db2.pending_training_count(), 0)
        finally:
            db1.close()
            db2.close()
        pm.set_current(p2["id"])
        again = ProfileManager(self.root)  # 再起動後も ID と選択を保持
        self.assertEqual(again.current()["id"], p2["id"])
        self.assertEqual(again.get(p1["id"])["name"], "ユーザー1")

    def test_legacy_data_becomes_first_profile(self):
        Database(self.root).close()  # 以前の版のデータ（直下の recsort.db）
        pm = ProfileManager(self.root)
        self.assertEqual(pm.data_dir(), self.root.resolve())


# =============================================================================
# 解析・学習（FFmpeg が必要）
# =============================================================================
@unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
class TrainingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_sample_videos
        cls._shared = tempfile.TemporaryDirectory()
        base = Path(cls._shared.name)
        cls.initial_root = base / "initial"
        make_sample_videos.create_initial(cls.initial_root, FFMPEG.ffmpeg)
        cls.video_root = base / "videos"
        make_sample_videos.create(cls.video_root, FFMPEG.ffmpeg)
        cls.samples_root = base / "shared_samples"
        store = SampleStore(cls.samples_root)
        store.register_folder(str(cls.initial_root))
        store.close()
        cls.analysis = samples.task_analyze_samples(cls.samples_root, FFMPEG, 12, threading.Event(),
                                                    lambda p: None)
        cls.before = snapshot(cls.initial_root)

    @classmethod
    def tearDownClass(cls):
        cls._shared.cleanup()

    def setUp(self):
        self._data = tempfile.TemporaryDirectory()
        self.db = Database(self._data.name)
        self.store = SampleStore(self.samples_root)
        self.g = genre_ids(self.db)

    def tearDown(self):
        self.store.close()
        self.db.close()
        self._data.cleanup()
        self.assertEqual(snapshot(self.initial_root), self.before, "初期サンプルが変更されました")

    def answer(self, db, indices):
        rows = self.store.samples()
        g = genre_ids(db)
        for i in indices:
            db.save_sample_answer(rows[i]["sample_id"], ANSWERED, {g[RULE[i]]}, rows[i]["content_hash"])

    def test_samples_analyzed_into_shared_cache(self):
        self.assertEqual(self.analysis["完了"] + self.analysis["一部失敗"], 12)
        self.assertEqual(len(self.store.usable_features()), 12)
        cols = {r[1] for r in self.store.conn.execute("PRAGMA table_info(samples)")}
        self.assertFalse(cols & {"genres", "status_answer", "profile_id"})  # 共通キャッシュに回答を持たない

    def test_interim_model_from_partial_answers(self):  # 12
        self.answer(self.db, [0, 4])
        r = ai.retrain(self.db, self.store)
        self.assertTrue(r["adopted"], r["message"])
        self.assertIn("初期12本の回答 2 件", r["message"])
        model = ai.load_active_model(self.db)
        self.assertEqual({s["video_id"] for s in model.samples},
                         {"sample:" + self.store.samples()[i]["sample_id"] for i in (0, 4)})
        self.assertEqual(self.db.pending_training_count(), 0)
        # 似た回答のない動画（授業風）は推測を強制しない
        row = self.store.samples()[2]
        z = model.normalizer.transform(ai.feature_vector(json.loads(row["meta"]), json.loads(row["features"])))
        self.assertEqual(model.classify(z)["state"], "未分類")

    def test_only_skips_cannot_train(self):
        rows = self.store.samples()
        self.db.save_sample_answer(rows[0]["sample_id"], SKIPPED, (), rows[0]["content_hash"])
        r = ai.retrain(self.db, self.store)
        self.assertFalse(r["adopted"])
        self.assertIsNone(self.db.active_model())

    def test_other_profile_answers_not_used(self):  # 13
        other_dir = tempfile.TemporaryDirectory()
        other = Database(other_dir.name)
        try:
            self.answer(other, range(12))
            self.answer(self.db, [0])
            ai.retrain(self.db, self.store)
            model = ai.load_active_model(self.db)
            self.assertEqual(len(model.samples), 1)
            self.assertIsNone(other.active_model())
        finally:
            other.close()
            other_dir.cleanup()

    def test_personal_classification_with_initial_model(self):  # 15, 16
        records: list = []
        scanner.scan(str(self.video_root), threading.Event(), on_batch=records.extend)
        self.db.register_videos([r for r in records if r.category == "動画"], str(self.video_root), True)
        ai.task_analyze(self.db.data_dir, FFMPEG, self.db.video_ids(), 12, threading.Event(), lambda p: None)
        ids = {r["name"]: r["id"] for r in self.db.videos()}
        self.assertFalse(any(self.store.is_sample_path(r["path"]) for r in self.db.videos()))  # 14
        # ユーザーが確定した分類（あえて別ジャンル）
        self.db.set_user_genres(ids["game_02.mp4"], {self.g["会議・仕事"]}, source="修正")
        self.answer(self.db, range(12))
        r = ai.retrain(self.db, self.store)
        self.assertTrue(r["adopted"], r["message"])
        v = lambda n: self.db.video(ids[n])  # noqa: E731
        self.assertEqual(self.db.user_genres(ids["game_02.mp4"]), {self.g["会議・仕事"]})  # 上書きしない
        self.assertIsNone(v("game_02.mp4")["ai_genres"])
        self.assertEqual(v("broken.mp4")["ai_state"], "未分類")  # 解析できない動画は未分類
        song = [e["genre_id"] for e in json.loads(v("song_01.mp4")["ai_genres"] or "[]")]
        self.assertEqual(song, [self.g["音楽・歌唱"]], v("song_01.mp4")["ai_reason"])
        # 提案の却下: 同じジャンルは再提案しない
        self.db.reject_ai_genres(ids["song_01.mp4"])
        ai.classify_all(self.db)
        self.assertEqual(json.loads(v("song_01.mp4")["ai_genres"]), [])
        self.assertEqual(v("song_01.mp4")["ai_state"], "未分類")
        # 厳選AIには混ざらない（17）
        self.assertEqual(ai.retrain_curation(self.db), {})
        self.assertEqual(set(self.db.active_models()), {"classify"})

    def test_replaced_sample_answer_is_not_reused(self):  # 18
        folder = Path(self._data.name) / "copy"
        shutil.copytree(self.initial_root, folder)
        store = SampleStore(Path(self._data.name) / "own_samples")
        try:
            store.register_folder(str(folder))
            samples.task_analyze_samples(store.root, FFMPEG, 12, threading.Event(), lambda p: None)
            rows = store.samples()
            g = genre_ids(self.db)
            self.db.save_sample_answer(rows[0]["sample_id"], ANSWERED, {g["ゲーム"]}, rows[0]["content_hash"])
            self.db.save_sample_answer(rows[4]["sample_id"], ANSWERED, {g["音楽・歌唱"]}, rows[4]["content_hash"])
            # sample_01 を別の動画（授業風の sample_03）に入れ替える
            time.sleep(0.02)
            shutil.copyfile(folder / "sample_03.mp4", folder / "sample_01.mp4")
            self.assertEqual(store.refresh().changed, ["sample_01"])
            r = ai.retrain(self.db, store)
            self.assertTrue(r["adopted"])
            self.assertIn("再確認まで除外", r["message"])
            model = ai.load_active_model(self.db)
            self.assertEqual([s["video_id"] for s in model.samples], ["sample:sample_05"])
        finally:
            store.close()


# =============================================================================
# GUI（FFmpeg と PySide6 が必要）
# =============================================================================
def wait_until(predicate, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@unittest.skipIf(QApplication is None, "PySide6 がインストールされていません")
@unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
class InitialLearningGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_sample_videos
        cls.app = QApplication.instance() or QApplication([])
        cls._shared = tempfile.TemporaryDirectory()
        base = Path(cls._shared.name)
        cls.initial_root = base / "initial"
        make_sample_videos.create_initial(cls.initial_root, FFMPEG.ffmpeg)
        cls.samples_root = base / "shared_samples"
        store = SampleStore(cls.samples_root)
        store.register_folder(str(cls.initial_root))
        store.close()
        samples.task_analyze_samples(cls.samples_root, FFMPEG, 12, threading.Event(), lambda p: None)
        cls.before = snapshot(cls.initial_root)

    @classmethod
    def tearDownClass(cls):
        cls._shared.cleanup()

    def setUp(self):
        from storage_organizer.main_window import MainWindow
        self.MainWindow = MainWindow
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
        self.assertEqual(snapshot(self.initial_root), self.before, "初期サンプルが変更されました")

    def _new_window(self, **kw):
        if "profiles" not in kw:
            kw["db"] = Database(Path(self._data.name) / "profile")
        w = self.MainWindow(samples_root=self.samples_root, **kw)
        w.ask_initial_learning = False
        w.show()
        return w

    def _close(self, w):
        w.close()
        w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def _wait_task(self, w=None):
        w = w or self.window
        self.assertTrue(wait_until(lambda: not w.ctx.is_busy(), 120), "処理が終わりません")
        QCoreApplication.processEvents()

    def test_all_twelve_in_order_skip_and_finish(self):  # 2, 3, 7, 12
        w = self.window
        page = w.initial
        w.show_page(page)
        page.start()
        self.assertTrue(page.step.isVisible())
        rows = w.ctx.samples.samples()
        self.assertEqual(page.sample_ids, [r["sample_id"] for r in rows])
        self.assertEqual(page.sample_list.count(), 12)
        g = genre_ids(w.ctx.db)
        seen = []
        for i in range(12):
            self.assertIn(f"{i + 1} / 12", page.step_label.text())
            seen.append(page.current_id())
            if i == 9:
                page.skip_button.click()
            elif i == 0:
                page.genres.set_checked({g["ゲーム"], g["配信・実況"]})  # 複数ジャンル
                page.next_button.click()
            else:
                page.genres.set_checked({g[RULE[i]]})
                page.next_button.click()
        self.assertEqual(seen, page.sample_ids)
        answers = w.ctx.db.sample_answers()
        self.assertEqual(answers[seen[0]]["genres"], {g["ゲーム"], g["配信・実況"]})
        self.assertEqual(answers[seen[9]]["status"], SKIPPED)
        self.assertIn("回答済み 11 / 12", page.status_label.text())
        page.finish_button.click()
        self._wait_task()
        self.assertEqual(w.ctx.db.initial_state(), "done")
        model = w.ctx.db.active_model()
        self.assertIsNotNone(model)
        self.assertEqual(model["n_samples"], 11)  # スキップは教師データにしない
        self.assertEqual(w.ctx.db.stats()["videos"], 0)  # 14: 通常の動画一覧に入らない

    def test_add_genre_and_correct_answer(self):  # 5, 10
        w = self.window
        page = w.initial
        w.show_page(page)
        page.start()
        page.new_genre.setText("料理")
        page.add_genre_button.click()
        gid = genre_ids(w.ctx.db)["料理"]
        self.assertIn(gid, page.genres.checked_ids())  # 追加したジャンルはすぐ選べる
        page.next_button.click()
        first = page.sample_ids[0]
        self.assertEqual(w.ctx.db.sample_answer(first)["genres"], {gid})
        page.new_genre.setText("ゲーム ")
        page.add_genre_button.click()  # 重複は追加しない
        self.assertTrue(any("同じジャンル" in str(m) for m in self.messages))
        # 一覧から戻って修正
        page.sample_list.setCurrentRow(0)
        self.assertEqual(page.current_id(), first)
        self.assertEqual(page.genres.checked_ids(), {gid})
        g = genre_ids(w.ctx.db)
        page.genres.set_checked({g["ゲーム"]})
        page.next_button.click()
        self.assertEqual(w.ctx.db.sample_answer(first)["genres"], {g["ゲーム"]})

    def test_suspend_and_resume_after_restart(self):  # 9
        w = self.window
        page = w.initial
        w.show_page(page)
        page.start()
        g = genre_ids(w.ctx.db)
        for i in range(3):
            page.genres.set_checked({g[RULE[i]]})
            page.next_button.click()
        page.genres.set_checked({g[RULE[3]]})
        page.suspend_button.click()  # 選びかけの4本目も保存
        self.assertFalse(page.step.isVisible())
        self.assertEqual(w.ctx.db.initial_state(), "in_progress")
        self._close(w)
        self.window = w = self._new_window()
        w.show_page(w.initial)
        self.assertEqual(w.initial.start_button.text(), "前回の続きから再開")
        w.initial.start()
        self.assertEqual(w.initial.index, 3)
        self.assertEqual(len([a for a in w.ctx.db.sample_answers().values() if a["status"] == ANSWERED]), 4)
        self.assertEqual(w.initial.genres.checked_ids(), {g[RULE[3]]})

    def test_skip_whole_initial_learning(self):  # 8
        w = self.window
        item = w.nav.item(w.page_widgets.index(w.initial))
        w.show_page(w.initial)
        self.assertFalse(item.isHidden())
        w.initial.skip_all_button.click()
        self.assertEqual(w.ctx.db.initial_state(), "skipped")
        self.assertTrue(item.isHidden())  # スキップしたらメニューから消える
        self.assertIs(w.stack.currentWidget(), w.dashboard)
        self.assertIn("精度は十分ではありません", w.dashboard.ai_label.text())
        self.assertIn("スキップ", w.dashboard.ai_label.text())
        # 再起動しても隠れたまま
        self._close(w)
        self.window = w = self._new_window()
        self.assertTrue(w.nav.item(w.page_widgets.index(w.initial)).isHidden())
        # 「設定」から再表示できる
        w.show_page(w.settings)
        next(b for b in w.settings.findChildren(QPushButton) if "もう一度表示" in b.text()).click()
        self.assertFalse(w.nav.item(w.page_widgets.index(w.initial)).isHidden())

    def test_bundled_samples_registered_automatically(self):
        bundled = samples.project_sample_dir()
        if not bundled.is_dir() or len(samples.list_sample_files(str(bundled))[0]) != 12:
            self.skipTest("同梱の12本がありません")
        store = SampleStore(Path(self._data.name) / "fresh_samples")
        try:
            store.ensure_registered()
            self.assertEqual(len(store.samples()), 12)
            self.assertEqual(Path(store.sample_dir()), bundled)
        finally:
            store.close()

    def test_scan_does_not_register_samples(self):  # 14
        w = self.window
        w.show_page(w.scan_page)
        w.scan_page.set_folder(str(self.initial_root))
        w.scan_page.scan_button.click()
        self.assertTrue(wait_until(lambda: not w.scan_page.is_scanning(), 30))
        QCoreApplication.processEvents()
        self.assertEqual(w.ctx.db.stats()["videos"], 0)
        self.assertIn("初期学習用サンプル 12 本は登録しません", w.scan_page.current_dir_label.toolTip())

    def test_missing_samples_show_error(self):
        empty_root = Path(self._data.name) / "empty_samples"
        self._close(self.window)
        with mock.patch.object(SampleStore, "candidate_dirs", lambda self: []):
            w = self.MainWindow(db=Database(Path(self._data.name) / "p2"), samples_root=empty_root)
            w.ask_initial_learning = False
            self.window = w
            w.show()
            w.show_page(w.initial)
            w.initial.start()
        self.assertFalse(w.initial.step.isVisible())
        self.assertIn("サンプルフォルダを指定", w.initial.error_label.text())

    def test_profile_switch_keeps_data_separate(self):  # 11
        self._close(self.window)
        pm = ProfileManager(Path(self._data.name) / "app")
        self.window = w = self._new_window(profiles=pm)
        g = genre_ids(w.ctx.db)
        w.ctx.db.add_genre("料理")
        w.ctx.db.save_sample_answer(w.ctx.samples.samples()[0]["sample_id"], ANSWERED, {g["ゲーム"]},
                                    w.ctx.samples.samples()[0]["content_hash"])
        first_dir = w.ctx.db.data_dir
        w.add_profile_button.click()
        self.assertEqual(w.profile_combo.currentText(), "ユーザー2")
        self.assertNotEqual(w.ctx.db.data_dir, first_dir)
        self.assertEqual(w.ctx.db.sample_answers(), {})
        self.assertNotIn("料理", genre_ids(w.ctx.db))
        self.assertEqual(len(w.ctx.samples.samples()), 12)  # 共通サンプルは使える
        # 解析中に切り替えても、結果は元のプロファイルにだけ書かれる
        self.assertTrue(w.ctx.analyze_samples(parent=w))
        w.switch_profile(pm.profiles()[0]["id"])
        self.assertFalse(w.ctx.is_busy())
        self.assertEqual(w.ctx.db.data_dir, first_dir)
        self.assertIn("料理", genre_ids(w.ctx.db))
        self.assertEqual(len(w.ctx.db.sample_answers()), 1)


if __name__ == "__main__":
    unittest.main()
