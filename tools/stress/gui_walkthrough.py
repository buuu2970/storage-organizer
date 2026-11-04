"""GUI の通し確認（画面なしモード）。各状態で表示と実際の状態を照合し、スクリーンショットを保存する。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_AUDIO_BACKEND", "alsa")
from common import MockTrash, fake_mp4  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication([])
from storage_organizer import autoscan, media, theme  # noqa: E402
from storage_organizer.app import install_japanese  # noqa: E402
from storage_organizer.formatting import format_size  # noqa: E402
from storage_organizer.main_window import MainWindow  # noqa: E402
from storage_organizer.store import Store  # noqa: E402

install_japanese(app)
theme.apply(app)
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "gui_shots")
OUT.mkdir(parents=True, exist_ok=True)
log = []
for n in ("information", "warning", "question"):
    setattr(QMessageBox, n, staticmethod(lambda *a, **k: (log.append(["message", str(a[1:3])]),
                                                           QMessageBox.StandardButton.Yes)[1]))


PATCHES = []


def pump(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def check(name, cond):
    log.append([name, bool(cond)])


def window(data, root, tools):
    loc = autoscan.Location(str(root), "known", "ビデオ", 1)
    # FFmpeg を探す処理そのものを差し替える（画面の部品は作成時の関数を使うため）
    finder = mock.patch.object(media, "find_ffmpeg", lambda *a, **k: tools)
    finder.start()
    PATCHES.append(finder)
    w = MainWindow(Store(data), start_autoscan=False)
    w.resize(1280, 800)
    w.show()
    with mock.patch.object(autoscan, "build_locations", lambda db: ([loc], [])):
        w.start_scan()
        end = time.time() + 60
        while time.time() < end and (w.state.runner.is_running() or w.state.scan_state()["kind"] == "未調査"):
            pump(0.1)
    pump(0.5)
    return w


def shot(w, name):
    w.grab().save(str(OUT / f"{name}.png"))


def run():
    tools = media.find_ffmpeg()
    work = Path(tempfile.mkdtemp())
    root = work / "Videos"
    same = os.urandom(3000)
    for p in ("録画/ボス戦 第1回.mp4", "バックアップ/ボス戦 第1回 (1).mp4", "バックアップ/ボス戦 第1回 (2).mp4"):
        fake_mp4(root / p, same)
    fake_mp4(root / "単独/配信.mp4", os.urandom(2000))
    data = work / "data"
    # 1. 正常な流れ（FFmpeg なし = 解析の一部が使えない状態の表示も確認）
    w = window(data, root, media.FFmpegTools(None, None))
    shot(w, "01_home_ffmpeg_missing")
    check("ホーム: 主な操作が「おすすめを確認する」", w.home.primary.text() == "おすすめを確認する")
    check("ホーム: もう一度調べる", w.home.scan_button.text() == "PCをもう一度調べる")
    check("ホームに FFmpeg 不足の案内が出る", w.home.banner.isVisibleTo(w.home))
    check("調査状態が完了", w.state.scan_state()["kind"] == "調査完了")
    w.show_plan(None)
    pump()
    p = w.plan
    ids = p.checked_ids()
    total = sum(os.path.getsize(w.store.video(i)["path"]) for i in ids)
    check("候補: 選択中の容量が実ファイルの合計と一致", format_size(total) in p.selected_label.text())
    check("候補: 最終確認へが有効", p.final_button.isEnabled())
    shot(w, "02_candidates")
    key = next(iter(w.state.ctx.dup_groups))
    w.open_group("duplicate", key)
    pump()
    shot(w, "03_compare")
    # 仕様変更（初心者向け UX）: 2 本を並べ、3 本以上は選択欄で切り替える
    check("比較: 2本を並べている", len(w.flow.compare.frame_labels) == 2)
    check("比較: 3本すべてを選択欄で選べる", w.flow.compare.left_combo.count() == 3)
    check("比較: 「すべて残す」の文言", w.flow.compare.keep_all.text() == "すべて残す")
    w.flow.compare.back_button.click()
    p.final_button.click()
    pump()
    final = w.flow.final
    shot(w, "04_final")
    check("最終確認: 件数が選択と一致", len(final.ids) == len(ids))
    check("最終確認: 残すファイルを分けて表示", final.keep_table.rowCount() >= 1)
    # 一部失敗＋途中中断
    names = [Path(w.store.video(i)["path"]).name for i in final.ids]
    final.backend = MockTrash(work / "trash", fail_names=names[:1], hook=lambda c, path: time.sleep(0.4))
    final.move_button.click()
    pump(0.15)
    shot(w, "05_moving")
    check("移動中: 戻るボタンが無効", not final.back_button.isEnabled())
    check("移動中: 中断ボタンが表示", final.cancel_button.isVisibleTo(final))
    end = time.time() + 30
    while time.time() < end and final.is_running():
        pump(0.05)
    shot(w, "06_result_partial_failure")
    res = w.flow.result.results
    check("結果: 失敗を含む", any(r.status == "failed" for r in res))
    check("結果: 表示と実ファイルが一致", all(os.path.exists(r.path) != (r.status == "moved") for r in res))
    check("結果: 重複の最後の1本が残る", any((root / q).exists() for q in
                                       ("録画/ボス戦 第1回.mp4", "バックアップ/ボス戦 第1回 (1).mp4",
                                        "バックアップ/ボス戦 第1回 (2).mp4")))
    w.close()
    keep = w.flow.compare.pair[0]
    # 2. 候補なし
    work2 = Path(tempfile.mkdtemp())
    fake_mp4(work2 / "Videos" / "ひとつだけ.mp4", os.urandom(500))
    w2 = window(work2 / "data", work2 / "Videos", tools)
    w2.show_plan(None)
    pump()
    shot(w2, "07_no_candidates")
    check("候補なし: 空の表示", w2.plan.content.currentWidget() is w2.plan.empty)
    check("候補なし: 最終確認へが無効", not w2.plan.final_button.isEnabled())
    w2.close()
    # 3a. 保護情報の破損（直前の正常なバックアップあり → 復旧して続けられる）
    (data / "user_state.json").write_text("[]", encoding="utf-8")
    w3 = window(data, root, tools)
    shot(w3, "08_broken_protection_recovered_from_backup")
    check("破損（バックアップあり）: 復旧した旨の案内", w3.home.data_banner.isVisibleTo(w3.home)
          and "復旧" in w3.home.data_banner.toolTip())
    check("破損（バックアップあり）: 整理を続けられる", w3.store.protection_ok)
    w3.close()
    # 3b. 保護情報の破損（バックアップなし → 整理操作を止める）
    for f in data.glob("user_state.json*"):
        f.unlink()
    (data / "user_state.json").write_text("[]", encoding="utf-8")
    w4 = window(data, root, tools)
    shot(w4, "09_broken_protection_home")
    check("破損（バックアップなし）: ホームに整理停止の案内", w4.home.data_banner.isVisibleTo(w4.home)
          and "止めています" in w4.home.data_banner.text.text())
    w4.show_plan(None)
    pump()
    if w4.plan.tree.topLevelItemCount():
        top = w4.plan.tree.topLevelItem(0)
        w4.plan.tree.setCurrentItem(top.child(top.childCount() - 1))
    pump()
    shot(w4, "10_broken_protection_candidates")
    check("破損（バックアップなし）: 最終確認へが無効", not w4.plan.final_button.isEnabled())
    check("破損（バックアップなし）: 保護するが無効", not w4.plan.protect_button.isEnabled())
    w4.close()
    for d in (work, work2):
        shutil.rmtree(d, ignore_errors=True)


run()
for p_ in PATCHES:
    p_.stop()
(OUT / "walkthrough.json").write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
bad = [x for x in log if x[0] != "message" and not x[1]]
print("確認", sum(1 for x in log if x[0] != "message"), "件 / 不一致", len(bad), bad)
