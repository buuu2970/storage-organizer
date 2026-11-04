"""再検証で残った 3 件の再発防止テスト。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "stress"))

from storage_organizer import autoscan, media, trash  # noqa: E402
from storage_organizer.store import Store  # noqa: E402

MP4 = b"\x00\x00\x00\x18ftypmp42"


class RescanDuringOrganizeTest(unittest.TestCase):
    """1. 整理の途中・確認のあとに再スキャンが残す側の情報を更新しても、古い重複関係を信用して移動しない。"""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.d = Path(self._t.name)
        self.s = Store(self.d / "data")
        self.data = MP4 + os.urandom(2000)
        self.paths = [self.d / f"c{i}.mp4" for i in range(3)]
        for p in self.paths:
            p.write_bytes(self.data)
        self.ids = [self.s.insert_raw(str(p), p.name, str(self.d), p.stat().st_size, p.stat().st_mtime, ".mp4",
                                      sha256="H") for p in self.paths]
        self.s.set_protected(self.ids[0], True)  # c0 を残す

    def tearDown(self):
        self._t.cleanup()

    def originals_left(self):
        return sum(p.exists() and p.read_bytes() == self.data for p in self.paths)

    def test_rescan_updates_keeper_mid_batch(self):
        moved = []

        def backend(path):
            moved.append(path)
            os.rename(path, path + ".t")
            if len(moved) == 1:  # 1件目の移動後に、残す側の内容が変わり、再スキャンが情報を更新する
                with open(self.paths[0], "ab") as f:
                    f.write(b"edited")
                st = self.paths[0].stat()
                self.s.update_file_state(self.ids[0], st.st_size, st.st_mtime)
        results = trash.move_to_trash(self.s, self.ids[1:], backend)
        self.assertEqual(len(moved), 1)
        self.assertEqual(results[1].status, "skipped")
        self.assertGreaterEqual(self.originals_left(), 1)  # 元の内容のコピーが残る

    def test_keeper_changed_without_store_update_mid_batch(self):
        moved = []

        def backend(path):
            moved.append(path)
            os.rename(path, path + ".t")
            if len(moved) == 1:
                with open(self.paths[0], "ab") as f:
                    f.write(b"edited")  # 保存されている情報は古いまま（確認時点の記録と比べて見つける）
        trash.move_to_trash(self.s, self.ids[1:], backend)
        self.assertEqual(len(moved), 1)
        self.assertGreaterEqual(self.originals_left(), 1)

    def test_generation_changed_after_confirmation(self):
        confirmed = self.s.generation  # 最終確認の時点
        st = self.paths[0].stat()
        self.s.update_file_state(self.ids[0], st.st_size + 0, st.st_mtime + 5)  # 確認後に再スキャンが更新
        moved = []
        results = trash.move_to_trash(self.s, self.ids[1:], moved.append, expected_generation=confirmed)
        self.assertEqual(moved, [])
        self.assertTrue(all("もう一度確認" in r.message for r in results))

    def test_scan_and_organize_are_exclusive(self):
        self.s.scan_active = True
        moved = []
        results = trash.move_to_trash(self.s, self.ids[1:], moved.append)
        self.assertEqual(moved, [])
        self.assertIn("調査中", results[0].message)
        self.s.scan_active = False
        self.s.organize_active = True
        summary = autoscan.task_autoscan(self.s, media.FFmpegTools(None, None), autoscan.Controller(), lambda p: None)
        self.assertTrue(summary["cancelled"])
        self.assertIn("ごみ箱へ移動している間", summary["error"])
        self.assertFalse(self.s.scan_active)

    def test_organize_flag_is_cleared_even_on_error(self):
        with mock.patch.object(trash, "_move_all", side_effect=RuntimeError("想定外")):
            with self.assertRaises(RuntimeError):
                trash.move_to_trash(self.s, self.ids[1:], lambda p: None)
        self.assertFalse(self.s.organize_active)  # 失敗後も調査をやり直せる


class NonAsciiDigitTest(unittest.TestCase):
    """2. 「²」など、isdigit() は真でも int() できない値で起動に失敗しない。"""

    def test_group_id_and_scan_gen(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "a.mp4"
            p.write_bytes(MP4)
            s = Store(t)
            s.insert_raw(str(p), p.name, t, p.stat().st_size, p.stat().st_mtime, ".mp4", location="L")
            s.save_inventory("L", 1)
            for gid in ("²", "١", "3a", "-1", ""):
                (Path(t) / "scan_state.json").write_text(json.dumps({"similar_groups": {gid: [str(p)]}}),
                                                         encoding="utf-8")
                (Path(t) / "settings.json").write_text(json.dumps({"scan_gen": gid}), encoding="utf-8")
                s2 = Store(t)  # 起動に失敗しない
                self.assertEqual(int(s2.get_setting("scan_gen", "0")), 0, gid)
                self.assertTrue(any("捨てました" in x for x in s2.load_problems), gid)
                for f in Path(t).glob("*.bak"):
                    f.unlink()


class StressExitCodeTest(unittest.TestCase):
    """3. 反復試験ツールは失敗があれば非 0 を返し、最終実行スクリプトは完了扱いにしない。"""

    def test_run_stress_returns_nonzero_on_failure(self):
        import run_stress
        from common import Fail

        def failing(seed, work, rec):
            raise Fail("試験用の失敗")
        with tempfile.TemporaryDirectory() as t, mock.patch.object(run_stress, "get_case", lambda name: failing), \
                mock.patch.object(sys, "argv", ["run_stress", "--category", "storage", "--count", "2",
                                                "--out", str(Path(t) / "x.jsonl")]):
            self.assertEqual(run_stress.main(), 1)
        with tempfile.TemporaryDirectory() as t, \
                mock.patch.object(run_stress, "get_case", lambda name: (lambda seed, work, rec: None)), \
                mock.patch.object(sys, "argv", ["run_stress", "--category", "storage", "--count", "2",
                                                "--out", str(Path(t) / "x.jsonl")]):
            self.assertEqual(run_stress.main(), 0)

    def test_final_script_stops_on_failure(self):
        text = (ROOT / "tools" / "stress" / "final_run.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("|| { echo \"反復試験", text)
        self.assertIn("|| { echo \"回帰テスト", text)
        self.assertLess(text.index("-eq 300"), text.index("echo finished > \"$OUT/DONE\""))  # 確認の後に完了の印


if __name__ == "__main__":
    unittest.main()


try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class CompareScopeAndUndoTest(unittest.TestCase):
    """初心者向け UX: 比較の操作範囲が文言どおりで、確認していない録画を黙って整理予定にせず、取り消せる。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, kind):
        import base64
        import numpy as np
        from storage_organizer import organizer, similar
        self._t = tempfile.TemporaryDirectory()
        t = self._t.name
        self.s = Store(Path(t) / "data")
        rng = np.random.default_rng(1)
        base = [rng.normal(128, 40, (similar.H, similar.W)) for _ in range(12)]
        self.ids = []
        same = MP4 + os.urandom(500)
        for i in range(4):
            p = Path(t) / f"録画 {i}.mp4"
            p.write_bytes(same if kind == "duplicate" else MP4 + os.urandom(500 + i))
            frames = [np.clip(f + rng.normal(0, 2, f.shape), 0, 255).astype(np.uint8) for f in base]
            sig = {"v": similar.VERSION, "duration": 60.0,
                   "frames": [base64.b64encode(f.tobytes()).decode() for f in frames]}
            self.ids.append(self.s.insert_raw(str(p), p.name, t, p.stat().st_size, p.stat().st_mtime, ".mp4",
                                              sha256="H" if kind == "duplicate" else None,
                                              meta={"duration": 60.0, "width": 640, "height": 360}, vsig=sig))
        if kind == "similar":
            self.s.set_video_groups([self.ids])
        self.state = organizer.MediaState(self.s, lambda: media.FFmpegTools(None, None))
        self.page = organizer.ComparePage(self.state)
        key = next(iter(self.state.ctx.dup_groups)) if kind == "duplicate" else 0
        self.page.show_group(kind, key)

    def tearDown(self):
        self.page.stop()
        self.state.close()
        self._t.cleanup()

    def decisions(self):
        return {m: self.s.video(m)["decision"] for m in self.ids}

    def test_similar_group_of_four_stages_only_compared_one_and_undo(self):
        self.make("similar")
        p = self.page
        self.assertTrue(p.pick_row.isVisibleTo(p))  # 4 本のうち比較する 2 本を選べる
        self.assertEqual(p.left_combo.count(), 4)  # グループ内のすべての録画に届く
        self.assertEqual(p.keep_all.text(), "すべて残す")
        self.assertTrue(p.keep_pair.isVisibleTo(p))
        a, b = p.pair
        p._keep_this(a)
        d = self.decisions()
        self.assertEqual(d[a], "残す")
        self.assertEqual(d[b], "削除候補")  # 比較中のもう1本だけ
        self.assertEqual([m for m in self.ids if d[m] == "削除候補"], [b])  # 確認していない録画は入れない
        self.assertIn("ほかの 1 本を削除候補に追加しました", p.result_text.text())
        self.assertIn("まだ移動していません", p.result_text.text())
        self.assertTrue(all(os.path.exists(self.s.video(m)["path"]) for m in self.ids))  # 比較ではファイルを動かさない
        p.undo()  # 取り消すと判断する前に戻る
        self.assertEqual(set(self.decisions().values()), {None})
        p.right_combo.setCurrentIndex(3)  # 別の録画と比べ直せる
        p._pick_changed()
        self.assertEqual(p.pair[1], p.all_ids[3])

    def test_duplicate_keep_stages_all_other_copies(self):
        self.make("duplicate")
        p = self.page
        keep = p.pair[0]
        p._keep_this(keep)
        d = self.decisions()
        self.assertEqual(sorted(m for m in self.ids if d[m] == "削除候補"), sorted(m for m in self.ids if m != keep))
        self.assertIn("ほかの 3 本を削除候補に追加しました", p.result_text.text())

    def test_hold_and_keep_pair_are_undoable(self):
        self.make("similar")
        p = self.page
        p._keep_pair()
        d = self.decisions()
        self.assertEqual(sorted(m for m in self.ids if d[m] == "残す"), sorted(p.pair))
        p.undo()
        p._decide_all("保留")
        self.assertEqual(set(self.decisions().values()), {"保留"})
        p.undo()
        self.assertEqual(set(self.decisions().values()), {None})
