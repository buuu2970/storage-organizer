"""録画・画像の整理支援（自動スキャン・候補判定・ごみ箱への移動・整理画面）のテスト。

実ファイルの削除は行わない。ごみ箱への移動はモックで確認する。
"""

from __future__ import annotations

import hashlib
import json
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

from storage_organizer import autoscan, candidates, media, scanner, trash  # noqa: E402
from storage_organizer.store import Store as Database  # noqa: E402
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
        locs = [r["kind"] for r in db.locations()]
        self.assertEqual(locs, ["known", "known", "known", "drive"])
        self.assertEqual(len(db.json_list("not_scanned_drives")), 2)
        db.close()

    def test_exclusions_and_no_loops(self):
        self.env.run()
        db = Database(self.env.data)
        names = self.names(db)
        for excluded in ("cache.jpg", "h.jpg", "x.jpg", "logo.png", "icon.png", "wall.jpg"):
            self.assertNotIn(excluded, names)
        reasons = {Path(r["path"]).name: r["reason"] for r in db.skips("excluded")}
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
        errs = [r["path"] for r in db.skips("error")]
        self.assertEqual(errs, [blocked])
        status = next(x["status"] for x in db.locations() if x["label"] == "ピクチャ")
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
        self.assertIn("中断", [x["status"] for x in db.locations()])
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
        self.db.set_field(b, cloud_only=1)
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
        self.assertEqual([r["result"] for r in self.db.trash_log()], ["error"])

    def test_default_backend_outside_windows_reports_error(self):
        if sys.platform == "win32":
            self.skipTest("Windows ではモックを使って確認する")
        pid = self.pid("Pictures/other.png")
        results = trash.move_to_trash(self.db, [pid])
        self.assertFalse(results[0].ok)
        self.assertTrue((self.env.home / "Pictures" / "other.png").exists())


