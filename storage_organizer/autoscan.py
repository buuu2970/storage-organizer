"""自動スキャン（担当: スキャン処理）。

PC 内の画像・動画を、ユーザーがフォルダを選ばなくても探す。
- ピクチャ・ビデオ・ダウンロード・デスクトップ・ドキュメントを先に調べ、その後に内蔵ドライブを調べる
- ユーザーフォルダの実際の保存先（OneDrive などへの移動を含む）は Windows から取得する
- 外付け・ネットワークドライブは自動では調べない（設定で追加できる）
- システム領域・アプリの素材やキャッシュは除外する
- 読み取り専用。リンク・ジャンクションはたどらない。アクセスできない場所はスキップして記録する
- オンライン専用ファイルは登録するが、中身は読まない（解析・ハッシュ計算の対象外）
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import candidates, media
from .db import Database
from .file_types import categorize
from .scanner import is_cloud_only, is_link

KIND_OF_CATEGORY = {"画像": "image", "動画": "video"}

# ユーザーフォルダ（Windows の既知フォルダ ID）。この順番で優先して調べる
KNOWN_FOLDERS = [
    ("ピクチャ", "{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
    ("ビデオ", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
    ("ダウンロード", "{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    ("デスクトップ", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
    ("ドキュメント", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
]

# ドライブ直下にある場合に除外するフォルダ（Windows・アプリの動作に使われる領域）
ROOT_EXCLUDES = {
    "windows", "program files", "program files (x86)", "programdata", "recovery", "perflogs",
    "msocache", "windows.old", "$windows.~bt", "$windows.~ws", "$winreagent", "config.msi",
    "intel", "amd", "nvidia", "drivers", "boot", "efi", "xboxgames", "inetpub",
}
# どこにあっても除外するフォルダ（アプリの内部素材・キャッシュ・開発用など）
ANYWHERE_EXCLUDES = {
    "$recycle.bin", "system volume information", "appdata", "windowsapps", "node_modules",
    ".git", "__pycache__", "site-packages", ".venv", "venv", "steamapps", "cache", "caches",
    ".cache", ".thumbnails", "temp", "tmp", "thumbnails",
}
APP_FILE_EXTS = {".exe", ".dll", ".sys", ".msi"}
SMALL_IMAGE_BYTES = 10 * 1024  # ドライブ全体の調査で、これ未満の画像はアプリの部品として除外する

EXCLUSION_RULES = [
    "Windows・Program Files・ProgramData など、Windows やアプリの動作に使われる領域",
    "AppData・キャッシュ・一時フォルダ・開発用フォルダ（node_modules など）・ごみ箱",
    "隠しフォルダ・システム属性のフォルダ",
    "ドライブ全体の調査で、実行ファイル（.exe・.dll）を含むアプリのフォルダ",
    f"ドライブ全体の調査で、{SMALL_IMAGE_BYTES // 1024}KB 未満の画像（アプリの部品の可能性）",
    "外付け・ネットワーク・種類を判別できないドライブ（設定で追加すると調査します）",
    "設定で指定したスキャン対象外のフォルダ",
    "このアプリのデータ保存先",
]

_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4


@dataclass
class Location:
    path: str
    kind: str  # known / drive / extra
    label: str
    priority: int


def norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def is_within(path: str, root: str) -> bool:
    p, r = norm(path), norm(root)
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


# --- 調査場所の検出 --------------------------------------------------------------
def _known_folder_path(guid: str) -> str | None:
    """Windows の SHGetKnownFolderPath で、移動後の実際の保存先を取得する。"""
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    u = uuid.UUID(guid)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    ptr = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_wchar_p)]
    if shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) != 0:
        return None
    try:
        return ptr.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ptr)


def known_folders() -> list[tuple[str, str]]:
    """(表示名, 実際の保存先) のリスト。存在するフォルダだけ。"""
    result = []
    for label, guid, fallback in KNOWN_FOLDERS:
        path = None
        if sys.platform == "win32":
            try:
                path = _known_folder_path(guid)
            except Exception:
                path = None
        if not path:
            path = str(Path.home() / fallback)
        if os.path.isdir(path):
            result.append((label, path))
    return result


def _bus_type(letter: str) -> int | None:
    """ドライブの接続方式（USB など）を調べる。管理者権限は不要。分からなければ None。"""
    import ctypes
    from ctypes import wintypes

    class STORAGE_PROPERTY_QUERY(ctypes.Structure):
        _fields_ = [("PropertyId", wintypes.DWORD), ("QueryType", wintypes.DWORD),
                    ("AdditionalParameters", ctypes.c_ubyte * 1)]

    class STORAGE_DEVICE_DESCRIPTOR(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD),
                    ("DeviceType", ctypes.c_ubyte), ("DeviceTypeModifier", ctypes.c_ubyte),
                    ("RemovableMedia", ctypes.c_ubyte), ("CommandQueueing", ctypes.c_ubyte),
                    ("VendorIdOffset", wintypes.DWORD), ("ProductIdOffset", wintypes.DWORD),
                    ("ProductRevisionOffset", wintypes.DWORD), ("SerialNumberOffset", wintypes.DWORD),
                    ("BusType", wintypes.DWORD), ("RawPropertiesLength", wintypes.DWORD),
                    ("RawDeviceProperties", ctypes.c_ubyte * 512)]

    k32 = ctypes.windll.kernel32
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW(f"\\\\.\\{letter}:", 0, 0x1 | 0x2, None, 3, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        return None
    try:
        query = STORAGE_PROPERTY_QUERY(0, 0)
        desc = STORAGE_DEVICE_DESCRIPTOR()
        returned = wintypes.DWORD()
        ok = k32.DeviceIoControl(wintypes.HANDLE(handle), 0x2D1400, ctypes.byref(query),
                                 ctypes.sizeof(query), ctypes.byref(desc), ctypes.sizeof(desc),
                                 ctypes.byref(returned), None)
        return int(desc.BusType) if ok else None
    finally:
        k32.CloseHandle(wintypes.HANDLE(handle))


def local_drives() -> list[dict]:
    """ドライブの一覧。kind: internal / external / network / removable / unknown。"""
    if sys.platform != "win32":
        return []  # ドライブ全体の自動調査は Windows のみ
    import ctypes
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    system = os.environ.get("SystemDrive", "C:").rstrip("\\").upper()
    drives = []
    for i in range(26):
        if not mask >> i & 1:
            continue
        letter = chr(65 + i)
        root = f"{letter}:\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype == 4:
            kind = "network"
        elif dtype in (2, 5):
            kind = "removable"
        elif dtype == 3:
            try:
                bus = _bus_type(letter)
            except Exception:
                bus = None
            if f"{letter}:" == system:
                kind = "internal"
            elif bus in (7, 4, 0x0C, 0x0D):  # USB / 1394 / SD / MMC
                kind = "external"
            elif bus is None:
                kind = "unknown"
            else:
                kind = "internal"
        else:
            continue
        drives.append({"root": root, "kind": kind})
    return drives


DRIVE_KIND_LABEL = {"internal": "内蔵ドライブ", "external": "外付けドライブ", "network": "ネットワークドライブ",
                    "removable": "リムーバブルドライブ", "unknown": "種類を判別できないドライブ"}


def build_locations(db: Database, include_disabled: bool = False) -> tuple[list[Location], list[dict]]:
    """調査する場所（優先順）と、自動では調べないドライブの一覧。

    「調べる場所を選ぶ」でチェックを外した場所は、include_disabled=True のときだけ含める。
    """
    locations: list[Location] = []
    seen: set[str] = set()
    disabled = set() if include_disabled else {norm(p) for p in db.json_list("disabled_locations")}

    def add(path, kind, label):
        path = os.path.normpath(path)  # 表記を統一（C:/… と C:\… を同じ表記に）
        if norm(path) not in seen and norm(path) not in disabled and os.path.isdir(path):
            seen.add(norm(path))
            locations.append(Location(path, kind, label, len(locations)))

    for label, path in known_folders():
        add(path, "known", label)
    not_scanned = []
    for d in local_drives():
        if d["kind"] == "internal":
            add(d["root"], "drive", f"{DRIVE_KIND_LABEL['internal']} {d['root']}")
        else:
            not_scanned.append(d)
    for path in db.json_list("extra_locations"):
        add(path, "extra", f"追加した場所 {path}")
    return locations, not_scanned


# --- 除外判定 ------------------------------------------------------------------
class Exclusions:
    def __init__(self, db: Database, skip_roots: list[str]) -> None:
        self.user = [norm(p) for p in db.json_list("excluded_folders")]
        self.skip_roots = {norm(p) for p in skip_roots}  # 別の調査場所として調べる場所
        self.fixed = {norm(str(db.data_dir))}
        for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            if os.environ.get(var):
                self.fixed.add(norm(os.environ[var]))

    def directory_reason(self, path: str, name: str, depth: int, st) -> str | None:
        n = norm(path)
        low = name.lower()
        if n in self.skip_roots:
            return None  # 別の調査場所として扱う（スキップ記録はしない）
        if n in self.fixed or (depth == 1 and low in ROOT_EXCLUDES):
            return "Windows やアプリの動作に使われる領域"
        if low in ANYWHERE_EXCLUDES:
            return "アプリの内部素材・キャッシュ・一時ファイルの領域"
        if name.startswith("."):
            return "隠しフォルダ"
        attrs = getattr(st, "st_file_attributes", 0) if st is not None else 0
        if attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM):
            return "隠しフォルダ・システムフォルダ"
        for u in self.user:
            if n == u or n.startswith(u.rstrip("\\/") + os.sep):
                return "設定でスキャン対象外にしたフォルダ"
        return None


# --- 探索 ----------------------------------------------------------------------
class Controller:
    """一時停止・再開・キャンセル。GUI スレッドから操作してよい。"""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self._run = threading.Event()
        self._run.set()

    def pause(self) -> None:
        self._run.clear()

    def resume(self) -> None:
        self._run.set()

    @property
    def paused(self) -> bool:
        return not self._run.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()
        self._run.set()

    def checkpoint(self, report=None, text: str = "") -> bool:
        """一時停止中は待つ。キャンセルされたら False。"""
        if not self._run.is_set() and report:
            report({"phase": "paused", "text": "一時停止中: " + text})
        while not self._run.wait(0.2):
            pass
        return not self.cancel_event.is_set()


def scan_location(db: Database, loc: Location, skip_roots: list[str], gen: int, ctl: Controller,
                  report, totals: dict, batch_interval: float = 0.5) -> str:
    """1つの場所を調べて登録する。戻り値は調査状態（完了 / 一部スキップ / 中断）。"""
    excl = Exclusions(db, skip_roots)
    # ドライブ全体を調べるとき（追加した場所がドライブ直下の場合も含む）はアプリ素材の除外を強める
    drive_mode = loc.kind == "drive" or Path(loc.path).parent == Path(loc.path)
    db.conn.execute("DELETE FROM scan_skips WHERE location=?", (loc.path,))
    db.conn.execute("UPDATE scan_locations SET status='調査中' WHERE path=?", (loc.path,))
    db.conn.commit()
    errors = 0
    files = size_total = 0
    batch: list[tuple] = []
    last = time.monotonic()

    def skip(path, reason, kind):
        db.conn.execute("INSERT OR REPLACE INTO scan_skips VALUES (?,?,?,?,?)",
                        (path, loc.path, reason, kind, time.time()))

    def flush(current):
        nonlocal batch, last
        if batch:
            db.upsert_media(batch, gen, loc.path)
            batch = []
        db.conn.commit()
        report({"phase": "scan", "location": loc.label, "current": current, "found": totals["files"],
                "bytes": totals["bytes"], "refresh": True})
        last = time.monotonic()

    stack = [os.path.normpath(loc.path)]
    visited: set[str] = set()
    while stack:
        current = stack.pop()
        if not ctl.checkpoint(report, current):
            flush(current)
            db.conn.execute("UPDATE scan_locations SET status='中断' WHERE path=?", (loc.path,))
            db.conn.commit()
            return "中断"
        key = norm(current)
        if key in visited:
            continue
        visited.add(key)
        try:
            entries = list(os.scandir(current))
        except OSError as e:
            errors += 1
            skip(current, e.strerror or type(e).__name__, "error")
            continue
        subdirs, found, app_folder = [], [], False
        at_drive_root = drive_mode and norm(current) == norm(loc.path)
        for entry in entries:
            try:
                if is_link(entry):
                    continue  # リンク・ジャンクションはたどらない（循環と二重集計を防ぐ）
                if entry.is_dir(follow_symlinks=False):
                    if norm(entry.path) in excl.skip_roots:
                        continue  # 別の調査場所として調べる
                    st = entry.stat(follow_symlinks=False) if sys.platform == "win32" else None
                    reason = excl.directory_reason(entry.path, entry.name, 1 if at_drive_root else 99, st)
                    if reason:
                        skip(entry.path, reason, "excluded")
                    else:
                        subdirs.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if drive_mode and ext in APP_FILE_EXTS:
                    app_folder = True
                kind = KIND_OF_CATEGORY.get(categorize(entry.name))
                if kind is None:
                    continue
                st = entry.stat(follow_symlinks=False)
                if drive_mode and kind == "image" and st.st_size < SMALL_IMAGE_BYTES:
                    continue
                found.append((entry.path, entry.name, current, st.st_size, st.st_mtime, ext, kind,
                              is_cloud_only(st)))
            except OSError as e:
                errors += 1
                skip(entry.path, e.strerror or type(e).__name__, "error")
        if app_folder and norm(current) != norm(loc.path):
            skip(current, "アプリのフォルダ（実行ファイルを含む）", "excluded")
            continue
        stack.extend(reversed(subdirs))
        batch.extend(found)
        for item in found:
            files += 1
            size_total += item[3]
        totals["files"] += len(found)
        totals["bytes"] += sum(item[3] for item in found)
        if len(batch) >= 500 or time.monotonic() - last >= batch_interval:
            flush(current)
    flush(loc.path)
    status = "一部スキップ" if errors else "完了"
    db.conn.execute("UPDATE scan_locations SET status=?, files=?, bytes=?, last_completed=? WHERE path=?",
                    (status, files, size_total, time.time(), loc.path))
    db.conn.commit()
    if status == "完了":
        db.mark_unseen_missing(loc.path, gen)  # 最後まで調べられた場所だけ
    return status


def task_autoscan(data_dir, tools: media.FFmpegTools, ctl: Controller, report) -> dict:
    """自動スキャンの一連の流れ（別スレッドで実行）。

    1. 調査場所を検出して探索・登録 → 2. 新規・変更ファイルの軽い解析
    → 3. サイズが同じファイルだけハッシュ計算（完全重複） → 4. 類似画像のグループ化
    """
    db = Database(data_dir)
    summary = {"cancelled": False, "statuses": {}}
    try:
        locations, not_scanned = build_locations(db)
        db.set_json_list("not_scanned_drives", [f"{d['root']}（{DRIVE_KIND_LABEL[d['kind']]}）"
                                                for d in not_scanned])
        keep = {loc.path for loc in locations}
        for row in db.conn.execute("SELECT path FROM scan_locations").fetchall():
            if row[0] not in keep:
                db.conn.execute("DELETE FROM scan_locations WHERE path=?", (row[0],))
        for loc in locations:
            db.conn.execute(
                "INSERT INTO scan_locations(path,kind,label,priority) VALUES (?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, label=excluded.label, "
                "priority=excluded.priority, status=CASE WHEN status='調査中' THEN '中断' ELSE status END",
                (loc.path, loc.kind, loc.label, loc.priority))
        db.conn.commit()
        gen = int(db.get_setting("scan_gen", "0")) + 1
        db.set_setting("scan_gen", str(gen))
        db.set_setting("scan_started", str(time.time()))
        totals = {"files": 0, "bytes": 0}
        for i, loc in enumerate(locations):
            # 優先度の高い場所（先に調べた場所）と、他の場所の中にある場所は二重に調べない
            others = [o.path for o in locations if o is not loc and is_within(o.path, loc.path)]
            status = scan_location(db, loc, others, gen, ctl, report, totals)
            summary["statuses"][loc.label] = status
            if status == "中断":
                summary["cancelled"] = True
                return summary
        db.set_setting("scan_finished", str(time.time()))
        if not candidates.run_analysis(db, tools, ctl, report):
            summary["cancelled"] = True
            return summary
        summary["scan_files"] = totals["files"]
        return summary
    finally:
        db.close()
