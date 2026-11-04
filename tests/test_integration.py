"""統合版（録画整理に特化・データベース不使用・機械学習なし）で追加・変更した仕様のテスト。

実際のユーザーの録画は使わない。一時フォルダの合成ダミーファイルだけを使う。
ごみ箱への移動は偽の移動処理（backend）で確かめる（本物のごみ箱は tools/recycle_bin_live_test.py で確認）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage_organizer import autoscan, candidates, media, store, trash  # noqa: E402
from storage_organizer.store import Store, StoreError  # noqa: E402

PKG = ROOT / "storage_organizer"


class NoDatabaseNoMLTest(unittest.TestCase):
    """データベース・機械学習のコードがアプリに含まれていないこと。"""

    DB_PATTERNS = r"\b(import\s+sqlite3|from\s+sqlite3|sqlalchemy|pymysql|psycopg|mysql\.connector|peewee|" \
                  r"pony\.orm|tinydb|shelve|dbm)\b"
    ML_PATTERNS = r"\b(sklearn|torch|tensorflow|keras|lightgbm|xgboost|onnx)\b"

    def sources(self):
        return {p: p.read_text(encoding="utf-8") for p in PKG.rglob("*.py")}

    def test_no_database_code(self):
        for path, text in self.sources().items():
            self.assertIsNone(re.search(self.DB_PATTERNS, text), f"{path.name} にデータベースの処理があります")
        for name in ("legacy_db.py", "migrate.py", "ai.py", "pages.py"):
            self.assertFalse((PKG / name).exists(), name)
        req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        for word in ("sqlalchemy", "pymysql", "psycopg"):
            self.assertNotIn(word, req)

    def test_no_machine_learning_code(self):
        for path, text in self.sources().items():
            self.assertIsNone(re.search(self.ML_PATTERNS, text), f"{path.name} に機械学習の処理があります")

    def test_running_app_does_not_load_sqlite(self):
        code = ("import sys, tempfile; from storage_organizer.store import Store; from storage_organizer import "
                "autoscan, candidates, trash, media; s = Store(tempfile.mkdtemp()); s.close(); "
                "print(int('sqlite3' in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "0", out.stderr)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.data = self.dir / "data"
        self.files = self.dir / "videos"
        self.files.mkdir()
        self.db = Store(self.data)
        self.ids = [self.add(f"rec{i}.mp4", 1000 + i) for i in range(3)]

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def add(self, name, size):
        path = self.files / name
        path.write_bytes(b"x" * size)
        st = path.stat()
        self.db.upsert_media([(str(path), name, str(self.files), size, st.st_mtime, ".mp4", "video", False)], 1,
                             str(self.files))
        return self.db.id_of(str(path))

    def reopen(self):
        self.db.close()
        self.db = Store(self.data)
        for name in ("rec0.mp4", "rec1.mp4", "rec2.mp4"):  # 一覧を作り直す（調査の代わり）
            p = self.files / name
            st = p.stat()
            self.db.upsert_media([(str(p), name, str(self.files), st.st_size, st.st_mtime, ".mp4", "video", False)],
                                 1, str(self.files))
        return [self.db.id_of(str(self.files / f"rec{i}.mp4")) for i in range(3)]

    def test_default_data_dir_is_separate_from_old_versions(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.dir), "RECSORT_DATA_DIR": ""}):
            d = store.default_data_dir()
        self.assertEqual(d.name, "RecSortV1")
        self.assertNotIn(d.name, ("RecSort", "RecSortAI"))

    def test_favorite_and_protection_are_separate_and_persist(self):
        a, b, c = self.ids
        self.db.set_decision(a, "削除候補")
        self.db.set_favorite(a, True)
        self.assertTrue(self.db.video(a)["favorite"])
        self.assertIsNone(self.db.video(a)["decision"])  # お気に入りにすると削除候補から外れる
        self.assertFalse(self.db.video(a)["protected"])  # 保護とは別の状態
        self.db.set_protected(b, True)
        with self.assertRaises(PermissionError):
            self.db.set_decision(b, "削除候補")
        self.db.set_decision(c, "保留")
        a, b, c = self.reopen()  # 再起動後も保持
        self.assertTrue(self.db.video(a)["favorite"])
        self.assertTrue(self.db.video(b)["protected"])
        self.assertEqual(self.db.video(c)["decision"], "保留")
        self.db.set_favorite(a, False)  # お気に入りを外すと整理の対象に戻る
        self.assertFalse(self.db.video(a)["favorite"])

    def test_bulk_decisions_write_once_and_skip_protected(self):
        a, b, c = self.ids
        self.db.set_protected(b, True)
        writes = []
        real = store._write_json
        with mock.patch.object(store, "_write_json", side_effect=lambda p, d: (writes.append(p.name), real(p, d))):
            refused = self.db.set_decisions([a, b, c], "削除候補")
        self.assertEqual(refused, [b])
        self.assertEqual(writes, ["user_state.json"])  # 件数にかかわらず1回だけ書く
        self.assertEqual([self.db.video(i)["decision"] for i in (a, b, c)], ["削除候補", None, "削除候補"])

    def test_viewed_time_is_batched_and_flushed_on_close(self):
        writes = []
        real = store._write_json
        with mock.patch.object(store, "_write_json", side_effect=lambda p, d: (writes.append(p.name), real(p, d))):
            self.db.mark_viewed(self.ids[0])
            self.db.mark_viewed(self.ids[1])
            self.assertNotIn("user_state.json", writes)  # 選ぶたびに全体を書き直さない
            self.db.close()
        self.assertIn("user_state.json", writes)
        state = json.loads((self.data / "user_state.json").read_text(encoding="utf-8"))
        self.assertEqual(len([e for e in state.values() if "last_viewed" in e]), 2)
        self.db = Store(self.data)

    def test_write_is_verified_and_failure_keeps_previous_file(self):
        a = self.ids[0]
        self.db.set_decision(a, "残す")
        before = (self.data / "user_state.json").read_bytes()
        with mock.patch.object(store.json, "load", side_effect=ValueError("broken")):
            with self.assertRaises(StoreError):
                self.db.set_decision(a, "保留")
        self.assertEqual((self.data / "user_state.json").read_bytes(), before)  # 元のファイルは変わらない
        self.assertEqual(self.db.video(a)["decision"], "残す")  # 画面上の状態も変えない

    def test_disk_error_is_reported_not_hidden(self):
        with mock.patch.object(store.os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(StoreError):
                self.db.set_decision(self.ids[0], "保留")
        self.assertIsNone(self.db.video(self.ids[0])["decision"])

    def test_corrupted_user_state_is_recovered_from_backup(self):
        a = self.ids[0]
        self.db.set_protected(a, True)
        self.db.set_decision(self.ids[1], "保留")  # .bak には保護が入った状態が残る
        self.db.close()
        (self.data / "user_state.json").write_text("{壊れた", encoding="utf-8")
        self.db = Store(self.data)
        self.assertTrue(self.db.protection_ok)
        self.assertTrue(any("復旧しました" in p for p in self.db.load_problems))
        self.assertTrue(list(self.data.glob("user_state.json.broken-*")))  # 壊れたファイルは消さずに退避
        a = self.reopen()[0]
        self.assertTrue(self.db.video(a)["protected"])

    def test_unrecoverable_protection_blocks_organizing(self):
        self.db.set_protected(self.ids[0], True)
        self.db.close()
        for name in ("user_state.json", "user_state.json.bak"):
            if (self.data / name).exists():
                (self.data / name).write_text("{壊れた", encoding="utf-8")
        self.db = Store(self.data)
        self.assertFalse(self.db.protection_ok)
        ids = self.reopen()
        self.assertFalse(self.db.protection_ok)  # 再起動しても整理禁止が続く
        results = trash.move_to_trash(self.db, ids, lambda p: self.fail("移動してはいけません"))
        self.assertTrue(all(not r.ok for r in results))

    def test_newer_schema_is_not_modified(self):
        self.db.set_setting("schema_version", store.SCHEMA_VERSION + 1)
        self.db.close()
        self.db = Store(self.data)
        self.assertFalse(self.db.protection_ok)
        with self.assertRaises(StoreError):
            self.db.set_decision(self.reopen()[0], "保留")
        self.assertEqual(self.db.get_setting("schema_version"), store.SCHEMA_VERSION + 1)


class CandidateRulesTest(unittest.TestCase):
    """機械学習を使わない、客観的な条件による整理候補。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = Store(self.dir / "data")
        self.ids = {}

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def add(self, name, size, sha=None, failure=None, duration=600):
        path = self.dir / name
        path.write_bytes(b"")
        self.db.upsert_media([(str(path), name, str(self.dir), size, time.time() - 86400, ".mp4", "video", False)],
                             1, str(self.dir))
        mid = self.db.id_of(str(path))
        self.db.update_video(mid, sha256=sha, meta={"duration": duration, "has_video": True},
                             failure=[failure] if failure else [])
        self.ids[name] = mid
        return mid

    def plan(self, target=None):
        return candidates.plan_recordings(candidates.Context(self.db), target)

    def test_objective_problems_are_shown_without_target_and_large_alone_is_not(self):
        self.add("short.mp4", 10_000, failure={"kind": "short", "reason": "再生時間が 1.0 秒と極端に短い録画です"})
        self.add("dark.mp4", 20_000, failure={"kind": "dark", "reason": "確認した3場面がほぼ真っ暗です"})
        self.add("fail.mp4", 30_000, failure={"kind": "read_error", "reason": "読み込めませんでした。破損とは限りません"})
        self.add("huge.mp4", 50 * 1024 ** 3)  # 大容量だけ
        p = self.plan()
        names = {self.db.video(it["id"])["name"]: it for it in p["items"]}
        self.assertEqual(set(names), {"short.mp4", "dark.mp4", "fail.mp4"})
        self.assertTrue(all(it["kind"] == "review" for it in names.values()))  # 内容を見てから選ぶ候補
        reasons = " ".join(r for it in p["items"] for r in it["reasons"])
        self.assertIn("極端に短い録画", reasons)
        self.assertIn("黒画面が多い", reasons)
        self.assertIn("読み込み失敗", reasons)
        self.assertNotIn("不要", reasons)
        huge = candidates.Context(self.db).reasons(self.db.video(self.ids["huge.mp4"]))
        self.assertIn("容量が大きいだけでは削除をおすすめしません", " ".join(huge))

    def test_corrupt_is_not_asserted_as_broken(self):
        self.assertNotIn("破損を確認", candidates.FAILURE_LABEL.values())
        self.assertEqual(candidates.FAILURE_LABEL["corrupt"], "再生できない可能性")

    def test_favorite_and_protected_are_excluded_and_kept(self):
        a = self.add("a.mp4", 1000, sha="h")
        b = self.add("b.mp4", 1000, sha="h")
        c = self.add("c.mp4", 1000, sha="h")
        self.db.set_favorite(b, True)
        p = self.plan()
        ids = {it["id"] for it in p["items"]}
        self.assertNotIn(b, ids)  # お気に入りはおすすめに含めない
        self.assertEqual(ids, {a, c})
        self.assertTrue(all(it["keep_id"] == b for it in p["items"]))  # お気に入りを残す1本にする
        self.assertEqual(p["excluded"]["お気に入り"], 1)
        f = self.add("failed.mp4", 500, failure={"kind": "dark", "reason": "暗い"})
        self.db.set_favorite(f, True)
        self.db.set_protected(a, True)
        ctx = candidates.Context(self.db)
        self.assertFalse(ctx.is_suggestable(ctx.rows[f]))
        ids = {it["id"] for it in self.plan()["items"]}
        self.assertNotIn(f, ids)
        self.assertNotIn(a, ids)
        self.db.set_favorite(f, False)  # お気に入りを外すと対象に戻る
        self.assertIn(f, {it["id"] for it in self.plan()["items"]})

    def test_duplicate_always_keeps_one(self):
        ids = [self.add(f"d{i}.mp4", 1000, sha="same") for i in range(3)]
        p = self.plan(10 ** 12)
        chosen = {it["id"] for it in p["items"]}
        self.assertEqual(len(chosen & set(ids)), 2)
        self.assertTrue(candidates.protect_group_rule(candidates.Context(self.db), set(ids)))

    def test_short_reasons_for_list(self):
        mid = self.add("s.mp4", 200 * 1024 * 1024, failure={"kind": "silent", "reason": "無音"})
        ctx = candidates.Context(self.db)
        self.assertEqual(ctx.short_reasons(ctx.rows[mid]), ["無音", "大容量"])


