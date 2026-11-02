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
                         ["ホーム", "ファイル一覧", "整理のおすすめ", "動画AI", "設定"])
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
        self.assertIn("ドライブ全体の使用量ではありません", w.home.total.detail.text())
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
        total = sum(r["size"] for r in self.state.ctx.rows.values())
        self.assertEqual(home.primary.text(), "整理をはじめる")
        self.assertEqual(home.total.value.text(), format_size(total))
        self.assertNotEqual(home.saving.value.text(), "算出できません")
        self.assertIn("別の値", home.drive.detail.text())
        home.primary.click()
        self.assertIs(self.w.stack.currentWidget(), self.w.recommend)
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


if __name__ == "__main__":
    unittest.main()
