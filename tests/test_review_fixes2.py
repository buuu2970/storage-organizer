"""第三者の独立テストで指摘された 7 項目の再発防止テスト。"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage_organizer import candidates, similar, trash  # noqa: E402
from storage_organizer.store import PENDING_MARK, Store  # noqa: E402

MP4 = b"\x00\x00\x00\x18ftypmp42"


def put(d, name, data):
    p = Path(d) / name
    p.write_bytes(data)
    return p


def register(s, p, **kw):
    st = p.stat()
    return s.insert_raw(str(p), p.name, str(p.parent), st.st_size, st.st_mtime, ".mp4", **kw)


class ProtectionPendingTest(unittest.TestCase):
    """1. 保護情報の破損後、再起動や再調査をまたいでも整理禁止を続ける。"""

    def test_pending_survives_restarts_until_explicit_reset(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "user_state.json").write_text("[]", encoding="utf-8")
            self.assertFalse(Store(t).protection_ok)
            for _ in range(3):  # 何度起動し直しても
                s = Store(t)
                self.assertFalse(s.protection_ok)
                self.assertTrue(any("復旧待ち" in p for p in s.load_problems))
            s.set_setting("scan_gen", "5")  # 再調査（設定の保存）をしても解除されない
            self.assertFalse(Store(t).protection_ok)
            s = Store(t)
            s.reset_user_state()  # 利用者が明示して初期化
            self.assertFalse((Path(t) / PENDING_MARK).exists())
            self.assertTrue(Store(t).protection_ok)

    def test_pending_mark_write_failure_keeps_broken_file_in_place(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "user_state.json").write_text("{broken", encoding="utf-8")
            with mock.patch("storage_organizer.store._write_raw", side_effect=OSError(28, "No space")):
                self.assertFalse(Store(t).protection_ok)
            self.assertTrue((Path(t) / "user_state.json").exists())  # 動かしていないので次も検出できる
            self.assertFalse(Store(t).protection_ok)

    def test_recovered_from_backup_is_not_pending(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(t)
            p = put(t, "a.mp4", MP4 + b"x" * 50)
            mid = register(s, p)
            s.set_protected(mid, True)
            s.set_decision(mid, "保留")
            (Path(t) / "user_state.json").write_text("[]", encoding="utf-8")
            self.assertTrue(Store(t).protection_ok)  # 正常なバックアップから復旧できた場合は止めない
            self.assertFalse((Path(t) / PENDING_MARK).exists())


class KeeperDuringBatchTest(unittest.TestCase):
    """2. 一括移動の途中で残す側が消失・変更しても、残りのコピーを移動しない。"""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)
        self.s = Store(self.d / "data")
        data = MP4 + os.urandom(3000)
        self.paths = [put(self.d, f"c{i}.mp4", data) for i in range(4)]
        self.ids = [register(self.s, p, sha256="H") for p in self.paths]
        self.s.set_protected(self.ids[0], True)  # c0 が残す側

    def tearDown(self):
        self._t.cleanup()

    def run_with(self, after_first):
        moved = []

        def backend(path):
            moved.append(path)
            os.rename(path, path + ".trashed")
            if len(moved) == 1:
                after_first()
        return trash.move_to_trash(self.s, self.ids[1:], backend), moved

    def test_keeper_deleted_mid_batch(self):
        results, moved = self.run_with(lambda: os.remove(self.paths[0]))
        self.assertEqual(len(moved), 1)  # 1件目のあとは続けない
        self.assertEqual([r.status for r in results], ["moved", "skipped", "skipped"])
        self.assertTrue(self.paths[2].exists() and self.paths[3].exists())  # 残るコピーがある
        self.assertIn("残すファイル", results[1].message)

    def test_keeper_changed_mid_batch(self):
        def change():
            with open(self.paths[0], "ab") as f:
                f.write(b"edited")
        results, moved = self.run_with(change)
        self.assertEqual(len(moved), 1)
        self.assertTrue(self.paths[2].exists() and self.paths[3].exists())

    def test_all_selected_still_keeps_one(self):
        moved = []
        results = trash.move_to_trash(self.s, self.ids, lambda p: (moved.append(p), os.rename(p, p + ".t")))
        self.assertEqual(sum(r.status == "moved" for r in results), 3)
        self.assertEqual(sum(p.exists() for p in self.paths), 1)  # 全部選んでも1本は残る


class RecordFailureTest(unittest.TestCase):
    """3. 移動成功後の記録の失敗で、成功済みの結果を失わない。"""

    def test_record_failure_is_separate_from_move_result(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t) / "data")
            ps = [put(t, f"a{i}.mp4", MP4 + os.urandom(100)) for i in range(3)]
            ids = [register(s, p) for p in ps]
            with mock.patch.object(s, "log_trash", side_effect=OSError(28, "No space")), \
                    mock.patch.object(s, "mark_trashed", side_effect=[None, OSError(28, "No space"), None]):
                results = trash.move_to_trash(s, ids, lambda x: os.rename(x, x + ".t"))
            self.assertEqual([r.status for r in results], ["moved", "moved", "moved"])  # 実際の結果
            self.assertTrue(all(not p.exists() for p in ps))
            self.assertTrue(all(r.record_error for r in results))  # 記録の失敗は別に伝える
            self.assertIn("記録の保存に失敗", results[1].message)

    def test_unexpected_error_on_one_file_keeps_earlier_results(self):
        with tempfile.TemporaryDirectory() as t:
            s = Store(Path(t) / "data")
            ps = [put(t, f"b{i}.mp4", MP4 + os.urandom(100)) for i in range(3)]
            ids = [register(s, p) for p in ps]
            real = trash.check
            calls = []

            def flaky(db, mid, ctx=None):
                calls.append(mid)
                if len(calls) == 2:
                    raise RuntimeError("想定外")
                return real(db, mid, ctx)
            with mock.patch.object(trash, "check", side_effect=flaky):
                results = trash.move_to_trash(s, ids, lambda x: os.rename(x, x + ".t"))
            self.assertEqual([r.status for r in results], ["moved", "failed", "moved"])
            self.assertTrue(ps[1].exists())  # 失敗したファイルは動かしていない


class DerivedCacheTest(unittest.TestCase):
    """4. 類似グループなどの派生キャッシュが不正でも起動でき、安全に捨てて作り直す。"""

    def test_bad_internal_forms(self):
        cases = [{"similar_groups": {"1": 5}}, {"similar_groups": {"x": ["/a.mp4"]}}, {"similar_groups": []},
                 {"video_groups": [["/a.mp4", 3]]}, {"video_groups": "x"}, {"stats": {"C:/": {"top_files": [[1]]}}},
                 {"stats": {"C:/": {"areas": [["p", "big", "r"]]}}}, {"skip_counts": []}]
        for extra in cases:
            with tempfile.TemporaryDirectory() as t:
                p = put(t, "a.mp4", MP4)
                state = {"locations": [{"path": t, "kind": "known", "label": "L", "priority": 1, "status": "完了"}],
                         **{k: (v if not isinstance(v, dict) else {kk: ([str(p)] if vv == ["/a.mp4"] else vv)
                                                                   for kk, vv in v.items()})
                            for k, v in extra.items()}}
                (Path(t) / "scan_state.json").write_text(json.dumps(state), encoding="utf-8")
                s = Store(t)  # 起動に失敗しない
                self.assertEqual(len(s.locations()), 1, extra)  # 調査場所（中心の情報）は残す
                self.assertTrue(any("捨てました" in x for x in s.load_problems), extra)
                s.video_groups()
                s.location_stats()
                candidates.Context(s)


def enc(frames, duration=60.0):
    return {"v": similar.VERSION, "duration": duration,
            "frames": [base64.b64encode(np.clip(f, 0, 255).astype(np.uint8).tobytes()).decode() for f in frames]}


def family(rng, kind):
    """いろいろな種類の場面（細かい模様・文字のような模様・グラデーションなど）。"""
    H, W = similar.H, similar.W
    if kind == "noise":
        return [rng.normal(128, 40, (H, W)) for _ in range(12)]
    if kind == "fine":  # 細かい模様だけ（区画の平均はほぼ一定）
        out = []
        for _ in range(12):
            cb = (np.indices((H, W)).sum(axis=0) % 2) * 60 + 100
            out.append(cb + rng.normal(0, 8, (H, W)) * (rng.random((H, W)) > 0.5))
        return out
    if kind == "text":
        out = []
        for _ in range(12):
            img = np.full((H, W), 30.0)
            img[rng.integers(0, H, 40), rng.integers(0, W, 40)] = 230
            out.append(img)
        return out
    if kind == "gradient":
        return [np.tile(np.linspace(rng.uniform(0, 100), rng.uniform(150, 255), W), (H, 1))
                + rng.normal(0, 10, (H, W)) for _ in range(12)]
    if kind == "partflat":
        out = []
        for i in range(12):
            img = rng.normal(128, 40, (H, W)) if i % 3 else np.zeros((H, W))
            out.append(img)
        return out
    raise ValueError(kind)


def variant(rng, frames, kind):
    if kind == "noise":
        return [f + rng.normal(0, rng.uniform(2, 15), f.shape) for f in frames]
    if kind == "contrast":
        a, b = rng.uniform(0.7, 1.3), rng.uniform(-30, 30)
        return [f * a + b for f in frames]
    if kind == "blur":
        return [(f + np.roll(f, 1, 0) + np.roll(f, 1, 1) + np.roll(f, -1, 0) + np.roll(f, -1, 1)) / 5 for f in frames]
    if kind == "downup":
        return [np.kron(f.reshape(similar.H // 2, 2, similar.W // 2, 2).mean(axis=(1, 3)), np.ones((2, 2)))
                for f in frames]
    if kind == "partial":  # 一部の場面だけ別内容
        out = list(frames)
        for i in rng.choice(12, 3, replace=False):
            out[i] = rng.normal(128, 40, out[i].shape)
        return out
    raise ValueError(kind)


class PrefilterNoMissTest(unittest.TestCase):
    """5. 軽い特徴による絞り込みで、詳しい比較なら類似になる組を見逃さない（全組の詳しい比較と一致する）。"""

    def test_group_matches_exhaustive_compare(self):
        rng = np.random.default_rng(20261104)
        checked = misses = similar_pairs = 0
        for fam in ("noise", "fine", "text", "gradient", "partflat"):
            for var in ("noise", "contrast", "blur", "downup", "partial"):
                for _ in range(40):
                    a = enc(family(rng, fam))
                    b = enc(variant(rng, [np.frombuffer(base64.b64decode(x), np.uint8).astype(float).reshape(
                        similar.H, similar.W) for x in a["frames"]], var), 60.4)
                    expected = similar.compare(a, b)["verdict"] == "similar"
                    groups, _ = similar.group({1: a, 2: b})
                    got = bool(groups)
                    checked += 1
                    similar_pairs += expected
                    misses += expected and not got
                    self.assertFalse(got and not expected)  # 絞り込みで新しい誤検出も増やさない
        self.assertEqual(misses, 0, f"{misses} / {similar_pairs} 組を見逃した")
        self.assertGreater(similar_pairs, 300)  # 類似の組が十分に含まれている試験であること
        self.assertEqual(checked, 1000)


class GroupCancelTest(unittest.TestCase):
    """6. グループ化の段階と終了直前でもキャンセルを受け付ける。"""

    def items(self, n=40):
        rng = np.random.default_rng(1)
        base = [rng.normal(128, 40, (similar.H, similar.W)) for _ in range(12)]
        return {i: enc([f + rng.normal(0, 3, f.shape) for f in base]) for i in range(n)}

    def test_cancel_during_grouping(self):
        ev = threading.Event()

        def progress(stage, done, total):
            if stage == "group":
                ev.set()
        with self.assertRaises(similar.Cancelled):
            similar.group(self.items(), cancel=ev, progress=progress)

    def test_cancel_just_before_return(self):
        ev = threading.Event()
        real = similar.compare
        calls = []

        def last_compare(*a, **k):
            calls.append(1)
            r = real(*a, **k)
            return r
        items = self.items(3)
        with mock.patch.object(similar, "compare", side_effect=last_compare):
            def progress(stage, done, total):
                if stage == "group":
                    ev.set()
            with self.assertRaises(similar.Cancelled):
                similar.group(items, cancel=ev, progress=progress)


class BuildScriptTest(unittest.TestCase):
    """7. テストが失敗したら配布ビルドを中断し、非0で終了する。"""

    def test_build_stops_on_test_failure(self):
        text = (ROOT / "packaging" / "build_windows.bat").read_text(encoding="utf-8")
        lines = [ln.strip() for ln in text.splitlines()]
        test_line = next(ln for ln in lines if "unittest" in ln)
        self.assertIn("goto :testfail", test_line)
        self.assertNotIn("echo", test_line.split("||")[1])
        i = lines.index(":testfail")
        self.assertTrue(any(ln.startswith("exit /b 1") for ln in lines[i:i + 3]))
        self.assertLess(lines.index(test_line), next(n for n, ln in enumerate(lines) if ln.startswith("pyinstaller")))
        self.assertIn(b"\r\n", (ROOT / "packaging" / "build_windows.bat").read_bytes())

    @unittest.skipUnless(sys.platform == "win32", "cmd.exe が必要（Windows のみ）")
    def test_build_script_exit_code_on_windows(self):  # pragma: no cover
        out = subprocess.run(["cmd", "/c", "exit 1 || echo x"], capture_output=True)
        self.assertNotEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()


try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class ViewWithBrokenProtectionTest(unittest.TestCase):
    """反復試験で発見: 保護情報が読めない状態でファイルを選ぶと、確認日時の保存で例外になっていた。"""

    def test_viewing_does_not_raise(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "user_state.json").write_text("[]", encoding="utf-8")
            s = Store(t)
            p = put(t, "a.mp4", MP4)
            mid = s.insert_raw(str(p), p.name, t, p.stat().st_size, p.stat().st_mtime, ".mp4")
            s.mark_viewed(mid)  # 例外にならない（情報の確認はできる）
            self.assertIsNone(s.video(mid)["last_viewed"])
            self.assertFalse((Path(t) / "user_state.json").exists())  # 復旧待ちのデータに書き込まない
            ok = Store(Path(t) / "ok")
            mid2 = ok.insert_raw(str(p), p.name, t, p.stat().st_size, p.stat().st_mtime, ".mp4")
            with mock.patch("storage_organizer.store.os.fsync", side_effect=OSError(28, "No space")):
                ok.mark_viewed(mid2)  # 保存に失敗しても例外にしない


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class AnalysisInterruptedStatusTest(unittest.TestCase):
    """反復試験で発見: 解析の途中で中断・異常終了すると、再起動後に「調査完了」と表示されていた。"""

    def test_status_after_restart(self):
        from storage_organizer import media, organizer
        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as t:
            s = Store(t)
            p = put(t, "a.mp4", MP4)
            s.insert_raw(str(p), p.name, t, p.stat().st_size, p.stat().st_mtime, ".mp4")
            for state, kind in (("running", "中断"), ("cancelled", "中断"), ("done", "調査完了")):
                s.set_setting("analysis_state", state)
                s.set_setting("scan_finished", "1700000000" if state == "done" else None)
                st = organizer.MediaState(s, lambda: media.FFmpegTools(None, None))
                got = st.scan_state()
                self.assertEqual(got["kind"], kind, state)
                if state != "done":
                    self.assertIn("確認が途中", got["text"])
                st.close()


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class DecisionButtonsWithBrokenProtectionTest(unittest.TestCase):
    """反復試験で発見: 保護情報が読めない状態で「残す」「保護する」が押せ、保存の例外が外へ出ていた。"""

    def test_buttons_disabled_and_errors_reported(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QMessageBox
        from storage_organizer.main_window import MainWindow
        QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "user_state.json").write_text("[]", encoding="utf-8")
            s = Store(t)
            a = put(t, "a.mp4", MP4 + b"same")
            b = put(t, "b.mp4", MP4 + b"same")
            for p in (a, b):
                s.insert_raw(str(p), p.name, t, p.stat().st_size, p.stat().st_mtime, ".mp4", sha256="H")
            w = MainWindow(s, start_autoscan=False)
            w.show()
            w.show_plan(None)
            top = w.plan.tree.topLevelItem(0)
            w.plan.tree.setCurrentItem(top.child(top.childCount() - 1))
            self.assertFalse(w.plan.protect_button.isEnabled())
            self.assertFalse(w.plan.keep_button.isEnabled())
            with mock.patch.object(QMessageBox, "warning") as warn:
                w.state.decide("保護する", [s.id_of(str(a))])  # 例外を外へ出さず、その場で伝える
            self.assertTrue(warn.called)
            self.assertFalse(s.video(s.id_of(str(a)))["protected"])
            w.close()
            w.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


class CommonScreenPrefilterTest(unittest.TestCase):
    """性能試験で発見: 共通画面が多いと絞り込みが効かず、1,000 本で上限（60 秒）を超えた。
    共通画面以外の場面で絞り込むよう改善した。全組の詳しい比較と結果が一致する（見逃さない）ことを確かめる。"""

    def test_matches_exhaustive_compare_with_common_screens(self):
        rng = np.random.default_rng(4242)
        H, W = similar.H, similar.W

        def scene():
            field = rng.normal(128, 50, (3, 4))
            return [np.kron(field + rng.normal(0, 25, (3, 4)), np.ones((H // 3 + 1, W // 4 + 1)))[:H, :W]
                    + rng.normal(0, 3, (H, W)) for _ in range(12)]
        common = scene()
        items = {}
        for i in range(90):
            own = scene()
            n_common = 7 if i % 3 else 10  # 共通画面以外が 5 場面のもの・2 場面だけのもの
            keep = set(range(n_common))
            items[i] = enc([common[k] if k in keep else own[k] for k in range(12)], 600.0)
        for k in range(12):  # 同じ録画の軽い変形（共通画面以外が 2 場面だけの録画も含む）
            src = [np.frombuffer(base64.b64decode(f), np.uint8).astype(float).reshape(H, W)
                   for f in items[k * 7]["frames"]]
            items[100 + k] = enc([f + rng.normal(0, 3, f.shape) for f in src], 600.2)
        ids = sorted(items)
        expected = {(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
                    if similar.compare(items[a], items[b])["verdict"] == "similar"}
        groups, stats = similar.group(items)
        together = {(a, b) for g in groups for a in g for b in g if a < b}
        self.assertTrue(expected)  # 試験として類似の組が含まれていること
        self.assertEqual(expected - together, set())  # 見逃さない
        self.assertEqual(together - expected, set())  # 新しい誤検出もない
        self.assertGreater(stats["common_frames"], 0)
        self.assertLess(stats["detailed_pairs"], stats["window_pairs"] // 4)  # 絞り込みが効いている


@unittest.skipUnless(sys.platform == "win32", "Windows のごみ箱が必要（Windows 実機で確認）")
class WindowsRecycleFromThreadTest(unittest.TestCase):  # pragma: no cover
    """Windows で「ごみ箱へ移動」で落ちた報告への対応: 別スレッドから実際のごみ箱へ移せること。

    テスト用に作った一時ファイルだけを、Windows のごみ箱へ移す（個人のファイルには触れない）。
    """

    def test_recycle_temp_file_from_worker_thread(self):
        with tempfile.TemporaryDirectory() as t:
            p = put(t, "recsort_テスト用の一時ファイル.txt", b"temporary test file")
            errors = []

            def work():
                try:
                    with trash.windows_com_thread():
                        trash._windows_recycle(str(p))
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
            th = threading.Thread(target=work)
            th.start()
            th.join(60)
            self.assertFalse(th.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(p.exists())
