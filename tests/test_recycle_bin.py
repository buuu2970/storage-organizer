"""Windows のごみ箱への移動（recycle_bin）の安全確認。OS への問い合わせはすべて差し替えて確かめる。

実際のごみ箱は使わない（実機での確認は tools/recycle_bin_live_test.py）。どの環境でも通る。
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from storage_organizer import recycle_bin as rb
from storage_organizer import trash

RECYCLE = rb.TSF_DELETE_RECYCLE_IF_POSSIBLE
NUKE_FLAGS = 0x202  # 「ごみ箱へ移す」指定が無い（Windows が完全削除しようとしている）
REAL_FLAGS = 0x282  # 実機で観測した値（TSF_DELETE_RECYCLE_IF_POSSIBLE を含む）


def fake_stat(size=1000, attrs=0, mode=stat.S_IFREG | 0o644):
    return SimpleNamespace(st_mode=mode, st_size=size, st_file_attributes=attrs)


class DecideFromSinkTest(unittest.TestCase):
    def test_success_only_when_everything_confirms(self):
        for hr in (rb.S_OK, rb.COPYENGINE_S_DONT_PROCESS_CHILDREN):
            ok, reason = rb.decide_from_sink(REAL_FLAGS, hr, True, False, False)
            self.assertTrue(ok, reason)

    def test_permanent_delete_intent_is_failure(self):
        ok, reason = rb.decide_from_sink(NUKE_FLAGS, rb.E_ABORT, False, False, True)
        self.assertFalse(ok)
        self.assertIn("完全削除しようとした", reason)
        self.assertIn("削除していません", reason)

    def test_permanent_delete_intent_wins_even_if_post_reports_success(self):
        ok, _ = rb.decide_from_sink(NUKE_FLAGS, rb.S_OK, True, False, False)
        self.assertFalse(ok)

    def test_missing_pre_delete(self):
        self.assertFalse(rb.decide_from_sink(None, rb.S_OK, True, False, False)[0])

    def test_missing_post_delete(self):
        ok, reason = rb.decide_from_sink(REAL_FLAGS, None, False, False, False)
        self.assertFalse(ok)
        self.assertIn("確認できません", reason)

    def test_error_hresult(self):
        for hr in (rb.E_ABORT, -2147024891, 0x00270001):  # E_ABORT / E_ACCESSDENIED / 想定外の成功コード
            self.assertFalse(rb.decide_from_sink(REAL_FLAGS, hr, True, False, False)[0], hex(hr))

    def test_no_item_created_in_recycle_bin(self):
        ok, reason = rb.decide_from_sink(REAL_FLAGS, rb.S_OK, False, False, False)
        self.assertFalse(ok)
        self.assertIn("ごみ箱", reason)

    def test_aborted(self):
        self.assertFalse(rb.decide_from_sink(REAL_FLAGS, rb.S_OK, True, True, False)[0])

    def test_still_exists(self):
        ok, reason = rb.decide_from_sink(REAL_FLAGS, rb.S_OK, True, False, True)
        self.assertFalse(ok)
        self.assertIn("残っています", reason)


class PrecheckTest(unittest.TestCase):
    """移動前の確認。どれか1つでも満たさなければ、Windows の移動処理を呼ばない。"""

    def setUp(self):
        self.path = os.path.abspath("recsort_dummy_video.mp4")
        self.patches = {
            "_lstat": mock.patch.object(rb, "_lstat", return_value=fake_stat()),
            "_realpath": mock.patch.object(rb, "_realpath", side_effect=lambda p: p),
            "_volume_root": mock.patch.object(rb, "_volume_root", return_value="C:\\"),
            "_drive_type": mock.patch.object(rb, "_drive_type", return_value=rb.DRIVE_FIXED),
            "_query_recycle_bin": mock.patch.object(rb, "_query_recycle_bin", return_value=None),
            "_volume_guid": mock.patch.object(rb, "_volume_guid", return_value="{guid}"),
            "_recycle_settings": mock.patch.object(rb, "_recycle_settings", return_value={}),
        }
        self.mocks = {k: p.start() for k, p in self.patches.items()}
        self.addCleanup(mock.patch.stopall)

    def refused(self, *words):
        with self.assertRaises(trash.TrashError) as cm:
            rb.precheck(self.path)
        for w in words:
            self.assertIn(w, str(cm.exception))
        self.assertIn("削除していません", str(cm.exception))
        return str(cm.exception)

    def test_ok(self):
        self.assertEqual(rb.precheck(self.path), os.path.normpath(self.path))

    def test_relative_path_refused(self):
        with self.assertRaises(trash.TrashError):
            rb.precheck("relative.mp4")

    def test_missing_refused(self):
        self.mocks["_lstat"].side_effect = FileNotFoundError(2, "見つかりません")
        self.refused("見つかりません")

    def test_directory_refused(self):
        self.mocks["_lstat"].return_value = fake_stat(mode=stat.S_IFDIR | 0o755)
        self.refused("通常のファイルではない")

    def test_reparse_point_refused(self):
        self.mocks["_lstat"].return_value = fake_stat(attrs=rb.FILE_ATTRIBUTE_REPARSE_POINT)
        self.refused("リンク")

    def test_symlink_refused(self):
        self.mocks["_lstat"].return_value = fake_stat(mode=stat.S_IFLNK | 0o777)
        self.refused("リンク")

    def test_cloud_only_refused(self):
        for attr in (rb.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS, rb.FILE_ATTRIBUTE_RECALL_ON_OPEN,
                     rb.FILE_ATTRIBUTE_OFFLINE):
            self.mocks["_lstat"].return_value = fake_stat(attrs=attr)
            self.refused("オンライン専用")

    def test_removable_network_and_unknown_drives_refused(self):
        for kind, word in ((2, "取り外し可能"), (4, "ネットワーク"), (0, "分からない"), (5, "CD")):
            self.mocks["_drive_type"].return_value = kind
            self.refused(word, "完全に削除")

    def test_link_target_on_network_drive_refused(self):
        # 固定ドライブ上のパスでも、実体（ジャンクション先など）がネットワーク上なら止める
        self.mocks["_realpath"].side_effect = lambda p: "\\\\server\\share\\x.mp4"
        self.mocks["_volume_root"].side_effect = lambda p: "\\\\server\\share\\" if p.startswith("\\\\") else "C:\\"
        self.mocks["_drive_type"].side_effect = lambda r: 4 if r.startswith("\\\\") else rb.DRIVE_FIXED
        self.refused("ネットワーク")

    def test_drive_type_error_refused(self):
        self.mocks["_drive_type"].side_effect = OSError("失敗")
        self.refused("ドライブの種類")

    def test_no_recycle_bin_refused(self):
        self.mocks["_query_recycle_bin"].side_effect = OSError("no bin")
        self.refused("ごみ箱を利用できない")

    def test_nuke_on_delete_refused(self):
        self.mocks["_recycle_settings"].return_value = {"nuke": 1}
        self.refused("すぐに削除")

    def test_global_nuke_on_delete_refused(self):
        self.mocks["_recycle_settings"].return_value = {"global_nuke": 1}
        self.refused("すぐに削除")

    def test_volume_setting_overrides_global(self):
        self.mocks["_recycle_settings"].return_value = {"global_nuke": 1, "nuke": 0}
        rb.precheck(self.path)

    def test_policy_no_recycle_refused(self):
        self.mocks["_recycle_settings"].return_value = {"policy_no_recycle": 1}
        self.refused("ポリシー")

    def test_larger_than_max_capacity_refused(self):
        self.mocks["_lstat"].return_value = fake_stat(size=3 * 1024 * 1024)
        self.mocks["_recycle_settings"].return_value = {"max_capacity_mb": 2}
        self.refused("最大サイズ")

    def test_within_max_capacity_ok(self):
        self.mocks["_lstat"].return_value = fake_stat(size=2 * 1024 * 1024)
        self.mocks["_recycle_settings"].return_value = {"max_capacity_mb": 2, "nuke": 0}
        rb.precheck(self.path)

    def test_registry_unreadable_is_not_fatal(self):
        # レジストリは早めの確認。読めなくても、移動時の確認（PreDeleteItem）で必ず止める
        self.mocks["_volume_guid"].side_effect = OSError("x")
        self.mocks["_recycle_settings"].side_effect = OSError("x")
        rb.precheck(self.path)


class FakeOperation:
    """IFileOperation の代わり。PerformOperations で sink の判定を呼び、指示どおりに振る舞う。"""

    def __init__(self, path, flags=REAL_FLAGS, post_hr=rb.COPYENGINE_S_DONT_PROCESS_CHILDREN, newly=True,
                 aborted=False, remove=True, call_pre=True, call_post=True, advise_error=None,
                 item_path=None):
        self.path, self.flags, self.post_hr, self.newly = path, flags, post_hr, newly
        self.aborted, self.remove, self.call_pre, self.call_post = aborted, remove, call_pre, call_post
        self.advise_error, self.item_path = advise_error, item_path or path
        self.state = None
        self.calls = []
        self.op_flags = None

    def SetOperationFlags(self, flags):
        self.op_flags = flags

    def Advise(self, sink):
        self.calls.append("Advise")
        if self.advise_error:
            raise self.advise_error
        self.state = sink
        return 7

    def Unadvise(self, cookie):
        self.calls.append("Unadvise")

    def DeleteItem(self, item, sink):
        self.calls.append("DeleteItem")

    def PerformOperations(self):
        self.calls.append("PerformOperations")
        go = True
        if self.call_pre:
            go = rb._on_pre_delete(self.state, self.flags, self.item_path)
        if go and self.remove:
            Path(self.path).unlink()  # 試験用の一時ファイル（ごみ箱へ移ったことの代わり）
        if self.call_post:
            self.state.post.append((self.post_hr if go else rb.E_ABORT, self.newly and go))

    def GetAnyOperationsAborted(self):
        return self.aborted


class PerformTest(unittest.TestCase):
    """IFileOperation の結果の扱い（COM は使わず、同じ手順を模擬する）。"""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.file = Path(d.name) / "recsort_dummy.mp4"
        self.file.write_bytes(b"x" * 100)
        self.path = str(self.file)

    def run_op(self, **kw):
        fo = FakeOperation(self.path, **kw)
        with mock.patch.object(rb, "_create_file_operation", return_value=fo), \
                mock.patch.object(rb, "_create_item", return_value=object()), \
                mock.patch.object(rb, "_make_sink", side_effect=lambda state: state):
            try:
                rb._perform(self.path)
                return fo, None
            except trash.TrashError as e:
                return fo, e

    def test_recycled(self):
        fo, err = self.run_op()
        self.assertIsNone(err)
        self.assertFalse(self.file.exists())
        self.assertEqual(fo.op_flags, rb.OPERATION_FLAGS)
        self.assertEqual(fo.calls, ["Advise", "DeleteItem", "PerformOperations", "Unadvise"])

    def test_permanent_delete_intent_aborts_and_keeps_file(self):
        fo, err = self.run_op(flags=NUKE_FLAGS)
        self.assertIsNotNone(err)
        self.assertIn("完全削除しようとした", str(err))
        self.assertTrue(self.file.exists())  # 削除の前に止めた
        self.assertTrue(rb.last_result()["pre_flags"] == [NUKE_FLAGS])

    def test_other_item_aborts(self):
        fo, err = self.run_op(item_path=self.path + ".other")
        self.assertIsNotNone(err)
        self.assertTrue(self.file.exists())

    def test_missing_post_delete_fails(self):
        fo, err = self.run_op(call_post=False)
        self.assertIsNotNone(err)

    def test_missing_pre_delete_fails(self):
        fo, err = self.run_op(call_pre=False, remove=False)
        self.assertIsNotNone(err)
        self.assertTrue(self.file.exists())

    def test_no_newly_created_item_fails(self):
        fo, err = self.run_op(newly=False)
        self.assertIsNotNone(err)

    def test_aborted_fails(self):
        fo, err = self.run_op(aborted=True)
        self.assertIsNotNone(err)

    def test_still_exists_fails(self):
        fo, err = self.run_op(remove=False)
        self.assertIsNotNone(err)
        self.assertIn("残っています", str(err))

    def test_error_hresult_fails(self):
        fo, err = self.run_op(post_hr=-2147024891, remove=False)
        self.assertIsNotNone(err)

    def test_advise_failure_refuses_without_deleting(self):
        fo, err = self.run_op(advise_error=OSError("advise failed"))
        self.assertIsNotNone(err)
        self.assertIn("安全確認を準備できなかった", str(err))
        self.assertNotIn("DeleteItem", fo.calls)
        self.assertNotIn("PerformOperations", fo.calls)
        self.assertTrue(self.file.exists())

    def test_sink_creation_failure_refuses_without_deleting(self):
        fo = FakeOperation(self.path)
        with mock.patch.object(rb, "_create_file_operation", return_value=fo), \
                mock.patch.object(rb, "_create_item", return_value=object()), \
                mock.patch.object(rb, "_make_sink", side_effect=RuntimeError("no sink")):
            with self.assertRaises(trash.TrashError):
                rb._perform(self.path)
        self.assertEqual(fo.calls, [])
        self.assertTrue(self.file.exists())

    def test_file_operation_unavailable_refuses(self):
        with mock.patch.object(rb, "_create_file_operation", side_effect=OSError("no COM")):
            with self.assertRaises(trash.TrashError):
                rb._perform(self.path)
        self.assertTrue(self.file.exists())


class FlagsAndPlatformTest(unittest.TestCase):
    def test_operation_flags(self):
        f = rb.OPERATION_FLAGS
        for need in (rb.FOF_ALLOWUNDO, rb.FOF_NOCONFIRMATION, rb.FOF_SILENT, rb.FOF_NOERRORUI,
                     rb.FOFX_EARLYFAILURE, rb.FOFX_RECYCLEONDELETE):
            self.assertTrue(f & need, hex(need))
        self.assertFalse(f & 0x4000)  # FOF_WANTNUKEWARNING（完全削除の確認ダイアログ）は使わない

    def test_non_windows_refused(self):
        with mock.patch.object(rb.sys, "platform", "linux"):
            self.assertIn("Windows", rb.available())
            with self.assertRaises(trash.TrashError):
                rb.backend()("/tmp/x.mp4")
            with self.assertRaises(trash.TrashError):
                trash.default_backend()("/tmp/x.mp4")

    def test_without_pywin32_refused(self):
        with mock.patch.object(rb.sys, "platform", "win32"), \
                mock.patch.object(rb, "_IMPORT_ERROR", ImportError("No module named 'pythoncom'")):
            self.assertIn("pywin32", rb.available())
            with mock.patch.object(rb, "precheck", side_effect=AssertionError("呼ばれてはいけない")):
                with self.assertRaises(trash.TrashError) as cm:
                    trash.default_backend()(os.path.abspath("x.mp4"))
            self.assertIn("pywin32", str(cm.exception))

    def test_trash_error_is_shared(self):
        self.assertIs(trash.TrashError, rb.TrashError)

    def test_no_permanent_delete_in_source(self):
        root = Path(rb.__file__).parent
        forbidden = [r"os\.remove", r"os\.unlink", r"shutil\.rmtree", r"SHFileOperation", r"FOF_WANTNUKEWARNING",
                     r"send2trash", r"\.unlink\(", r"os\.rmdir", r"DeleteFile"]
        for name in ("recycle_bin.py", "trash.py"):
            src = (root / name).read_text(encoding="utf-8")
            for pat in forbidden:
                self.assertIsNone(re.search(pat, src), f"{name} に {pat} が含まれています")


if __name__ == "__main__":
    unittest.main()
