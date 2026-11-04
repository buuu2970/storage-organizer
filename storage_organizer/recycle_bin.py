"""Windows のごみ箱へ 1 ファイルを移動する（完全削除は絶対にしない・失敗したら止める）。

方針（フェイルクローズ）:
- 移動の前に、通常ファイルであること・リンク/ジャンクションでないこと・オンライン専用でないこと・
  固定ドライブであること・ごみ箱が使えること・「ごみ箱に移動しない」設定でないこと・
  ごみ箱の最大サイズを超えないこと、を確かめる。1つでも満たさなければ何もせずにエラーにする
- 移動は IFileOperation（FOF_ALLOWUNDO | FOFX_RECYCLEONDELETE）で行い、進行状況の通知（sink）を必ず登録する。
  削除の直前の通知（PreDeleteItem）で「ごみ箱へ移す」指定が無ければ、Windows が完全削除しようとしているので
  E_ABORT を返してその項目を止める
- 完了後、「ごみ箱へ移す」指定で始まり・成功し・ごみ箱内の項目が作られ・中止が無く・元の場所に無い、
  をすべて満たしたときだけ成功とする。それ以外はエラーにし、別の方法で再試行しない
- pywin32 が無い・Windows 以外のときは使えない（ほかの削除方法へは切り替えない）
"""

from __future__ import annotations

import os
import stat as _stat
import sys
import threading
from typing import Callable

try:  # Windows + pywin32 のときだけ使える
    import pythoncom
    import pywintypes
    import win32file
    import winerror
    from win32com.server.exception import COMException
    from win32com.server.policy import DesignatedWrapPolicy
    from win32com.shell import shell, shellcon
    _IMPORT_ERROR: Exception | None = None
except Exception as _e:  # noqa: BLE001
    _IMPORT_ERROR = _e


class TrashError(Exception):
    """ごみ箱へ移動できなかった（ファイルは完全削除していない）。"""


# ---- 定数（pywin32 に無いものは値をそのまま書く） ----
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOFX_EARLYFAILURE = 0x00100000
FOFX_RECYCLEONDELETE = 0x00080000  # Windows 8 以降
OPERATION_FLAGS = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
                   | FOFX_EARLYFAILURE | FOFX_RECYCLEONDELETE)

TSF_DELETE_RECYCLE_IF_POSSIBLE = 0x80
S_OK = 0
# ごみ箱へ移した項目の PostDeleteItem の結果。Windows 11 の実機では、ごみ箱へ移せたとき
# COPYENGINE_S_DONT_PROCESS_CHILDREN（0x00270008、成功コード）が返る。この2つ以外は成功にしない
COPYENGINE_S_DONT_PROCESS_CHILDREN = 0x00270008
RECYCLED_HRESULTS = frozenset({S_OK, COPYENGINE_S_DONT_PROCESS_CHILDREN})
E_ABORT = -2147467260  # 0x80004004
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
CLOUD_ONLY_ATTRIBUTES = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

DRIVE_FIXED = 3
DRIVE_NAMES = {0: "種類が分からないドライブ", 1: "存在しないドライブ", 2: "取り外し可能なドライブ（USB メモリ・SD カードなど）",
               4: "ネットワークドライブ", 5: "CD/DVD ドライブ", 6: "RAM ディスク"}

BITBUCKET_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\BitBucket"
POLICY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"

NOT_DELETED = "（ファイルは削除していません）"


# ---- 判定（純粋関数・テスト用） ----

def decide_from_sink(pre_flags: int | None, post_hr: int | None, newly_created: bool, any_aborted: bool,
                     still_exists: bool) -> tuple[bool, str]:
    """IFileOperation の通知内容から「ごみ箱へ移動できた」と言えるかを決める。(成功か, 理由) を返す。"""
    gone = "" if still_exists else "（元の場所からは無くなっています。ごみ箱を確認してください）"
    kept = NOT_DELETED if still_exists else gone
    if pre_flags is None:
        return False, f"Windows から削除前の確認が届かなかったため、ごみ箱へ移動できたか確認できません{kept}"
    if not (pre_flags & TSF_DELETE_RECYCLE_IF_POSSIBLE):
        return False, ("Windows がごみ箱を使わずに完全削除しようとしたため中止しました"
                       f"{NOT_DELETED if still_exists else gone}")
    if post_hr is None:
        return False, f"Windows から削除後の結果が届かなかったため、ごみ箱へ移動できたか確認できません{kept}"
    if post_hr not in RECYCLED_HRESULTS:
        return False, f"ごみ箱へ移動できませんでした（エラーコード {post_hr & 0xFFFFFFFF:#010x}）{kept}"
    if not newly_created:
        return False, f"ごみ箱の中に移動後の項目が見つからないため、ごみ箱へ移動できたか確認できません{kept}"
    if any_aborted:
        return False, f"Windows が操作を中止したため、ごみ箱へ移動できませんでした{kept}"
    if still_exists:
        return False, f"移動後も元の場所にファイルが残っています{NOT_DELETED}"
    return True, "ごみ箱へ移動しました"


