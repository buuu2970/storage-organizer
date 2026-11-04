"""RecSort AI の追加機能のテスト（GUI なし）。FFmpeg がない環境では解析系をスキップする。"""

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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from storage_organizer import ai, media, scanner  # noqa: E402
from storage_organizer.legacy_db import CLASSIFY_SCOPE, CURATE_ALL_SCOPE, Database, curate_genre_scope  # noqa: E402

FFMPEG = media.find_ffmpeg()


def snapshot(root: Path) -> dict:
    """フォルダ内の全ファイルの (サイズ, 更新日時, SHA-256)。元動画が変わっていないことの確認用。"""
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


def scan_and_register(db: Database, root: Path) -> list:
    records: list = []
    result = scanner.scan(str(root), threading.Event(), on_batch=records.extend)
    videos = [r for r in records if r.category == "動画"]
    db.register_videos(videos, str(root), complete_scan=not result.cancelled and result.skipped == 0)
    return videos


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(self._tmp.name)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _add_video(self, name="a.mp4", size=100, mtime=1.0):
        rec = scanner.FileRecord(name, size, "動画", mtime, "/videos")
        self.db.register_videos([rec])
        return self.db.conn.execute("SELECT id FROM videos WHERE name=?", (name,)).fetchone()[0]

    def test_default_genres_and_rename_keeps_links(self):
        names = [g["name"] for g in self.db.genres()]
        self.assertEqual(names[0], "ゲーム")
        self.assertIn("その他・不明", names)
        vid = self._add_video()
        game = self.db.genres()[0]["id"]
        self.db.set_user_genres(vid, {game}, source="修正")
        self.db.rename_genre(game, "ゲームプレイ")
        self.assertEqual(self.db.user_genres(vid), {game})
        self.assertEqual(self.db.genre_names()[game], "ゲームプレイ")
        new_id = self.db.add_genre("料理")
        self.db.set_user_genres(vid, {game, new_id}, source="修正")  # 複数ジャンル
        self.assertEqual(self.db.user_genres(vid), {game, new_id})
        self.db.delete_genre(new_id)
        self.assertEqual(self.db.user_genres(vid), {game})

    def test_training_registration_and_pending(self):
        vid = self._add_video()
        g = self.db.genres()[0]["id"]
        self.assertFalse(self.db.set_user_genres(vid, set()))
        self.db.set_user_genres(vid, {g}, source="初期学習")
        self.assertEqual(self.db.pending_training_count(), 1)
        self.db.mark_training_applied(1, [vid])
        self.assertEqual(self.db.pending_training_count(), 0)
        self.db.set_user_genres(vid, {g, self.db.genres()[1]["id"]}, source="修正")
        self.assertEqual(self.db.pending_training_count(), 1)  # 修正は未反映に戻る

    def test_change_detection_invalidates_analysis_but_keeps_user_data(self):
        vid = self._add_video(size=100, mtime=1.0)
        g = self.db.genres()[0]["id"]
        self.db.set_user_genres(vid, {g}, source="修正")
        self.db.set_flag(vid, "protected", True)
        self.db.update_video(vid, analysis_state="完了", features={"video": None}, sha256="x")
        self.db.suggest_ai_tag(vid, "黒画面")
        rec = scanner.FileRecord("a.mp4", 200, "動画", 2.0, "/videos")
        added, invalidated = self.db.register_videos([rec])
        self.assertEqual((added, invalidated), (0, 1))
        row = self.db.video(vid)
        self.assertEqual(row["analysis_state"], "未解析")
        self.assertIsNone(row["sha256"])
        self.assertEqual(self.db.user_genres(vid), {g})
        self.assertTrue(row["protected"])
        self.assertEqual([t for t in self.db.video_tags(vid) if t["source"] == "ai"], [])

    def test_tags_manual_and_ai_status(self):
        vid = self._add_video()
        self.db.set_manual_tag(vid, "会話あり", True)
        self.db.suggest_ai_tag(vid, "黒画面")
        self.db.suggest_ai_tag(vid, "会話あり")  # 手動タグには触れない
        tags = {t["name"]: t for t in self.db.video_tags(vid)}
        self.assertEqual((tags["会話あり"]["source"], tags["会話あり"]["status"]), ("manual", "採用"))
        self.assertEqual(tags["黒画面"]["status"], "保留")
        self.db.set_ai_tag_status(vid, tags["黒画面"]["id"], "却下")
        self.db.suggest_ai_tag(vid, "黒画面")  # 却下済みは復活させない
        self.assertEqual({t["name"]: t["status"] for t in self.db.video_tags(vid)}["黒画面"], "却下")

    def test_protected_cannot_be_curated(self):
        vid = self._add_video()
        self.db.answer(vid, "all", None, "整理候補", "all")
        self.assertEqual(self.db.effective_labels("all")[vid]["label"], "整理候補")
        self.db.set_flag(vid, "protected", True)
        with self.assertRaises(PermissionError):
            self.db.answer(vid, "all", None, "残す", "all")
        g = self.db.genres()[0]["id"]
        with self.assertRaises(PermissionError):
            self.db.answer(vid, "genre", g, "整理候補", "common")

    def test_persistence_after_reopen(self):
        vid = self._add_video()
        g = self.db.genres()[2]["id"]
        self.db.set_user_genres(vid, {g}, source="修正")
        self.db.set_flag(vid, "favorite", True)
        self.db.answer(vid, "all", None, "保留", "all")
        self.db.set_setting("initial_state", "done")
        self.db.close()
        self.db = Database(self._tmp.name)
        row = self.db.video(vid)
        self.assertEqual(self.db.user_genres(vid), {g})
        self.assertTrue(row["favorite"])
        self.assertEqual(self.db.effective_labels("all")[vid]["label"], "保留")
        self.assertEqual(self.db.get_setting("initial_state"), "done")

    def test_missing_marked_only_after_complete_scan(self):
        self._add_video("gone.mp4")
        self.db.register_videos([], "/videos", complete_scan=False)
        self.assertEqual(self.db.stats()["missing"], 0)
        self.db.register_videos([], "/videos", complete_scan=True)
        self.assertEqual(self.db.stats()["missing"], 1)
        self.assertEqual(len(self.db.videos(include_missing=True)), 1)  # 記録は消さない

    def test_recommendation_priority(self):
        vid = self._add_video()
        self.db.update_video(vid, analysis_state="完了", sha256="h",
                             meta={"has_video": True, "width": 320, "height": 180, "fps": 30,
                                   "bitrate": 1000, "vcodec": "mjpeg"})
        groups = {"h": [vid, 999]}
        rec = ai.recommend(self.db.video(vid), groups)
        self.assertEqual(rec["main"], "重複確認")
        self.assertEqual([e[0] for e in rec["extra"]], ["圧縮候補"])
        self.db.set_flag(vid, "protected", True)
        rec = ai.recommend(self.db.video(vid), groups)
        self.assertEqual(rec["main"], "保護対象")
        self.assertIn("重複確認", [e[0] for e in rec["extra"]])  # 重複情報は表示できる
        vid2 = self._add_video("b.mp4")
        self.assertEqual(ai.recommend(self.db.video(vid2))["main"], "内容確認")
        self.assertIn("解析情報が不足", ai.recommend(self.db.video(vid2))["reason"])


@unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
class MediaAndAITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import make_sample_videos
        cls._videos = tempfile.TemporaryDirectory()
        cls.video_root = Path(cls._videos.name) / "videos"
        make_sample_videos.create(cls.video_root, FFMPEG.ffmpeg)
        cls.before = snapshot(cls.video_root)

    @classmethod
    def tearDownClass(cls):
        cls._videos.cleanup()

    def setUp(self):
        self._data = tempfile.TemporaryDirectory()
        self.db = Database(self._data.name)
        scan_and_register(self.db, self.video_root)
        self.ids = {r["name"]: r["id"] for r in self.db.videos()}

    def tearDown(self):
        self.db.close()
        self._data.cleanup()
        self.assertEqual(snapshot(self.video_root), self.before, "元の動画が変更されました")

    def analyze_all(self):
        return ai.task_analyze(self.db.data_dir, FFMPEG, self.db.video_ids(), 20,
                               threading.Event(), lambda p: None)

    def label(self, mapping):
        g = {x["name"]: x["id"] for x in self.db.genres()}
        for name, genres in mapping.items():
            self.db.set_user_genres(self.ids[name], {g[x] for x in genres}, source="初期学習")
        return g

    def test_scan_registers_videos_in_subfolders(self):
        self.assertEqual(len(self.ids), 11)
        self.assertEqual(Path(self.db.video(self.ids["lecture_01.mp4"])["folder"]).name, "授業")

    def test_analysis_thumbnails_and_failure(self):
        result = self.analyze_all()
        self.assertEqual(result["失敗"], 1)  # broken.mp4
        broken = self.db.video(self.ids["broken.mp4"])
        self.assertEqual(broken["analysis_state"], "失敗")
        self.assertTrue(broken["analysis_error"])
        game = self.db.video(self.ids["game_01.mp4"])
        self.assertEqual(game["analysis_state"], "完了")
        thumbs = json.loads(game["thumbs"])
        self.assertEqual(len(thumbs), 3)
        self.assertTrue(all(os.path.exists(t) and str(self.db.thumb_dir) in t for t in thumbs))
        song = json.loads(self.db.video(self.ids["song_01.mp4"])["features"])
        self.assertGreater(song["video"]["black_ratio"], 0.9)
        self.assertIn("黒画面", [t["name"] for t in self.db.video_tags(self.ids["song_01.mp4"])])
        silent = self.db.video(self.ids["silent_gray.mp4"])
        self.assertEqual(silent["analysis_state"], "完了")  # 音声なしは失敗扱いにしない
        self.assertIsNone(json.loads(silent["features"])["audio"])
        # 解析失敗を理由に整理を勧めない
        self.assertEqual(ai.recommend(broken)["main"], "内容確認")

    def test_analysis_cancel_keeps_finished_results(self):
        cancel = threading.Event()
        count = [0]

        def report(p):
            count[0] += 1
            if count[0] == 3:
                cancel.set()
        result = ai.task_analyze(self.db.data_dir, FFMPEG, self.db.video_ids(), 20, cancel, report)
        self.assertTrue(result.get("cancelled"))
        states = [r["analysis_state"] for r in self.db.videos()]
        self.assertGreaterEqual(states.count("完了"), 1)
        self.assertNotIn("解析中", states)

    def test_file_changed_during_read_is_rejected(self):
        tmp = Path(self._data.name) / "copy.mp4"
        shutil.copyfile(self.video_root / "ゲーム/game_01.mp4", tmp)
        before = media.file_state(str(tmp))
        with open(tmp, "ab") as f:
            f.write(b"x")
        with self.assertRaises(media.FileChanged):
            media.ensure_unchanged(str(tmp), before)

    def test_cloud_only_files_are_not_read(self):
        vid = self.ids["game_01.mp4"]
        self.db.conn.execute("UPDATE videos SET cloud_only=1 WHERE id=?", (vid,))
        self.db.conn.commit()
        result = ai.task_analyze(self.db.data_dir, FFMPEG, [vid], 20, threading.Event(), lambda p: None)
        self.assertEqual(result["対象外"], 1)
        self.assertEqual(self.db.video(vid)["analysis_state"], "未解析")

    def test_duplicates_by_size_then_sha256(self):
        result = ai.task_find_duplicates(self.db.data_dir, threading.Event(), lambda p: None)
        self.assertEqual(result["groups"], 1)
        self.assertEqual(result["candidates"], 2)  # サイズが一致した2本だけハッシュ計算
        groups = ai.duplicate_groups(self.db)
        members = {self.db.video(v)["name"] for v in next(iter(groups.values()))}
        self.assertEqual(members, {"game_01.mp4", "game_01_copy.mp4"})
        self.assertIsNone(self.db.video(self.ids["song_01.mp4"])["sha256"])

    def test_duplicate_cancel(self):
        cancel = threading.Event()
        cancel.set()
        result = ai.task_find_duplicates(self.db.data_dir, cancel, lambda p: None)
        self.assertTrue(result["cancelled"])

    def test_diverse_selection(self):
        self.analyze_all()
        ai.task_find_duplicates(self.db.data_dir, threading.Event(), lambda p: None)
        chosen = ai.select_diverse(self.db, 5)
        self.assertEqual(len(chosen), 5)
        names = {self.db.video(v)["name"] for v in chosen}
        self.assertNotIn("broken.mp4", names)  # 解析できない動画は選ばない
        self.assertFalse({"game_01.mp4", "game_01_copy.mp4"} <= names)  # 重複は1本だけ

    def test_classification_unclassified_and_retrain(self):
        self.analyze_all()
        g = self.label({"song_01.mp4": ["音楽・歌唱"], "game_01.mp4": ["ゲーム"],
                        "camera_01.mp4": ["個人撮影"], "game_02.mp4": ["ゲーム", "配信・実況"],
                        "old_format.avi": ["その他・不明"]})
        r1 = ai.retrain(self.db)
        self.assertTrue(r1["adopted"])
        self.assertEqual(self.db.pending_training_count(), 0)
        v = lambda n: self.db.video(self.ids[n])  # noqa: E731
        self.assertEqual(v("song_02.mp4")["ai_state"], "分類済み")
        self.assertEqual([e["genre_id"] for e in json.loads(v("song_02.mp4")["ai_genres"])],
                         [g["音楽・歌唱"]])
        # 似た教師データがない講義動画は無理に分類しない
        self.assertEqual(v("lecture_01.mp4")["ai_state"], "未分類")
        self.assertIn("類似度が低い", v("lecture_01.mp4")["ai_reason"])
        self.assertEqual(v("broken.mp4")["ai_state"], "未分類")
        # 手動分類はAIで上書きしない
        self.assertEqual(self.db.user_genres(self.ids["game_01.mp4"]), {g["ゲーム"]})
        self.assertIsNone(v("game_01.mp4")["ai_genres"])
        # 未分類一覧で修正 → 再学習で反映
        self.db.set_user_genres(self.ids["lecture_01.mp4"], {g["授業・学習"]}, source="未分類一覧")
        self.assertEqual(self.db.pending_training_count(), 1)
        self.assertEqual(v("lecture_02.mp4")["ai_state"], "未分類")  # 自動再学習しない
        r2 = ai.retrain(self.db)
        self.assertTrue(r2["adopted"])
        self.assertEqual(r2["version"], 2)
        self.assertEqual([e["genre_id"] for e in json.loads(v("lecture_02.mp4")["ai_genres"])],
                         [g["授業・学習"]])
        self.assertEqual(len(self.db.models()), 2)

    def test_retrain_without_data_keeps_previous_model(self):
        r = ai.retrain(self.db)  # 未解析・教師データなし
        self.assertFalse(r["adopted"])
        self.assertIsNone(self.db.active_model())
        self.analyze_all()
        self.label({"song_01.mp4": ["音楽・歌唱"]})
        self.assertTrue(ai.retrain(self.db)["adopted"])
        self.db.set_user_genres(self.ids["song_01.mp4"], set())  # 教師データがなくなる
        r = ai.retrain(self.db)
        self.assertFalse(r["adopted"])
        self.assertEqual(self.db.active_model()["version"], 1)  # 以前のモデルを維持

    def test_only_other_genre_cannot_train(self):
        self.analyze_all()
        self.label({"song_01.mp4": ["その他・不明"]})
        r = ai.retrain(self.db)
        self.assertFalse(r["adopted"])
        self.assertIn("その他・不明", r["message"])

    def test_compress_to_separate_file(self):
        self.analyze_all()
        out = Path(self._data.name) / "out"
        out.mkdir()
        vid = self.ids["old_format.avi"]
        progress = []
        result = ai.task_compress(self.db.data_dir, FFMPEG, vid, str(out), "容量優先",
                                  threading.Event(), progress.append)
        self.assertTrue(os.path.exists(result["output"]))
        self.assertEqual(Path(result["output"]).parent, out)
        self.assertLess(result["output_size"], result["source_size"])
        self.assertFalse(list(out.glob("*.part")))
        self.assertTrue(progress)
        # 2回目は別名で出力し、既存ファイルを上書きしない
        result2 = ai.task_compress(self.db.data_dir, FFMPEG, vid, str(out), "容量優先",
                                   threading.Event(), lambda p: None)
        self.assertNotEqual(result["output"], result2["output"])

    def test_compress_protected_is_refused(self):
        vid = self.ids["game_01.mp4"]
        self.db.set_flag(vid, "protected", True)
        out = Path(self._data.name)
        with self.assertRaises(PermissionError):
            ai.task_compress(self.db.data_dir, FFMPEG, vid, str(out), "バランス",
                             threading.Event(), lambda p: None)
        self.assertFalse(list(out.glob("*_compressed_*")))

    def test_compress_cancel_removes_temp(self):
        out = Path(self._data.name) / "out"
        out.mkdir()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(media.Cancelled):
            ai.task_compress(self.db.data_dir, FFMPEG, self.ids["game_02.mp4"], str(out), "画質優先",
                             cancel, lambda p: None)
        self.assertEqual(list(out.iterdir()), [])

    def test_compress_rejects_same_path(self):
        src = str(self.video_root / "ゲーム/game_01.mp4")
        with self.assertRaises(ValueError):
            media.compress(FFMPEG, src, src, "バランス", 8, threading.Event(), lambda p: None)



