"""反復試験の各区分（GUI 以外）。1回の試行で複数の操作を続け、処理をまたいだ整合性を確かめる。"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import numpy as np

from common import (ROOT, Fail, MockTrash, check, fake_mp4, frames_family, mild_variant, sha,
                    to_sig, unique)
from storage_organizer import autoscan, candidates, media, similar, trash
from storage_organizer.store import Store, StoreError

NO_FF = media.FFmpegTools(None, None)


def corrupt(path: Path, how: str) -> None:
    if how == "missing":
        if path.exists():
            path.unlink()
        return
    text = {"list": "[]", "null": "null", "number": "123", "truncated": None, "garbage": None,
            "wrongtype": None}[how]
    if how == "truncated":
        raw = path.read_bytes() if path.exists() else b'{"a": 1}'
        path.write_bytes(raw[: max(1, len(raw) // 2)])
    elif how == "garbage":
        path.write_bytes(os.urandom(64))
    elif how == "wrongtype":
        path.write_text(json.dumps({"x": {"protected": "yes", "decision": 5}}), encoding="utf-8")
    else:
        path.write_text(text, encoding="utf-8")


# =============================================================================
# 区分 A: 保存・破損・復旧・再起動
# =============================================================================
CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, {root!r})
from storage_organizer.store import Store
import storage_organizer.store as st
s = Store({data!r})
ids = [s.id_of(p) for p in {paths!r}]
calls = [0]
real = os.replace
def crashing(a, b):
    calls[0] += 1
    if calls[0] == {crash_at}:
        os._exit(3)  # 書き込みの途中（置き換え直前）で異常終了
    return real(a, b)
st.os.replace = crashing
for i, (k, v) in enumerate({ops!r}):
    if k == "protect":
        s.set_protected(ids[i % len(ids)], v)
    else:
        s.set_decision(ids[i % len(ids)], v)
os._exit(0)
"""