# ---- 事前確認（テストで差し替えられるよう、OS への問い合わせは小さな関数に分ける） ----

def _lstat(path: str):
    return os.stat(path, follow_symlinks=False)


def _realpath(path: str) -> str:
    return os.path.realpath(path)


def _volume_root(path: str) -> str:
    return win32file.GetVolumePathName(path)


def _drive_type(root: str) -> int:
    return win32file.GetDriveType(root)


def _query_recycle_bin(root: str) -> None:
    shell.SHQueryRecycleBin(root)  # 使えなければ例外


def _volume_guid(root: str) -> str | None:
    name = win32file.GetVolumeNameForVolumeMountPoint(root)  # \\?\Volume{GUID}\
    start, end = name.find("{"), name.find("}")
    return name[start:end + 1] if 0 <= start < end else None


def _read_dword(hive, key: str, value: str) -> int | None:
    import winreg
    try:
        with winreg.OpenKey(hive, key) as k:
            v, kind = winreg.QueryValueEx(k, value)
    except OSError:
        return None
    return v if kind == winreg.REG_DWORD else None


def _recycle_settings(guid: str | None) -> dict:
    """ごみ箱の設定（レジストリ）を読む。読めないものは入れない。"""
    import winreg
    hkcu, hklm = winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE
    s = {
        "global_nuke": _read_dword(hkcu, BITBUCKET_KEY, "NukeOnDelete"),
        "policy_no_recycle": max((_read_dword(h, POLICY_KEY, "NoRecycleFiles") or 0) for h in (hkcu, hklm)),
    }
    if guid:
        vkey = BITBUCKET_KEY + "\\Volume\\" + guid
        s["nuke"] = _read_dword(hkcu, vkey, "NukeOnDelete")
        s["max_capacity_mb"] = _read_dword(hkcu, vkey, "MaxCapacity")
    return s


def precheck(path: str) -> str:
    """移動してよいかを確かめ、絶対パスを返す。問題があれば TrashError（何も移動しない）。"""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        raise TrashError(f"ファイルの場所が正しく指定されていません: {path!r}{NOT_DELETED}")
    full = os.path.normpath(path)
    try:
        st = _lstat(full)
    except OSError as e:
        raise TrashError(f"ファイルが見つかりません（{e.strerror or e}）{NOT_DELETED}") from e
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & FILE_ATTRIBUTE_REPARSE_POINT or _stat.S_ISLNK(st.st_mode):
        raise TrashError(f"リンク（ショートカットの実体・ジャンクションなど）は移動しません{NOT_DELETED}")
    if not _stat.S_ISREG(st.st_mode):
        raise TrashError(f"通常のファイルではない（フォルダーなど）ため移動しません{NOT_DELETED}")
    if attrs & CLOUD_ONLY_ATTRIBUTES:
        raise TrashError(f"オンライン専用（クラウドにだけある）ファイルは移動しません{NOT_DELETED}")
    roots = []
    for p in (full, _realpath(full)):
        try:
            r = _volume_root(p)
        except Exception as e:  # noqa: BLE001
            raise TrashError(f"ファイルのあるドライブを確認できません（{e}）{NOT_DELETED}") from e
        if r not in roots:
            roots.append(r)
    for root in roots:
        try:
            kind = _drive_type(root)
        except Exception as e:  # noqa: BLE001
            raise TrashError(f"ドライブの種類を確認できません（{e}）{NOT_DELETED}") from e
        if kind != DRIVE_FIXED:
            name = DRIVE_NAMES.get(kind, "固定ドライブ以外のドライブ")
            raise TrashError(f"{name}（{root}）のファイルは、ごみ箱に入らず完全に削除されることがあるため移動しません"
                             f"{NOT_DELETED}")
        try:
            _query_recycle_bin(root)
        except Exception as e:  # noqa: BLE001
            raise TrashError(f"{root} のごみ箱を利用できないため移動しません（{e}）{NOT_DELETED}") from e
        # レジストリの設定は早めの確認。読めない場合でも、移動時の確認（PreDeleteItem）で必ず止める
        try:
            guid = _volume_guid(root)
        except Exception:  # noqa: BLE001
            guid = None
        try:
            s = _recycle_settings(guid)
        except Exception:  # noqa: BLE001
            s = {}
        if s.get("policy_no_recycle") == 1:
            raise TrashError(f"このパソコンは「ごみ箱を使わずにすぐ削除する」設定（ポリシー）のため移動しません{NOT_DELETED}")
        if s.get("nuke") == 1 or (s.get("nuke") is None and s.get("global_nuke") == 1):
            raise TrashError(f"{root} は「ごみ箱に移動しないで、すぐに削除する」設定のため移動しません{NOT_DELETED}")
        cap = s.get("max_capacity_mb")
        if cap is not None and st.st_size > cap * 1024 * 1024:
            raise TrashError(f"ファイルがごみ箱の最大サイズ（{cap} MB）より大きく、完全に削除されてしまうため移動しません"
                             f"{NOT_DELETED}")
    return full