class CurationAITest(unittest.TestCase):
    """ジャンル別厳選AI・全動画厳選AI・再確認待ち・学習リセット（FFmpeg 不要）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.video_root = base / "videos"
        self.video_root.mkdir()
        now = time.time()
        # 小さく古い動画と、大きく新しい動画（判断の傾向を学習できるように分ける）
        for i in range(5):
            small = self.video_root / f"small_{i}.mp4"
            small.write_bytes(b"s" * (1000 + i * 37))
            os.utime(small, (now - 400 * 86400, now - 400 * 86400))
            big = self.video_root / f"big_{i}.mp4"
            big.write_bytes(b"b" * (2_000_000 + i * 4001))
            os.utime(big, (now - 2 * 86400, now - 2 * 86400))
        self.before = snapshot(self.video_root)
        self.db = Database(base / "data")
        scan_and_register(self.db, self.video_root)
        self.ids = {r["name"]: r["id"] for r in self.db.videos()}
        g = {x["name"]: x["id"] for x in self.db.genres()}
        self.G1, self.G2, self.G3 = g["授業・学習"], g["ゲーム"], g["会議・仕事"]
        for name, vid in self.ids.items():
            self.db.set_user_genres(vid, {self.G1, self.G2}, source="修正")

    def tearDown(self):
        self.db.close()
        self.assertEqual(snapshot(self.video_root), self.before, "動画ファイルが変更されました")
        self._tmp.cleanup()

    def answer_all(self, scope, genre_id=None, small="整理候補", big="残す", count=4):
        for i in range(count):
            for prefix, label in (("small", small), ("big", big)):
                vid = self.ids[f"{prefix}_{i}.mp4"]
                if scope == "all":
                    self.db.answer(vid, "all", None, label, "all")
                else:
                    self.db.answer(vid, "genre", genre_id, label, "common")

    def rows(self):
        from storage_organizer.pages import build_video_rows  # 画面と同じ行データ
        return build_video_rows(self.db)

    def predict(self, name, mode, selected=None):
        rows = [r for r in self.rows() if r["name"] == name]
        return ai.predict_for_mode(self.db, rows, mode, selected or set())[self.ids[name]]

    def test_genre_and_all_training_data_do_not_mix(self):
        self.answer_all("genre", self.G1)
        self.assertEqual(self.db.effective_labels("all"), {})
        self.assertEqual(self.db.training_labels("all"), [])
        res = ai.retrain_curation(self.db)
        self.assertEqual(set(res), {curate_genre_scope(self.G1)})  # 全動画AIは学習しない
        self.assertEqual(self.predict("small_4.mp4", "all")["suggest"], ai.HOLD)  # ジャンル別AIを使わない
        self.answer_all("all", small="残す", big="整理候補")
        self.assertEqual(len(self.db.training_labels("genre", self.G1)), 8)  # 全動画の回答は混ざらない
        ai.retrain_curation(self.db)
        self.assertEqual(self.predict("small_4.mp4", "all")["suggest"], ai.KEEP)
        self.assertEqual(self.predict("small_4.mp4", "genre", {self.G1})["suggest"], ai.DROP)

    def test_or_selection_without_duplicates(self):
        self.db.set_user_genres(self.ids["small_0.mp4"], {self.G1}, source="修正")
        self.db.set_user_genres(self.ids["small_1.mp4"], {self.G3}, source="修正")
        rows = ai.select_curation_rows(self.rows(), {self.G1, self.G2}, include_ai=False)
        names = [r["name"] for r in rows]
        self.assertIn("small_0.mp4", names)       # どちらか一方に属していれば対象（OR）
        self.assertNotIn("small_1.mp4", names)    # どちらにも属していない
        self.assertEqual(len(names), len(set(names)))  # 両方に属していても1本
        self.assertEqual(len(names), 9)

    def test_conflicting_genres_are_held(self):
        self.answer_all("genre", self.G1, small="整理候補", big="残す")
        self.answer_all("genre", self.G2, small="残す", big="整理候補")
        ai.retrain_curation(self.db)
        p = self.predict("small_4.mp4", "genre", {self.G1, self.G2})
        self.assertEqual(p["suggest"], ai.HOLD)
        self.assertIn("分かれた", p["reason"])
        self.assertEqual({x[1] for x in p["per_genre"]}, {ai.KEEP, ai.DROP})  # 個別判定も表示
        self.assertEqual(self.predict("small_4.mp4", "genre", {self.G1})["suggest"], ai.DROP)
        self.assertEqual(ai.combine_genre_predictions([(ai.KEEP, ""), (ai.HOLD, "")])[0], ai.HOLD)
        self.assertEqual(ai.combine_genre_predictions([(ai.HOLD, ""), (ai.HOLD, "")])[0], ai.HOLD)
        self.assertEqual(ai.combine_genre_predictions([(ai.KEEP, ""), (ai.KEEP, "")])[0], ai.KEEP)
        self.assertEqual(ai.combine_genre_predictions([])[0], ai.HOLD)

    def test_insufficient_data_is_held(self):
        self.answer_all("all", count=1)  # 残す1件・整理候補1件
        res = ai.retrain_curation(self.db)
        self.assertEqual(res[CURATE_ALL_SCOPE]["status"], "データ不足")
        self.assertEqual(self.predict("small_4.mp4", "all")["suggest"], ai.HOLD)
        self.db.answer(self.ids["small_3.mp4"], "all", None, "保留", "all")  # 保留は学習に使わない
        self.assertEqual(len(self.db.training_labels("all")), 2)

    def test_correction_reflected_only_after_manual_retrain(self):
        self.answer_all("all")
        self.answer_all("genre", self.G1)
        ai.retrain_curation(self.db)
        self.assertEqual(self.predict("small_4.mp4", "all")["suggest"], ai.DROP)
        self.answer_all("all", small="残す", big="整理候補")  # 修正
        self.assertEqual(self.predict("small_4.mp4", "all")["suggest"], ai.DROP)  # 自動再学習しない
        res = ai.retrain_curation(self.db)
        self.assertEqual(res[CURATE_ALL_SCOPE]["status"], "更新")
        self.assertEqual(res[curate_genre_scope(self.G1)]["status"], "変更なし")  # 変更した範囲だけ
        self.assertEqual(self.predict("small_4.mp4", "all")["suggest"], ai.KEEP)

    def test_protected_video_is_not_curated(self):
        self.answer_all("all")
        ai.retrain_curation(self.db)
        vid = self.ids["small_4.mp4"]
        self.db.set_flag(vid, "protected", True)
        with self.assertRaises(PermissionError):
            self.db.answer(vid, "all", None, "整理候補", "all")
        p = self.predict("small_4.mp4", "all")
        self.assertEqual(p["suggest"], "—")
        self.assertEqual(ai.recommend(self.db.video(vid))["main"], "保護対象")

    def test_common_and_individual_answers(self):
        vid = self.ids["small_0.mp4"]
        self.db.answer(vid, "genre", self.G1, "残す", "common")
        self.db.answer(vid, "genre", self.G2, "残す", "common")
        self.db.answer(vid, "genre", self.G2, "整理候補", "individual")
        self.db.answer(vid, "genre", self.G1, "保留", "common")
        self.db.answer(vid, "genre", self.G2, "保留", "common")  # 個別回答を上書きしない
        eff = self.db.effective_labels("genre")
        self.assertEqual(eff[(vid, self.G1)]["label"], "保留")
        self.assertEqual((eff[(vid, self.G2)]["label"], eff[(vid, self.G2)]["source"]),
                         ("整理候補", "individual"))
        self.assertFalse(self.db.answer(vid, "genre", self.G2, "整理候補", "individual"))  # 重複登録しない
        history = self.db.label_history(vid)
        self.assertEqual(len(history), 4)  # 以前の回答は履歴として保持
        self.assertEqual(sum(h["active"] for h in history if h["genre_id"] == self.G2), 1)
        self.assertEqual(self.db.effective_labels("all"), {})  # 全動画AIには入らない

    def test_genre_change_marks_review_and_confirm_restores(self):
        self.answer_all("genre", self.G1)
        self.answer_all("all")
        vid = self.ids["small_0.mp4"]
        self.db.set_user_genres(vid, {self.G2, self.G3}, source="修正")  # 授業を外し会議を追加
        label = self.db.effective_labels("genre", self.G1)[vid]
        self.assertTrue(label["needs_review"])
        self.assertNotIn(vid, [lb["video_id"] for lb in self.db.training_labels("genre", self.G1)])
        self.assertEqual([r["video_id"] for r in self.db.review_queue()], [vid])
        self.assertEqual(self.db.effective_labels("genre", self.G3), {})  # 別ジャンルへ移さない
        self.assertFalse(self.db.effective_labels("all")[vid]["needs_review"])  # 全動画は影響なし
        self.db.confirm_label(label["id"])  # 確認して戻す
        self.assertIn(vid, [lb["video_id"] for lb in self.db.training_labels("genre", self.G1)])
        vid2 = self.ids["big_0.mp4"]
        self.db.set_user_genres(vid2, {self.G2}, source="修正")
        self.db.answer(vid2, "genre", self.G1, "整理候補", "common")  # 修正して戻す
        self.assertEqual(self.db.review_queue(), [])
        self.assertEqual(len([h for h in self.db.label_history(vid2) if h["scope"] == "genre"]), 2)

    def _train_everything(self):
        self.answer_all("genre", self.G1)
        self.answer_all("genre", self.G2, small="残す", big="整理候補")
        self.answer_all("all")
        ai.retrain_curation(self.db)
        path = self.db.model_dir / "dummy_classify.json"
        path.write_text("{}")
        self.db.add_model(self.db.next_model_version(), 1, str(path), {}, True, CLASSIFY_SCOPE)
        self.db.update_video(self.ids["small_4.mp4"], ai_state="分類済み", ai_genres=[])
        self.db.set_flag(self.ids["big_4.mp4"], "protected", True)
        self.db.set_flag(self.ids["big_3.mp4"], "favorite", True)

    def _state(self):
        return {"labels": [tuple(r) for r in self.db.conn.execute("SELECT * FROM curation_labels")],
                "genres": self.db.all_user_genres(),
                "flags": [tuple(r) for r in self.db.conn.execute("SELECT id, favorite, protected FROM videos")]}

    def test_reset_genre_ai_only(self):
        self._train_everything()
        before = self._state()
        plan = ai.reset_plan(self.db, [curate_genre_scope(self.G1)])
        self.assertTrue(plan["reset_models"] and plan["kept_labels"])
        self.assertEqual(plan["cleared_predictions"], [])
        self.assertEqual(len(plan["untouched"]), 3)
        ai.reset_models(self.db, [curate_genre_scope(self.G1)])
        active = self.db.active_models()
        self.assertNotIn(curate_genre_scope(self.G1), active)
        self.assertTrue({curate_genre_scope(self.G2), CURATE_ALL_SCOPE, CLASSIFY_SCOPE} <= set(active))
        self.assertEqual(self._state(), before)  # 教師データ・履歴・ジャンル・お気に入り・保護は不変
        self.assertEqual(self.db.video(self.ids["small_4.mp4"])["ai_state"], "分類済み")
        self.assertIn("未学習", self.predict("small_4.mp4", "genre", {self.G1})["reason"])
        res = ai.retrain_curation(self.db, [curate_genre_scope(self.G1)])  # 手動で再学習できる
        self.assertEqual(res[curate_genre_scope(self.G1)]["status"], "更新")

    def test_reset_all_ai_only(self):
        self._train_everything()
        before = self._state()
        ai.reset_models(self.db, [CURATE_ALL_SCOPE])
        active = self.db.active_models()
        self.assertNotIn(CURATE_ALL_SCOPE, active)
        self.assertTrue({curate_genre_scope(self.G1), curate_genre_scope(self.G2), CLASSIFY_SCOPE} <= set(active))
        self.assertEqual(self._state(), before)
        self.assertEqual(self.predict("small_4.mp4", "all")["suggest"], ai.HOLD)
        self.assertEqual(self.predict("small_4.mp4", "genre", {self.G1})["suggest"], ai.DROP)

    def test_classify_reset_only_when_explicit(self):
        self._train_everything()
        ai.reset_models(self.db, [CURATE_ALL_SCOPE, curate_genre_scope(self.G1)])
        self.assertIn(CLASSIFY_SCOPE, self.db.active_models())
        plan = ai.reset_plan(self.db, [CLASSIFY_SCOPE])
        self.assertTrue(plan["cleared_predictions"])
        genres_before = self.db.all_user_genres()
        ai.reset_models(self.db, [CLASSIFY_SCOPE])
        self.assertNotIn(CLASSIFY_SCOPE, self.db.active_models())
        self.assertEqual(self.db.video(self.ids["small_4.mp4"])["ai_state"], "未判定")
        self.assertEqual(self.db.all_user_genres(), genres_before)  # 手動分類は保持
        self.assertIn(curate_genre_scope(self.G2), self.db.active_models())

    def test_persistence_after_restart(self):
        self._train_everything()
        vid = self.ids["small_0.mp4"]
        self.db.answer(vid, "genre", self.G2, "保留", "individual")
        data_dir = self.db.data_dir
        self.db.close()
        self.db = Database(data_dir)
        self.assertEqual(self.db.effective_labels("genre")[(vid, self.G2)]["source"], "individual")
        self.assertEqual(len(self.db.training_labels("all")), 8)
        self.assertEqual(set(ai.load_curation_models(self.db)),
                         {CURATE_ALL_SCOPE, curate_genre_scope(self.G1), curate_genre_scope(self.G2)})

    def test_old_curation_column_is_migrated(self):
        vid = self.ids["small_0.mp4"]
        self.db.conn.execute("UPDATE videos SET curation='整理候補' WHERE id=?", (vid,))
        self.db.conn.execute("DELETE FROM settings WHERE key='curation_migrated'")
        self.db.conn.commit()
        data_dir = self.db.data_dir
        self.db.close()
        self.db = Database(data_dir)
        self.assertEqual(self.db.effective_labels("all")[vid]["label"], "整理候補")
        self.db.close()
        self.db = Database(data_dir)  # 2回目は移行しない
        self.assertEqual(len(self.db.label_history(vid)), 1)


if __name__ == "__main__":
    unittest.main()
