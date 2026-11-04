"""Windows 実機で「ごみ箱へ移動」を確かめる手動テスト（このテストが作ったダミーファイルだけを扱う）。

使い方:
    python tools/recycle_bin_live_test.py [--base 作業フォルダーの親]

行うこと:
1. 専用の新しいフォルダー <base>/recycle_live/<uuid>/ を作り、一意な名前のダミーファイルを作る（sha256 を記録）
2. 安全な失敗の確認: フォルダー・存在しないパス・相対パスを拒否すること、
   「Windows が完全削除しようとした」状況を通知の段階で模擬し、削除前に止まって元のファイルが残ること
3. アプリの移動処理（trash.default_backend()）でダミーをごみ箱へ移す（メインスレッドと別スレッドで 1 回ずつ）
4. 元の場所に無いこと、ごみ箱に「名前」「元の場所」「内容（sha256）」がすべて一致する項目がちょうど 1 つあることを確認
5. その 1 項目だけを「元に戻す（undelete）」で元の場所へ戻し、sha256 が一致することを確認
6. 自分で作ったダミーファイルと専用フォルダーだけを片付ける

ごみ箱を空にしない・ほかのごみ箱の項目には触れない。項目を一意に特定・復元できない場合は、
ダミーをごみ箱に残したまま止まり、その名前を表示する。
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import os
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage_organizer import recycle_bin, trash  # noqa: E402

# System.Recycle.DeletedFrom（元の場所）
PKEY_DELETED_FROM = ("{9B174B33-40FF-11D2-A27E-00C04FC30871}", 2)


class Stop(Exception):
    """安全のため中断する（ダミーはそのまま残す）。"""


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def log(msg: str) -> None:
    print(msg, flush=True)


def recycle_bin_folder():
    from win32com.shell import shell, shellcon
    desktop = shell.SHGetDesktopFolder()
    pidl = shell.SHGetSpecialFolderLocation(0, shellcon.CSIDL_BITBUCKET)
    return desktop.BindToObject(pidl, None, shell.IID_IShellFolder2)


def find_in_bin(rb, name: str, folder: str, digest: str):
    """名前・元の場所・内容がすべて一致するごみ箱の項目を探す（読むだけ）。一致した pidl の一覧を返す。"""
    from win32com.shell import shellcon
    stem = os.path.splitext(name)[0]
    matches = []
    flags = shellcon.SHCONTF_FOLDERS | shellcon.SHCONTF_NONFOLDERS | shellcon.SHCONTF_INCLUDEHIDDEN
    for child in rb.EnumObjects(0, flags):
        shown = rb.GetDisplayNameOf(child, shellcon.SHGDN_INFOLDER)
        if shown not in (name, stem):  # 拡張子の表示設定によっては拡張子が付かない
            continue
        try:
            origin = rb.GetDetailsEx(child, PKEY_DELETED_FROM)
        except Exception:  # noqa: BLE001
            continue
        if norm(str(origin)) != norm(folder):
            continue
        stored = rb.GetDisplayNameOf(child, shellcon.SHGDN_FORPARSING)  # ごみ箱内の実体（$R...）
        try:
            same = os.path.isfile(stored) and sha256(stored) == digest
        except OSError:
            same = False
        if same:
            matches.append((child, stored))
    return matches


def undelete(stored: str) -> None:
    """ごみ箱の 1 項目だけを「元に戻す」（canonical verb "undelete"）。

    対象は、ごみ箱内の実体のパス（$R...、find_in_bin で名前・元の場所・内容を確認済み）が完全に一致する
    ただ 1 つの項目に限る。IContextMenu.InvokeCommand は pywin32 から呼ぶと異常終了することがあったため、
    Shell.Application（FolderItem.InvokeVerb）を使う。
    """
    import win32com.client
    from win32com.shell import shellcon
    ns = win32com.client.Dispatch("Shell.Application").NameSpace(shellcon.CSIDL_BITBUCKET)
    targets = [it for it in ns.Items() if norm(it.Path) == norm(stored)]
    if len(targets) != 1:
        raise Stop(f"元に戻す項目を一意に特定できません（{len(targets)} 件）: {stored}")
    targets[0].InvokeVerb("undelete")


def wait_for(cond, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def make_dummy(folder: str, label: str) -> tuple[str, str]:
    name = f"recsort_live_test_{label}_{uuid.uuid4().hex}.txt"
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(b"RecSort recycle bin live test dummy\n" + os.urandom(256).hex().encode())
    return path, sha256(path)


def negative_tests(folder: str) -> list[str]:
    backend = trash.default_backend()
    failures = []
    cases = {
        "フォルダー": folder,
        "存在しないパス": os.path.join(folder, f"missing_{uuid.uuid4().hex}.txt"),
        "相対パス": "relative_dummy.txt",
    }
    for label, p in cases.items():
        try:
            backend(p)
        except trash.TrashError as e:
            log(f"  [OK] {label}: 拒否されました → {e}")
        else:
            failures.append(label)
            log(f"  [NG] {label}: 拒否されませんでした")
    if not os.path.isdir(folder):
        failures.append("フォルダーが消えた")
    # 「Windows が完全削除しようとした」状況を模擬: 通知の flags から「ごみ箱へ移す」を外す
    path, digest = make_dummy(folder, "nuke_sim")
    real = recycle_bin._on_pre_delete

    def masked(state, flags, item_path):
        return real(state, flags & ~recycle_bin.TSF_DELETE_RECYCLE_IF_POSSIBLE, item_path)
    recycle_bin._on_pre_delete = masked
    try:
        backend(path)
    except trash.TrashError as e:
        log(f"  [OK] 完全削除の模擬: 止まりました → {e}")
    else:
        failures.append("完全削除の模擬で止まらなかった")
        log("  [NG] 完全削除の模擬: 止まりませんでした")
    finally:
        recycle_bin._on_pre_delete = real
    if os.path.isfile(path) and sha256(path) == digest:
        log(f"  [OK] 完全削除の模擬: 元のファイルが残っています（{os.path.basename(path)}）")
        os.remove(path)  # このテストが作ったダミーの片付け
    else:
        failures.append("完全削除の模擬でファイルが無くなった")
        log(f"  [NG] 完全削除の模擬: ファイルが見つかりません（{path}）。ごみ箱を確認してください")
    return failures


def roundtrip(folder: str, label: str, in_thread: bool) -> dict:
    path, digest = make_dummy(folder, label)
    name = os.path.basename(path)
    rec = {"name": name, "original": path, "sha256": digest, "thread": in_thread,
           "moved": False, "found_in_bin": False, "restored": False, "sha256_match": False}
    log(f"  ダミー: {name}\n    元の場所: {path}\n    sha256: {digest}")
    errors = []
    if in_thread:
        def work():
            try:
                with trash.windows_com_thread():
                    trash.default_backend()(path)
                    errors.append(None)
                    rec["sink"] = recycle_bin.last_result()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                rec["sink"] = recycle_bin.last_result()
        t = threading.Thread(target=work)
        t.start()
        t.join(120)
        if t.is_alive():
            raise Stop(f"別スレッドでの移動が終わりません（ダミー: {name}）")
    else:
        try:
            trash.default_backend()(path)
            errors.append(None)
        except trash.TrashError as e:
            errors.append(e)
        rec["sink"] = recycle_bin.last_result()
    log(f"    通知の内容: {rec.get('sink')}")
    if errors and errors[0] is not None:
        log(f"    移動処理がエラーを返しました: {errors[0]}")
        if not os.path.lexists(path):  # 移動はされていた場合、このダミーだけを探して戻す
            rb = recycle_bin_folder()
            matches = find_in_bin(rb, name, folder, digest)
            if len(matches) == 1:
                undelete(matches[0][1])
                ok = wait_for(lambda: os.path.isfile(path)) and sha256(path) == digest
                log(f"    ごみ箱から元に戻しました: {ok}")
            del rb, matches
        raise Stop(f"移動処理がエラーを返しました（ダミー: {name}, 元の場所にある: {os.path.isfile(path)}）")
    rec["moved"] = not os.path.lexists(path)
    log(f"    移動後に元の場所に無い: {rec['moved']}")
    if not rec["moved"]:
        raise Stop(f"移動後も元の場所に残っています: {path}")
    rb = recycle_bin_folder()
    matches = find_in_bin(rb, name, folder, digest)
    log(f"    ごみ箱で一致した項目数: {len(matches)}" + (f"（実体: {matches[0][1]}）" if matches else ""))
    if len(matches) != 1:
        raise Stop(f"ごみ箱の項目を一意に特定できません（{len(matches)} 件）。ダミー {name} はごみ箱に残しています")
    rec["found_in_bin"] = True
    undelete(matches[0][1])
    rec["restored"] = wait_for(lambda: os.path.isfile(path))
    log(f"    元に戻した: {rec['restored']}")
    if not rec["restored"]:
        raise Stop(f"元に戻せませんでした。ダミー {name} はごみ箱に残っている可能性があります")
    rec["sha256_match"] = sha256(path) == digest
    stored = matches[0][1]
    info = os.path.join(os.path.dirname(stored), "$I" + os.path.basename(stored)[2:])
    rec["stored_left"] = os.path.exists(stored)
    rec["info_left"] = os.path.exists(info)  # 読むだけ（ごみ箱のフォルダーには書き込まない）
    log(f"    ごみ箱内の実体 $R が残っている: {rec['stored_left']} / 情報ファイル $I が残っている: {rec['info_left']}")
    log(f"    sha256 一致: {rec['sha256_match']}")
    still = find_in_bin(recycle_bin_folder(), name, folder, digest)
    log(f"    復元後にごみ箱に残っている一致項目: {len(still)}")
    if rec["sha256_match"]:
        os.remove(path)  # このテストが作ったダミーの片付け
    return rec


def main() -> int:
    faulthandler.enable()
    if sys.platform != "win32":
        log("Windows でのみ実行できます")
        return 2
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=tempfile.gettempdir())
    args = ap.parse_args()
    reason = recycle_bin.available()
    if reason:
        log(f"利用できません: {reason}")
        return 2
    folder = os.path.join(os.path.abspath(args.base), "recycle_live", uuid.uuid4().hex)
    os.makedirs(folder)
    log(f"作業フォルダー: {folder}")
    results, failures = [], []
    try:
        log("[1] 拒否・中止の確認")
        failures += negative_tests(folder)
        log("[2] メインスレッドで移動 → ごみ箱で確認 → 元に戻す")
        results.append(roundtrip(folder, "main", False))
        log("[3] 別スレッド（windows_com_thread）で移動 → ごみ箱で確認 → 元に戻す")
        results.append(roundtrip(folder, "thread", True))
    except Stop as e:
        log(f"[中断] {e}")
        log(f"作業フォルダーはそのまま残しています: {folder}")
        return 1
    for r in results:
        if not all(r[k] for k in ("moved", "found_in_bin", "restored", "sha256_match")):
            failures.append(r["name"])
    if not failures:
        try:
            os.rmdir(folder)  # 空になった専用フォルダーだけを片付ける
            os.rmdir(os.path.dirname(folder)) if not os.listdir(os.path.dirname(folder)) else None
        except OSError as e:
            log(f"作業フォルダーを片付けられませんでした: {e}")
    log("結果: " + ("すべて成功" if not failures else f"失敗あり: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