class SilenceTest(unittest.TestCase):
    TOOLS = media.FFmpegTools("ffmpeg", "ffprobe")

    def test_parse_volumedetect(self):
        self.assertEqual(media.parse_max_volume("[Parsed_volumedetect_0] max_volume: -61.3 dB"), -61.3)
        self.assertEqual(media.parse_max_volume("max_volume: -inf dB"), float("-inf"))
        self.assertIsNone(media.parse_max_volume("no audio"))

    def check(self, values, info=None):
        info = info or {"duration": 120.0, "has_audio": True}
        with mock.patch.object(media, "measure_max_volume", return_value=values):
            return candidates.silence_failure(self.TOOLS, "x.mp4", info)

    def test_all_sections_silent(self):
        f = self.check([-70.0, float("-inf"), -55.0])
        self.assertEqual(f["kind"], "silent")
        self.assertIn("可能性", f["reason"])

    def test_not_silent_or_not_measured(self):
        self.assertIsNone(self.check([-70.0, -20.0, -60.0]))  # 1か所でも音がある
        self.assertIsNone(self.check([-70.0]))  # 測れなかった区間がある → 判定しない
        self.assertIsNone(self.check([-70.0] * 3, {"duration": 120.0, "has_audio": False}))  # 音声なしは対象外
        self.assertIsNone(self.check([-70.0] * 3, {"duration": 0, "has_audio": True}))  # 長さ不明は判定しない
        with mock.patch.object(media, "measure_max_volume", side_effect=TimeoutError("遅い")):
            self.assertIsNone(candidates.silence_failure(self.TOOLS, "x.mp4", {"duration": 60, "has_audio": True}))


