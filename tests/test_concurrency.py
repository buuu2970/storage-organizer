"""整理（ごみ箱への移動）と調査の同時実行を、処理の内部で原子的に防ぐことのテスト。

外部の検証で、move_to_trash() を2つのスレッドから同時に呼ぶと、同じファイルに対して
ごみ箱への移動処理（backend）が2回呼ばれることが再現された（30/30回）。
ここでは、イベントとバリアで順番を決めて再現可能にし、sleep に頼らずに確かめる。
ファイルはすべて一時フォルダのダミー。backend は名前を変えるだけの偽物で、本物のごみ箱は使わない。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage_organizer import autoscan, media, trash  # noqa: E402
from storage_organizer.store import Store  # noqa: E402

REPEAT = 100
WAIT = 10  # 秒。イベント待ちの上限（通常はすぐに進む。止まったときにテストが固まらないため）
BUSY = "ほかの整理"


class Env:
    def __init__(self, n: int = 3) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = Store(self.dir / "data")
        self.ids = [self.add(f"rec{i}.mp4", os.urandom(64 + i)) for i in range(n)]

    def add(self, name: str, data: bytes) -> int:
        p = self.dir / name
        p.write_bytes(data)
        st = p.stat()
        self.db.upsert_media([(str(p), name, str(self.dir), st.st_size, st.st_mtime, ".mp4", "video", False)], 1,
                             str(self.dir))
        return self.db.id_of(str(p))

    def path(self, mid: int) -> str:
        return self.db.video(mid)["path"]

    def close(self) -> None:
        self.db.close()
        self.tmp.cleanup()


class BlockingBackend:
    """1回目の呼び出しの中で止まる偽の backend。呼ばれたパスを記録する（実際には名前を変えるだけ）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, path: str) -> None:
        with self.lock:
            self.calls.append(path)
            first = len(self.calls) == 1
        if first:
            self.entered.set()
            if not self.release.wait(WAIT):
                raise RuntimeError("テストの待機が時間切れになりました")
        if os.path.exists(path):  # 存在しなくてもエラーにしない（二重呼び出しを見つけやすくするため）
            os.rename(path, path + ".mock-trash")


def run_in_thread(fn):
    box: dict = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, box


class OrganizeIsExclusiveTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def start_first(self, ids):
        """1本目の整理を始め、backend の中で止まったところまで進める。"""
        backend = BlockingBackend()
        t, box = run_in_thread(lambda: trash.move_to_trash(self.env.db, ids, backend))
        self.assertTrue(backend.entered.wait(WAIT), "1本目が backend に到達しませんでした")
        return backend, t, box

    def finish(self, backend, t, box):
        backend.release.set()
        t.join(WAIT)
        self.assertFalse(t.is_alive())
        self.assertNotIn("error", box)
        return box["result"]

    def test_second_call_on_same_file_never_reaches_backend(self):
        mid = self.env.ids[0]
        backend, t, box = self.start_first([mid])
        second = trash.move_to_trash(self.env.db, [mid], backend)  # 1本目が移動中に、同じファイルを要求
        first = self.finish(backend, t, box)
        self.assertEqual(backend.calls, [self.env.path(mid)])  # backend は1回だけ
        self.assertEqual([r.status for r in second], ["skipped"])
        self.assertFalse(second[0].ok)
        self.assertIn(BUSY, second[0].message)
        self.assertTrue(first[0].ok)

    def test_second_call_on_other_file_is_also_refused(self):
        a, b = self.env.ids[0], self.env.ids[1]
        backend, t, box = self.start_first([a])
        second = trash.move_to_trash(self.env.db, [b], backend)
        self.finish(backend, t, box)
        self.assertEqual(backend.calls, [self.env.path(a)])
        self.assertTrue(os.path.exists(self.env.path(b)))  # 2本目の対象には触れない
        self.assertIn(BUSY, second[0].message)

    def test_race_is_refused_every_time(self):
        """同時実行を 100 回くり返し、毎回 backend が1回だけ呼ばれることを確かめる。"""
        for i in range(REPEAT):
            with self.subTest(round=i):
                mid = self.env.add(f"race{i}.mp4", os.urandom(128) + bytes([i % 256]))
                backend, t, box = self.start_first([mid])
                second = trash.move_to_trash(self.env.db, [mid], backend)
                first = self.finish(backend, t, box)
                self.assertEqual(len(backend.calls), 1)
                self.assertEqual(second[0].status, "skipped")
                self.assertTrue(first[0].ok)
                self.assertFalse(self.env.db.organize_active)  # 終わったら解除されている

    def test_simultaneous_start_from_two_threads(self):
        """2つのスレッドがバリアで同時に開始しても、整理を始められるのは1つだけ（100 回）。"""
        for i in range(REPEAT):
            with self.subTest(round=i):
                mid = self.env.add(f"start{i}.mp4", os.urandom(96) + bytes([i % 256]))
                backend = BlockingBackend()
                barrier = threading.Barrier(2)

                def call():
                    barrier.wait(WAIT)
                    return trash.move_to_trash(self.env.db, [mid], backend)
                t1, b1 = run_in_thread(call)
                t2, b2 = run_in_thread(call)
                self.assertTrue(backend.entered.wait(WAIT))
                # 先に backend に入った方が止まっている間に、もう一方は必ず終わっている
                loser = [(t, b) for t, b in ((t1, b1), (t2, b2)) if (t.join(0.5) or not t.is_alive())]
                self.assertEqual(len(loser), 1, "2つとも backend に向かっています")
                backend.release.set()
                for t in (t1, t2):
                    t.join(WAIT)
                    self.assertFalse(t.is_alive())
                results = [b1["result"][0], b2["result"][0]]
                self.assertEqual(sorted(r.status for r in results), ["moved", "skipped"])
                self.assertEqual(len(backend.calls), 1)

    def test_lock_is_released_after_error_and_next_run_works(self):
        a, b = self.env.ids[0], self.env.ids[1]
        with mock.patch.object(trash, "_move_all", side_effect=RuntimeError("想定外")):
            with self.assertRaises(RuntimeError):
                trash.move_to_trash(self.env.db, [a], lambda p: None)
        self.assertFalse(self.env.db.organize_active)
        moved = []
        results = trash.move_to_trash(self.env.db, [b], lambda p: (moved.append(p), os.rename(p, p + ".t")))
        self.assertTrue(results[0].ok)
        self.assertEqual(len(moved), 1)

    def test_lock_is_released_after_backend_error(self):
        a, b = self.env.ids[0], self.env.ids[1]

        def boom(path):
            raise trash.TrashError("ごみ箱へ移動できませんでした（テスト）")
        results = trash.move_to_trash(self.env.db, [a], boom)
        self.assertEqual(results[0].status, "failed")
        self.assertTrue(os.path.exists(self.env.path(a)))  # 失敗しても完全削除へ切り替えない
        self.assertTrue(trash.move_to_trash(self.env.db, [b], lambda p: os.rename(p, p + ".t"))[0].ok)

    def test_lock_is_released_after_cancel(self):
        cancel = threading.Event()
        cancel.set()
        results = trash.move_to_trash(self.env.db, self.env.ids, lambda p: self.fail("移動してはいけません"),
                                      cancel=cancel)
        self.assertTrue(all("中断" in r.message for r in results))
        self.assertFalse(self.env.db.organize_active)
        self.assertTrue(trash.move_to_trash(self.env.db, [self.env.ids[0]],
                                            lambda p: os.rename(p, p + ".t"))[0].ok)

    def test_duplicate_ids_in_one_request_move_once(self):
        mid = self.env.ids[0]
        calls = []
        results = trash.move_to_trash(self.env.db, [mid, mid], lambda p: (calls.append(p), os.rename(p, p + ".t")))
        self.assertEqual(len(calls), 1)
        self.assertEqual([r.ok for r in results], [True, False])

    def test_protection_is_still_enforced(self):
        mid = self.env.ids[0]
        self.env.db.set_protected(mid, True)
        results = trash.move_to_trash(self.env.db, [mid], lambda p: self.fail("保護中を移動しようとしました"))
        self.assertEqual(results[0].status, "skipped")
        self.assertFalse(self.env.db.organize_active)

    def test_releasing_does_not_clear_another_owner(self):
        """自分が取っていない整理の権利を、解除しないこと。"""
        token = self.env.db.begin_activity("organize")
        self.assertIsNotNone(token)
        self.assertIsNone(self.env.db.begin_activity("organize"))  # 2つ目は取れない
        self.env.db.end_activity(object())  # 別の持ち主（偽の札）では解除されない
        self.assertTrue(self.env.db.organize_active)
        self.env.db.end_activity(token)
        self.assertFalse(self.env.db.organize_active)


class ScanAndOrganizeTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_scan_does_not_start_while_organizing(self):
        backend = BlockingBackend()
        mid = self.env.ids[0]
        t, box = run_in_thread(lambda: trash.move_to_trash(self.env.db, [mid], backend))
        self.assertTrue(backend.entered.wait(WAIT))
        with mock.patch.object(autoscan, "build_locations", side_effect=AssertionError("調査を始めてはいけません")):
            summary = autoscan.task_autoscan(self.env.db, media.FFmpegTools(None, None), autoscan.Controller(),
                                             lambda p: None)
        backend.release.set()
        t.join(WAIT)
        self.assertTrue(summary["cancelled"])
        self.assertIn("ごみ箱へ移動している間", summary["error"])
        self.assertFalse(self.env.db.scan_active)
        self.assertTrue(box["result"][0].ok)

    def test_organize_does_not_start_while_scanning(self):
        entered, release = threading.Event(), threading.Event()

        def slow_locations(db, include_disabled=False):
            entered.set()
            release.wait(WAIT)
            return [], []
        with mock.patch.object(autoscan, "build_locations", side_effect=slow_locations):
            t, box = run_in_thread(lambda: autoscan.task_autoscan(self.env.db, media.FFmpegTools(None, None),
                                                                  autoscan.Controller(), lambda p: None))
            self.assertTrue(entered.wait(WAIT))
            results = trash.move_to_trash(self.env.db, [self.env.ids[0]], lambda p: self.fail("移動してはいけません"))
            release.set()
            t.join(WAIT)
        self.assertIn("調査中", results[0].message)
        self.assertTrue(os.path.exists(self.env.path(self.env.ids[0])))
        self.assertFalse(self.env.db.scan_active)
        self.assertFalse(self.env.db.organize_active)

    def test_scan_and_organize_start_simultaneously(self):
        """調査と整理をバリアで同時に始めても、両方が始まることはない（100 回）。"""
        for i in range(REPEAT):
            with self.subTest(round=i):
                barrier = threading.Barrier(2)
                got: dict = {}

                def take(kind):
                    barrier.wait(WAIT)
                    got[kind] = self.env.db.begin_activity(kind)
                threads = [threading.Thread(target=take, args=(k,)) for k in ("scan", "organize")]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(WAIT)
                winners = [k for k, tok in got.items() if tok is not None]
                self.assertEqual(len(winners), 1)
                self.env.db.end_activity(got[winners[0]])
                self.assertFalse(self.env.db.scan_active or self.env.db.organize_active)

    def test_second_scan_is_refused_while_scanning(self):
        token = self.env.db.begin_activity("scan")
        try:
            summary = autoscan.task_autoscan(self.env.db, media.FFmpegTools(None, None), autoscan.Controller(),
                                             lambda p: None)
        finally:
            self.env.db.end_activity(token)
        self.assertTrue(summary["cancelled"])
        self.assertIn("調査中", summary["error"])
        self.assertFalse(self.env.db.scan_active)


if __name__ == "__main__":
    unittest.main()