def case_storage(seed: int, work: Path, rec: dict) -> None:
    rng = random.Random(seed)
    d = work / "data"
    media_dir = work / "media"
    s = Store(d)
    paths = []
    for _ in range(rng.randint(3, 10)):
        p = fake_mp4(unique(rng, media_dir, ".mp4", long=rng.random() < 0.2), os.urandom(rng.randint(10, 300)))
        paths.append(p)
        st = p.stat()
        s.insert_raw(str(p), p.name, str(p.parent), st.st_size, st.st_mtime, ".mp4", location="L")
    s.save_inventory("L", 1)
    rec["setup"] = {"files": len(paths)}
    history = [{}]  # 保存に成功した user_state の状態（最後が最新）
    expected: dict[str, dict] = {}
    for _ in range(rng.randint(3, 9)):
        p = rng.choice(paths)
        mid = s.id_of(str(p))
        op = rng.choice(["protect", "unprotect", "decide", "decide_none", "fail_protect", "setting"])
        rec["ops"].append([op, p.name])
        if op == "fail_protect":  # 書き込みの失敗（容量不足）を注入
            before = dict(s.video(mid))
            with mock.patch("storage_organizer.store.os.fsync", side_effect=OSError(28, "No space left")):
                try:
                    s.set_protected(mid, not before["protected"])
                    raise Fail("保存に失敗したのに例外にならない")
                except StoreError:
                    pass
            check(s.video(mid)["protected"] == before["protected"], "保存失敗後に画面上の保護状態が変わった", rec)
            continue
        if op == "setting":
            s.set_setting("ffmpeg_dir", f"C:/ff{rng.randint(0, 9)}")
            continue
        e = dict(expected.get(str(p), {}))
        if op == "protect":
            s.set_protected(mid, True)
            e["protected"] = True
            if e.get("decision") == "削除候補":
                e.pop("decision")
        elif op == "unprotect":
            s.set_protected(mid, False)
            e.pop("protected", None)
        elif op == "decide":
            v = rng.choice(["残す", "保留", "削除候補"])
            try:
                s.set_decision(mid, v)
                e["decision"] = v
            except PermissionError:
                check(e.get("protected"), "保護していないのに削除候補にできない", rec)
        else:
            s.set_decision(mid, None)
            e.pop("decision", None)
        if e:
            expected[str(p)] = e
        else:
            expected.pop(str(p), None)
        history.append({k: dict(v) for k, v in expected.items()})
    on_disk = json.loads((d / "user_state.json").read_text(encoding="utf-8")) if (d / "user_state.json").exists() else {}
    check({k: {kk: bool(vv) if kk == "protected" else vv for kk, vv in v.items()} for k, v in on_disk.items()}
          == {k: {kk: bool(vv) if kk == "protected" else vv for kk, vv in v.items()} for k, v in expected.items()},
          "保存された保護・判断が、行った操作と一致しない", rec)
    # 破損 → 再起動
    target = rng.choice(["none", "settings.json", "user_state.json", "scan_state.json", "analysis.jsonl", "inventory",
                         "crash"])
    how = rng.choice(["list", "null", "number", "truncated", "garbage", "wrongtype", "missing"])
    rec["ops"].append(["corrupt", target, how])
    saves = len(history) - 1
    if target == "crash":
        ops = [(rng.choice(["protect", "decide"]), rng.choice([True, "残す", "保留"])) for _ in range(4)]
        ops = [("protect", True) if k == "protect" else ("decide", v if isinstance(v, str) else "保留") for k, v in ops]
        s.close()
        crash_at = rng.randint(1, 6)
        code = CRASH_CHILD.format(root=str(ROOT), data=str(d), paths=[str(p) for p in paths], ops=ops,
                                  crash_at=crash_at)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        rec["ops"].append(["child_exit", out.returncode])
        s2 = Store(d)
        check(s2.protection_ok, "途中で異常終了したあと、保護情報が読めなくなった", rec)
        check(not any("user_state" in x for x in s2.load_problems) or any("復旧" in x for x in s2.load_problems),
              f"異常終了後の読み込みで問題: {s2.load_problems}", rec)
        return
    if target == "inventory":
        files = list((d / "inventory").glob("*.json.gz"))
        if files:
            files[0].write_bytes(os.urandom(40) if how != "missing" else b"")
    elif target != "none":
        corrupt(d / target, how)
    s.close() if hasattr(s, "close") else None
    s2 = Store(d)  # 起動直後に落ちない
    candidates.Context(s2)
    if target == "user_state.json" and how != "missing":
        if saves >= 2:  # 直前の正常なバックアップから復旧できる
            check(s2.protection_ok, "バックアップがあるのに整理禁止になった", rec)
            exp = history[-2]
            got = {k: {kk: bool(vv) if kk == "protected" else vv for kk, vv in v.items()} for k, v in s2._user.items()}
            want = {k: {kk: bool(vv) if kk == "protected" else vv for kk, vv in v.items()} for k, v in exp.items()}
            check(got == want, "バックアップから復旧した保護・判断が、直前の保存内容と違う", rec)
        elif saves >= 1 and how in ("list", "null", "number", "truncated", "garbage", "wrongtype"):
            check(not s2.protection_ok, "読めない保護情報を「保護なし」として扱った", rec)
            for _ in range(2):  # 再起動をまたいでも整理禁止が続く
                s3 = Store(d)
                check(not s3.protection_ok, "再起動で整理禁止が解除された", rec)
            any_id = s3.id_of(str(paths[0]))
            if any_id is not None:
                moved = []
                res = trash.move_to_trash(s3, [any_id], moved.append)
                check(not moved and res[0].status == "skipped", "保護情報が読めないのに移動した", rec)
            s3.reset_user_state()
            check(Store(d).protection_ok, "明示の初期化後も整理禁止のまま", rec)
    elif target == "user_state.json" and how == "missing":
        if saves >= 2:
            check(s2.protection_ok, "削除されたがバックアップがあるのに復旧しない", rec)
        rec["setup"]["note"] = "user_state.json の削除（外部からの削除）は、バックアップがなければ初回起動と区別できない（制限事項）"
    else:
        check(s2.protection_ok, f"{target} の破損で整理禁止になった", rec)
        if target != "inventory":
            for p in paths:
                mid = s2.id_of(str(p))
                if mid is not None:
                    want = expected.get(str(p), {})
                    check(bool(s2.video(mid)["protected"]) == bool(want.get("protected")),
                          "別のファイルの破損で保護状態が変わった", rec)
    if target == "settings.json" and how != "missing":
        check(s2.get_setting("ffmpeg_dir") is None or isinstance(s2.get_setting("ffmpeg_dir"), str),
              "壊れた設定の値をそのまま使った", rec)