# ---- 移動（IFileOperation） ----

class SinkState:
    """進行状況の通知で受け取った内容。"""

    def __init__(self, target: str) -> None:
        self.target = target
        self.pre_flags: list[int] = []
        self.post: list[tuple[int, bool]] = []
        self.nuke_aborted = False
        self.other_item = False


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    def norm(p: str) -> str:
        return os.path.normcase(os.path.normpath(p))
    if norm(a) == norm(b):
        return True
    try:  # 短い名前（8.3 形式）などの表記の違い
        return norm(os.path.realpath(a)) == norm(os.path.realpath(b))
    except OSError:
        return False


def _item_path(item) -> str:
    try:
        return item.GetDisplayName(shellcon.SHGDN_FORPARSING)
    except Exception:  # noqa: BLE001
        return ""


def _on_pre_delete(state: SinkState, flags: int, item_path: str) -> bool:
    """削除の直前に呼ばれる。続けてよければ True（False なら E_ABORT で止める）。"""
    state.pre_flags.append(flags)
    if not _same_path(item_path, state.target):
        state.other_item = True
        return False
    if not (flags & TSF_DELETE_RECYCLE_IF_POSSIBLE):
        state.nuke_aborted = True
        return False
    return True


def _make_sink(state: SinkState):
    """IFileOperationProgressSink を Python で実装して COM オブジェクトにする。"""

    class _Sink(DesignatedWrapPolicy):
        _com_interfaces_ = [shell.IID_IFileOperationProgressSink]
        _public_methods_ = ["StartOperations", "FinishOperations", "PreRenameItem", "PostRenameItem",
                            "PreMoveItem", "PostMoveItem", "PreCopyItem", "PostCopyItem", "PreDeleteItem",
                            "PostDeleteItem", "PreNewItem", "PostNewItem", "UpdateProgress", "ResetTimer",
                            "PauseTimer", "ResumeTimer"]

        def __init__(self):
            self._wrap_(self)

        def StartOperations(self):
            pass

        def FinishOperations(self, hr):
            pass

        def PreDeleteItem(self, flags, item):
            try:
                ok = _on_pre_delete(state, int(flags), _item_path(item))
            except Exception:  # noqa: BLE001  判定できなければ止める
                ok = False
            if not ok:
                raise COMException(hresult=winerror.E_ABORT)

        def PostDeleteItem(self, flags, item, hr, newly_created):
            state.post.append((int(hr), newly_created is not None))

        # 削除以外の操作はこのアプリでは行わない。万一呼ばれても止める
        def _refuse(self, *a):
            raise COMException(hresult=winerror.E_ABORT)
        PreRenameItem = PreMoveItem = PreCopyItem = PreNewItem = _refuse

        def PostRenameItem(self, *a):
            pass
        PostMoveItem = PostCopyItem = PostNewItem = PostRenameItem

        def UpdateProgress(self, total, so_far):
            pass

        def ResetTimer(self):
            pass

        def PauseTimer(self):
            pass

        def ResumeTimer(self):
            pass

    return pythoncom.WrapObject(_Sink(), shell.IID_IFileOperationProgressSink)


def _create_file_operation():
    return pythoncom.CoCreateInstance(shell.CLSID_FileOperation, None, pythoncom.CLSCTX_ALL, shell.IID_IFileOperation)


def _create_item(path: str):
    return shell.SHCreateItemFromParsingName(path, None, shell.IID_IShellItem)