class FFmpegDetectionTest(unittest.TestCase):
    def make_pair(self, folder: Path, both=True):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / media._exe("ffmpeg")).write_bytes(b"")
        if both:
            (folder / media._exe("ffprobe")).write_bytes(b"")

    def test_winget_packages_are_found_without_installing(self):
        # Windows の動作を確かめるため、テスト用のファイルを作る前から Windows として扱う
        # （Linux などで実行しても ffmpeg.exe / ffprobe.exe の名前で作られるようにする）
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media.sys, "platform", "win32"):
            bin_dir = Path(tmp) / "Microsoft" / "WinGet" / "Packages" / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe" \
                / "ffmpeg-7.1-full_build" / "bin"
            self.make_pair(bin_dir)
            self.assertTrue((bin_dir / "ffmpeg.exe").is_file())
            dirs = media.winget_ffmpeg_dirs(tmp)
            self.assertIn(str(bin_dir), dirs)
            with mock.patch.object(media.shutil, "which", return_value=None), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                    mock.patch.object(media.subprocess, "Popen", side_effect=AssertionError("実行しない")):
                tools = media.find_ffmpeg()
            self.assertTrue(tools.available)
            self.assertEqual(Path(tools.ffmpeg).parent, bin_dir)
            self.assertIn("WinGet", tools.source)

    def test_pair_must_come_from_same_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_pair(Path(tmp) / "only_ffmpeg", both=False)
            self.assertIsNone(media._pair_in(Path(tmp) / "only_ffmpeg"))
            with mock.patch.object(media.shutil, "which", return_value=None), \
                    mock.patch.dict(os.environ, {"LOCALAPPDATA": str(Path(tmp) / "none")}):
                self.assertFalse(media.find_ffmpeg(str(Path(tmp) / "only_ffmpeg")).available)

    def test_custom_folder_has_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_pair(Path(tmp))
            tools = media.find_ffmpeg(tmp)
            self.assertEqual(tools.source, "設定で指定したフォルダ")


class ScanScopeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.videos = self.base / "Videos"
        self.videos.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def run_scan(self, folders):
        with mock.patch.object(autoscan, "known_folders", lambda: folders), \
                mock.patch.object(autoscan, "local_drives", lambda: []):
            autoscan.task_autoscan(self.base / "data", media.FFmpegTools(None, None), autoscan.Controller(),
                                   lambda p: None)
        return Store(self.base / "data")

    def test_only_recordings_are_listed_but_all_sizes_are_counted(self):
        (self.videos / "rec.mp4").write_bytes(b"\x00" * 3000)
        (self.videos / "game.mts").write_bytes(b"\x00" * 1000)
        (self.videos / "photo.jpg").write_bytes(b"\xff\xd8" + b"i" * 5000)
        (self.videos / "notes.txt").write_bytes(b"t" * 100)
        db = self.run_scan([("ビデオ", str(self.videos))])
        try:
            self.assertEqual({r["name"] for r in db.media()}, {"rec.mp4", "game.mts"})
            cats = db.location_stats()[str(self.videos)]["by_category"]
            self.assertEqual(cats["画像"][1], 5002)  # 画像は容量の集計にだけ数える
        finally:
            db.close()

    def test_system_folders_come_from_known_folder_api(self):
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.base / "la")}):
            found = autoscan.system_folders()
        self.assertIn(autoscan.norm(str(self.base / "la")), found)
        if sys.platform == "win32":
            self.assertIn(autoscan.norm(os.environ["SystemRoot"]), found)

    @unittest.skipUnless(sys.platform == "win32", "ジャンクションは Windows のみ")
    def test_junction_is_not_followed_or_double_counted(self):
        (self.videos / "real").mkdir()
        (self.videos / "real" / "rec.mp4").write_bytes(b"\x00" * 2000)
        link = self.videos / "junction_to_real"
        loop = self.videos / "real" / "loop_to_parent"
        for target, name in ((self.videos / "real", link), (self.videos, loop)):
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(name), str(target)], capture_output=True)
            if r.returncode != 0:
                self.skipTest("ジャンクションを作成できません")
        db = self.run_scan([("ビデオ", str(self.videos))])
        try:
            paths = [r["path"] for r in db.media()]
            self.assertEqual(paths, [str(self.videos / "real" / "rec.mp4")])  # 1回だけ・循環しない
        finally:
            db.close()
            for name in (link, loop):
                os.rmdir(name)  # ジャンクション（テストで作ったリンク）だけを外す。中身には触れない

    def test_many_files_and_cancel(self):
        for i in range(3000):
            sub = self.videos / f"d{i // 300}"
            sub.mkdir(exist_ok=True)
            (sub / f"rec{i}.mp4").write_bytes(b"\x00" * (i % 7 + 1))
        t0 = time.monotonic()
        db = self.run_scan([("ビデオ", str(self.videos))])
        try:
            self.assertEqual(len(db.media()), 3000)
            self.assertLess(time.monotonic() - t0, 60)
        finally:
            db.close()
        ctl = autoscan.Controller()
        ctl.cancel()
        with mock.patch.object(autoscan, "known_folders", lambda: [("ビデオ", str(self.videos))]), \
                mock.patch.object(autoscan, "local_drives", lambda: []):
            result = autoscan.task_autoscan(self.base / "data", media.FFmpegTools(None, None), ctl, lambda p: None)
        self.assertTrue(result["cancelled"])