# =============================================================================
# 区分 B: スキャン・除外・キャンセル・再調査
# =============================================================================
def case_scan(seed: int, work: Path, rec: dict) -> None:
    rng = random.Random(seed)
    root = work / "drive"
    personal: set[str] = set()   # 一覧に出るべき録画（正解）
    hidden_away: set[str] = set()  # 出てはいけないもの
    drive_mode = rng.random() < 0.5
    dirs = []
    for _ in range(rng.randint(1, 4)):
        fname = rng.choice(["Downloads", "録画", "Videos", "ゲーム 録画", "clips", "Recordings", "まとめ"]) + str(rng.randint(0, 99))
        folder = root / fname
        dirs.append(folder)
        for _ in range(rng.randint(1, 4)):
            p = fake_mp4(unique(rng, folder, ".mp4", long=rng.random() < 0.15), os.urandom(rng.randint(50, 500)))
            personal.add(str(p))
        if rng.random() < 0.6:
            (folder / f"setup{rng.randint(0, 9)}{rng.choice(['.exe', '.msi'])}").write_bytes(b"MZ")
        if rng.random() < 0.3:
            (folder / "メモ.txt").write_text("x")
    if drive_mode:
        app = root / "Tools" / f"App{rng.randint(0, 9)}"
        (app / "core.dll").parent.mkdir(parents=True, exist_ok=True)
        (app / "core.dll").write_bytes(b"MZ")
        (app / "app.exe").write_bytes(b"MZ")
        hidden_away.add(str(fake_mp4(app / "intro.mp4", os.urandom(80))))
    proj = root / "proj"
    proj.mkdir(parents=True)
    (proj / "package.json").write_text("{}")
    hidden_away.add(str(fake_mp4(proj / "demo.mp4", os.urandom(50))))
    hidden = root / ".hidden_cache"
    hidden_away.add(str(fake_mp4(hidden / "h.mp4", os.urandom(50))))
    if rng.random() < 0.5:
        try:
            os.symlink(root, dirs[0] / "loop")  # 循環するリンク
        except OSError:
            pass
    blocked = rng.choice(dirs) if rng.random() < 0.3 else None
    rec["setup"] = {"drive_mode": drive_mode, "personal": len(personal), "blocked": str(blocked) if blocked else None}
    data = work / "data"
    loc = autoscan.Location(str(root), "drive" if drive_mode else "known", "テスト", 1)
    tools = NO_FF if rng.random() < 0.5 else media.find_ffmpeg()
    real_scandir = os.scandir

    def scandir(p):
        if blocked is not None and os.path.normpath(p) == os.path.normpath(str(blocked)):
            raise PermissionError(13, "Permission denied", str(p))
        return real_scandir(p)

    def run(cancel_after=None):
        ctl = autoscan.Controller()
        count = [0]

        def report(pr):
            count[0] += 1
            if cancel_after is not None and count[0] >= cancel_after:
                ctl.cancel()
        with mock.patch.object(autoscan, "build_locations", lambda db: ([loc], [])), \
                mock.patch.object(autoscan.os, "scandir", side_effect=scandir):
            return autoscan.task_autoscan(store, tools, ctl, report)

    store = Store(data)
    if rng.random() < 0.5:  # 途中でキャンセル
        k = rng.randint(1, 3)
        summary = run(cancel_after=k)
        rec["ops"].append(["scan_cancel", k, summary.get("cancelled")])
        if summary.get("cancelled"):
            check(store.get_setting("scan_finished") is None, "キャンセルしたのに調査完了の日時を記録した", rec)
            check(store.get_setting("analysis_state") != "done", "キャンセルしたのに解析完了と記録した", rec)
    summary = run()
    rec["ops"].append(["scan", summary.get("cancelled")])
    check(not summary.get("cancelled"), "最後まで調べたのに中断扱い", rec)
    check(store.get_setting("analysis_state") == "done" and store.get_setting("scan_finished"),
          "最後まで調べたのに完了を記録していない", rec)
    got = {r["path"] for r in store.media()}
    exp = {p for p in personal if blocked is None or not p.startswith(str(blocked) + os.sep)}
    check(exp <= got, f"個人のフォルダの録画が見つからない: {sorted(exp - got)[:3]}", rec)
    check(not (got & hidden_away), f"除外すべきファイルが一覧に出た: {sorted(got & hidden_away)[:3]}", rec)
    status = store.locations()[0]["status"]
    check(status == ("一部スキップ" if blocked is not None else "完了"), f"調査状態の表示が実際と違う: {status}", rec)
    excluded = {os.path.normpath(x["path"]) for x in store.skips("excluded")}
    for dpath in dirs:
        check(os.path.normpath(str(dpath)) not in excluded, f"個人のフォルダを除外と表示: {dpath.name}", rec)
    # 追加・変更・削除 → 再調査
    victims = sorted(exp)
    if victims:
        gone = rng.choice(victims)
        os.remove(gone)
        exp.discard(gone)
        rec["ops"].append(["delete", Path(gone).name])
    changed = None
    if len(victims) > 1:
        changed = rng.choice(sorted(exp))
        with open(changed, "ab") as f:
            f.write(b"changed")
        rec["ops"].append(["modify", Path(changed).name])
    newp = fake_mp4(unique(rng, dirs[0] if blocked != dirs[0] else root / "new", ".mp4"), os.urandom(70))
    exp.add(str(newp))
    rec["ops"].append(["add", newp.name])
    if blocked is not None:
        blocked_files = {p for p in personal if p.startswith(str(blocked) + os.sep)}
        exp |= {p for p in blocked_files if os.path.exists(p)} & got  # 前回分（読めないフォルダ）は残っていてよい
    summary = run()
    got = {r["path"] for r in store.media()}
    check(exp - {p for p in exp if not os.path.exists(p)} <= got, "再調査で追加・既存の録画が見つからない", rec)
    if victims and blocked is None:
        check(gone not in got, "削除した録画が一覧に残った", rec)
    if changed:
        row = store.video(store.id_of(changed))
        check(row["size"] == os.path.getsize(changed), "変更したファイルのサイズが古いまま", rec)
    store.close()
    s2 = Store(data)  # 再起動後も同じ一覧
    check({r["path"] for r in s2.media()} == got, "再起動で一覧が変わった", rec)


