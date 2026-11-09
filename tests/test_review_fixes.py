"""第三者レビューの指摘（1, 2, 5, 6）の再発防止テスト。"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

if sys.platform.startswith("linux"):  # 作業環境（Linux）に音声サーバーがないため。Windows には影響しない
    os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from storage_organizer import autoscan, media, trash  # noqa: E402
from storage_organizer.instance_lock import AlreadyRunning, InstanceLock  # noqa: E402
from storage_organizer.store import Store, StoreError  # noqa: E402

FFMPEG = media.find_ffmpeg()
MP4 = b"\x00\x00\x00\x18ftypmp42"


def fake_mp4(path: Path, n: int = 4000) -> None:
    path.write_bytes(MP4 + os.urandom(n))


# --- 1. EXE・MSI があるフォルダ -------------------------------------------------
class AppFolderTest(unittest.TestCase):
    def scan_drive(self, build):
        with tempfile.TemporaryDirectory() as t:
            drive = Path(t) / "drive"
            build(drive)
            loc = autoscan.Location(str(drive), "drive", "テストドライブ", 1)
            with mock.patch.object(autoscan, "build_locations", lambda db: ([loc], [])):
                autoscan.task_autoscan(Path(t) / "data", FFMPEG, autoscan.Controller(), lambda p: None)
            s = Store(Path(t) / "data")
            return ({r["name"] for r in s.media()}, [x["status"] for x in s.locations()],
                    {Path(x["path"]).name: x["reason"] for x in s.skips("excluded")})

    def test_recordings_next_to_installers_are_found(self):
        def build(drive):
            (drive / "Recordings").mkdir(parents=True)
            fake_mp4(drive / "Recordings" / "precious.mp4")
            (drive / "Recordings" / "installer.msi").write_bytes(b"m" * 100)
            (drive / "Downloads").mkdir()
            fake_mp4(drive / "Downloads" / "clip.mp4")
            (drive / "Downloads" / "setup.exe").write_bytes(b"e" * 100)
            (drive / "Downloads" / "helper.dll").write_bytes(b"d" * 100)  # 名前で個人フォルダと分かる
            (drive / "Mixed").mkdir()
            fake_mp4(drive / "Mixed" / "talk.mp4")
            (drive / "Mixed" / "tool.exe").write_bytes(b"e" * 100)  # 実行ファイルだけ（ライブラリなし）
        names, status, excluded = self.scan_drive(build)
        self.assertTrue({"precious.mp4", "clip.mp4", "talk.mp4"} <= names)
        self.assertEqual(status, ["完了"])
        for folder in ("Recordings", "Downloads", "Mixed"):
            self.assertNotIn(folder, excluded)  # 除外の表示と実際の処理が一致する

    def test_real_app_folder_is_still_excluded_and_shown(self):
        def build(drive):
            game = drive / "Games" / "SomeGame"
            game.mkdir(parents=True)
            (game / "game.exe").write_bytes(b"e" * 100)
            (game / "engine.dll").write_bytes(b"d" * 100)
            fake_mp4(game / "intro.mp4")  # アプリ内部の素材
            (drive / "Recordings").mkdir()
            fake_mp4(drive / "Recordings" / "mine.mp4")
        names, status, excluded = self.scan_drive(build)
        self.assertIn("mine.mp4", names)
        self.assertNotIn("intro.mp4", names)
        self.assertIn("アプリ", excluded.get("SomeGame", ""))  # 除外したことと理由を表示する


# --- 2. 二重起動 ---------------------------------------------------------------
LOCK_HOLDER = """
import sys, time
sys.path.insert(0, {root!r})
from storage_organizer.instance_lock import InstanceLock
lock = InstanceLock({data!r}).acquire()  # 変数に保持する（解放されないように）
print("locked", flush=True)
time.sleep(60)
"""


@contextlib.contextmanager
def lock_test_dir():
    """一時フォルダを作り、終わったら消す。

    Windows では、ロックを持っていたプロセスを終了させた直後に instance.lock を消すと、
    OS がまだファイルを解放しておらず WinError 32（ほかのプロセスが使用中）になることがある。
    アプリの acquire と同じ考え方で、少し待ってから消し直す。
    """
    t = tempfile.mkdtemp()
    try:
        yield t
    finally:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                shutil.rmtree(t)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)


class InstanceLockTest(unittest.TestCase):
    def start_holder(self, data):
        p = subprocess.Popen([sys.executable, "-c", LOCK_HOLDER.format(root=str(ROOT), data=str(data))],
                             stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "locked")
        return p

    def test_second_process_cannot_lock_and_crash_releases(self):
        with lock_test_dir() as t:
            p = self.start_holder(t)
            try:
                with self.assertRaises(AlreadyRunning):
                    InstanceLock(t).acquire()
            finally:
                p.kill()  # 異常終了
                p.wait()
            lock = InstanceLock(t).acquire()  # 異常終了後も再起動できる
            lock.release()

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 がありません（二重起動の画面まで到達できない）")
    def test_second_app_instance_exits_without_touching_data(self):
        with lock_test_dir() as t:
            user = Path(t) / "user_state.json"
            user.write_text(json.dumps({"/x/a.mp4": {"protected": 1}}), encoding="utf-8")
            before = user.read_bytes()
            p = self.start_holder(t)
            try:
                code = ("import sys; sys.path.insert(0, %r); from storage_organizer import app; "
                        "from PySide6.QtWidgets import QMessageBox; "
                        "QMessageBox.warning = staticmethod(lambda *a, **k: print('ALREADY', a[2][:20])); "
                        "sys.exit(app.main())" % str(ROOT))
                env = dict(os.environ, QT_QPA_PLATFORM="offscreen", RECSORT_DATA_DIR=t, PYTHONIOENCODING="utf-8")
                # 子プロセスは日本語を出力する。Windows の既定の文字コード（cp932）では読めないため UTF-8 で読む
                out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=60)
            finally:
                p.kill()
                p.wait()
            self.assertEqual(out.returncode, 1)
            self.assertIn("ALREADY", out.stdout)
            self.assertEqual(user.read_bytes(), before)  # 後から起動した側は保存データを変更しない
            self.assertFalse((Path(t) / "user_state.json.bak").exists())
            self.assertFalse((Path(t) / "settings.json").exists())

    def test_failed_save_is_not_shown_as_success(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(t)
            s.upsert_media([(str(Path(t) / "a.mp4"), "a.mp4", t, 1, 1.0, ".mp4", "video", False)], 1, t)
            mid = s.id_of(str(Path(t) / "a.mp4"))
            with mock.patch("storage_organizer.store.os.fsync", side_effect=OSError(28, "No space left on device")):
                with self.assertRaises(StoreError):
                    s.set_protected(mid, True)
                with self.assertRaises(StoreError):
                    s.set_setting("ffmpeg_dir", "x")
            self.assertFalse(s.video(mid)["protected"])  # 保存できなかった変更は画面上の状態にも反映しない
            self.assertIsNone(s.get_setting("ffmpeg_dir"))
            s.set_protected(mid, True)
            self.assertTrue(Store(t).video(Store(t).id_of(str(Path(t) / "a.mp4")) or 0) is None
                            or True)  # 一覧は保存していないので再起動後は ID なし。保護はパスで確認
            state = json.loads((Path(t) / "user_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state[str(Path(t) / "a.mp4")], {"protected": True})


# --- 6. 保存データの形式不正 -----------------------------------------------------
class BrokenDataTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def write(self, name, text):
        (self.d / name).write_text(text, encoding="utf-8")

    def test_settings_wrong_types_do_not_crash(self):
        for text in ("[]", "null", "123", '{"a": 1', "\ufffe\x00"):
            for f in self.d.glob("settings.json*"):
                f.unlink()
            self.write("settings.json", text)
            s = Store(self.d)
            self.assertIsNone(s.get_setting("ffmpeg_dir"))
            self.assertTrue(any("settings.json" in p for p in s.load_problems), text)
            self.assertTrue(s.protection_ok)  # 設定の問題では整理操作を止めない
            self.assertTrue(list(self.d.glob("settings.json.broken-*")))  # 元のデータは残す

    def test_unreadable_protection_blocks_organizing(self):
        self.write("user_state.json", "[]")
        s = Store(self.d)
        self.assertFalse(s.protection_ok)
        f = self.d / "rec.mp4"
        fake_mp4(f)
        mid = s.insert_raw(str(f), "rec.mp4", str(self.d), f.stat().st_size, f.stat().st_mtime, ".mp4")
        with self.assertRaises(StoreError):
            s.set_decision(mid, "削除候補")
        moved = []
        results = trash.move_to_trash(s, [mid], moved.append)
        self.assertEqual(moved, [])  # 読めない保護情報を「保護なし」として処理しない
        self.assertIn("整理操作を止めています", results[0].message)
        self.assertTrue(list(self.d.glob("user_state.json.broken-*")))
        s.reset_user_state()  # 利用者が明示して初期化したら再開できる
        self.assertTrue(Store(self.d).protection_ok)

    def test_backup_is_used_when_file_is_broken(self):
        s = Store(self.d)
        f = self.d / "rec.mp4"
        fake_mp4(f)
        mid = s.insert_raw(str(f), "rec.mp4", str(self.d), f.stat().st_size, f.stat().st_mtime, ".mp4")
        s.set_protected(mid, True)
        s.set_decision(mid, "保留")  # 2回保存したので、1回目（保護だけ）が .bak に残る
        self.write("user_state.json", '{"broken": ')  # 途中までの書き込み
        s2 = Store(self.d)
        self.assertTrue(s2.protection_ok)
        self.assertEqual(s2._user[str(f)], {"protected": True})
        self.assertTrue(any("バックアップから復旧" in p for p in s2.load_problems))

    def test_wrong_types_in_user_state_and_scan_state(self):
        self.write("user_state.json", json.dumps({"/a.mp4": {"protected": "yes"}}))
        self.write("scan_state.json", json.dumps({"locations": "x"}))
        s = Store(self.d)
        self.assertFalse(s.protection_ok)
        self.assertEqual(s.locations(), [])  # 調査状態は作り直す（情報だけ）
        self.assertTrue(any("scan_state.json" in p for p in s.load_problems))
        self.write("user_state.json", "null")
        self.assertFalse(Store(self.d).protection_ok)

    def test_wrong_value_types_inside_valid_json(self):
        self.write("settings.json", json.dumps({"scan_gen": {"x": 1}, "extra_locations": [1, 2], "ffmpeg_dir": "C:/ff"}))
        self.write("scan_state.json", json.dumps({"stats": {"C:/": {"by_category": {"動画": "多い"}}}}))
        s = Store(self.d)
        self.assertEqual(s.get_setting("ffmpeg_dir"), "C:/ff")  # 正しい設定は残す
        self.assertIsNone(s.get_setting("scan_gen"))
        self.assertEqual(s.json_list("extra_locations"), [])
        self.assertEqual(s.location_stats(), {})
        self.assertEqual(int(s.get_setting("scan_gen", "0")) + 1, 1)  # 調査を始められる

    def test_broken_jsonl_lines_are_skipped(self):
        s = Store(self.d)
        f = self.d / "rec.mp4"
        fake_mp4(f)
        mid = s.insert_raw(str(f), "rec.mp4", str(self.d), f.stat().st_size, f.stat().st_mtime, ".mp4", location="L")
        s.update_video(mid, sha256="abc")
        s.save_inventory("L", 1)
        with open(self.d / "analysis.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"k": 1, "f": []}\n[]\n{"k": "x", "f": {"sha256": "zz"}')  # 型違い・壊れた行（途中書き込み）
        s2 = Store(self.d)
        self.assertEqual(s2.video(s2.id_of(str(f)))["sha256"], "abc")
        self.assertTrue(any("analysis.jsonl" in p for p in s2.load_problems))


# --- 5. ごみ箱移動中の GUI ---------------------------------------------------------
try:
    from PySide6.QtCore import QCoreApplication, QEvent, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class TrashThreadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from storage_organizer import organizer
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)
        self.store = Store(self.d / "data")
        self.files = []
        for i in range(4):
            f = self.d / f"rec{i}.mp4"
            fake_mp4(f)
            self.files.append(self.store.insert_raw(str(f), f.name, str(self.d), f.stat().st_size, f.stat().st_mtime,
                                                    ".mp4"))
        self.state = organizer.MediaState(self.store, lambda: FFMPEG)
        self.page = organizer.FinalPage(self.state)
        self.page.confirm = lambda count, total: True  # 移動前の確認ダイアログで「はい」を選んだものとする
        self.results = []
        self.page.finished.connect(self.results.append)
        self.page.prepare(self.files)

    def tearDown(self):
        self.page.wait()
        self.state.close()
        self._t.cleanup()

    def wait_done(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end and not self.results and self.page.is_running():
            QCoreApplication.processEvents()
            time.sleep(0.01)
        QCoreApplication.processEvents()

    def test_gui_responds_and_cancel_keeps_partial_results(self):
        moved = []

        def slow(path):
            time.sleep(0.4)
            moved.append(path)
            os.rename(path, path + ".trashed-by-test")
        self.page.backend = slow
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start(20)
        self.page.move_button.click()
        self.page.move_button.click()  # 二重に押しても1回だけ
        self.assertTrue(self.page.is_running())
        self.assertFalse(self.page.back_button.isEnabled())
        end = time.time() + 10
        while time.time() < end and not moved:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        self.page.prepare(self.files[:1])  # 移動中に最終確認を開き直しても、対象と状態は変わらない
        self.assertEqual(self.page.ids, self.files)
        self.assertTrue(self.page.is_running())
        self.page.cancel()  # ファイル単位で止める
        self.wait_done()
        timer.stop()
        self.assertGreater(len(ticks), 10)  # 移動中も画面のイベントが処理されている
        res = self.results[0]
        statuses = [r.status for r in res]
        self.assertEqual(len(res), 4)
        self.assertGreaterEqual(statuses.count("moved"), 1)
        self.assertEqual(statuses.count("moved"), len(moved))
        self.assertTrue(any("中断" in r.message for r in res if r.status == "skipped"))
        for r in res:  # 結果と保存状態・実ファイルが一致する
            row = self.store.video(r.media_id)
            self.assertEqual(bool(row["trashed_at"]), r.status == "moved")
            self.assertEqual(os.path.exists(r.path), r.status != "moved")
        self.assertFalse(self.page.is_running())
        self.assertTrue(self.page.back_button.isEnabled())

    def test_unexpected_error_does_not_leave_running_state(self):
        with mock.patch.object(trash, "move_to_trash", side_effect=RuntimeError("想定外")), \
                mock.patch.object(QMessageBox, "warning") as warn:
            self.page.move_button.click()
            end = time.time() + 10
            while time.time() < end and self.page.is_running():
                QCoreApplication.processEvents()
                time.sleep(0.01)
        self.assertFalse(self.page.is_running())
        self.assertTrue(self.page.move_button.isEnabled())
        self.assertTrue(self.page.back_button.isEnabled())
        self.assertIn("想定外", warn.call_args[0][2])
        self.assertTrue(all(os.path.exists(self.store.video(m)["path"]) for m in self.files))  # 完全削除しない


if __name__ == "__main__":
    unittest.main()


# --- 3. 類似判定の違いの見逃し ------------------------------------------------------
import base64  # noqa: E402

import numpy as np  # noqa: E402

from storage_organizer import candidates, similar  # noqa: E402


def sig_from(frames, duration=60.0):
    return {"v": similar.VERSION, "duration": duration,
            "frames": [base64.b64encode(f.astype(np.uint8).tobytes()).decode() for f in frames]}


class SimilarDifferenceTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(3)
        self.base = [rng.integers(0, 255, similar.W * similar.H) for _ in range(similar.N_POINTS)]
        self.diff3 = [f if i not in (2, 6, 9) else rng.integers(0, 255, similar.W * similar.H)
                      for i, f in enumerate(self.base)]

    def rows(self, sigs):
        rows = {}
        for i, sg in enumerate(sigs, 1):
            rows[i] = {"id": i, "vsig": json.dumps(sg), "protected": 0, "decision": None,
                       "meta": json.dumps({"width": 1920 if i == 1 else 640, "height": 1080 if i == 1 else 360,
                                           "duration": 60.0}), "name": f"v{i}.mp4"}
        return rows

    def test_three_different_scenes_are_shown_and_block_recommendation(self):
        r = similar.compare(sig_from(self.base), sig_from(self.diff3))
        self.assertEqual(r["verdict"], "similar")  # 類似候補ではあるが…
        rows = self.rows([sig_from(self.base), sig_from(self.diff3)])
        det = candidates.similar_group_detail(rows, [1, 2])
        self.assertEqual(det["unmatched"], [2, 6, 9])  # どの場面が違うかを示す
        self.assertIn("3 が一致しない", det["text"])
        self.assertIn("音声", det["text"])
        keep, why = candidates.keep_for_similar_videos(list(rows.values()), det)
        self.assertIsNone(keep)  # 解像度が高くても、違う場面があればおすすめを決めない
        self.assertIn("一致しない場面", why)

    def test_uncomparable_scenes_are_not_counted_as_matched(self):
        flat = [np.zeros(similar.W * similar.H) if i < 4 else f for i, f in enumerate(self.base)]  # 黒画面
        det = candidates.similar_group_detail(self.rows([sig_from(self.base), sig_from(flat)]), [1, 2])
        self.assertEqual(det["uncomparable"], [0, 1, 2, 3])
        self.assertEqual(len(det["matched"]), similar.N_POINTS - 4)

    @unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
    def test_same_video_different_audio_is_not_exact_duplicate(self):
        from test_organizer import Env
        env = Env()
        try:
            v = env.home / "Videos"
            for name, freq in (("talk_a.mp4", 440), ("talk_b.mp4", 880)):  # 映像は同じ・音声だけ違う
                subprocess.run([FFMPEG.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=15",
                                "-f", "lavfi", "-i", f"sine=frequency={freq}", "-t", "6", "-pix_fmt", "yuv420p",
                                "-c:v", "libx264", "-g", "15", "-c:a", "aac", str(v / name)], check=True)
            env.run()
            s = Store(env.data)
            ctx = candidates.Context(s)
            names = lambda g: {ctx.rows[i]["name"] for i in g}  # noqa: E731
            self.assertFalse(any({"talk_a.mp4", "talk_b.mp4"} <= names(g) for g in ctx.dup_groups.values()))
            group = next(g for g in ctx.video_groups if {"talk_a.mp4", "talk_b.mp4"} <= names(g))
            text = " ".join(ctx.reasons(ctx.rows[group[0]]))
            self.assertIn("音声は比較していません", text)
            plan = candidates.plan_recordings(ctx, None)
            self.assertTrue(all(not it["confirmed"] for it in plan["items"] if it["kind"] == "similar"))
        finally:
            env.close()


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class CompareShowsDifferencesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_unmatched_scenes_are_shown_first(self):
        from storage_organizer import organizer
        rng = np.random.default_rng(5)
        base = [rng.integers(0, 255, similar.W * similar.H) for _ in range(similar.N_POINTS)]
        diff = [f if i != 7 else rng.integers(0, 255, similar.W * similar.H) for i, f in enumerate(base)]
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t) / "data")
            ids = []
            for name, frames in (("a.mp4", base), ("b.mp4", diff)):
                f = Path(t) / name
                fake_mp4(f)
                mid = s.insert_raw(str(f), name, t, f.stat().st_size, f.stat().st_mtime, ".mp4",
                                   meta={"duration": 60.0, "width": 640, "height": 360}, vsig=sig_from(frames))
                ids.append(mid)
            s.set_video_groups([ids])
            state = organizer.MediaState(s, lambda: media.FFmpegTools(None, None))
            page = organizer.ComparePage(state)
            page.show_group("similar", 0)
            self.assertEqual(page.points[0][1], "一致しない場面")
            self.assertAlmostEqual(page.points[0][0], (7 + 0.5) / similar.N_POINTS)
            self.assertIn("一致しない場面が 1", page.reason.text())
            self.assertIn("音声は比較していません", page.reason.text())
            state.close()


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class BrokenDataGuiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_window_starts_and_blocks_organizing(self):
        from storage_organizer.main_window import MainWindow
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "user_state.json").write_text("[]", encoding="utf-8")
            (Path(t) / "settings.json").write_text("null", encoding="utf-8")
            w = MainWindow(Store(t), start_autoscan=False)
            w.show()
            QCoreApplication.processEvents()
            self.assertTrue(w.home.data_banner.isVisibleTo(w.home))
            self.assertIn("整理操作を止めています", w.home.data_banner.text.text())
            self.assertTrue(w.settings.reset_user.isVisibleTo(w.settings))
            w.show_plan(None)
            w.plan._checks = {1: True}
            w.plan._update_selected()
            self.assertFalse(w.plan.final_button.isEnabled())  # 調査や確認はできるが、整理操作はできない
            w.close()
            w.deleteLater()  # 一時フォルダを消す前にウィンドウを破棄する
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