try:
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
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

    def test_navigation_reaches_all_sections(self):
        # 仕様変更: 動画AI（試作）をアプリから外し、メニューを録画整理の流れに合わせた
        w = self.w
        self.assertEqual([w.nav.item(i).data(0x0100) for i in range(w.nav.count())],
                         ["ホーム", "整理候補", "容量の内訳", "ファイル一覧", "画像の整理（補助）", "設定"])
        for page in w.sections:
            w.show_section(page)
            QCoreApplication.processEvents()
            self.assertTrue(page.isVisible())
        w.show_page(w.settings)
        self.assertIs(w.stack.currentWidget(), w.settings_page)
        for name in ("重複", "類似画像", "録画確認候補", "大容量", "削除候補"):
            w.show_category(name)
            self.assertIs(w.stack.currentWidget(), w.recommend)
            self.assertEqual(w.recommend.category, name)
        for name, state in (("保護", "保護中"), ("保留", "保留"), ("すべて", "すべての判断")):
            w.show_category(name)
            self.assertIs(w.stack.currentWidget(), w.files)
            self.assertEqual(w.files.state_filter.currentText(), state)
        # 通常の動作では旧 DB（SQLite）を読み込まない（ほかのテストの影響を受けないよう、別プロセスで確かめる）
        code = ("import sys, tempfile; from PySide6.QtWidgets import QApplication; app = QApplication([]); "
                "from storage_organizer.store import Store; from storage_organizer.main_window import MainWindow; "
                "w = MainWindow(Store(tempfile.mkdtemp()), start_autoscan=False); w.show(); app.processEvents(); "
                "print(int('sqlite3' in sys.modules), int('storage_organizer.legacy_db' in sys.modules)); w.close()")
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=120).stdout.strip().splitlines()
        self.assertEqual(out[-1], "0 0")

    def test_first_run_home_and_empty_states(self):
        w = self.w
        self.assertFalse(self.state.runner.is_running())  # 初回は自動で始めず、調べる場所を選んでもらう
        self.assertEqual(w.home.primary.text(), "PCを調べる")  # 仕様変更: 主操作の名称
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
        self.assertEqual(home.primary.text(), "おすすめを確認する")  # 仕様変更: 主操作の名称
        self.assertEqual(home.total.value.text(), format_size(total))  # 録画（動画）だけの合計
        self.assertIn("ドライブ全体の使用量とは別", home.total.detail.text())
        self.assertNotEqual(home.saving.value.text(), "算出できません")
        # 仕様変更: 主操作「おすすめを確認する」は目標なしで候補へ。目標は「この目標で提案」から
        home.primary.click()
        self.assertIs(self.w.stack.currentWidget(), self.w.flow)
        self.assertEqual(self.w.plan.target_bytes(), None)
        home.target.setValue(3.5)
        home.goal_button.click()
        self.assertIs(self.w.flow.stack.currentWidget(), self.w.plan)
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

    def test_release_ui_details(self):
        from storage_organizer import organizer
        self.scan()
        w = self.w
        home = w.home
        self.assertIn("調査範囲: ピクチャ", home.scope.text())
        self.assertIn("最終調査:", home.scope.text())
        for width, cols in ((1400, 3), (800, 2), (560, 1)):  # 幅に応じて列数を変え、横にはみ出さない
            home.resize(width, 700)
            QCoreApplication.processEvents()
            self.assertEqual(home._cols, cols)
        # プレビューできないファイルでも「既定のアプリで開く」を案内する
        w.show_section(w.files)
        f = w.files
        i = next(i for i, it in enumerate(f.model.items) if it["name"] == "notimage.jpg")
        f.view.setCurrentIndex(f.model.index(i, 0))
        QCoreApplication.processEvents()
        self.assertEqual(f.detail.preview_msg.action.text(), "既定のアプリで開く")
        opened = []
        with mock.patch("PySide6.QtGui.QDesktopServices.openUrl", side_effect=lambda u: opened.append(u.toLocalFile())):
            f.detail.preview_msg.action.click()
        self.assertTrue(opened and opened[0].endswith("notimage.jpg"))
        # 設定で FFmpeg を指定し直すと、案内が更新される
        with mock.patch.object(media, "find_ffmpeg", lambda *a, **k: media.FFmpegTools(None, None)):
            w.settings.changed.emit()
            self.assertTrue(home.banner.isVisibleTo(home))
        w.settings.changed.emit()
        self.assertEqual(home.banner.isVisibleTo(home), not FFMPEG.available)
        # キャッシュの上限（古いものから、アプリのキャッシュだけを消す）
        cache = Path(self.env.tmp.name) / "cache"
        cache.mkdir()
        for k in range(10):
            fpath = cache / f"t{k}.jpg"
            fpath.write_bytes(b"x" * 1000)
            os.utime(fpath, (k, k))
        removed = organizer.prune_thumb_cache(cache, max_bytes=5000)
        left = sorted(p.name for p in cache.iterdir())
        self.assertEqual(removed, 6)
        self.assertEqual(left, ["t6.jpg", "t7.jpg", "t8.jpg", "t9.jpg"])

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
        Path(path).touch()  # 提案は実在するファイルだけを対象にするため、空のファイルを置く（容量は DB 上の値）
        mtime = self.now - days_old * 86400
        self.db.upsert_media([(path, name, self._tmp.name, size, mtime, ".mp4", kind, False)], 1, self._tmp.name)
        mid = self.db.id_of(path)
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
        self.db.set_field(self.rows["b_copy.mp4"], cloud_only=1)
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

    def test_missing_files_are_not_proposed(self):
        self.make_duplicates()
        self.add("huge.mp4", 4 * self.GB)
        os.remove(Path(self._tmp.name) / "huge.mp4")          # スキャン後に消えた
        os.remove(Path(self._tmp.name) / "b_original.mp4")    # 「残す1本」が消えた → b_copy は重複分ではない
        os.remove(Path(self._tmp.name) / "a_original.mp4")    # 3本中の残す1本が消えた → 実在する1本を残す
        p = self.plan(100)
        names = self.names(p)
        self.assertNotIn("huge.mp4", names)
        self.assertNotIn("b_copy.mp4", [n for n, it in names.items() if it["kind"] == "duplicate"])
        dups = [n for n, it in names.items() if it["kind"] == "duplicate"]
        self.assertEqual(len(dups), 1)  # a_copy1 / a_copy2 のうち1本は必ず残す
        kept = self.db.video(names[dups[0]]["keep_id"])["name"]
        self.assertIn(kept, ("a_copy1.mp4", "a_copy2.mp4"))
        self.assertIn(kept, names[dups[0]]["reasons"][0])
        self.assertEqual(p["excluded"]["見つからない"], 3)

    def test_unchecked_duplicates_are_reported(self):
        self.add("x1.mp4", 3 * self.GB)  # サイズが同じだが、まだハッシュを計算していない
        self.add("x2.mp4", 3 * self.GB)
        self.add("y.mp4", 1 * self.GB, sha="Y")
        p = self.plan(1)
        self.assertEqual(p["unchecked"], 2)
        self.assertEqual([it for it in p["items"] if it["kind"] == "duplicate"], [])  # 確認前は重複として提案しない

    def test_images_are_not_included_by_default(self):
        self.add("pic1.jpg", 1 * self.GB, sha="P", kind="image")
        self.add("pic2.jpg", 1 * self.GB, sha="P", kind="image")
        self.assertEqual(self.plan(1)["items"], [])
        self.assertEqual(len(self.plan(1, include_images=True)["items"]), 1)

    def test_plan_targets_one_drive(self):
        fake = lambda path: "D:\\" if os.path.basename(path).startswith("d_") else "C:\\"  # noqa: E731
        with mock.patch.object(candidates, "drive_of", fake):
            self.add("c_orig.mp4", 2 * self.GB, days_old=30, sha="S")
            self.add("d_copy.mp4", 2 * self.GB, days_old=1, sha="S")   # 同じ録画が D にもある
            self.add("d_big.mp4", 3 * self.GB)
            self.add("c_big.mp4", 3 * self.GB)
            p = self.plan(10, drive="C:\\")
            names = self.names(p)
            # C を空けたいので、D のコピーを残して C の1本を重複分として提案する
            self.assertEqual(names["c_orig.mp4"]["kind"], "duplicate")
            self.assertEqual(self.db.video(names["c_orig.mp4"]["keep_id"])["name"], "d_copy.mp4")
            self.assertIn("別のドライブ", names["c_orig.mp4"]["reasons"][0])
            self.assertNotIn("d_big.mp4", names)  # 別ドライブの録画は C の解放見込みに含めない
            self.assertEqual(p["total"], 5 * self.GB)
            self.assertEqual(sorted(candidates.media_drives(candidates.Context(self.db))), ["C:\\", "D:\\"])

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

    def test_skip_and_failure_are_distinguished(self):
        ok_id = self.by[str(self.env.home / "Downloads" / "photo_copy.jpg")]
        prot = self.by[str(self.env.home / "Pictures" / "noexif.png")]
        bad = self.by[str(self.env.home / "Pictures" / "other.png")]
        self.db.set_protected(prot, True)

        def flaky(path):
            if path.endswith("other.png"):
                raise trash.TrashError("ごみ箱が利用できません（テスト）")
            self.backend(path)
        results = {r.media_id: r.status for r in trash.move_to_trash(self.db, [ok_id, prot, bad], flaky)}
        self.assertEqual(results, {ok_id: "moved", prot: "skipped", bad: "failed"})

    def test_trash_dialog_cannot_run_twice(self):
        from storage_organizer import organizer
        if QApplication is None:
            self.skipTest("PySide6 がありません")
        QApplication.instance() or QApplication([])
        mid = self.by[str(self.env.home / "Downloads" / "photo_copy.jpg")]
        calls = []
        dlg = organizer.TrashDialog(self.db, [mid], None, backend=lambda p: (calls.append(p), self.backend(p)))
        with mock.patch.object(QMessageBox, "information"), mock.patch.object(QMessageBox, "warning"):
            dlg.execute()
            dlg.execute()  # 2回目は何もしない
        self.assertEqual(len(calls), 1)

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
        return self.db.insert_raw(path, os.path.basename(path), os.path.dirname(path), st.st_size, st.st_mtime, ".mp4",
                                  "video", str(self.dir), sha256=sha)

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

    def test_legacy_database_is_migrated(self):
        """仕様変更（DB 廃止）: 旧版の SQLite から、設定・保護・判断を表記を統一して取り込む（旧 DB は消さない）。"""
        from storage_organizer import legacy_db, migrate
        old_dir = Path(self._tmp.name) / "old"
        old = legacy_db.Database(old_dir)
        alias = str(self.dir) + os.sep + "." + os.sep + "rec.mp4"
        rec = scanner.FileRecord("rec.mp4", 10, "動画", 1.0, str(self.dir) + os.sep + ".")
        old.register_videos([rec])
        old.conn.execute("UPDATE videos SET path=?, protected=1, decision='保留' WHERE name='rec.mp4'", (alias,))
        old.set_json_list("extra_locations", [str(self.dir) + os.sep + "." + os.sep])
        old.set_setting("ffmpeg_dir", "C:/ffmpeg/bin")
        old.close()
        result = migrate.migrate_from_legacy(old_dir / "recsort.db", self.db)
        self.assertEqual(result["files"], 1)
        state = json.loads((self.data / "user_state.json").read_text(encoding="utf-8"))
        self.assertEqual(state[str(self.file)], {"protected": 1, "decision": "保留"})  # 表記を統一したパスで保存
        self.assertEqual(self.db.json_list("extra_locations"), [str(self.dir)])
        self.assertEqual(self.db.get_setting("ffmpeg_dir"), "C:/ffmpeg/bin")
        self.assertTrue((old_dir / "recsort.db").exists())
        mid = self.insert(str(self.file))
        self.assertTrue(self.db.video(mid)["protected"])  # 移行後に見つかったファイルにも保護が付く

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


@unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
class SimilarVideoTest(unittest.TestCase):
    """類似録画（解像度違い・再圧縮）: 検出・比較が必要な候補としての扱い・ごみ箱移動の止め方。"""

    def setUp(self):
        self.env = Env()
        vids = self.env.home / "Videos"
        src = vids / "normal.mp4"
        subprocess.run([FFMPEG.ffmpeg, "-v", "error", "-i", str(src), "-vf", "scale=160:90", "-c:v", "libx264",
                        "-crf", "30", str(self.env.home / "Downloads" / "normal_low.mp4")], check=True)
        subprocess.run([FFMPEG.ffmpeg, "-v", "error", "-i", str(src), "-c:v", "libx264", "-crf", "40",
                        str(self.env.home / "Downloads" / "normal_recompressed.mp4")], check=True)
        self.env.run()
        self.db = Database(self.env.data)
        self.by = {r["name"]: r["id"] for r in self.db.media()}
        self.moved = []

    def tearDown(self):
        self.db.close()
        self.env.close()

    def backend(self, path):
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def test_detected_as_similar_group_not_as_duplicates(self):
        ctx = candidates.Context(self.db)
        groups = [{ctx.rows[i]["name"] for i in g} for g in ctx.video_groups]
        self.assertIn({"normal.mp4", "normal_low.mp4", "normal_recompressed.mp4"}, groups)
        self.assertNotIn("dark.mp4", {n for g in groups for n in g})   # 黒画面だけの動画は混ざらない
        self.assertNotIn("short.mp4", {n for g in groups for n in g})  # 長さが違う動画は比べない
        n = ctx.vgroup_of[self.by["normal.mp4"]]
        keep, why = ctx.vgroup_keep[n]
        # 元と再圧縮版は同じ解像度。ビットレートなどで高画質と断定せず、おすすめを決めない
        self.assertIsNone(keep)
        self.assertIn("明確な優劣は判断できません", why)
        self.db.set_protected(self.by["normal_recompressed.mp4"], True)
        ctx = candidates.Context(self.db)
        self.assertEqual(ctx.vgroup_keep[n][0], self.by["normal_recompressed.mp4"])  # 保護したものを残す
        self.assertIn("同じ内容とは限りません", " ".join(ctx.reasons(ctx.rows[self.by["normal_low.mp4"]])))

    def test_unconfirmed_similar_cannot_be_trashed(self):
        ctx = candidates.Context(self.db)
        plan = candidates.plan_recordings(ctx, None)
        sim = [it for it in plan["items"] if it["kind"] == "similar"]
        self.assertEqual({self.db.video(it["id"])["name"] for it in sim}, {"normal_low.mp4", "normal_recompressed.mp4"})
        self.assertTrue(all(not it["confirmed"] for it in sim))  # 比較して確定するまでは未確定
        ids = [it["id"] for it in sim]
        results = trash.move_to_trash(self.db, ids, self.backend)
        self.assertEqual({r.status for r in results}, {"skipped"})
        self.assertIn("比較して残す1本を選んでから", results[0].message)
        self.assertEqual(self.moved, [])
        # 比較して残す1本を確定すると、移動できる
        self.db.set_decision(self.by["normal.mp4"], "残す")
        ctx = candidates.Context(self.db)
        plan = candidates.plan_recordings(ctx, None)
        self.assertTrue(all(it["confirmed"] for it in plan["items"] if it["kind"] == "similar"))
        results = trash.move_to_trash(self.db, ids, self.backend)
        self.assertEqual([r.status for r in results], ["moved", "moved"])
        self.assertTrue((self.env.home / "Videos" / "normal.mp4").exists())

    def test_kept_file_changed_blocks_similar_trash(self):
        self.db.set_decision(self.by["normal.mp4"], "残す")
        with open(self.env.home / "Videos" / "normal.mp4", "ab") as f:
            f.write(b"changed")  # 残す側が判定時から変わった
        results = trash.move_to_trash(self.db, [self.by["normal_low.mp4"]], self.backend)
        self.assertEqual(results[0].status, "skipped")
        self.assertEqual(self.moved, [])

    def test_target_uses_duplicates_first_then_similar_without_loosening(self):
        shutil.copyfile(self.env.home / "Videos" / "short.mp4", self.env.home / "Downloads" / "short (copy).mp4")
        self.db.close()
        self.env.run()  # 録画の完全重複を加えて調べ直す
        self.db = Database(self.env.data)
        ctx = candidates.Context(self.db)
        dup_pool = sum(it["size"] for it in candidates.plan_recordings(ctx, None)["items"] if it["kind"] == "duplicate")
        plan = candidates.plan_recordings(ctx, dup_pool + 1)
        kinds = [it["kind"] for it in plan["items"]]
        self.assertEqual(kinds[0], "duplicate")
        self.assertIn("similar", kinds)
        self.assertLess(kinds.index("duplicate"), kinds.index("similar"))
        ids = [it["id"] for it in plan["items"]]
        self.assertEqual(len(ids), len(set(ids)))  # 二重計上しない
        self.assertEqual(plan["total"], sum(it["size"] for it in plan["items"]))
        huge = candidates.plan_recordings(ctx, 10 ** 12)
        self.assertGreater(huge["shortage"], 0)  # 足りなくても基準を緩めない
        self.assertFalse(any(ctx.rows[it["id"]]["name"] == "other.png" for it in huge["items"]))

    def test_signatures_are_reused_until_file_changes(self):
        calls = []
        real = candidates.similar.signature

        def spy(*a, **k):
            calls.append(a[1])
            return real(*a, **k)
        self.db.close()
        with mock.patch.object(candidates.similar, "signature", spy):
            self.env.run()
        self.assertEqual(calls, [])  # 変更がなければ再解析しない
        self.db = Database(self.env.data)
        self.assertTrue(self.db.video_groups())