# =============================================================================
# 区分 C: 重複・類似・グループ化・容量計算
# =============================================================================
def case_groups(seed: int, work: Path, rec: dict) -> None:
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    H, W = similar.H, similar.W
    items, origin, mild_pairs = {}, {}, []
    nid = 1
    # 同じ内容として扱う素材は、素材ごとに確実に違う内容になる種類だけ（試験ハーネスの修正: 以前は
    # 決め打ちの市松模様やグラデーションで「別の素材」が見た目上同じになり、混入と誤って数えていた）
    kinds = ["scene", "scene", "text"]
    for b in range(rng.randint(2, 8)):
        kind = rng.choice(kinds)
        base = frames_family(nrng, kind, H, W)
        dur = rng.choice([60.0, 60.5, 61.0, 300.0, 1800.0])
        items[nid], origin[nid] = to_sig(base, dur, similar.VERSION), b
        first = nid
        nid += 1
        for _ in range(rng.randint(0, 3)):
            items[nid], origin[nid] = to_sig(mild_variant(nrng, base, allow_downscale=kind == "scene"),
                                             dur + rng.uniform(-0.4, 0.4), similar.VERSION), b
            mild_pairs.append((first, nid))
            nid += 1
    for _ in range(rng.randint(0, 3)):  # 黒画面・情報の少ない映像
        items[nid], origin[nid] = to_sig(frames_family(nrng, rng.choice(["black", "lowinfo"]), H, W), 60.0,
                                         similar.VERSION), f"x{nid}"
        nid += 1
    rec["setup"] = {"videos": len(items), "mild_pairs": len(mild_pairs)}
    cancel = threading.Event()
    stop_at = rng.choice([None, None, "prepare", "compare", "group"])
    if stop_at:
        def progress(stage, done, total):
            if stage == stop_at:
                cancel.set()
        try:
            similar.group(items, cancel=cancel, progress=progress)
            raise Fail(f"{stop_at} 段階でキャンセルしたのに完了した")
        except similar.Cancelled:
            rec["ops"].append(["cancel", stop_at])
    groups, stats = similar.group(items)
    rec["ops"].append(["group", len(groups), stats])
    for g in groups:
        check(len({origin[i] for i in g}) == 1, f"別の内容の動画が同じグループに混ざった: {[origin[i] for i in g]}", rec)
        check(not any(str(origin[i]).startswith("x") for i in g), "黒画面・情報の少ない動画がグループになった", rec)
    grouped = {frozenset(g) for g in groups}
    for a, b in mild_pairs:
        check(any(a in g and b in g for g in grouped), "同じ録画の軽い変形（再圧縮・縮小相当）を見逃した", rec)
    seen = [i for g in groups for i in g]
    check(len(seen) == len(set(seen)), "同じ動画が2つのグループに入った", rec)
    # 完全重複と容量（実ファイル）
    d = work / "files"
    s = Store(work / "data")
    contents = [os.urandom(rng.randint(20, 400)) for _ in range(rng.randint(2, 6))]
    by_hash: dict[str, list[int]] = {}
    drives = {}
    for c in contents:
        for _ in range(rng.randint(1, 4)):
            sub = rng.choice(["c_side", "d_side"])
            p = fake_mp4(unique(rng, d / sub, ".mp4"), c)
            st = p.stat()
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            mid = s.insert_raw(str(p), p.name, str(p.parent), st.st_size, st.st_mtime, ".mp4", sha256=h)
            by_hash.setdefault(h, []).append(mid)
            drives[str(p)] = "C:\\" if sub == "c_side" else "D:\\"
            if rng.random() < 0.15:  # 同じファイルを別の表記で二重に登録
                alias = str(p.parent) + os.sep + "." + os.sep + p.name
                s.insert_raw(alias, p.name, str(p.parent), st.st_size, st.st_mtime, ".mp4", sha256=h)
                drives[alias] = drives[str(p)]
    for mid in [m for ms in by_hash.values() for m in ms]:
        r = rng.random()
        if r < 0.15:
            s.set_protected(mid, True)
        elif r < 0.25:
            s.set_decision(mid, rng.choice(["残す", "保留"]))
    target_drive = rng.choice([None, "C:\\", "D:\\"])
    target = rng.choice([None, rng.randint(1, 2000)])
    with mock.patch.object(candidates, "drive_of", lambda p: drives.get(p, "C:\\")):
        ctx = candidates.Context(s)
        plan = candidates.plan_recordings(ctx, target, drive=target_drive)
    rec["ops"].append(["plan", target, target_drive, len(plan["items"])])
    ids = [it["id"] for it in plan["items"]]
    check(len(ids) == len(set(ids)), "同じファイルを二重に提案", rec)
    real_paths = [os.path.realpath(s.video(i)["path"]) for i in ids]
    check(len(real_paths) == len(set(real_paths)), "同じ実体のファイルを二重に提案", rec)
    check(plan["total"] == sum(s.video(i)["size"] for i in ids), "提案の合計が候補の容量の合計と違う", rec)
    for i in ids:
        row = s.video(i)
        check(not row["protected"] and row["decision"] not in ("残す", "保留"), "保護・残す・保留を提案した", rec)
        if target_drive:
            check(drives[row["path"]] == target_drive, "対象外のドライブのファイルを提案した", rec)
    for h, members in by_hash.items():  # 各内容で最低1本は提案されずに残る（実体で数える）
        reals = {os.path.realpath(s.video(m)["path"]) for m in members}
        chosen = {os.path.realpath(s.video(m)["path"]) for m in members if m in ids}
        if reals:
            check(reals - chosen, "同じ内容のファイルをすべて提案した", rec)
    if target is not None:
        check(plan["achieved"] == (plan["total"] >= target), "目標の達成状況が合計と食い違う", rec)
        check(plan["shortage"] == max(target - plan["total"], 0), "不足分の計算が違う", rec)