def _operate(state: SinkState) -> tuple[str | None, int | None, str]:
    """IFileOperation で削除（ごみ箱へ移動）を 1 回だけ行う。

    (準備で止まった理由, PerformOperations のエラーコード, 中止の有無) を返す。COM オブジェクトは
    この関数の中だけで使い、例外の記録（traceback）経由で COM 終了後まで残らないよう、例外は外へ出さない。
    """
    full = state.target
    try:
        fo = _create_file_operation()
        fo.SetOperationFlags(OPERATION_FLAGS)
        item = _create_item(full)
    except Exception as e:  # noqa: BLE001
        return f"ごみ箱へ移動する準備ができませんでした（{e}）", None, True
    try:
        sink = _make_sink(state)
        cookie = fo.Advise(sink)
    except Exception as e:  # noqa: BLE001  確認の仕組みを付けられないなら移動しない
        return f"移動の安全確認を準備できなかったため、移動しませんでした（{e}）", None, True
    perform_hr = None
    any_aborted = True
    try:
        fo.DeleteItem(item, None)
        try:
            fo.PerformOperations()
        except Exception as e:  # noqa: BLE001  結果は呼び出し元でまとめて判定する
            perform_hr = getattr(e, "hresult", None) or -1
        try:
            any_aborted = bool(fo.GetAnyOperationsAborted())
        except Exception:  # noqa: BLE001
            any_aborted = True
    except Exception as e:  # noqa: BLE001
        return f"ごみ箱へ移動できませんでした（{e}）", None, True
    finally:
        try:
            fo.Unadvise(cookie)
        except Exception:  # noqa: BLE001
            pass
    return None, perform_hr, any_aborted


_last = threading.local()


def last_result() -> dict | None:
    """このスレッドで最後に行った移動の通知内容（確認・診断用）。"""
    return getattr(_last, "result", None)


def _perform(full: str) -> None:
    """COM（STA）初期化済みのスレッドで呼ぶ。ごみ箱へ移せたと確認できなければ TrashError。"""
    state = SinkState(full)
    setup_error, perform_hr, any_aborted = _operate(state)
    still_exists = os.path.lexists(full)
    if setup_error is not None:
        raise TrashError(setup_error + (NOT_DELETED if still_exists else "（ごみ箱を確認してください）"))
    if state.other_item:
        raise TrashError("指定と違うファイルを削除しようとしたため中止しました" + (NOT_DELETED if still_exists else ""))
    if len(state.pre_flags) > 1 or len(state.post) > 1:  # 1 ファイルだけを指定している。想定外なので成功にしない
        pre, post_hr, newly = None, None, False
    else:
        pre = state.pre_flags[0] if state.pre_flags else None
        post_hr, newly = state.post[0] if state.post else (None, False)
    _last.result = {"pre_flags": list(state.pre_flags), "post": list(state.post), "any_aborted": any_aborted,
                    "perform_hr": perform_hr, "still_exists": still_exists}
    ok, reason = decide_from_sink(pre, post_hr, newly, any_aborted, still_exists)
    if ok and perform_hr is not None:
        ok, reason = False, f"ごみ箱への移動で Windows がエラーを返しました（エラーコード {perform_hr & 0xFFFFFFFF:#010x}）"
    if not ok:
        raise TrashError(reason)


def _run_in_sta(func: Callable[[], None]) -> None:
    """このスレッドで COM（STA）を初期化して func を呼ぶ。初期化した分だけ終了する。

    すでに MTA で初期化されたスレッドでは IFileOperation を使えないため、専用のスレッドで行う。
    """
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
    except pythoncom.com_error as e:
        if e.hresult != RPC_E_CHANGED_MODE:
            raise TrashError(f"Windows の COM を初期化できませんでした（{e}）{NOT_DELETED}") from e
        box: list = []

        def worker():
            try:
                _run_in_sta(func)
            except BaseException as ex:  # noqa: BLE001
                box.append(ex)
        t = threading.Thread(target=worker, name="recsort-recycle", daemon=True)
        t.start()
        t.join()
        if box:
            raise box[0]
        return
    try:  # S_OK でも S_FALSE（初期化済み）でも、1 回分の終了が必要
        func()
    finally:
        pythoncom.CoUninitialize()


def available() -> str | None:
    """使えなければ理由を返す。"""
    if sys.platform != "win32":
        return "ごみ箱への移動は Windows でのみ利用できます（完全削除は行いません）"
    if _IMPORT_ERROR is not None:
        return f"ごみ箱への移動には pywin32 が必要です（pip install pywin32）。完全削除は行いません（{_IMPORT_ERROR}）"
    return None


def recycle(path: str) -> None:
    """path（1 ファイル）を Windows のごみ箱へ移す。確実に移せたと確認できなければ TrashError。"""
    reason = available()
    if reason:
        raise TrashError(reason)
    full = precheck(path)
    _run_in_sta(lambda: _perform(full))


def backend() -> Callable[[str], None]:
    """アプリが使う移動処理。使えない環境では、常にエラーにする関数を返す（ほかの方法へは切り替えない）。"""
    reason = available()
    if reason is None:
        return recycle

    def unsupported(path: str) -> None:
        raise TrashError(reason)
    return unsupported
