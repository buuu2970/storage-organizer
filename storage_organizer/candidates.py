"""整理候補の判定（担当: 候補判定）。

内容の面白さや大切さは判断しない。候補にした理由（重複・類似・失敗の可能性・容量）を必ず添える。
「不要」とは表示せず、「整理候補」「確認候補」とする。
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone

from . import media
from .db import Database, loads

LARGE_BYTES = 100 * 1024 * 1024  # 大容量として優先表示する目安
SHORT_VIDEO_SEC = 3.0
SIMILAR_THRESHOLD = 5  # dHash（64ビット）のハミング距離。これ以下を「似ている」とする
_BANDS = 6  # 類似候補を絞るための区切り（ハミング距離5以下なら必ずどこかの区切りが一致する）

FAILURE_LABEL = {"short": "極端に短い録画", "dark": "ほぼ真っ暗", "read_error": "読み込みエラー",
                 "corrupt": "破損を確認"}


# =============================================================================
# 軽い解析（新規・変更されたファイルだけ）
# =============================================================================
def _parse_exif_date(value) -> float | None:
    try:
        return datetime.strptime(str(value).strip()[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def dhash(img) -> str:
    """縮小・再圧縮・軽い編集に強い知覚ハッシュ（64ビット）。"""
    from PIL import Image
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def analyze_image(path: str) -> dict:
    """画像のサイズ・撮影日時・類似判定用ハッシュ。撮影日時が取れなければ None（不明）。"""
    from PIL import Image, UnidentifiedImageError
    if os.path.getsize(path) == 0:
        return {"state": "完了", "failures": [{"kind": "corrupt", "reason": "ファイルの中身が空です（0バイト）"}]}
    try:
        with Image.open(path) as img:
            meta = {"width": img.width, "height": img.height, "format": img.format}
            taken = None
            try:
                exif = img.getexif()
                sub = exif.get_ifd(0x8769)
                taken = _parse_exif_date(sub.get(36867) or sub.get(36868)) if sub else None
            except Exception:
                taken = None  # EXIF が読めなくても画像自体は扱う
            img.draft("RGB", (64, 64))
            h = dhash(img)
        return {"state": "完了", "meta": meta, "taken_at": taken, "dhash": h, "failures": []}
    except UnidentifiedImageError:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".heic", ".heif", ".raw", ".cr2", ".nef", ".arw", ".psd", ".svg"):
            return {"state": "対象外", "note": f"{ext} は未対応の形式のため、プレビュー・類似判定はできません"
                                              "（HEIC は pillow-heif を導入すると対応できます）", "failures": []}
        return {"state": "完了", "failures": [{"kind": "read_error", "reason":
                "画像として読み込めませんでした。破損とは限りません（形式が違う・未対応の可能性があります）"}]}
    except OSError as e:
        if "truncated" in str(e).lower():
            return {"state": "完了", "failures": [{"kind": "corrupt", "reason": "画像データが途中で切れています"}]}
        return {"state": "完了", "failures": [{"kind": "read_error", "reason":
                f"読み込めませんでした（{e}）。破損とは限りません"}]}


def _parse_creation_time(text: str | None) -> float | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp() if dt.year > 1971 else None
    except ValueError:
        return None


def analyze_video_light(tools: media.FFmpegTools, path: str, cancel=None) -> dict:
    """動画の長さ・解像度・録画日時と、確認用3場面の明るさ。全編は読み込まない。"""
    if os.path.getsize(path) == 0:
        return {"state": "完了", "failures": [{"kind": "corrupt", "reason": "ファイルの中身が空です（0バイト）"}]}
    try:
        info = media.probe(tools, path, cancel)
    except RuntimeError as e:
        msg = str(e)
        if "moov atom not found" in msg:
            return {"state": "完了", "failures": [{"kind": "corrupt", "reason":
                    "動画の索引情報がありません（録画が途中で終わった可能性があります）"}]}
        return {"state": "完了", "failures": [{"kind": "read_error", "reason":
                "動画として読み込めませんでした。破損とは限りません（コーデック未対応などの可能性があります）"}]}
    cp = media._run([tools.ffprobe, "-v", "error", "-show_entries", "format_tags=creation_time",
                     "-of", "default=nw=1:nk=1", path], cancel, timeout=30)
    taken = _parse_creation_time(cp.stdout.decode("utf-8", "replace").strip().splitlines()[0]
                                 if cp.returncode == 0 and cp.stdout.strip() else None)
    failures = []
    duration = info.get("duration", 0)
    if 0 < duration < SHORT_VIDEO_SEC:
        failures.append({"kind": "short", "reason": f"再生時間が {duration:.1f} 秒と極端に短い録画です"
                                                   "（意図した短い動画の可能性もあります）"})
    if info.get("has_video"):
        try:
            frames = media.extract_frames(tools, path, duration, 3, cancel)
            feat = media.frame_features(frames)
            if feat["black_ratio"] >= 0.99 or (feat["frames"] >= 3 and feat["brightness"] < 0.04):
                failures.append({"kind": "dark", "reason": "確認した3場面がほぼ真っ暗です"
                                                          "（音声だけの録画や暗い場面の可能性もあります）"})
        except RuntimeError:
            failures.append({"kind": "read_error", "reason": "映像の場面を取り出せませんでした。破損とは限りません"})
    return {"state": "完了", "meta": info, "taken_at": taken, "failures": failures}


def light_analyze(db: Database, row, tools: media.FFmpegTools, cancel=None) -> str:
    """1件を解析して保存する。読み取りの前後でファイル状態を比べ、変わっていたら結果を捨てる。"""
    if row["cloud_only"]:
        db.update_video(row["id"], light_state="対象外", light_note="オンライン専用ファイルのため内容を読み込みません")
        return "対象外"
    if row["kind"] == "video" and not tools.available:
        db.update_video(row["id"], light_note="FFmpeg が見つからないため、動画の解析ができません（設定で場所を指定できます）")
        return "未解析"
    try:
        before = media.file_state(row["path"])
        db.update_file_state(row["id"], before[0], before[1] / 1e9)
        result = (analyze_image(row["path"]) if row["kind"] == "image"
                  else analyze_video_light(tools, row["path"], cancel))
        media.ensure_unchanged(row["path"], before)
    except media.FileChanged as e:
        db.update_video(row["id"], light_note=str(e))
        return "未解析"
    except (PermissionError, FileNotFoundError, OSError) as e:
        result = {"state": "完了", "failures": [{"kind": "read_error", "reason":
                  f"ファイルを開けませんでした（{e.strerror or e}）。破損とは限りません"}]}
    fields = {"light_state": result["state"], "light_note": result.get("note"), "failure": result["failures"]}
    if result.get("meta"):
        merged = loads(db.video(row["id"])["meta"], {}) or {}
        merged.update(result["meta"])
        fields["meta"] = merged
    if "taken_at" in result:
        fields["taken_at"] = result["taken_at"]
    if result.get("dhash"):
        fields["dhash"] = result["dhash"]
    db.update_video(row["id"], **fields)
    return result["state"]


# =============================================================================
# 重複・類似のグループ
# =============================================================================
def find_exact_duplicates(db: Database, ctl, report) -> bool:
    """サイズが同じファイルだけ SHA-256 を計算する。計算済みで変更のないファイルはやり直さない。"""
    by_size = defaultdict(list)
    for row in db.media():
        if not row["cloud_only"] and row["size"] > 0:
            by_size[row["size"]].append(row)
    todo = [r for rows in by_size.values() if len(rows) > 1 for r in rows if not r["sha256"]]
    total = sum(r["size"] for r in todo)
    done = [0]

    def on_bytes(n):
        done[0] += n

    for i, row in enumerate(todo):
        if not ctl.checkpoint(report, "完全重複の確認"):
            return False
        report({"phase": "hash", "text": f"完全重複を確認中（{done[0] / 1024 ** 2:,.0f} / {total / 1024 ** 2:,.0f} MB）",
                "done": done[0], "total": total})
        try:
            size, mtime_ns = media.file_state(row["path"])
            if db.update_file_state(row["id"], size, mtime_ns / 1e9):
                continue
            db.update_video(row["id"], sha256=media.sha256_file(row["path"], ctl.cancel_event, on_bytes))
        except media.Cancelled:
            return False
        except (OSError, media.FileChanged):
            continue
    return True


def group_similar_images(db: Database) -> int:
    """似た画像をグループにまとめる（完全一致だけのグループは「重複」で扱うので除く）。"""
    rows = [r for r in db.media() if r["kind"] == "image" and r["dhash"] and not r["cloud_only"]]
    n = len(rows)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    bits = [int(r["dhash"], 16) for r in rows]
    width = 64 // _BANDS + 1
    for b in range(_BANDS):
        buckets = defaultdict(list)
        shift = b * width
        for i, v in enumerate(bits):
            buckets[(v >> shift) & ((1 << width) - 1)].append(i)
        for members in buckets.values():
            if len(members) < 2:
                continue
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    i, j = members[x], members[y]
                    if bin(bits[i] ^ bits[j]).count("1") <= SIMILAR_THRESHOLD:
                        parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(rows[i])
    db.conn.execute("UPDATE videos SET similar_group=NULL")
    gid = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        hashes = {m["sha256"] for m in members}
        if len(hashes) == 1 and None not in hashes:
            continue  # すべて完全一致 → 「重複」で扱う
        gid += 1
        db.conn.executemany("UPDATE videos SET similar_group=? WHERE id=?", [(gid, m["id"]) for m in members])
    db.conn.commit()
    return gid


def run_analysis(db: Database, tools: media.FFmpegTools, ctl, report) -> bool:
    """スキャン後の解析。新規・変更されたファイルだけを対象にする。"""
    todo = [r for r in db.media() if r["light_state"] == "未解析"
            and not (r["kind"] == "video" and not tools.available and r["light_note"])]
    for i, row in enumerate(todo):
        if not ctl.checkpoint(report, "解析"):
            return False
        report({"phase": "analyze", "text": f"新しいファイルを解析中 {i + 1}/{len(todo)}: {row['name']}",
                "done": i, "total": len(todo), "refresh": i % 50 == 0})
        try:
            light_analyze(db, row, tools, ctl.cancel_event)
        except media.Cancelled:
            return False
    if not find_exact_duplicates(db, ctl, report):
        return False
    report({"phase": "similar", "text": "似た画像をまとめています…"})
    group_similar_images(db)
    report({"phase": "done", "text": "", "refresh": True})
    return True


# =============================================================================
# 候補と理由
# =============================================================================
def pixels(row) -> int:
    meta = loads(row["meta"], {}) or {}
    return int(meta.get("width") or 0) * int(meta.get("height") or 0)


def duration(row) -> float | None:
    meta = loads(row["meta"], {}) or {}
    d = meta.get("duration")
    return float(d) if d else None


def per_minute(row) -> float | None:
    d = duration(row)
    return row["size"] / (d / 60) if d and d > 0 else None


def failures(row) -> list[dict]:
    return loads(row["failure"], []) or []


def file_identity(path: str) -> tuple:
    """実体としてのファイルの識別子。

    別の表記（C:\\… と C:/…、大文字小文字の違い）やハードリンクでも、同じ実体なら同じ値になる。
    識別番号が取れない場合は、正規化した実際のパスで代用する。
    """
    try:
        st = os.stat(path)
        if st.st_ino:
            return ("id", st.st_dev, st.st_ino)
    except OSError:
        pass
    return ("path", os.path.normcase(os.path.realpath(path)))


class Context:
    """一覧表示のたびに使う集計（重複・類似グループと残すおすすめ）。"""

    def __init__(self, db: Database) -> None:
        self.rows = {r["id"]: r for r in db.media()}
        dup = defaultdict(list)
        sim = defaultdict(list)
        for r in self.rows.values():
            if r["sha256"]:
                dup[r["sha256"]].append(r["id"])
            if r["similar_group"]:
                sim[r["similar_group"]].append(r["id"])
        # 同じ実体を指す登録（別の表記・ハードリンク）は重複として扱わない。消しても容量は空かず、
        # 片方を「重複分」として移動すると、実際には1本しかない録画を失うため
        self.aliases: dict[int, int] = {}
        groups = {}
        for h, ids in dup.items():
            if len(ids) < 2:
                continue
            seen: dict[tuple, int] = {}
            members = []
            for i in sorted(ids, key=lambda i: (self.rows[i]["path"] != os.path.normpath(self.rows[i]["path"]),
                                                self.rows[i]["path"])):
                ident = file_identity(self.rows[i]["path"])
                if ident in seen:
                    self.aliases[i] = seen[ident]
                    continue
                seen[ident] = i
                members.append(i)
            if len(members) > 1:
                groups[h] = members
        self.dup_groups = groups
        self.sim_groups = {g: ids for g, ids in sim.items() if len(ids) > 1}
        self.dup_of = {i: h for h, ids in self.dup_groups.items() for i in ids}
        self.sim_of = {i: g for g, ids in self.sim_groups.items() for i in ids}
        self.dup_keep = {h: keep_for_duplicates([self.rows[i] for i in ids]) for h, ids in self.dup_groups.items()}
        self.sim_keep = {g: keep_for_similar([self.rows[i] for i in ids]) for g, ids in self.sim_groups.items()}

    def is_large(self, row) -> bool:
        return row["size"] >= LARGE_BYTES

    def reasons(self, row) -> list[str]:
        """そのファイルが候補になった理由（大切さは判断していないことを前提に表示）。"""
        out = []
        if row["id"] in self.aliases:
            other = self.rows[self.aliases[row["id"]]]
            out.append(f"同じファイルが別の表記で二重に登録されています（{other['path']}）。重複ではないため候補にしません")
        h = self.dup_of.get(row["id"])
        if h:
            keep_id, why = self.dup_keep[h]
            n = len(self.dup_groups[h]) - 1
            if keep_id == row["id"]:
                out.append(f"完全重複: 内容が完全に一致するファイルが他に {n} 個あります。このファイルを残すおすすめです（{why}）")
            else:
                out.append(f"完全重複: 内容が完全に一致するファイルが他に {n} 個あります（SHA-256 で確認）。"
                           "1つは残す前提で、重複分の整理候補です")
        g = self.sim_of.get(row["id"])
        if g:
            keep_id, why = self.sim_keep[g]
            others = [i for i in self.sim_groups[g] if i != row["id"]]
            exact = sum(1 for i in others if row["sha256"] and self.rows[i]["sha256"] == row["sha256"])
            detail = f"うち {exact} 枚は完全一致、残りは似ているだけで同一ではありません" if exact \
                else "完全一致ではありません"
            if keep_id == row["id"]:
                out.append(f"類似画像: 似た画像が他に {len(others)} 枚あります（{detail}）。"
                           f"このファイルを残すおすすめです（{why}）")
            else:
                out.append(f"類似画像: 似た画像が他に {len(others)} 枚あります（{detail}）。"
                           "比較して残す画像を選んでください")
        for f in failures(row):
            out.append(f"{FAILURE_LABEL[f['kind']]}: {f['reason']}")
        if self.is_large(row):
            text = "大容量: 容量の大きいファイルです"
            pm = per_minute(row)
            if pm:
                text += f"（1分あたり {pm / 1024 ** 2:,.1f} MB。これだけでは整理候補と判断しません）"
            out.append(text)
        return out

    def is_suggestable(self, row) -> bool:
        """目標容量の提案に含めてよい候補（大容量だけの理由では含めない）。"""
        if row["protected"] or row["decision"] == "残す" or row["cloud_only"] or row["id"] in self.aliases:
            return False
        h = self.dup_of.get(row["id"])
        if h and self.dup_keep[h][0] != row["id"]:
            return True
        g = self.sim_of.get(row["id"])
        if g and self.sim_keep[g][0] != row["id"]:
            return True
        return bool(failures(row))


def keep_for_duplicates(rows) -> tuple[int, str]:
    protected = [r for r in rows if r["protected"]]
    if protected:
        return protected[0]["id"], "「絶対に残す」が付いています"
    best = min(rows, key=lambda r: (r["mtime"], len(r["path"])))
    return best["id"], "最も古くから保存されているファイルです"


def keep_for_similar(rows) -> tuple[int, str]:
    protected = [r for r in rows if r["protected"]]
    if protected:
        return protected[0]["id"], "「絶対に残す」が付いています"
    best = max(rows, key=lambda r: (pixels(r), r["size"]))
    meta = loads(best["meta"], {}) or {}
    return best["id"], f"解像度が最も高い（{meta.get('width')}×{meta.get('height')}）。大切さは判断していません"


CATEGORIES = ["すべて", "重複", "類似画像", "録画確認候補", "大容量", "保護", "保留", "削除候補"]


def category_ids(ctx: Context, category: str) -> list[int]:
    rows = ctx.rows.values()
    if category == "重複":
        return [i for ids in ctx.dup_groups.values() for i in ids]
    if category == "類似画像":
        return [i for ids in ctx.sim_groups.values() for i in ids]
    if category == "録画確認候補":
        return [r["id"] for r in rows if failures(r)]
    if category == "大容量":
        return [r["id"] for r in rows if ctx.is_large(r)]
    if category == "保護":
        return [r["id"] for r in rows if r["protected"]]
    if category == "保留":
        return [r["id"] for r in rows if r["decision"] == "保留"]
    if category == "削除候補":
        return [r["id"] for r in rows if r["decision"] == "削除候補"]
    return [r["id"] for r in rows]


# =============================================================================
# 目標容量からの提案
# =============================================================================
def propose_for_target(ctx: Context, target_bytes: int) -> dict:
    """整理候補（重複分・類似の残すおすすめ以外・失敗の可能性）から、目標に近い組み合わせを選ぶ。

    候補が足りなければ、ほかのファイルを足さずに不足分を返す。
    """
    pool = [r for r in ctx.rows.values() if ctx.is_suggestable(r)]
    pool_total = sum(r["size"] for r in pool)
    if pool_total <= target_bytes:
        chosen = pool
    else:
        chosen, total = [], 0
        for r in sorted(pool, key=lambda r: -r["size"]):  # 大きいものから目標に達するまで
            if total >= target_bytes:
                break
            chosen.append(r)
            total += r["size"]
        for r in sorted(chosen, key=lambda r: r["size"]):  # 超えすぎた分を減らす
            if total - r["size"] >= target_bytes:
                chosen.remove(r)
                total -= r["size"]
    total = sum(r["size"] for r in chosen)
    return {"ids": [r["id"] for r in chosen], "total": total, "shortage": max(target_bytes - total, 0),
            "pool_total": pool_total}


def protect_group_rule(ctx: Context, ids: set[int]) -> list[str]:
    """重複グループのすべてを対象にしていないか確認する（最低1つは残す）。"""
    problems = []
    for h, members in ctx.dup_groups.items():
        if set(members) <= ids:
            problems.append(f"内容が同じファイル {len(members)} 個がすべて対象になっています: "
                            + "、".join(ctx.rows[i]["name"] for i in members))
    return problems


# =============================================================================
# 容量の集計
# =============================================================================
def summary(db: Database) -> dict:
    rows = db.media()
    by_kind = defaultdict(lambda: [0, 0])
    by_folder = defaultdict(lambda: [0, 0])
    by_ext = defaultdict(lambda: [0, 0])
    for r in rows:
        for table, key in ((by_kind, r["kind"]), (by_folder, r["folder"]), (by_ext, r["ext"] or "(なし)")):
            table[key][0] += 1
            table[key][1] += r["size"]
    cloud = [r for r in rows if r["cloud_only"]]
    return {
        "image": tuple(by_kind["image"]), "video": tuple(by_kind["video"]),
        "folders": sorted(((k, *v) for k, v in by_folder.items()), key=lambda x: -x[2]),
        "exts": sorted(((k, *v) for k, v in by_ext.items()), key=lambda x: -x[2]),
        "cloud_only": (len(cloud), sum(r["size"] for r in cloud)),
    }


def format_date(ts: float | None) -> str:
    if not ts:
        return "不明"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "不明"



# =============================================================================
# 録画の整理候補（目標容量に合わせたルールによる提案）
#   1. 完全一致の重複（各グループで必ず1本を残す）を最優先にする
#   2. 重複だけで目標に届かない場合だけ、根拠のある「要確認」候補を追加する
#      （失敗した可能性・大容量・長期間更新なし）。古い・大きいだけで不要とは判断しない
#   3. 保護・「残す」・オンライン専用は対象外。根拠のないファイルは追加しない
#   視聴履歴や重要度は推測しない。機械学習は使わない。
# =============================================================================
REVIEW_LARGE_BYTES = 500 * 1024 * 1024  # 「容量が大きい」とする目安
REVIEW_OLD_DAYS = 365                   # 「長期間更新されていない」とする目安
REVIEW_OLD_MIN_BYTES = 50 * 1024 * 1024  # 古いだけの小さなファイルは候補にしない


def drive_of(path: str) -> str:
    """ファイルがあるドライブ（Windows はドライブ文字、それ以外はマウント位置）。"""
    if os.name == "nt":
        d = os.path.splitdrive(os.path.abspath(path))[0]
        return (d + "\\") if d else path
    p = os.path.abspath(path)
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def drive_space(paths) -> list[dict]:
    """調査した場所があるドライブごとの実際の空き容量。"""
    import shutil
    result, seen = [], set()
    for path in paths:
        root = drive_of(path)
        if root in seen:
            continue
        seen.add(root)
        try:
            u = shutil.disk_usage(root)
        except OSError:
            continue
        result.append({"root": root, "free": u.free, "total": u.total, "used": u.used})
    return result


def _review_reasons(row, now_ts: float) -> list[str]:
    reasons = []
    for f in failures(row):
        prefix = {"corrupt": "破損を確認: ", "read_error": "読み込みエラー: "}.get(f["kind"], "")
        reasons.append(prefix + f["reason"])
    if row["size"] >= REVIEW_LARGE_BYTES:
        text = f"容量が大きい録画です（{_fmt(row['size'])}）"
        pm = per_minute(row)
        if pm:
            text += f"。1分あたり {_fmt(int(pm))}"
        reasons.append(text + "。内容を確認してください")
    days = (now_ts - row["mtime"]) / 86400
    if days >= REVIEW_OLD_DAYS and row["size"] >= REVIEW_OLD_MIN_BYTES:
        reasons.append(f"約 {int(days // 30)} か月更新されていません（視聴したかどうかは分かりません）")
    return reasons


def _fmt(n: int) -> str:
    from .formatting import format_size
    return format_size(n)


def plan_recordings(ctx: Context, target_bytes: int, include_images: bool = False,
                    now_ts: float | None = None) -> dict:
    """目標容量に合わせて録画の整理候補を提案する（ルールによる提案）。

    戻り値:
      items: [{"id", "kind": "duplicate"/"review", "label", "reasons", "size", "keep_id"}]
      total（提案の合計）, target, shortage（不足分）, achieved,
      duplicate_total, review_total, review_pool, excluded（対象外の内訳）
    """
    import time as _time
    now_ts = now_ts or _time.time()
    kinds = {"video", "image"} if include_images else {"video"}
    rows = {i: r for i, r in ctx.rows.items() if r["kind"] in kinds}
    excluded = {"保護": 0, "残す": 0, "オンライン専用": 0}

    def usable(r) -> bool:
        if r["id"] in ctx.aliases:  # 同じ実体の別表記は、提案の対象にも数にも入れない
            return False
        if r["protected"]:
            excluded["保護"] += 1
            return False
        if r["decision"] == "残す":
            excluded["残す"] += 1
            return False
        if r["cloud_only"]:
            excluded["オンライン専用"] += 1
            return False
        return True

    usable_ids = {i for i, r in rows.items() if usable(r)}
    keep_ids = set()
    dup_items = []
    for h, members in ctx.dup_groups.items():
        members = [m for m in members if m in rows]
        if len(members) < 2:
            continue
        keep_id, why = ctx.dup_keep[h]
        if keep_id not in members:
            keep_id = members[0]
        keep_ids.add(keep_id)
        keep_name = ctx.rows[keep_id]["name"]
        for m in members:
            if m == keep_id or m not in usable_ids:
                continue
            r = rows[m]
            dup_items.append({
                "id": m, "kind": "duplicate", "label": "重複", "size": r["size"], "keep_id": keep_id,
                "reasons": [f"「{keep_name}」と内容が完全に一致します（SHA-256 で確認）。"
                            f"そちらを残すため、この1本を整理しても同じ録画は残ります（{why}）"]})
    # 重複の中では大きいものから。目標に達したら、超えすぎた分を減らす
    dup_items.sort(key=lambda it: -it["size"])
    chosen, total = [], 0
    for it in dup_items:
        if total >= target_bytes:
            break
        chosen.append(it)
        total += it["size"]
    for it in sorted(list(chosen), key=lambda it: it["size"]):
        if total - it["size"] >= target_bytes:
            chosen.remove(it)
            total -= it["size"]
    review_pool = []
    if total < target_bytes:
        # 重複だけで届かない場合だけ、根拠のある「要確認」候補を追加する（残す1本は対象外）
        for i in usable_ids - keep_ids - {it["id"] for it in dup_items}:
            reasons = _review_reasons(rows[i], now_ts)
            if reasons:
                review_pool.append({"id": i, "kind": "review", "label": "要確認", "size": rows[i]["size"],
                                    "keep_id": None, "reasons": reasons, "failure": bool(failures(rows[i]))})
        review_pool.sort(key=lambda it: (not it["failure"], -it["size"]))
        for it in review_pool:
            if total >= target_bytes:
                break
            chosen.append(it)
            total += it["size"]
    dup_total = sum(it["size"] for it in chosen if it["kind"] == "duplicate")
    return {"items": chosen, "total": total, "target": target_bytes, "shortage": max(target_bytes - total, 0),
            "achieved": total >= target_bytes, "duplicate_total": dup_total, "review_total": total - dup_total,
            "duplicate_pool": sum(it["size"] for it in dup_items), "review_pool": len(review_pool),
            "excluded": excluded}