class CapacitySurveyTest(unittest.TestCase):
    """PC 全体の調査: 全種類のファイルの容量を集計し、整理候補（一覧）には画像・動画だけを入れる。"""

    def test_all_file_types_are_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            docs = base / "Documents"
            (docs / "report").mkdir(parents=True)
            (docs / "report" / "報告書.docx").write_bytes(b"d" * 3000)
            (docs / "backup.zip").write_bytes(b"z" * 5000)
            (docs / "memo.xyz").write_bytes(b"x" * 700)
            (docs / "photo.jpg").write_bytes(b"\xff\xd8" + b"i" * 2000)
            proj = docs / "game-project"
            proj.mkdir()
            (proj / "package.json").write_text("{}")
            (proj / "big.bin").write_bytes(b"b" * 9000)
            with mock.patch.object(autoscan, "known_folders", lambda: [("ドキュメント", str(docs))]), \
                    mock.patch.object(autoscan, "local_drives", lambda: []):
                autoscan.task_autoscan(base / "data", FFMPEG, autoscan.Controller(), lambda p: None)
            db = Database(base / "data")
            stats = db.location_stats()[str(docs)]
            cats = {k: v for k, v in stats["by_category"].items()}
            self.assertEqual(cats["圧縮"][1], 5000)
            self.assertEqual(cats["文書"][1], 3000)
            self.assertEqual(cats["その他"][1], 700)
            self.assertEqual(cats["開発用フォルダ"][1], 9000 + 2)  # 容量は数えるが候補にはしない
            self.assertEqual(stats["by_folder"]["report"][1], 3000)
            self.assertEqual(stats["top_files"][0][0], 5000)  # 大きいファイルの順
            self.assertEqual({r["name"] for r in db.media()}, {"photo.jpg"})  # 一覧に入るのは画像・動画だけ
            self.assertTrue(any("開発用" in a[2] for a in stats["areas"]))
            db.close()


