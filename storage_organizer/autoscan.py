"""自動スキャン（担当: スキャン処理）。

PC 内の録画（動画ファイル）を、ユーザーがフォルダを選ばなくても探す。
- 一覧に登録するのは録画（動画）だけ。画像などほかのファイルは、容量の集計にだけ数える
- ピクチャ・ビデオ・ダウンロード・デスクトップ・ドキュメントを先に調べ、その後に内蔵ドライブ全体を調べる
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
from .store import Store
from .file_types import categorize
from .scanner import is_cloud_only, is_link

# 一覧に登録する種類（今の版は録画の整理に特化する。画像の整理は将来ここに "画像": "image" を足す）
KIND_OF_CATEGORY = {"動画": "video"}

# ユーザーフォルダ（Windows の既知フォルダ ID）。この順番で優先して調べる
KNOWN_FOLDERS = [
    ("ピクチャ", "{33E28130-4E1E-4676-835A-98395C3BC3BB}", "Pictures"),
    ("ビデオ", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "Videos"),
    ("ダウンロード", "{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
    ("デスクトップ", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}", "Desktop"),
    ("ドキュメント", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}", "Documents"),
]

# 調査しない Windows・アプリの領域（既知フォルダ ID で実際の場所を取得する。固定のパスだけに頼らない）
SYSTEM_KNOWN_FOLDERS = [
    "{F38BF404-1D43-42F2-9305-67DE0B28FC23}",  # Windows
    "{905E63B6-C1BF-494E-B29C-65B732D3D21A}",  # Program Files
    "{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}",  # Program Files (x86)
    "{62AB5D82-FDC1-4DC3-A9DD-070D1D495D97}",  # ProgramData
    "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",  # AppData\Local
    "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}",  # AppData\Roaming
    "{A520A1A4-1780-4FF6-BD18-167343C5AF16}",  # AppData\LocalLow
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
LIBRARY_EXTS = {".dll", ".sys"}
# 個人のファイルを置くことが多いフォルダ名（実行ファイルが混ざっていてもアプリのフォルダとはみなさない）
PERSONAL_FOLDER_NAMES = {"downloads", "ダウンロード", "desktop", "デスクトップ", "videos", "ビデオ", "recordings",
                         "録画", "captures", "キャプチャ", "documents", "ドキュメント", "pictures", "ピクチャ",
                         "music", "ミュージック"}


def looks_like_app_folder(name: str, exts: list[str]) -> bool:
    """アプリが入ったフォルダらしいか。インストーラー（.exe・.msi）があるだけでは、アプリのフォルダとしない。

    ライブラリ（.dll・.sys）があり、さらに実行ファイル（.exe）があるか、ライブラリが2つ以上あるときだけ。
    """
    if name.lower() in PERSONAL_FOLDER_NAMES:
        return False
    libs = sum(1 for e in exts if e in LIBRARY_EXTS)
    return libs >= 1 and (".exe" in exts or libs >= 2)
SMALL_IMAGE_BYTES = 10 * 1024  # ドライブ全体の調査で、これ未満の画像はアプリの部品として除外する

# 開発用のプロジェクトフォルダの目印（ソースコードの .ts などを録画と取り違えないため、丸ごと除外する）
PROJECT_MARKERS = {".git", "package.json", "tsconfig.json", "pyproject.toml", "cargo.toml", "go.mod",
                   "pom.xml", "build.gradle", "composer.json", "gemfile", ".hg", ".svn"}
# 拡張子だけでは録画と決められないもの（.ts は TypeScript のソースコードにも使われる）
AMBIGUOUS_VIDEO_EXTS = {".ts"}

EXCLUSION_RULES = [
    "開発用のプロジェクトフォルダ（.git・package.json・tsconfig.json などがあるフォルダ）",
    "拡張子が .ts でも、中身が録画（MPEG-TS 形式）でないファイル（TypeScript のソースコードなど）",
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


def system_folders() -> set[str]:
    """Windows・アプリの領域（正規化したパス）。既知フォルダ API で取得し、取れなければ環境変数で補う。"""
    found = set()
    if sys.platform == "win32":
        for guid in SYSTEM_KNOWN_FOLDERS:
            try:
                path = _known_folder_path(guid)
            except Exception:
                path = None
            if path:
                found.add(norm(path))
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA"):
        if os.environ.get(var):
            found.add(norm(os.environ[var]))
    return found


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


def build_locations(db: Store, include_disabled: bool = False) -> tuple[list[Location], list[dict]]:
    """調査する場所（優先順）と、自動では調べないドライブの一覧。

    「調べる場所を選ぶ」でチェックを外した場所は、include_disabled=True のときだけ含める。
    """
    locations: list[Location] = []
    seen: set[str] = set()
    disabled = set() if include_disabled else {norm(p) for p in db.json_list("disabled_locations")}
    demo = db.json_list("demo_locations")
    if demo:  # デモ（main.py --demo）: 決めたフォルダだけを調べる。自分の録画やファイル名を画面に映さないため
        for path in demo:
            path = os.path.normpath(path)
            if norm(path) not in seen and os.path.isdir(path):
                seen.add(norm(path))
                locations.append(Location(path, "extra", f"デモ {path}", len(locations)))
        return locations, []

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
    def __init__(self, db: Store, skip_roots: list[str]) -> None:
        self.user = [norm(p) for p in db.json_list("excluded_folders")]
        self.skip_roots = {norm(p) for p in skip_roots}  # 別の調査場所として調べる場所
        self.fixed = {norm(str(db.data_dir))} | system_folders()

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


def is_mpeg_ts(path: str) -> bool:
    """先頭の2パケット（188バイトごと）の同期バイトを確かめる。読み取るのは先頭 189 バイトだけ。"""
    try:
        with open(path, "rb") as f:  # 読み取り専用
            head = f.read(189)
    except OSError:
        return False
    return len(head) == 189 and head[0] == 0x47 and head[188] == 0x47


def is_project_folder(names: set[str]) -> bool:
    return bool({n.lower() for n in names} & PROJECT_MARKERS)


def purge_non_media(db: Store) -> int:
    """以前の版で録画として登録された .ts のソースコードなどを、一覧から外す（保護・判断の記録は残す）。"""
    count = 0
    touched = set()
    for row in db.media():
        if row["ext"] in AMBIGUOUS_VIDEO_EXTS and (row["cloud_only"] or not os.path.exists(row["path"])
                                                   or not is_mpeg_ts(row["path"])):
            db.hide(row["id"])
            touched.add(row["location"])
            count += 1
    gen = int(db.get_setting("scan_gen", "0"))
    for loc in touched - {None}:  # 外した結果を保存する（再起動しても戻らないように）
        db.save_inventory(loc, gen)
    return count


# 容量の集計で、除外した場所を測るかどうか（利用者が対象外にした場所は測らない）
MEASURE_REASONS = {
    "Windows やアプリの動作に使われる領域": "システム・アプリ",
    "アプリの内部素材・キャッシュ・一時ファイルの領域": "システム・アプリ",
    "アプリのフォルダ（実行ファイルを含む）": "システム・アプリ",
    "開発用のプロジェクトフォルダ（ソースコードなど）": "開発用フォルダ",
    "隠しフォルダ": "隠しフォルダ",
    "隠しフォルダ・システムフォルダ": "隠しフォルダ",
}
TOP_FILES = 200


def measure(path: str, ctl: Controller) -> tuple[int, int, int]:
    """フォルダの合計サイズだけを測る（内容は読まない・リンクはたどらない）。(バイト, 件数, 読めなかった数)"""
    total = count = errors = 0
    stack = [path]
    while stack:
        if ctl.cancel_event.is_set():
            break
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            errors += 1
            continue
        for e in entries:
            try:
                if e.is_symlink() or (getattr(e, "is_junction", None) and e.is_junction()):
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    count += 1
            except OSError:
                errors += 1
    return total, count, errors


def scan_location(db: Store, loc: Location, skip_roots: list[str], gen: int, ctl: Controller,
                  report, totals: dict, batch_interval: float = 0.5) -> str:
    """1つの場所を調べる。全ファイルの容量を集計し、画像・動画は一覧に登録する。

    戻り値は調査状態（完了 / 一部スキップ / 中断）。
    """
    import heapq
    excl = Exclusions(db, skip_roots)
    # ドライブ全体を調べるとき（追加した場所がドライブ直下の場合も含む）はアプリ素材の除外を強める
    drive_mode = loc.kind == "drive" or Path(loc.path).parent == Path(loc.path)
    db.clear_skips(loc.path)
    db.update_location(loc.path, status="調査中")
    errors = 0
    files = size_total = 0
    batch: list[tuple] = []
    last = time.monotonic()
    root = os.path.normpath(loc.path)
    by_category: dict[str, list[int]] = {}
    by_folder: dict[str, list[int]] = {}
    top: list[tuple[int, str, str]] = []
    areas: list[list] = []

    def folder_key(path: str) -> str:
        rel = os.path.relpath(path, root)
        first = rel.split(os.sep)[0]
        return "（この場所の直下）" if first in (".", "") else first

    def account(path: str, size: int, category: str, key: str) -> None:
        c = by_category.setdefault(category, [0, 0])
        c[0] += 1
        c[1] += size
        f = by_folder.setdefault(key, [0, 0])
        f[0] += 1
        f[1] += size
        if len(top) < TOP_FILES:
            heapq.heappush(top, (size, path, category))
        elif size > top[0][0]:
            heapq.heapreplace(top, (size, path, category))

    def skip(path, reason, kind):
        db.add_skip(path, loc.path, reason, kind)
        category = MEASURE_REASONS.get(reason) if kind == "excluded" else None
        if category:  # 容量の原因としては測る（整理候補にはしない）
            size, count, _ = measure(path, ctl)
            areas.append([path, size, reason])
            by_category.setdefault(category, [0, 0])
            by_category[category][0] += count
            by_category[category][1] += size
            k = by_folder.setdefault(folder_key(path), [0, 0])
            k[0] += count
            k[1] += size

    def save_stats():
        db.set_location_stats(loc.path, {
            "by_category": by_category, "by_folder": by_folder,
            "top_files": [[sz, p, c] for sz, p, c in sorted(top, reverse=True)],
            "areas": sorted(areas, key=lambda a: -a[1])[:200], "updated": time.time()})

    def flush(current):
        nonlocal batch, last
        if batch:
            db.upsert_media(batch, gen, loc.path)
            batch = []
        report({"phase": "scan", "location": loc.label, "current": current, "found": totals["files"],
                "bytes": totals["bytes"], "refresh": True})
        last = time.monotonic()

    stack = [root]
    visited: set[str] = set()
    while stack:
        current = stack.pop()
        if not ctl.checkpoint(report, current):
            flush(current)
            save_stats()
            db.update_location(loc.path, status="中断")
            db.save_inventory(loc.path, gen)
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
        subdirs, found, plain, app_exts = [], [], [], []
        at_drive_root = drive_mode and norm(current) == norm(loc.path)
        if norm(current) != norm(loc.path) and is_project_folder({e.name for e in entries}):
            skip(current, "開発用のプロジェクトフォルダ（ソースコードなど）", "excluded")
            continue
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
                        subdirs.append(("skip", entry.path, reason))
                    else:
                        subdirs.append(("dir", entry.path, None))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                if drive_mode and ext in APP_FILE_EXTS:
                    app_exts.append(ext)
                st = entry.stat(follow_symlinks=False)
                category = categorize(entry.name)
                kind = KIND_OF_CATEGORY.get(category)
                if kind and ext in AMBIGUOUS_VIDEO_EXTS and (is_cloud_only(st) or not is_mpeg_ts(entry.path)):
                    kind, category = None, "その他"  # TypeScript のソースコードなどを録画と取り違えない
                if kind == "image" and drive_mode and st.st_size < SMALL_IMAGE_BYTES:
                    kind = None  # アプリの部品の可能性がある小さな画像は一覧に入れない（容量は数える）
                plain.append((entry.path, st.st_size, category))
                if kind:
                    found.append((entry.path, entry.name, current, st.st_size, st.st_mtime, ext, kind,
                                  is_cloud_only(st)))
            except OSError as e:
                errors += 1
                skip(entry.path, e.strerror or type(e).__name__, "error")
        fk = folder_key(current)
        app_folder = bool(app_exts) and looks_like_app_folder(os.path.basename(current), app_exts)
        if app_folder and norm(current) != norm(loc.path):
            # アプリのフォルダ: フォルダ全体（直下のファイルを含む）の容量を一度だけ測り、一覧には入れない
            skip(current, "アプリのフォルダ（実行ファイルを含む）", "excluded")
            continue
        for path, size, category in plain:
            account(path, size, category, fk)
        for kind_, path, reason in reversed(subdirs):
            if kind_ == "skip":
                skip(path, reason, "excluded")
            else:
                stack.append(path)
        batch.extend(found)
        for item in found:
            files += 1
            size_total += item[3]
        totals["files"] += len(found)
        totals["bytes"] += sum(item[3] for item in found)
        if len(batch) >= 500 or time.monotonic() - last >= batch_interval:
            flush(current)
    flush(loc.path)
    save_stats()
    status = "一部スキップ" if errors else "完了"
    db.update_location(loc.path, status=status, files=files, bytes=size_total, last_completed=time.time(),
                       all_bytes=sum(v[1] for v in by_category.values()))
    if status == "完了":
        db.mark_unseen_missing(loc.path, gen)  # 最後まで調べられた場所だけ
    # 除外した場所（プロジェクトフォルダなど）にある以前の登録は、調査が一部スキップでも一覧から外す
    excluded = [x["path"] for x in db.skips("excluded", loc.path)]
    if excluded:
        prefixes = tuple(norm(ex).rstrip("\\/") + os.sep for ex in excluded)
        for row in db.media():
            if row["seen_gen"] < gen and norm(row["path"]).startswith(prefixes):
                db.hide(row["id"])
    db.save_inventory(loc.path, gen)
    db.save_scan_state()
    return status


def task_autoscan(data, tools: media.FFmpegTools, ctl: Controller, report) -> dict:
    """自動スキャンの一連の流れ（別スレッドで実行）。data は Store か、データの保存先フォルダ。

    1. 調査場所を探索し、全ファイルの容量を集計・画像と動画を登録 → 2. 新規・変更ファイルの軽い解析
    → 3. サイズが同じファイルだけハッシュ計算（完全重複） → 4. 類似録画・類似画像のグループ化
    """
    own = not isinstance(data, Store)
    db = Store(data) if own else data
    summary = {"cancelled": False, "statuses": {}}
    token = db.begin_activity("scan")  # 「実行中か確かめる」と「調査中にする」を1度に行う
    if token is None:
        if db.organize_active:  # 整理（ごみ箱への移動）中は調査を始めない
            summary.update(cancelled=True, error="ごみ箱へ移動している間は調査できません。終わってからもう一度調べてください")
        else:
            summary.update(cancelled=True, error="ほかの調査中のため、新しい調査は始めませんでした")
        if own:
            db.close()
        return summary
    try:
        locations, not_scanned = build_locations(db)
        db.set_json_list("not_scanned_drives", [f"{d['root']}（{DRIVE_KIND_LABEL[d['kind']]}）"
                                                for d in not_scanned])
        db.set_locations(locations)
        gen = int(db.get_setting("scan_gen", "0")) + 1
        db.set_setting("scan_gen", str(gen))
        db.set_setting("scan_started", str(time.time()))
        totals = {"files": 0, "bytes": 0}
        for loc in locations:
            # 優先度の高い場所（先に調べた場所）と、他の場所の中にある場所は二重に調べない
            others = [o.path for o in locations if o is not loc and is_within(o.path, loc.path)]
            status = scan_location(db, loc, others, gen, ctl, report, totals)
            summary["statuses"][loc.label] = status
            if status == "中断":
                summary["cancelled"] = True
                db.set_setting("analysis_state", "cancelled")
                return summary
        db.set_setting("files_scanned", str(time.time()))
        # 解析（ハッシュ・類似の確認）が終わるまでは「調査完了」としない。途中で落ちても running が残り、中断と分かる
        db.set_setting("analysis_state", "running")
        purge_non_media(db)
        if not candidates.run_analysis(db, tools, ctl, report):
            summary["cancelled"] = True
            db.set_setting("analysis_state", "cancelled")
            return summary
        db.set_setting("analysis_state", "done")
        db.set_setting("scan_finished", str(time.time()))
        summary["scan_files"] = totals["files"]
        return summary
    finally:
        db.end_activity(token)  # 自分が取った権利だけを返す（例外・中断でも必ず返す）
        if own:
            db.close()
        else:
            db.save_scan_state()
