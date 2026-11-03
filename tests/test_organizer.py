"""録画・画像の整理支援（自動スキャン・候補判定・ごみ箱への移動・整理画面）のテスト。

実ファイルの削除は行わない。ごみ箱への移動はモックで確認する。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if sys.platform.startswith("linux"):
    os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from storage_organizer import autoscan, candidates, media, trash  # noqa: E402
from storage_organizer.db import Database  # noqa: E402
from storage_organizer.formatting import format_size  # noqa: E402

FFMPEG = media.find_ffmpeg()


def snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def make_photo(path: Path, size=(800, 600), color=(30, 120, 200), exif_date: str | None = None, **kw):
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse((w // 4, h // 4, w * 3 // 4, h * 3 // 4), fill=(250, 200, 0))
    d.rectangle((0, 0, w // 5, h // 5), fill=(200, 30, 30))
    path.parent.mkdir(parents=True, exist_ok=True)
    if exif_date:
        exif = img.getexif()
        exif.get_ifd(0x8769)[36867] = exif_date
        img.save(path, exif=exif, **kw)
    else:
        img.save(path, **kw)
    return img


def make_video(path: Path, src: str, seconds: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG.ffmpeg, "-v", "error", "-f", "lavfi", "-i", src, "-t", str(seconds),
                    "-pix_fmt", "yuv420p", str(path)], check=True)


class Env:
    """仮のユーザーフォルダ（ピクチャ・ビデオ・ダウンロード）と内蔵ドライブを用意する。"""

    def __init__(self, with_videos: bool = True) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.drive = base / "drive"
        self.data = base / "data"
        pics, vids, dl = self.home / "Pictures", self.home / "Videos", self.home / "Downloads"
        for d in (pics, vids, dl, self.drive):
            d.mkdir(parents=True)
        img = make_photo(pics / "photo.jpg", exif_date="2024:05:01 10:20:30", quality=95)
        img.resize((400, 300)).save(pics / "photo_small.jpg", quality=80)             # 縮小版
        img.filter(ImageFilter.GaussianBlur(1)).save(pics / "photo_edit.jpg", quality=90)  # 軽い編集
        shutil.copyfile(pics / "photo.jpg", dl / "photo_copy.jpg")                   # 完全一致
        other = Image.new("RGB", (640, 480), (10, 200, 60))  # 構図の違う画像
        od = ImageDraw.Draw(other)
        for x in range(0, 640, 80):
            od.rectangle((x, 0, x + 30, 480), fill=(240, 240, 240))
        od.polygon([(0, 480), (320, 100), (640, 480)], fill=(20, 20, 120))
        other.save(pics / "other.png")
        Image.new("RGB", (300, 300), (128, 0, 128)).save(pics / "noexif.png")
        (pics / "trunc.jpg").write_bytes((pics / "photo.jpg").read_bytes()[:2000])   # 途中で切れた画像
        (pics / "empty.jpg").write_bytes(b"")
        (pics / "notimage.jpg").write_bytes(b"hello" * 100)
        (pics / "same_name").mkdir()
        make_photo(pics / "same_name" / "photo.jpg", color=(90, 90, 90), size=(500, 500))  # 名前だけ同じ
        make_photo(pics / "AppData" / "cache.jpg")                                    # 除外
        make_photo(pics / ".hidden" / "h.jpg")                                        # 隠しフォルダ
        make_photo(pics / "excluded_by_user" / "x.jpg")
        os.symlink(self.home, pics / "loop", target_is_directory=True)               # 循環リンク
        # 内蔵ドライブ（ドライブ全体の調査）: アプリのフォルダと小さな画像は除外する
        make_photo(self.drive / "Media" / "drive_photo.jpg", size=(1200, 900))
        (self.drive / "SomeApp").mkdir()
        (self.drive / "SomeApp" / "app.exe").write_bytes(b"MZ")
        make_photo(self.drive / "SomeApp" / "logo.png")
        Image.new("RGB", (16, 16), (1, 2, 3)).save(self.drive / "Media" / "icon.png")
        (self.drive / "Windows").mkdir()
        make_photo(self.drive / "Windows" / "wall.jpg")
        if with_videos and FFMPEG.available:
            make_video(vids / "dark.mp4", "color=c=black:s=320x180:r=30", 5)
            make_video(vids / "short.mp4", "testsrc2=s=320x180:r=30", 1.5)
            make_video(vids / "normal.mp4", "testsrc2=s=320x180:r=30", 5)
            (vids / "broken.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 500)
        self.patches = [
            mock.patch.object(autoscan, "known_folders", lambda: [
                ("ピクチャ", str(pics)), ("ビデオ", str(vids)), ("ダウンロード", str(dl))]),
            mock.patch.object(autoscan, "local_drives", lambda: [
                {"root": str(self.drive), "kind": "internal"}, {"root": "X:\\", "kind": "external"},
                {"root": "N:\\", "kind": "network"}]),
        ]
        for p in self.patches:
            p.start()
        db = Database(self.data)
        db.set_json_list("excluded_folders", [str(pics / "excluded_by_user")])
        db.close()

    def run(self, ctl=None, report=None):
        return autoscan.task_autoscan(self.data, FFMPEG, ctl or autoscan.Controller(), report or (lambda p: None))

    def close(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class AutoScanTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.before = snapshot(self.env.home) | snapshot(self.env.drive)

    def tearDown(self):
        self.assertEqual(snapshot(self.env.home) | snapshot(self.env.drive), self.before, "元ファイルが変更されました")
        self.env.close()

    def names(self, db):
        return {r["name"] for r in db.media()}

    def test_auto_detects_without_folder_selection(self):
        result = self.env.run()
        self.assertFalse(result["cancelled"])
        db = Database(self.env.data)
        names = self.names(db)
        self.assertTrue({"photo.jpg", "photo_copy.jpg", "other.png", "drive_photo.jpg"} <= names)
        # 優先: ユーザーフォルダ → 内蔵ドライブ。外付け・ネットワークは自動では調べない
        locs = [r["kind"] for r in db.conn.execute("SELECT kind FROM scan_locations ORDER BY priority")]
        self.assertEqual(locs, ["known", "known", "known", "drive"])
        self.assertEqual(len(db.json_list("not_scanned_drives")), 2)
        db.close()

    def test_exclusions_and_no_loops(self):
        self.env.run()
        db = Database(self.env.data)
        names = self.names(db)
        for excluded in ("cache.jpg", "h.jpg", "x.jpg", "logo.png", "icon.png", "wall.jpg"):
            self.assertNotIn(excluded, names)
        reasons = {Path(r["path"]).name: r["reason"] for r in db.conn.execute(
            "SELECT * FROM scan_skips WHERE kind='excluded'")}
        self.assertIn("AppData", reasons)
        self.assertIn("SomeApp", reasons)
        self.assertIn("excluded_by_user", reasons)
        self.assertIn("Windows", reasons)
        # 循環リンクをたどらず、同じファイルを二重に数えない
        paths = [r["path"] for r in db.media()]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertFalse(any("loop" in p for p in paths))
        db.close()

    def test_permission_error_is_skipped_and_reported(self):
        real = os.scandir
        blocked = str(self.env.home / "Pictures" / "same_name")

        def fake(path):
            if os.path.normpath(path) == os.path.normpath(blocked):
                raise PermissionError(13, "Permission denied", path)
            return real(path)
        with mock.patch.object(autoscan.os, "scandir", side_effect=fake):
            self.env.run()
        db = Database(self.env.data)
        errs = [r["path"] for r in db.conn.execute("SELECT * FROM scan_skips WHERE kind='error'")]
        self.assertEqual(errs, [blocked])
        status = db.conn.execute("SELECT status FROM scan_locations WHERE label='ピクチャ'").fetchone()[0]
        self.assertEqual(status, "一部スキップ")  # 「完了」とは表示しない
        db.close()

    def test_pause_resume_and_cancel(self):
        ctl = autoscan.Controller()
        events = []

        def report(p):
            events.append(p.get("phase"))
            if p.get("phase") == "scan" and not events.count("paused") and not ctl.paused:
                ctl.pause()
                threading.Timer(0.5, ctl.resume).start()
        t0 = time.monotonic()
        self.env.run(ctl, report)
        self.assertIn("paused", events)
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)
        ctl2 = autoscan.Controller()
        ctl2.cancel()
        result = self.env.run(ctl2)
        self.assertTrue(result["cancelled"])
        db = Database(self.env.data)
        self.assertIn("中断", [r[0] for r in db.conn.execute("SELECT status FROM scan_locations")])
        db.close()

    def test_progress_has_no_fake_percentage_during_scan(self):
        reports = []
        self.env.run(report=reports.append)
        scan = [r for r in reports if r.get("phase") == "scan"]
        self.assertTrue(scan)
        self.assertTrue(all("total" not in r for r in scan))
        self.assertTrue(all({"location", "current", "found", "bytes"} <= set(r) for r in scan))

    def test_incremental_rescan(self):
        self.env.run()
        calls = {"hash": 0, "image": 0}
        real_hash, real_img = media.sha256_file, candidates.analyze_image

        def count_hash(*a, **k):
            calls["hash"] += 1
            return real_hash(*a, **k)

        def count_img(*a, **k):
            calls["image"] += 1
            return real_img(*a, **k)
        db = Database(self.env.data)
        pid = next(r["id"] for r in db.media() if r["name"] == "other.png")
        db.set_decision(pid, "保留")
        db.set_protected(next(r["id"] for r in db.media() if r["name"] == "photo.jpg"), True)
        db.close()
        with mock.patch.object(media, "sha256_file", count_hash), \
                mock.patch.object(candidates, "analyze_image", count_img):
            self.env.run()
        self.assertEqual(calls, {"hash": 0, "image": 0})  # 変更がなければやり直さない
        db = Database(self.env.data)
        self.assertEqual(db.video(pid)["decision"], "保留")  # 判断は次回も保持
        self.assertTrue(next(r for r in db.media() if r["name"] == "photo.jpg")["protected"])
        db.close()

    def test_cloud_only_files_are_not_read(self):
        target = str(self.env.home / "Downloads" / "photo_copy.jpg")
        real = autoscan.is_cloud_only
        opened = []
        real_open = open

        def spy_open(file, *a, **k):
            if str(file) == target:
                opened.append(file)
            return real_open(file, *a, **k)
        with mock.patch.object(autoscan, "is_cloud_only",
                               side_effect=lambda st: st.st_size == os.path.getsize(target) or real(st)), \
                mock.patch("builtins.open", spy_open):
            self.env.run()
        db = Database(self.env.data)
        row = next(r for r in db.media() if r["path"] == target)
        self.assertTrue(row["cloud_only"])
        self.assertEqual(row["light_state"], "対象外")
        self.assertIsNone(row["sha256"])
        self.assertEqual(opened, [])
        db.close()


class CandidateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env()
        cls.env.run()

    @classmethod
    def tearDownClass(cls):
        cls.env.close()

    def setUp(self):
        self.db = Database(self.env.data)
        self.ctx = candidates.Context(self.db)
        self.by = {r["name"]: r for r in self.db.media() if r["folder"] != str(self.env.home / "Pictures" / "same_name")}

    def tearDown(self):
        self.db.close()

    def test_exact_duplicates_not_by_name(self):
        groups = [{self.ctx.rows[i]["name"] for i in ids} for ids in self.ctx.dup_groups.values()]
        self.assertIn({"photo.jpg", "photo_copy.jpg"}, groups)
        same_name = [r for r in self.db.media() if r["name"] == "photo.jpg"]
        self.assertEqual(len(same_name), 2)
        self.assertEqual(sum(1 for r in same_name if r["id"] in self.ctx.dup_of), 1)  # 名前が同じだけでは重複にしない
        h = self.ctx.dup_of[self.by["photo.jpg"]["id"]]
        self.assertIn(self.ctx.dup_keep[h][0], self.ctx.dup_groups[h])

    def test_similar_images_are_distinct_from_exact(self):
        pid = self.by["photo.jpg"]["id"]
        g = self.ctx.sim_of[pid]
        names = {self.ctx.rows[i]["name"] for i in self.ctx.sim_groups[g]}
        self.assertTrue({"photo.jpg", "photo_small.jpg", "photo_edit.jpg"} <= names)
        self.assertNotIn("other.png", names)
        keep, why = self.ctx.sim_keep[g]
        best = max(candidates.pixels(self.ctx.rows[i]) for i in self.ctx.sim_groups[g])
        self.assertEqual(candidates.pixels(self.ctx.rows[keep]), best)  # 解像度が最も高いものを残すおすすめに
        self.assertIn("解像度", why)
        self.assertIn("大切さは判断していません", why)
        reasons = " ".join(self.ctx.reasons(self.by["photo_small.jpg"]))
        self.assertIn("類似画像", reasons)

    def test_failure_candidates(self):
        kinds = {n: [f["kind"] for f in candidates.failures(self.by[n])] for n in
                 ("trunc.jpg", "empty.jpg", "notimage.jpg")}
        self.assertEqual(kinds, {"trunc.jpg": ["corrupt"], "empty.jpg": ["corrupt"], "notimage.jpg": ["read_error"]})
        self.assertIn("破損とは限りません", candidates.failures(self.by["notimage.jpg"])[0]["reason"])
        if FFMPEG.available:
            self.assertEqual([f["kind"] for f in candidates.failures(self.by["short.mp4"])], ["short"])
            self.assertEqual([f["kind"] for f in candidates.failures(self.by["dark.mp4"])], ["dark"])
            self.assertEqual([f["kind"] for f in candidates.failures(self.by["broken.mp4"])], ["corrupt"])
            self.assertEqual(candidates.failures(self.by["normal.mp4"]), [])
            self.assertAlmostEqual(candidates.duration(self.by["normal.mp4"]), 5, delta=0.2)
            self.assertGreater(candidates.per_minute(self.by["normal.mp4"]), 0)
            reasons = " ".join(self.ctx.reasons(self.by["dark.mp4"]))
            self.assertIn("可能性", reasons)
            self.assertNotIn("不要", reasons)

    def test_dates_unknown_are_not_replaced(self):
        self.assertEqual(candidates.format_date(self.by["photo.jpg"]["taken_at"]), "2024-05-01 10:20")
        self.assertIsNone(self.by["noexif.png"]["taken_at"])  # 更新日時で代用しない
        self.assertEqual(candidates.format_date(self.by["noexif.png"]["taken_at"]), "不明")

    def test_categories(self):
        self.assertTrue(candidates.category_ids(self.ctx, "録画確認候補"))
        self.assertEqual(len(candidates.category_ids(self.ctx, "すべて")), len(self.ctx.rows))
        self.assertEqual(candidates.category_ids(self.ctx, "削除候補"), [])  # 初期状態では何も選ばない

    def test_target_proposal(self):
        pool = [r for r in self.ctx.rows.values() if self.ctx.is_suggestable(r)]
        self.assertTrue(pool)
        small = min(r["size"] for r in pool)
        prop = candidates.propose_for_target(self.ctx, small)
        self.assertEqual(prop["shortage"], 0)
        self.assertGreaterEqual(prop["total"], small)
        big = candidates.propose_for_target(self.ctx, 10 ** 12)
        self.assertEqual(set(big["ids"]), {r["id"] for r in pool})  # 足りなくても他を足さない
        self.assertGreater(big["shortage"], 0)
        names = {self.ctx.rows[i]["name"] for i in big["ids"]}
        self.assertNotIn("other.png", names)  # 理由のないファイルは提案しない
        keep = self.ctx.sim_keep[self.ctx.sim_of[self.by["photo.jpg"]["id"]]][0]
        self.assertNotIn(keep, big["ids"])


class TrashTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(with_videos=False)
        self.env.run()
        self.db = Database(self.env.data)
        self.by = {r["path"]: r for r in self.db.media()}
        self.moved = []

    def tearDown(self):
        self.db.close()
        self.env.close()

    def backend(self, path):  # 実際には削除せず、移動したことにするモック
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def pid(self, rel):
        return self.by[str(self.env.home / rel)]["id"]

    def test_moves_only_checked_and_verified_files(self):
        pid = self.pid("Pictures/photo_small.jpg")
        results = trash.move_to_trash(self.db, [pid], self.backend)
        self.assertTrue(results[0].ok)
        self.assertEqual(len(self.moved), 1)
        self.assertIsNotNone(self.db.video(pid)["trashed_at"])
        self.assertNotIn(pid, [r["id"] for r in self.db.media()])

    def test_changed_file_is_not_moved(self):
        path = self.env.home / "Pictures" / "other.png"
        pid = self.pid("Pictures/other.png")
        with open(path, "ab") as f:
            f.write(b"changed")
        results = trash.move_to_trash(self.db, [pid], self.backend)
        self.assertFalse(results[0].ok)
        self.assertIn("変更", results[0].message)
        self.assertEqual(self.moved, [])

    def test_protected_and_cloud_are_not_moved(self):
        a, b = self.pid("Pictures/other.png"), self.pid("Pictures/noexif.png")
        self.db.set_protected(a, True)
        self.db.conn.execute("UPDATE videos SET cloud_only=1 WHERE id=?", (b,))
        self.db.conn.commit()
        results = trash.move_to_trash(self.db, [a, b], self.backend)
        self.assertEqual([r.ok for r in results], [False, False])
        self.assertEqual(self.moved, [])
        with self.assertRaises(PermissionError):
            self.db.set_decision(a, "削除候補")

    def test_keeps_one_of_duplicates(self):
        ids = [self.pid("Pictures/photo.jpg"), self.pid("Downloads/photo_copy.jpg")]
        results = trash.move_to_trash(self.db, ids, self.backend)
        self.assertEqual(sum(r.ok for r in results), 1)
        self.assertEqual(len(self.moved), 1)

    def test_failure_does_not_fall_back_to_delete(self):
        pid = self.pid("Pictures/other.png")
        path = self.env.home / "Pictures" / "other.png"

        def failing(p):
            raise trash.TrashError("ごみ箱がありません")
        with mock.patch.object(os, "remove", side_effect=AssertionError("完全削除しようとしました")), \
                mock.patch.object(os, "unlink", side_effect=AssertionError("完全削除しようとしました")):
            results = trash.move_to_trash(self.db, [pid], failing)
        self.assertFalse(results[0].ok)
        self.assertTrue(path.exists())
        self.assertIsNone(self.db.video(pid)["trashed_at"])
        log = self.db.conn.execute("SELECT result FROM trash_log").fetchall()
        self.assertEqual([r[0] for r in log], ["error"])

    def test_default_backend_outside_windows_reports_error(self):
        if sys.platform == "win32":
            self.skipTest("Windows ではモックを使って確認する")
        pid = self.pid("Pictures/other.png")
        results = trash.move_to_trash(self.db, [pid])
        self.assertFalse(results[0].ok)
        self.assertTrue((self.env.home / "Pictures" / "other.png").exists())


try:
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    QApplication = None


def wait_until(pred, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class OrganizerGuiTest(unittest.TestCase):
    """改修した UI（ホーム・ファイル一覧・整理のおすすめ・詳細パネル・ステータス欄）。"""

    @classmethod
    def setUpClass(cls):
        from storage_organizer import theme
        cls.app = QApplication.instance() or QApplication([])
        theme.apply(cls.app)

    def setUp(self):
        from storage_organizer.main_window import MainWindow
        self.env = Env()
        self.messages = []
        ans = lambda *a, **k: (self.messages.append(a[1:3]) or QMessageBox.StandardButton.Yes)  # noqa: E731
        self.mb = [mock.patch.object(QMessageBox, n, staticmethod(ans)) for n in ("information", "warning", "question")]
        for p in self.mb:
            p.start()
        self.w = MainWindow(Database(self.env.data), start_autoscan=True)
        self.w.resize(1280, 800)
        self.w.show()
        self.state = self.w.state

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for p in self.mb:
            p.stop()
        self.env.close()

    def scan(self):
        self.w.start_scan()
        self.assertTrue(wait_until(lambda: self.state.runner.is_running(), 10))
        self.assertTrue(wait_until(lambda: not self.state.runner.is_running(), 120))
        QCoreApplication.processEvents()

    def select(self, browser, name):
        i = next(i for i, it in enumerate(browser.model.items) if it["name"] == name)
        browser.view.setCurrentIndex(browser.model.index(i, 0))
        QCoreApplication.processEvents()

    def test_navigation_has_five_sections_and_reaches_all_pages(self):
        w = self.w
        self.assertEqual([w.nav.item(i).data(0x0100) for i in range(w.nav.count())],
                         ["ホーム", "整理候補", "ファイル一覧", "その他の整理", "設定", "動画AI（試作）"])
        self.assertTrue(w.nav.item(5).isHidden())  # 動画AI（試作）は通常メニューに出さない
        self.assertFalse(w.ask_initial_learning)
        for page in (w.dashboard, w.video_list, w.initial, w.unclassified, w.curation, w.review,
                     w.genre_page, w.duplicates, w.compress, w.scan_page, w.settings):
            w.show_page(page)
            QCoreApplication.processEvents()
            self.assertTrue(page.isVisible(), f"{type(page).__name__} に到達できません")
        for name in ("重複", "類似画像", "録画確認候補", "大容量", "削除候補"):
            w.show_category(name)
            self.assertIs(w.stack.currentWidget(), w.recommend)
            self.assertEqual(w.recommend.category, name)
        for name, state in (("保護", "保護中"), ("保留", "保留"), ("すべて", "すべての判断")):
            w.show_category(name)
            self.assertIs(w.stack.currentWidget(), w.files)
            self.assertEqual(w.files.state_filter.currentText(), state)

    def test_first_run_home_and_empty_states(self):
        w = self.w
        self.assertFalse(self.state.runner.is_running())  # 初回は自動で始めず、調べる場所を選んでもらう
        self.assertEqual(w.home.primary.text(), "調べる場所を選ぶ")
        self.assertEqual(w.home.total.value.text(), "未調査")
        self.assertEqual(w.home.saving.value.text(), "算出できません")
        self.assertIn("実際の値", w.home.free.detail.text())
        w.show_section(w.files)
        self.assertIs(w.files.content.currentWidget(), w.files.empty)
        self.assertEqual(w.files.empty.action.text(), "調べる場所を選ぶ")
        self.assertIn("まだ調査していません", w.scan_status.text.text())
        self.assertFalse(w.scan_status.pause.isVisible())
        self.assertTrue(w.scan_status.rescan.isVisible())

    def test_locations_dialog_controls_scan_targets(self):
        from storage_organizer.organizer import LocationsDialog
        dlg = LocationsDialog(self.w.ctx.db, self.w)
        self.assertEqual(dlg.list.count(), 4)
        for i in range(dlg.list.count()):
            if "ダウンロード" in dlg.list.item(i).text():
                dlg.list.item(i).setCheckState(Qt.CheckState.Unchecked)
        dlg.save()
        self.scan()
        names = {r["name"] for r in self.w.ctx.db.media()}
        self.assertNotIn("photo_copy.jpg", names)  # チェックを外した場所は調べない
        self.assertIn("photo.jpg", names)

    def test_home_after_scan(self):
        self.scan()
        home = self.w.home
        total = sum(r["size"] for r in self.state.ctx.rows.values() if r["kind"] == "video")
        self.assertEqual(home.primary.text(), "整理候補を見る")
        self.assertEqual(home.total.value.text(), format_size(total))  # 録画（動画）だけの合計
        self.assertIn("ドライブ全体の使用量とは別", home.total.detail.text())
        self.assertNotEqual(home.saving.value.text(), "算出できません")
        home.target.setValue(3.5)
        home.primary.click()
        self.assertIs(self.w.stack.currentWidget(), self.w.plan)
        self.assertEqual(self.w.plan.target.value(), 3.5)
        self.assertIn("調査完了", self.w.scan_status.text.text())

    def test_files_detail_selection_bar_and_views(self):
        self.scan()
        w = self.w
        w.show_section(w.files)
        f = w.files
        self.assertFalse(f.detail.isVisible())  # 未選択のときは詳細を出さない
        self.assertFalse(f.selection_bar.isVisible())
        self.select(f, "noexif.png")
        self.assertTrue(f.detail.isVisible())
        self.assertTrue(f.selection_bar.isVisible())
        self.assertEqual(f.detail.title.text(), "noexif.png")
        self.assertIn("1 件を選択", f.selection_bar.count.text())
        # 判断（削除候補にしても削除しない）
        f.selection_bar.delete.click()
        mid = next(r["id"] for r in w.ctx.db.media() if r["name"] == "noexif.png")
        self.assertEqual(w.ctx.db.video(mid)["decision"], "削除候補")
        self.assertTrue(os.path.exists(w.ctx.db.video(mid)["path"]))
        self.assertNotEqual(f.selection_bar.delete.property("kind"), "danger")  # 赤はごみ箱だけ
        f.selection_bar.protect.click()
        self.assertTrue(w.ctx.db.video(mid)["protected"])
        self.assertIsNone(w.ctx.db.video(mid)["decision"])  # 保護すると削除候補から外れる
        # 複数選択
        f.view.selectAll()
        QCoreApplication.processEvents()
        self.assertEqual(f.detail.stack.currentIndex(), 1)
        self.assertIn(f"{len(f.model.items)} 件を選択", f.detail.title.text())
        # カード表示とリスト表示の切り替え（選択を保つ）
        self.select(f, "photo.jpg")
        f.list_button.click()
        self.assertIs(f.content.currentWidget(), f.table)
        self.assertEqual(len(f.selected_ids()), 1)
        f.grid_button.click()
        # 閉じる
        f.detail.closed.emit()
        self.assertFalse(f.detail.isVisible())
        self.assertFalse(f.selection_bar.isVisible())
        # 検索結果なし → 理由と次の操作
        f.search.setText("該当しない名前")
        self.assertIs(f.content.currentWidget(), f.empty)
        self.assertEqual(f.empty.action.text(), "条件をクリア")
        f.empty.action.click()
        self.assertEqual(f.search.text(), "")
        f.kind_filter.setCurrentIndex(2)
        self.assertTrue(all(it["kind"] == "video" for it in f.model.items))

    def test_recommend_groups_compare_and_trash(self):
        from storage_organizer import organizer
        self.scan()
        w = self.w
        w.show_category("重複")
        r = w.recommend
        self.assertEqual(r.model.rowCount(), 1)
        r.view.setCurrentIndex(r.model.index(0, 0))
        QCoreApplication.processEvents()
        self.assertFalse(r.selection_bar.delete.isVisible())  # グループ全体を一度に削除候補にしない
        self.assertIs(r.detail.stack.currentWidget(), r.detail.compare_scroll)
        self.assertTrue(wait_until(lambda: all(lb.pixmap() is not None and not lb.pixmap().isNull()
                                               for lb in r.detail.compare.thumbs.values()), 20))
        r.detail.compare.keep_others_button.click()
        decisions = sorted(row["decision"] or "" for row in w.ctx.db.media()
                           if row["id"] in self.state.ctx.dup_of)
        self.assertEqual(decisions, ["削除候補", "残す"])
        w.show_category("大容量")
        self.assertIs(r.content.currentWidget(), r.empty)
        self.assertIn("候補はありません", r.empty.title.text())
        w.show_category("削除候補")
        self.assertTrue(r.trash_button.isVisible())
        self.assertEqual(r.trash_button.property("kind"), "danger")
        ids = [row["id"] for row in w.ctx.db.media() if row["decision"] == "削除候補"]
        moved = []

        def fake_backend(path):  # 実際には削除しない
            moved.append(path)
            os.rename(path, path + ".trashed-by-test")
        dlg = organizer.TrashDialog(w.ctx.db, ids, w, backend=fake_backend)
        self.assertIn(f"{len(ids)} 件", dlg.summary.text())
        dlg.execute()
        self.assertEqual(len(moved), 1)
        w.show_category("類似画像")
        self.assertGreaterEqual(r.model.rowCount(), 1)

    def test_target_dialog(self):
        from storage_organizer import organizer
        self.scan()
        dlg = organizer.TargetDialog(self.w.recommend)
        dlg.target.setValue(0.0001)
        dlg.propose()
        self.assertGreater(dlg.table.rowCount(), 0)
        before = dlg.result.text()
        dlg.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
        self.assertNotEqual(dlg.result.text(), before)
        dlg.target.setValue(100000)
        dlg.propose()
        self.assertIn("不足", dlg.result.text())

    def test_status_while_scanning(self):
        w = self.w
        w.start_scan()
        self.assertTrue(wait_until(lambda: self.state.runner.is_running(), 10))
        QCoreApplication.processEvents()
        st = w.scan_status
        self.assertTrue(st.pause.isVisible() and st.cancel.isVisible())
        self.assertFalse(st.rescan.isEnabled())
        st.pause.click()
        self.assertTrue(self.state.runner.ctl.paused)
        self.assertEqual(st.pause.text(), "再開")
        self.assertIn("一時停止中", st.text.text())
        st.pause.click()
        st.cancel.click()
        self.assertTrue(wait_until(lambda: not self.state.runner.is_running(), 60))
        QCoreApplication.processEvents()
        self.assertFalse(st.pause.isVisible())
        self.assertTrue(st.rescan.isEnabled())

    def test_small_screens_keep_main_actions_reachable(self):
        self.scan()
        w = self.w
        for size in ((1093, 614), (911, 512)):  # 1366×768 の表示倍率 125% / 150%
            w.resize(*size)
            w.show_section(w.files)
            self.select(w.files, "photo.jpg")
            QCoreApplication.processEvents()
            bar = w.files.selection_bar
            for b in (bar.keep, bar.hold, bar.delete, bar.protect, bar.more):
                self.assertTrue(b.isVisible())
                self.assertGreaterEqual(b.width(), b.sizeHint().width() - 2, f"{b.text()} が切れています")
            self.assertTrue(w.scan_status.rescan.isVisible())
            self.assertLessEqual(w.files.detail.width(), w.files.width())


class PlanEngineTest(unittest.TestCase):
    """目標容量に合わせた録画の整理候補（ルールによる提案）。"""

    GB = 1024 ** 3

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "data")
        self.now = time.time()
        self.rows = {}

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def add(self, name, size, days_old=1, sha=None, kind="video", duration=600, failure=None):
        path = str(Path(self._tmp.name) / name)
        mtime = self.now - days_old * 86400
        self.db.upsert_media([(path, name, self._tmp.name, size, mtime, ".mp4", kind, False)], 1, self._tmp.name)
        mid = self.db.conn.execute("SELECT id FROM videos WHERE path=?", (path,)).fetchone()[0]
        self.db.update_video(mid, sha256=sha, meta={"duration": duration, "has_video": True},
                             failure=[failure] if failure else [])
        self.rows[name] = mid
        return mid

    def plan(self, target_gb, **kw):
        return candidates.plan_recordings(candidates.Context(self.db), int(target_gb * self.GB), now_ts=self.now, **kw)

    def names(self, plan):
        return {self.db.video(it["id"])["name"]: it for it in plan["items"]}

    def make_duplicates(self):
        self.add("a_original.mp4", 1 * self.GB, days_old=30, sha="A")
        self.add("a_copy1.mp4", 1 * self.GB, days_old=10, sha="A")
        self.add("a_copy2.mp4", 1 * self.GB, days_old=5, sha="A")
        self.add("b_original.mp4", 2 * self.GB, days_old=20, sha="B")
        self.add("b_copy.mp4", 2 * self.GB, days_old=2, sha="B")

    def test_duplicates_first_and_one_copy_always_kept(self):
        self.make_duplicates()
        self.add("big_old.mp4", 3 * self.GB, days_old=800)  # 要確認になりうるが、重複で足りるので出さない
        p = self.plan(2)
        names = self.names(p)
        self.assertTrue(p["achieved"])
        self.assertEqual({it["kind"] for it in p["items"]}, {"duplicate"})
        self.assertEqual(set(names), {"b_copy.mp4"})  # 目標に最も近く、超えすぎない
        all_dup = self.plan(4)
        names = set(self.names(all_dup))
        self.assertNotIn("a_original.mp4", names)  # 各グループで最も古い1本を残す
        self.assertNotIn("b_original.mp4", names)
        self.assertEqual(names, {"a_copy1.mp4", "a_copy2.mp4", "b_copy.mp4"})
        for it in all_dup["items"]:
            self.assertIn("SHA-256", it["reasons"][0])
            self.assertIsNotNone(it["keep_id"])

    def test_protected_kept_and_cloud_are_excluded(self):
        self.make_duplicates()
        self.db.set_protected(self.rows["a_copy1.mp4"], True)
        self.db.set_decision(self.rows["a_copy2.mp4"], "残す")
        self.db.conn.execute("UPDATE videos SET cloud_only=1 WHERE id=?", (self.rows["b_copy.mp4"],))
        self.db.conn.commit()
        p = self.plan(10)
        names = self.names(p)
        # 保護したコピーを「残す1本」にするので、元のファイルは重複分として提案してよい
        self.assertEqual(set(names), {"a_original.mp4"})
        self.assertEqual(names["a_original.mp4"]["keep_id"], self.rows["a_copy1.mp4"])
        self.assertEqual(p["excluded"], {"保護": 1, "残す": 1, "オンライン専用": 1})
        self.assertFalse(p["achieved"])
        self.assertEqual(p["shortage"], 9 * self.GB)  # 無理に候補を足さず、不足分を表示

    def test_review_candidates_only_when_duplicates_are_not_enough(self):
        self.make_duplicates()
        self.add("huge.mp4", 4 * self.GB, days_old=3)
        self.add("old_mid.mp4", 300 * 1024 ** 2, days_old=500)
        self.add("broken_rec.mp4", 10 * 1024 ** 2, failure={"kind": "short", "reason": "再生時間が 1.0 秒と極端に短い録画です"})
        self.add("small_new.mp4", 20 * 1024 ** 2, days_old=3)    # 理由がないので候補にしない
        self.add("old_tiny.mp4", 1 * 1024 ** 2, days_old=900)    # 古いだけの小さなファイルも候補にしない
        p = self.plan(8)
        names = self.names(p)
        self.assertTrue(p["achieved"])
        self.assertEqual(p["duplicate_total"], 4 * self.GB)
        self.assertEqual(names["huge.mp4"]["label"], "要確認")
        self.assertIn("容量が大きい", names["huge.mp4"]["reasons"][0])
        self.assertIn("broken_rec.mp4", names)  # 失敗した可能性のある録画を先に
        p = self.plan(100)
        names = self.names(p)
        self.assertFalse(p["achieved"])
        self.assertGreater(p["shortage"], 0)
        self.assertIn("old_mid.mp4", names)
        self.assertIn("更新されていません", names["old_mid.mp4"]["reasons"][0])
        self.assertIn("視聴したかどうかは分かりません", names["old_mid.mp4"]["reasons"][0])
        self.assertNotIn("small_new.mp4", names)
        self.assertNotIn("old_tiny.mp4", names)
        for it in p["items"]:
            text = " ".join(it["reasons"])
            self.assertTrue(it["reasons"] and it["size"] > 0)
            self.assertNotIn("不要", text)
            self.assertNotIn("AI", text)

    def test_kept_copy_is_never_a_review_candidate(self):
        self.add("orig.mp4", 2 * self.GB, days_old=800, sha="X")  # 大きくて古いが、重複の「残す1本」
        self.add("copy.mp4", 2 * self.GB, days_old=1, sha="X")
        names = self.names(self.plan(100))
        self.assertIn("copy.mp4", names)
        self.assertNotIn("orig.mp4", names)

    def test_images_are_not_included_by_default(self):
        self.add("pic1.jpg", 1 * self.GB, sha="P", kind="image")
        self.add("pic2.jpg", 1 * self.GB, sha="P", kind="image")
        self.assertEqual(self.plan(1)["items"], [])
        self.assertEqual(len(self.plan(1, include_images=True)["items"]), 1)

    def test_drive_space_is_reported_per_drive(self):
        spaces = candidates.drive_space([self._tmp.name, self._tmp.name])
        self.assertEqual(len(spaces), 1)
        self.assertGreater(spaces[0]["total"], 0)


class SurvivorCheckTest(unittest.TestCase):
    """最終確認時に、重複の最後の1本がディスクに実在するかを確かめる。"""

    def setUp(self):
        self.env = Env(with_videos=False)
        self.env.run()
        self.db = Database(self.env.data)
        self.by = {r["path"]: r["id"] for r in self.db.media()}
        self.moved = []

    def tearDown(self):
        self.db.close()
        self.env.close()

    def backend(self, path):
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def test_copy_is_not_moved_when_the_other_copy_disappeared(self):
        keep = self.env.home / "Pictures" / "photo.jpg"
        copy = self.by[str(self.env.home / "Downloads" / "photo_copy.jpg")]
        os.rename(keep, str(keep) + ".moved-away")  # スキャン後に、残すはずのファイルがなくなった
        results = trash.move_to_trash(self.db, [copy], self.backend)
        self.assertFalse(results[0].ok)
        self.assertIn("最後の1本", results[0].message)
        self.assertEqual(self.moved, [])

    def test_copy_is_not_moved_when_the_other_copy_changed(self):
        keep = self.env.home / "Pictures" / "photo.jpg"
        copy = self.by[str(self.env.home / "Downloads" / "photo_copy.jpg")]
        with open(keep, "ab") as f:
            f.write(b"edited")
        results = trash.move_to_trash(self.db, [copy], self.backend)
        self.assertFalse(results[0].ok)
        self.assertEqual(self.moved, [])

    def test_partial_failure_is_reported_separately(self):
        ids = [self.by[str(self.env.home / "Downloads" / "photo_copy.jpg")],
               self.by[str(self.env.home / "Pictures" / "other.png")]]

        def flaky(path):
            if path.endswith("other.png"):
                raise trash.TrashError("ごみ箱へ移動できませんでした（テスト）")
            self.backend(path)
        results = trash.move_to_trash(self.db, ids, flaky)
        self.assertEqual([r.ok for r in results], [True, False])
        self.assertTrue((self.env.home / "Pictures" / "other.png").exists())


class SameFileTest(unittest.TestCase):
    """同じ実体のファイル（別の表記・ハードリンク）を重複として扱わない。実際には1本しかない録画を守る。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "videos"
        self.dir.mkdir()
        self.file = self.dir / "rec.mp4"
        self.file.write_bytes(b"recording" * 1000)
        self.data = Path(self._tmp.name) / "data"
        self.db = Database(self.data)
        self.moved = []

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def insert(self, path: str, sha="S"):
        """以前の版のように、表記を統一せずに登録する（移行前のデータの再現）。"""
        st = os.stat(path)
        self.db.conn.execute(
            "INSERT INTO videos(path,name,folder,size,mtime,ext,added_at,kind,location,sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (path, os.path.basename(path), os.path.dirname(path), st.st_size, st.st_mtime, ".mp4", time.time(),
             "video", str(self.dir), sha))
        self.db.conn.commit()
        return self.db.conn.execute("SELECT id FROM videos WHERE path=?", (path,)).fetchone()[0]

    def backend(self, path):
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def test_same_file_with_two_spellings_is_not_a_duplicate(self):
        a = self.insert(str(self.file))
        b = self.insert(str(self.dir) + os.sep + "." + os.sep + "rec.mp4")  # 別の表記（C:/… と C:\… に相当）
        ctx = candidates.Context(self.db)
        self.assertEqual(ctx.dup_groups, {})
        self.assertEqual(ctx.aliases, {b: a})
        self.assertIn("二重に登録", " ".join(ctx.reasons(ctx.rows[b])))
        plan = candidates.plan_recordings(ctx, 10 ** 9)
        self.assertEqual(plan["items"], [])
        results = trash.move_to_trash(self.db, [b], self.backend)  # 直接指定しても移動しない
        self.assertFalse(results[0].ok)
        self.assertEqual(self.moved, [])
        self.assertTrue(self.file.exists())

    def test_hard_link_is_not_a_duplicate(self):
        link = self.dir / "rec_link.mp4"
        try:
            os.link(self.file, link)
        except OSError:
            self.skipTest("ハードリンクを作れない環境")
        self.insert(str(self.file))
        self.insert(str(link))
        ctx = candidates.Context(self.db)
        self.assertEqual(ctx.dup_groups, {})  # 消しても容量が空かず、同じ実体を失うおそれがある
        self.assertEqual(candidates.plan_recordings(ctx, 10 ** 9)["items"], [])

    def test_real_copy_is_still_a_duplicate(self):
        copy = self.dir / "rec_copy.mp4"
        shutil.copyfile(self.file, copy)
        a = self.insert(str(self.file))
        b = self.insert(str(self.dir) + os.sep + "." + os.sep + "rec.mp4")
        c = self.insert(str(copy))
        ctx = candidates.Context(self.db)
        group = next(iter(ctx.dup_groups.values()))
        self.assertEqual(sorted(group), sorted([a, c]))  # 別の表記の b は含めない
        plan = candidates.plan_recordings(ctx, 10 ** 9)
        self.assertEqual(len(plan["items"]), 1)
        self.assertNotIn(b, [it["id"] for it in plan["items"]])

    def test_existing_data_is_normalized_once(self):
        canonical = self.insert(str(self.file))
        alias = self.insert(str(self.dir) + os.sep + "." + os.sep + "rec.mp4")
        other = self.dir / "other.mp4"
        other.write_bytes(b"x" * 10)
        lonely = self.insert(str(self.dir) + os.sep + "." + os.sep + "other.mp4", sha=None)
        self.db.set_protected(alias, True)
        self.db.set_decision(alias, "保留")
        self.db.conn.execute("DELETE FROM settings WHERE key='paths_normalized'")
        self.db.conn.execute("UPDATE settings SET value=? WHERE key='extra_locations'", ("[]",))
        self.db.conn.execute("INSERT OR REPLACE INTO settings VALUES ('extra_locations', ?)",
                             (f'["{self.dir.as_posix()}/./", "{self.dir.as_posix()}"]',))
        self.db.conn.commit()
        self.db.close()
        self.db = Database(self.data)
        row = self.db.video(canonical)
        self.assertTrue(row["protected"])        # 保護と判断を引き継ぐ
        self.assertEqual(row["decision"], "保留")
        self.assertTrue(self.db.video(alias)["missing"])  # 別の表記の行は非表示（記録は残す）
        self.assertEqual(self.db.video(lonely)["path"], str(other))
        self.assertEqual(self.db.json_list("extra_locations"), [os.path.normpath(str(self.dir))])

    def test_new_scans_store_one_spelling(self):
        self.db.set_json_list("extra_locations", [str(self.dir) + os.sep + "." + os.sep])
        self.assertEqual(self.db.json_list("extra_locations"), [str(self.dir)])
        self.db.close()
        with mock.patch.object(autoscan, "known_folders", lambda: []), \
                mock.patch.object(autoscan, "local_drives", lambda: []):
            autoscan.task_autoscan(self.data, FFMPEG, autoscan.Controller(), lambda p: None)
        self.db = Database(self.data)
        paths = [r["path"] for r in self.db.media()]
        self.assertEqual(paths, [str(self.file)])