class FileNameTest(unittest.TestCase):
    """日本語・空白・長いファイル名でも、調査 → 提案 → ごみ箱への移動（モック）まで扱える。"""

    def test_japanese_spaces_and_long_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            videos = base / "ビデオ フォルダ"
            videos.mkdir()
            name = "ゲーム録画 2024年 3月 " + "とても長いファイル名" * 5 + " (1).mp4"
            data = b"\x00\x00\x00\x18ftypmp42" + os.urandom(4096)
            (videos / "ゲーム録画 2024年 3月.mp4").write_bytes(data)
            (videos / name).write_bytes(data)
            with mock.patch.object(autoscan, "known_folders", lambda: [("ビデオ", str(videos))]), \
                    mock.patch.object(autoscan, "local_drives", lambda: []):
                autoscan.task_autoscan(base / "data", FFMPEG, autoscan.Controller(), lambda p: None)
            db = Database(base / "data")
            plan = candidates.plan_recordings(candidates.Context(db), 1)
            self.assertEqual([db.video(it["id"])["name"] for it in plan["items"]], [name])
            moved = []
            results = trash.move_to_trash(db, [plan["items"][0]["id"]],
                                          lambda p: (moved.append(p), os.rename(p, p + ".trashed-by-test")))
            db.close()
            self.assertEqual([r.status for r in results], ["moved"])
            self.assertTrue(moved[0].endswith(name))
            self.assertTrue((videos / "ゲーム録画 2024年 3月.mp4").exists())


class StartupTest(unittest.TestCase):
    """main.py で実際に起動し、落ちずに動き続けること（起動処理はほかのテストでは通らないため）。"""

    def test_main_starts_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", RECSORT_DATA_DIR=tmp, QT_AUDIO_BACKEND="alsa")
            proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                proc.wait(timeout=6)
                alive = False
            except subprocess.TimeoutExpired:
                alive = True
            finally:
                proc.kill()
                _, err = proc.communicate(timeout=10)
            self.assertTrue(alive, err)  # 起動してすぐ終わっていない
            self.assertNotIn("Traceback", err)
            self.assertTrue((Path(tmp) / "settings.json").exists() or (Path(tmp) / "scan_state.json").exists()
                            or (Path(tmp) / "thumbs").is_dir())


class ErrorLogTest(unittest.TestCase):
    def test_error_log_hides_home_folder(self):
        from storage_organizer import app
        old = sys.excepthook
        with tempfile.TemporaryDirectory() as tmp:
            try:
                app.install_error_log(tmp)
                home = os.path.expanduser("~")
                with mock.patch.object(sys, "__excepthook__", lambda *a: None), \
                        mock.patch("PySide6.QtWidgets.QMessageBox.warning"):
                    try:
                        raise RuntimeError(f"読めません: {home}/Videos/個人の録画.mp4")
                    except RuntimeError:
                        sys.excepthook(*sys.exc_info())
                text = (Path(tmp) / "logs" / "recsort-error.log").read_text(encoding="utf-8")
            finally:
                sys.excepthook = old
        self.assertIn("%USERPROFILE%", text)
        self.assertNotIn(home + "/", text)
        self.assertIn("RecSort 0.9.0rc2", text)  # バージョン更新


