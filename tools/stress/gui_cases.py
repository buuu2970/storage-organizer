"""区分 E: GUI の状態遷移・反復操作・終了・再起動（画面なしモード）。"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path
from unittest import mock

from common import Fail, MockTrash, check, fake_mp4, unique
from storage_organizer import autoscan, media
from storage_organizer.formatting import format_size
from storage_organizer.store import Store

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

APP = QApplication.instance() or QApplication([])
NO_FF = media.FFmpegTools(None, None)


def pump(sec=0.05):
    end = time.time() + sec
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def case_gui(seed: int, work: Path, rec: dict) -> None:
    from storage_organizer.main_window import MainWindow
    rng = random.Random(seed)
    root = work / "Videos"
    groups = []  # 同じ内容のファイルのパス（正解）
    for _ in range(rng.randint(1, 3)):
        c = os.urandom(rng.randint(100, 2000))
        groups.append([str(fake_mp4(unique(rng, root / rng.choice(["a", "b c", "録画"]), ".mp4"), c))
                       for _ in range(rng.randint(2, 3))])
    for _ in range(rng.randint(0, 3)):
        fake_mp4(unique(rng, root / "single", ".mp4"), os.urandom(rng.randint(100, 3000)))
    data = work / "data"
    broken = rng.random() < 0.15
    if broken:
        data.mkdir(parents=True)
        (data / "user_state.json").write_text("[]", encoding="utf-8")
    loc = autoscan.Location(str(root), "known", "ビデオ", 1)
    patches = [mock.patch.object(autoscan, "build_locations", lambda db: ([loc], []))]
    errors, messages = [], []
    old_hook = sys.excepthook
    sys.excepthook = lambda *a: errors.append("".join(__import__("traceback").format_exception(*a))[-1500:])
    for n in ("information", "warning", "question"):
        patches.append(mock.patch.object(QMessageBox, n, staticmethod(
            lambda *a, **k: (messages.append(str(a[1:3])), QMessageBox.StandardButton.Yes)[1])))
    for p in patches:
        p.start()
    rec["setup"] = {"groups": [len(g) for g in groups], "broken_protection": broken}
    w = None
    try:
        def open_window():
            win = MainWindow(Store(data), start_autoscan=False)
            win.tools = lambda: NO_FF
            win.state.tools_fn = lambda: NO_FF
            win.resize(*rng.choice([(911, 512), (1280, 800), (1100, 620)]))
            win.show()
            pump()
            return win

        def close_window(win):
            win.close()
            win.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            pump()

        w = open_window()
        w.start_scan()
        end = time.time() + 30
        while time.time() < end and (w.state.runner.is_running() or not w.state.ctx.rows):
            pump(0.05)
        check(not w.state.runner.is_running(), "調査が終わらない", rec)
        protected_paths: set[str] = set()

        def verify(tag):
            check(not errors, f"{tag}: 想定外の例外 {errors[:1]}", rec)
            for g in groups:
                check(any(os.path.exists(p) for p in g), f"{tag}: 同じ内容のファイルがすべてなくなった", rec)
            for p in protected_paths:
                check(os.path.exists(p), f"{tag}: 保護したファイルがなくなった", rec)
            plan = w.plan
            if plan.plan is not None:
                ids = plan.checked_ids()
                total = sum(os.path.getsize(w.store.video(i)["path"]) for i in ids
                            if os.path.exists(w.store.video(i)["path"]))
                check(format_size(total) in plan.selected_label.text(),
                      f"{tag}: 画面の選択中の容量が実際のファイルサイズと違う", rec)
                check(plan.final_button.isEnabled() == (bool(ids) and w.store.protection_ok),
                      f"{tag}: 「最終確認へ」の有効・無効が状態と違う", rec)
                if not w.store.protection_ok:
                    check(not plan.protect_button.isEnabled() and not plan.keep_button.isEnabled(),
                          f"{tag}: 保護情報の復旧待ちなのに判断のボタンが押せる", rec)

        for step in range(rng.randint(8, 15)):
            op = rng.choice(["nav", "plan", "select", "toggle", "protect", "keep", "compare", "final", "resize",
                             "restart", "final"])
            rec["ops"].append(op)
            plan = w.plan
            if op == "nav":
                w.show_section(rng.choice(w.sections))
            elif op == "plan":
                w.show_plan(rng.choice([0.0, 0.000001, 0.00001, 5.0]))
            elif op in ("select", "toggle", "protect", "keep"):
                w.show_plan(None)
                rows = [plan.tree.topLevelItem(i).child(j) for i in range(plan.tree.topLevelItemCount())
                        for j in range(plan.tree.topLevelItem(i).childCount())]
                files = [r for r in rows if r.data(0, Qt.ItemDataRole.UserRole) is not None]
                if files:
                    it = rng.choice(files)
                    plan.tree.setCurrentItem(it)
                    if op == "toggle" and it.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                        on = it.checkState(0) == Qt.CheckState.Checked
                        it.setCheckState(0, Qt.CheckState.Unchecked if on else Qt.CheckState.Checked)
                    elif op == "protect" and plan.protect_button.isEnabled():
                        path = w.store.video(it.data(0, Qt.ItemDataRole.UserRole))["path"]
                        plan.protect_button.click()
                        if w.store.protection_ok:
                            protected_paths.add(path)
                    elif op == "keep" and plan.keep_button.isEnabled():
                        plan.keep_button.click()
            elif op == "compare":
                ctx = w.state.ctx
                if ctx.dup_groups:
                    key = rng.choice(list(ctx.dup_groups))
                    w.open_group("duplicate", key)
                    cmp = w.flow.compare
                    action = rng.choice(["keep_this", "keep_all", "hold", "back"])
                    members = cmp.members()
                    if action == "keep_this" and members and w.store.protection_ok:
                        cmp._keep_this(rng.choice(members))
                    elif action == "keep_all" and w.store.protection_ok:
                        cmp.keep_all.click()
                    elif action == "hold" and w.store.protection_ok:
                        cmp.hold.click()
                    else:
                        cmp.back_button.click()
            elif op == "final":
                w.show_plan(None)
                if plan.final_button.isEnabled():
                    plan.final_button.click()
                    final = w.flow.final
                    fails = {Path(w.store.video(i)["path"]).name for i in final.ids if rng.random() < 0.2}

                    def slow(count, path):
                        time.sleep(0.05)
                    final.backend = MockTrash(work / "trash", fails, slow)
                    ticks = []
                    timer = QTimer()
                    timer.timeout.connect(lambda: ticks.append(1))
                    timer.start(10)
                    final.move_button.click()
                    final.move_button.click()  # 連打
                    if rng.random() < 0.3:
                        pump(0.03)
                        final.cancel()
                    end = time.time() + 30
                    while time.time() < end and final.is_running():
                        pump(0.02)
                    timer.stop()
                    check(not final.is_running(), "移動が終わらない（実行中のまま）", rec)
                    check(ticks, "移動中に画面のイベントが処理されなかった", rec)
                    res = w.flow.result.results if w.flow.stack.currentWidget() is w.flow.result else []
                    for r in res:
                        check(os.path.exists(r.path) != (r.status == "moved"), "結果と実ファイルの状態が違う", rec)
                    w.flow.result.next_candidates.emit()
            elif op == "resize":
                w.resize(*rng.choice([(911, 512), (1280, 800), (1366, 728), (880, 520)]))
            elif op == "restart":
                before = {r["path"]: bool(r["protected"]) for r in w.store.media()}
                close_window(w)
                w = open_window()
                after = {r["path"]: bool(r["protected"]) for r in w.store.media()}
                for p, v in before.items():
                    if p in after:
                        check(after[p] == v, "再起動で保護の状態が変わった", rec)
                if broken:
                    check(not w.store.protection_ok, "再起動で保護情報の復旧待ちが解除された", rec)
            pump(0.02)
            verify(f"step{step}:{op}")
        rec["messages"] = messages[-5:]
    finally:
        if w is not None:
            try:
                w.flow.final.wait()
                w.close()
                w.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            except Exception:  # noqa: BLE001
                pass
        for p in patches:
            p.stop()
        sys.excepthook = old_hook
    if errors:
        raise Fail(f"想定外の例外: {errors[0]}")