# =============================================================================
# 区分 D: 保護・整理操作・途中変更・部分失敗
# =============================================================================
def case_trash(seed: int, work: Path, rec: dict) -> None:
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    d = work / "files"
    data = work / "data"
    if rng.random() < 0.1:  # 保護情報が読めない状態
        data.mkdir(parents=True)
        (data / "user_state.json").write_text(rng.choice(["[]", "{broken", "null"]), encoding="utf-8")
    s = Store(data)
    broken = not s.protection_ok
    dup_groups: list[list[int]] = []
    for _ in range(rng.randint(1, 4)):
        c = os.urandom(rng.randint(30, 300))
        members = []
        for _ in range(rng.randint(2, 4)):
            p = fake_mp4(unique(rng, d / rng.choice(["a", "b", "日本語 フォルダ"]), ".mp4"), c)
            st = p.stat()
            members.append(s.insert_raw(str(p), p.name, str(p.parent), st.st_size, st.st_mtime, ".mp4",
                                        sha256=sha(p)))
        dup_groups.append(members)
    sim_members = []
    if rng.random() < 0.6:  # 類似録画のグループ（内容は別バイト・特徴は同じ録画）
        base = frames_family(nrng, "scene", similar.H, similar.W)
        for k in range(rng.randint(2, 3)):
            p = fake_mp4(unique(rng, d / "sim", ".mp4"), os.urandom(200 + k))
            st = p.stat()
            frames = base if k == 0 else mild_variant(nrng, base)
            sim_members.append(s.insert_raw(str(p), p.name, str(p.parent), st.st_size, st.st_mtime, ".mp4",
                                            meta={"duration": 60.0, "width": 640, "height": 360},
                                            vsig=to_sig(frames, 60.0, similar.VERSION)))
        s.set_video_groups([sim_members])
    singles = []
    for _ in range(rng.randint(0, 3)):
        p = fake_mp4(unique(rng, d / "single", ".mp4"), os.urandom(rng.randint(30, 300)))
        st = p.stat()
        singles.append(s.insert_raw(str(p), p.name, str(p.parent), st.st_size, st.st_mtime, ".mp4", sha256=sha(p)))
    all_ids = [m for g in dup_groups for m in g] + sim_members + singles
    if not broken:
        for m in all_ids:
            r = rng.random()
            try:
                if r < 0.15:
                    s.set_protected(m, True)
                elif r < 0.25:
                    s.set_decision(m, "残す")
                elif r < 0.3:
                    s.set_decision(m, "保留")
            except StoreError:
                raise Fail("保存に失敗した")
    before = {m: (s.video(m)["path"], sha(Path(s.video(m)["path"]))) for m in all_ids}
    protected = {m for m in all_ids if s.video(m)["protected"]}
    confirmed_sim = any(s.video(m)["protected"] or s.video(m)["decision"] == "残す" for m in sim_members)
    selection = [m for m in all_ids if rng.random() < 0.7]
    rng.shuffle(selection)
    rec["setup"] = {"dup_groups": [len(g) for g in dup_groups], "similar": len(sim_members), "singles": len(singles),
                    "selected": len(selection), "broken_protection": broken}
    events = {}
    trash_dir = work / "trash"
    fail_names = {Path(s.video(m)["path"]).name for m in selection if rng.random() < 0.15}
    victim_keep = None
    if dup_groups and rng.random() < 0.4:  # 途中で残す側を消す・変える
        g = rng.choice(dup_groups)
        outside = [m for m in g if m not in selection]
        if outside:
            victim_keep = rng.choice(outside)
            events["keeper_event_at"] = rng.randint(1, 2)
            events["keeper_event"] = rng.choice(["delete", "modify"])
    cancel = threading.Event()
    cancel_at = rng.randint(1, max(1, len(selection))) if rng.random() < 0.2 else None

    def hook(count, path):
        if victim_keep is not None and count == events["keeper_event_at"]:
            vp = s.video(victim_keep)["path"]
            if events["keeper_event"] == "delete":
                os.remove(vp)
            else:
                with open(vp, "ab") as f:
                    f.write(b"x")
        if cancel_at is not None and count >= cancel_at:
            cancel.set()
    group_of = {before[m][0]: (g, before[g[0]][1]) for g in dup_groups for m in g}
    violations = []

    def pre(path):
        # 移動の瞬間に、同じ内容で変更されていない別のコピーが実在しているか（アプリの責任の範囲）
        if path in group_of:
            g, h = group_of[path]
            if not any(before[m][0] != path and os.path.exists(before[m][0]) and sha(Path(before[m][0])) == h
                       for m in g):
                violations.append(Path(path).name)
    backend = MockTrash(trash_dir, fail_names, hook, pre)
    record_fail = rng.random() < 0.15
    rec["ops"] += [["events", events], ["fail_names", len(fail_names)], ["cancel_at", cancel_at],
                   ["record_fail", record_fail]]
    if record_fail:
        with mock.patch.object(s, "log_trash", side_effect=OSError(28, "No space")):
            results = trash.move_to_trash(s, selection, backend, cancel=cancel)
    else:
        results = trash.move_to_trash(s, selection, backend, cancel=cancel)
    check(len(results) == len(selection), "結果の件数が選択と違う", rec)
    moved = {r.media_id for r in results if r.status == "moved"}
    if broken:
        check(not moved and backend.calls == 0, "保護情報が読めないのに移動した", rec)
    check(not (moved & protected), "保護したファイルを移動した", rec)
    for r in results:
        path = before[r.media_id][0]
        check(os.path.exists(path) != (r.status == "moved"), f"結果（{r.status}）と実ファイルの状態が違う", rec)
    check(set(backend.moved) == {before[m][0] for m in moved}, "ごみ箱へ移したファイルと結果が一致しない", rec)
    trashed = {hashlib.sha256(p.read_bytes()).hexdigest() for p in trash_dir.iterdir()} if trash_dir.exists() else set()
    for m in moved:  # 完全削除していない（内容がごみ箱側に残っている）
        check(before[m][1] in trashed, "移動したファイルの内容がごみ箱にない（完全削除の疑い）", rec)
    check(not violations, f"同じ内容の別のコピーがない状態で移動した: {violations[:3]}", rec)
    for g in dup_groups:
        if victim_keep is None or victim_keep not in g:  # 外部から消していないグループは最低1本残る
            check(any(os.path.exists(before[m][0]) for m in g), "同じ内容のファイルがすべてなくなった", rec)
        elif events.get("keeper_event") == "modify":
            check(any(os.path.exists(before[m][0]) for m in g), "残す側が変わったあと、すべてなくなった", rec)
    if sim_members and not confirmed_sim:
        check(not (moved & set(sim_members)), "未確定の類似録画を移動した", rec)
    if sim_members:
        check(any(os.path.exists(before[m][0]) for m in sim_members), "類似録画がすべてなくなった", rec)
    for r in results:  # 記録の状態と結果の整合
        row = s.video(r.media_id)
        if r.status == "moved":
            check(bool(row["trashed_at"]) or bool(r.record_error), "移動した記録が残っていない", rec)
        else:
            check(not row["trashed_at"], "移動していないのに移動済みと記録した", rec)
    if record_fail and moved:
        check(all(r.record_error for r in results if r.status == "moved"), "記録の失敗を伝えていない", rec)
    # 再操作できる
    again = trash.move_to_trash(s, [m for m in selection if m not in moved], MockTrash(work / "trash2"))
    check(not ({r.media_id for r in again if r.status == "moved"} & protected), "再操作で保護したファイルを移動した", rec)