@unittest.skipIf(QApplication is None, "PySide6 がありません")
class PlanGuiTest(unittest.TestCase):
    """ホーム → 整理候補 → 内容確認 → 最終確認。"""

    @classmethod
    def setUpClass(cls):
        from storage_organizer import theme
        cls.app = QApplication.instance() or QApplication([])
        theme.apply(cls.app)

    def setUp(self):
        from storage_organizer.main_window import MainWindow
        self.env = Env()
        if FFMPEG.available:
            shutil.copyfile(self.env.home / "Videos" / "normal.mp4", self.env.home / "Downloads" / "normal (1).mp4")
        self.messages = []
        ans = lambda *a, **k: (self.messages.append(a[1:3]) or QMessageBox.StandardButton.Yes)  # noqa: E731
        self.mb = [mock.patch.object(QMessageBox, n, staticmethod(ans)) for n in ("information", "warning", "question")]
        self.large = mock.patch.object(candidates, "REVIEW_LARGE_BYTES", 40 * 1024)  # 小さなテスト用録画でも確認
        for p in self.mb + [self.large]:
            p.start()
        self.w = MainWindow(Database(self.env.data), start_autoscan=False)
        self.w.resize(1280, 800)
        self.w.show()
        self.w.start_scan()
        self.assertTrue(wait_until(lambda: self.w.state.runner.is_running(), 10))
        self.assertTrue(wait_until(lambda: not self.w.state.runner.is_running(), 120))
        QCoreApplication.processEvents()

    def tearDown(self):
        self.w.close()
        self.w.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for p in self.mb + [self.large]:
            p.stop()
        self.env.close()

    def plan_with(self, gb):
        p = self.w.plan
        p.target.setDecimals(4)
        p.target.setMinimum(0)
        p.set_target_gb(gb)
        QCoreApplication.processEvents()
        return p

    def child(self, p, name):
        for i in range(p.tree.topLevelItemCount()):
            top = p.tree.topLevelItem(i)
            for j in range(top.childCount()):
                if top.child(j).text(0) == name:
                    return top.child(j)
        return None

    @unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
    def test_plan_reasons_status_selection_and_final_confirm(self):
        from storage_organizer import organizer
        w = self.w
        w.show_plan(None)
        p = self.plan_with(0.0002)
        dup = self.child(p, "normal (1).mp4")
        self.assertIsNotNone(dup)
        self.assertEqual(dup.checkState(0), Qt.CheckState.Checked)  # 重複は提案どおり選択
        review = self.child(p, "short.mp4")
        self.assertEqual(review.checkState(0), Qt.CheckState.Unchecked)  # 要確認は内容を見てから
        self.assertIn("極端に短い", review.text(2))
        self.assertIsNone(self.child(p, "normal.mp4"))  # 残す1本は候補にしない
        self.assertIn(p.status_card.value.text().split()[0], ("達成できます", "不足"))
        self.assertIn("ファイルサイズ", p.total_card.body.itemAt(0).layout().itemAt(1).widget().text())
        self.assertIn("実際", p.free_card.detail.text())
        # 内容確認（プレビュー・情報・提案の理由）
        p.tree.setCurrentItem(review)
        QCoreApplication.processEvents()
        self.assertTrue(p.detail.isVisible())
        self.assertIn("極端に短い", p.detail.reasons.text())
        self.assertGreater(p.detail.info_form.rowCount(), 3)
        # 保護すると提案から外れ、起動し直しても残る
        pid = review.data(0, Qt.ItemDataRole.UserRole)
        p.protect_button.click()
        self.assertIsNone(self.child(p, "short.mp4"))
        self.assertTrue(w.ctx.db.video(pid)["protected"])
        # 最終確認（モックのごみ箱）。一部失敗は分けて報告
        dark = self.child(p, "dark.mp4")
        dark.setCheckState(0, Qt.CheckState.Checked)
        moved = []

        def backend(path):
            if path.endswith("dark.mp4"):
                raise trash.TrashError("テスト用の失敗")
            moved.append(path)
            os.rename(path, path + ".trashed-by-test")

        def run_dialog(dlg):
            dlg.backend = backend
            self.assertIn("ごみ箱を空にするまで", dlg.summary.text())
            dlg.execute()
            return 1
        with mock.patch.object(organizer.TrashDialog, "exec", run_dialog):
            p.final_button.click()
        self.assertEqual(len(moved), 1)
        report = str(self.messages[-1])
        self.assertIn("ごみ箱へ移動: 1 件", report)
        self.assertIn("移動しなかった: 1 件", report)
        db_path = w.ctx.data_dir
        self.w.close()
        db = Database(db_path)
        self.assertTrue(db.video(pid)["protected"])
        self.assertEqual(db.conn.execute("SELECT decision FROM videos WHERE name='dark.mp4'").fetchone()[0], "削除候補")
        db.close()

    def test_preview_failure_still_shows_information_and_reasons(self):
        w = self.w
        w.show_section(w.files)
        f = w.files
        i = next(i for i, it in enumerate(f.model.items) if it["name"] == "notimage.jpg")
        f.view.setCurrentIndex(f.model.index(i, 0))
        QCoreApplication.processEvents()
        self.assertIs(f.detail.preview_stack.currentWidget(), f.detail.preview_msg)
        self.assertIn("プレビューできません", f.detail.preview_msg.title.text())
        self.assertGreater(f.detail.info_form.rowCount(), 3)
        self.assertIn("読み込めません", f.detail.reasons.text())

    def test_ffmpeg_missing_is_guided_to_settings(self):
        w = self.w
        with mock.patch.object(media, "find_ffmpeg", lambda *a, **k: media.FFmpegTools(None, None)):
            w.home.refresh()
            self.assertTrue(w.home.banner.isVisibleTo(w.home))
            w.home.banner.open_settings.emit()
            self.assertIs(w.stack.currentWidget(), w.settings_page)
            w.plan.replan()
            self.assertTrue(w.plan.banner.isVisibleTo(w.plan))

    def test_experimental_menu_toggle(self):
        w = self.w
        row = w.sections.index(w.video_ai)
        self.assertTrue(w.nav.item(row).isHidden())
        w.settings.experimental.setChecked(True)
        QCoreApplication.processEvents()
        self.assertFalse(w.nav.item(row).isHidden())
        w.show_page(w.video_list)  # 既存の導線
        self.assertIs(w.stack.currentWidget(), w.video_ai)


if __name__ == "__main__":
    unittest.main()