class SourceCodeTest(unittest.TestCase):
    """拡張子 .ts の TypeScript などのソースコードを録画と取り違えない。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.videos = base / "Videos"
        self.docs = base / "Documents"
        self.data = base / "data"
        self.videos.mkdir()
        self.docs.mkdir()
        code = b"export function battle(): void {\n  console.log('hit');\n}\n" * 20
        (self.videos / "loose_code.ts").write_bytes(code)          # プロジェクト外にある TypeScript
        proj = self.docs / "dq-explore"
        (proj / "src").mkdir(parents=True)
        (proj / "package.json").write_text("{}")
        (proj / "src" / "battle.ts").write_bytes(code)
        (proj / "demo.mp4").write_bytes(b"\x00" * 100)
        repo = self.docs / "other-repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "game.ts").write_bytes(code)
        self.code = code
        self.patches = [
            mock.patch.object(autoscan, "known_folders", lambda: [("ビデオ", str(self.videos)),
                                                                  ("ドキュメント", str(self.docs))]),
            mock.patch.object(autoscan, "local_drives", lambda: [])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def run_scan(self):
        return autoscan.task_autoscan(self.data, FFMPEG, autoscan.Controller(), lambda p: None)

    def names(self):
        db = Database(self.data)
        try:
            return {r["name"] for r in db.media()}, [(x["path"], x["reason"]) for x in db.skips("excluded")]
        finally:
            db.close()

    def test_typescript_is_not_registered_but_real_mpeg_ts_is(self):
        if FFMPEG.available:
            subprocess.run([FFMPEG.ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=10", "-t", "1",
                            "-f", "mpegts", str(self.videos / "broadcast.ts")], check=True)
        self.run_scan()
        names, skips = self.names()
        self.assertNotIn("loose_code.ts", names)
        self.assertNotIn("battle.ts", names)
        self.assertNotIn("game.ts", names)
        if FFMPEG.available:
            self.assertIn("broadcast.ts", names)  # 本物の録画（MPEG-TS）は対象
        reasons = {Path(p).name: r for p, r in skips}
        self.assertIn("開発用", reasons["dq-explore"])
        self.assertIn("開発用", reasons["other-repo"])

    def test_previously_registered_source_code_is_removed_from_the_list(self):
        db = Database(self.data)
        loc = str(self.docs)
        for path in (self.docs / "dq-explore" / "src" / "battle.ts", self.videos / "loose_code.ts"):
            st = os.stat(path)
            mid = db.insert_raw(str(path), path.name, str(path.parent), st.st_size, st.st_mtime, ".ts", "video", loc,
                                sha256="SAME")
            db.set_decision(mid, "削除候補")
        db.save_inventory(loc, 0)  # 以前の版で登録されていた状態を再現
        self.assertEqual(len(candidates.Context(db).dup_groups), 1)  # 以前の版では重複として提案されていた
        db.close()
        real = os.scandir

        def partly_blocked(path):  # 一部の場所を調査できなかった場合でも外す
            if os.path.basename(os.path.normpath(path)) == "other-repo":
                raise PermissionError(13, "Permission denied", path)
            return real(path)
        with mock.patch.object(autoscan.os, "scandir", side_effect=partly_blocked):
            self.run_scan()
        db = Database(self.data)
        ctx = candidates.Context(db)
        self.assertEqual(ctx.dup_groups, {})
        self.assertNotIn(".ts", {r["ext"] for r in ctx.rows.values()})
        self.assertEqual(candidates.plan_recordings(ctx, 10 ** 9)["items"], [])
        # 仕様変更（DB 廃止）: 一覧から外した行は保存しない。付いていた「削除候補」の判断は外す
        state = json.loads((self.data / "user_state.json").read_text(encoding="utf-8"))
        self.assertFalse(any(v.get("decision") == "削除候補" for k, v in state.items() if k.endswith(".ts")))
        db.close()
        self.assertEqual((self.docs / "dq-explore" / "src" / "battle.ts").read_bytes(), self.code)

    def test_final_confirm_refuses_source_code(self):
        db = Database(self.data)
        path = self.videos / "loose_code.ts"
        st = os.stat(path)
        mid = db.insert_raw(str(path), path.name, str(self.videos), st.st_size, st.st_mtime, ".ts", "video")
        moved = []
        results = trash.move_to_trash(db, [mid], moved.append)
        db.close()
        self.assertFalse(results[0].ok)
        self.assertIn("ソースコード", results[0].message)
        self.assertEqual(moved, [])


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
        # 仕様変更: 達成状況は「チェックしている録画の合計」が基準（以前は提案全体で「達成できます」と表示）
        self.assertIn(p.status_card.value.text().split()[0], ("目標に届きます", "不足"))
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
        protected_path = w.ctx.db.video(pid)["path"]
        p.protect_button.click()
        self.assertIsNone(self.child(p, "short.mp4"))
        self.assertFalse(p.detail.isVisible())  # 候補から外れた録画の詳細は残さない
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

        # 仕様変更: 最終確認はダイアログではなく「最終確認」画面 →「結果」画面で行う
        p.final_button.click()
        flow = w.flow
        self.assertIs(flow.stack.currentWidget(), flow.final)
        self.assertIn("件・合計", flow.final.summary.text())
        self.assertGreater(flow.final.keep_table.rowCount(), 0)  # 残すファイル（重複で残す1本）を分けて示す
        flow.final.backend = backend
        flow.final.move_button.click()
        flow.final.move_button.click()  # 二重に押しても1回だけ
        self.assertEqual(len(moved), 1)
        self.assertIs(flow.stack.currentWidget(), flow.result)
        report = flow.result.summary.text()
        self.assertIn("ごみ箱へ移動: 1 件", report)
        self.assertIn("移動に失敗した: 1 件", report)
        home_button = next(b for b in flow.result.findChildren(QPushButton) if b.text() == "ホームへ")
        home_button.click()
        self.assertIs(w.stack.currentWidget(), w.home)
        db_path = w.ctx.data_dir
        self.w.close()
        db = Database(db_path)
        # 仕様変更（DB 廃止）: ID は起動中だけの番号。保護・判断はパスで保存しているので、パスで探す
        self.assertTrue(db.video(db.id_of(protected_path))["protected"])
        self.assertEqual(next(r["decision"] for r in db.all_rows() if r["name"] == "dark.mp4"), "削除候補")
        db.close()

    @unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
    def test_status_follows_checked_files(self):
        w = self.w
        w.show_plan(None)
        p = self.plan_with(1)
        sizes = {it["kind"]: 0 for it in p.plan["items"]}
        for it in p.plan["items"]:
            sizes[it["kind"]] += it["size"]
        p.target.setDecimals(9)
        # 重複（選択済み）だけでは届かず、要確認（未選択）まで全部選ぶとちょうど届く目標
        p.set_target_gb((sizes["duplicate"] + sizes["review"] - 1) / 1024 ** 3)
        QCoreApplication.processEvents()
        self.assertTrue(p.plan["achieved"])
        self.assertTrue(p.status_card.value.text().startswith("不足"))  # 未選択の要確認は含めない
        self.assertIn("提案をすべて選ぶと", p.status_card.detail.text())
        for name in ("short.mp4", "dark.mp4", "broken.mp4"):
            self.child(p, name).setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(p.status_card.value.text(), "目標に届きます")  # チェックの変更に追従
        self.child(p, "short.mp4").setCheckState(0, Qt.CheckState.Unchecked)
        self.assertTrue(p.status_card.value.text().startswith("不足"))
        selected = sum(w.ctx.db.video(i)["size"] for i in p.checked_ids())
        self.assertIn(format_size(selected), p.status_card.detail.text())

    @unittest.skipUnless(FFMPEG.available, "FFmpeg がありません")
    def test_preview_states_reasons_and_narrow_screen(self):
        w = self.w
        w.resize(911, 512)  # 1366×768・表示倍率150% 相当
        w.show_plan(None)
        p = self.plan_with(1)
        item = self.child(p, "short.mp4")
        p.tree.setCurrentItem(item)
        QCoreApplication.processEvents()
        player = p.detail.player
        self.assertIn(player.state_text, ("読み込み中…", "再生ボタンで再生できます。"))  # 白いままにしない
        self.assertTrue(wait_until(lambda: player.state_text == "再生ボタンで再生できます。", 10))
        player.player.play()
        self.assertTrue(wait_until(lambda: player.state_text == "再生中", 10))
        player.stop_button.click()
        # 理由は詳細パネルで全文を読める（一覧では省略される）
        full = next(it for it in p.plan["items"] if it["id"] == item.data(0, Qt.ItemDataRole.UserRole))["reasons"]
        for reason in full:
            self.assertIn(reason, p.detail.reasons.text())
        # 狭い画面でも、再生の操作（スクロールで届く）と判断の操作が見える
        scroll = p.detail.stack.widget(0)
        scroll.ensureWidgetVisible(player.play_button)
        QCoreApplication.processEvents()
        self.assertFalse(player.play_button.visibleRegion().isEmpty())
        for b in (p.protect_button, p.keep_button, p.final_button):
            self.assertTrue(b.isVisible() and not b.visibleRegion().isEmpty())
        # 再生できない録画は「再生できません」と表示する
        broken = self.child(p, "broken.mp4")
        p.tree.setCurrentItem(broken)
        self.assertTrue(wait_until(lambda: player.state_text.startswith("再生できません"), 10))
        self.assertTrue(player.error_label.text())

    def test_home_shows_no_stale_calculating_after_scan(self):
        w = self.w
        self.assertFalse(w.state.runner.is_running())
        self.assertNotEqual(w.home.saving.value.text(), "算出中")
        self.assertNotIn("調査中", w.home.total.detail.text())
        self.assertNotIn("調査中のため", w.plan.status_card.detail.text())

    def test_check_state_is_reset_when_candidate_kind_changes(self):
        """調査途中（重複の確認前）に「要確認・未選択」だった録画が、確認後に「重複」になったら提案どおり選択する。"""
        w, p = self.w, self.w.plan
        w.show_plan(None)
        p = self.plan_with(1)  # 「要確認」の候補は目標を指定したときだけ出す（仕様）
        dup = next((it for it in (p.plan or {}).get("items", []) if it["kind"] == "duplicate"), None)
        if dup is None:
            self.skipTest("重複の録画がありません（FFmpeg がない環境）")
        db = w.ctx.db
        sha = db.video(dup["id"])["sha256"]
        same = [r["id"] for r in db.media() if r["sha256"] == sha]
        for i in same:  # 確認前の状態を再現
            db.update_video(i, sha256=None)
        w.state.refresh()
        self.assertEqual(self._kind_of(p, dup["id"]), "review")
        self.assertFalse(p._checks[dup["id"]])
        for i in same:
            db.update_video(i, sha256=sha)
        w.state.refresh()
        self.assertEqual(self._kind_of(p, dup["id"]), "duplicate")
        self.assertTrue(p._checks[dup["id"]])

    def _kind_of(self, p, mid):
        return next((it["kind"] for it in p.plan["items"] if it["id"] == mid), None)

    def test_compare_flow_keep_this_and_final(self):
        w = self.w
        subprocess.run([FFMPEG.ffmpeg, "-v", "error", "-i", str(self.env.home / "Videos" / "normal.mp4"), "-vf",
                        "scale=160:90", "-c:v", "libx264", "-crf", "30",
                        str(self.env.home / "Downloads" / "normal_low.mp4")], check=True)
        w.start_scan()
        self.assertTrue(wait_until(lambda: w.state.runner.is_running(), 10))
        self.assertTrue(wait_until(lambda: not w.state.runner.is_running(), 120))
        QCoreApplication.processEvents()
        ctx = w.state.ctx
        self.assertTrue(ctx.video_groups)
        # ホームのおすすめ → 比較して選ぶ
        w.show_section(w.home)
        go = [b for b in w.home.rec.findChildren(QPushButton) if b.text() == "比較して選ぶ"]
        self.assertTrue(go)
        n = ctx.vgroup_of[next(i for i in ctx.rows if ctx.rows[i]["name"] == "normal_low.mp4")]
        w.open_group("similar", n)
        cmp = w.flow.compare
        self.assertIs(w.flow.stack.currentWidget(), cmp)
        self.assertIn("同じ内容とは限りません", cmp.reason.text())
        self.assertEqual(len(cmp.frame_labels), len(ctx.video_groups[n]))
        # 完全重複のコピー（normal (1).mp4）は類似グループに入れない（重複グループで扱い、二重計上しない）
        self.assertNotIn("normal (1).mp4", {ctx.rows[i]["name"] for i in ctx.video_groups[n]})
        dup_members = {i for ids in ctx.dup_groups.values() for i in ids}
        for g in ctx.video_groups:
            self.assertLessEqual(len(set(g) & dup_members), 1)
        self.assertTrue(wait_until(lambda: all(lb.pixmap() is not None and not lb.pixmap().isNull()
                                               for lbs in cmp.frame_labels.values() for lb in lbs), 30))
        low = next(i for i in ctx.rows if ctx.rows[i]["name"] == "normal_low.mp4")
        orig = next(i for i in ctx.rows if ctx.rows[i]["name"] == "normal.mp4")
        cmp._keep_this(orig)
        self.assertEqual(w.ctx.db.video(orig)["decision"], "残す")
        self.assertEqual(w.ctx.db.video(low)["decision"], "削除候補")
        self.assertTrue(os.path.exists(w.ctx.db.video(low)["path"]))  # 比較中はファイルを移動しない
        cmp.next_button.click()
        self.assertIs(w.flow.stack.currentWidget(), w.flow.final)
        self.assertIn(low, w.flow.final.ids)
        self.assertNotIn(orig, w.flow.final.ids)
        w.flow.final.back_button.click()
        self.assertIs(w.flow.stack.currentWidget(), w.plan)
        # すべて残す・保留
        w.open_group("similar", n)
        cmp.keep_all.click()
        self.assertEqual({w.ctx.db.video(i)["decision"] for i in (orig, low)}, {"残す"})
        self.assertFalse(any(it["id"] in (orig, low) for it in w.plan.plan["items"]))

    def test_unchecked_and_scanning_notes_are_shown(self):
        w = self.w
        db = w.ctx.db
        vid = next(r for r in db.media() if r["kind"] == "video" and r["sha256"]) if any(
            r["sha256"] for r in db.media() if r["kind"] == "video") else None
        if vid is None:
            self.skipTest("重複の録画がありません（FFmpeg がない環境）")
        db.update_video(vid["id"], sha256=None)
        w.state.refresh()
        w.show_plan(None)
        self.assertIn("重複の確認が終わっていない録画", w.plan.status_card.detail.text())
        w.start_scan()
        self.assertTrue(wait_until(lambda: w.state.runner.is_running(), 10))
        w.state.refresh()
        self.assertIn("調査中のため", w.plan.status_card.detail.text())
        self.assertTrue(wait_until(lambda: not w.state.runner.is_running(), 120))
        QCoreApplication.processEvents()
        self.assertNotIn("調査中のため", w.plan.status_card.detail.text())

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
    # test_experimental_menu_toggle は削除: 仕様変更で動画AI（試作）をアプリから外したため（RELEASE_CHECKLIST 参照）

if __name__ == "__main__":
    unittest.main()