class TrashSafetyTest(unittest.TestCase):
    """ごみ箱への移動の安全確認（偽の移動処理を使う。実際には削除しない）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = Store(self.dir / "data")
        self.moved = []

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def backend(self, path):
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def add(self, name, data=b"x" * 100):
        p = self.dir / name
        p.write_bytes(data)
        st = p.stat()
        self.db.upsert_media([(str(p), name, str(self.dir), st.st_size, st.st_mtime, ".mp4", "video", False)], 1,
                             str(self.dir))
        return self.db.id_of(str(p))

    def test_protected_changed_and_failing_files_are_not_deleted(self):
        ok = self.add("ok.mp4")
        prot = self.add("prot.mp4")
        changed = self.add("changed.mp4")
        fail = self.add("fail.mp4")
        self.db.set_protected(prot, True)
        time.sleep(0.01)
        (self.dir / "changed.mp4").write_bytes(b"y" * 300)  # スキャン後に変更

        def flaky(path):
            if path.endswith("fail.mp4"):
                raise trash.TrashError("ごみ箱へ移動できませんでした（テスト）")
            self.backend(path)
        with mock.patch.object(os, "remove", side_effect=AssertionError("完全削除")), \
                mock.patch.object(os, "unlink", side_effect=AssertionError("完全削除")):
            results = {r.media_id: r.status for r in trash.move_to_trash(self.db, [ok, prot, changed, fail], flaky)}
        self.assertEqual(results, {ok: "moved", prot: "skipped", changed: "skipped", fail: "failed"})
        self.assertTrue((self.dir / "prot.mp4").exists())
        self.assertTrue((self.dir / "changed.mp4").exists())
        self.assertTrue((self.dir / "fail.mp4").exists())  # 失敗しても完全削除に切り替えない
        log = [e["result"] for e in self.db.trash_log()]
        self.assertIn("error", log)

    def test_default_backend_never_falls_back(self):
        backend = trash.default_backend()
        src = Path(trash.__file__).read_text(encoding="utf-8") + (PKG / "recycle_bin.py").read_text(encoding="utf-8")
        for word in ("os.remove(", "os.unlink(", "shutil.rmtree", "SHFileOperation", "FOF_WANTNUKEWARNING",
                     "send2trash"):
            self.assertNotIn(word, src)
        self.assertTrue(callable(backend))


if __name__ == "__main__":
    unittest.main()
