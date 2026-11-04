"""整理候補の判定（担当: 候補判定）。

内容の面白さや大切さは判断しない。候補にした理由（重複・類似・失敗の可能性・容量）を必ず添える。
「不要」とは表示せず、「整理候補」「確認候補」とする。機械学習は使わない（決まったルールだけで判定する）。
読み込めないことは「破損」の証明ではないので、「再生できない可能性」「読み込み失敗」と表示する。
保護中・お気に入り・「残す」の録画は、おすすめに含めない。
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone

from . import media
from . import similar
from .store import Store as Database, loads

LARGE_BYTES = 100 * 1024 * 1024  # 大容量として優先表示する目安
SHORT_VIDEO_SEC = 3.0
SIMILAR_THRESHOLD = 5  # dHash（64ビット）のハミング距離。これ以下を「似ている」とする
_BANDS = 6  # 類似候補を絞るための区切り（ハミング距離5以下なら必ずどこかの区切りが一致する）

SILENT_DB = -50.0  # 確認した区間の最大音量がこれ未満なら「ほぼ無音」（音声トラックがある録画だけ）

FAILURE_LABEL = {"short": "極端に短い録画", "dark": "黒画面が多い", "read_error": "読み込み失敗",
                 "corrupt": "再生できない可能性", "silent": "音声がほぼ無音"}
# 一覧に出す短い理由
SHORT_LABEL = {"short": "短い", "dark": "黒画面", "read_error": "読込失敗", "corrupt": "再生不可?", "silent": "無音"}


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
    px = list(small.tobytes())  # 白黒（L）なので1画素1バイト。getdata() は Pillow 14 で廃止予定
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
        except (RuntimeError, TimeoutError):
            failures.append({"kind": "read_error", "reason": "映像の場面を取り出せませんでした。破損とは限りません"})
    silent = silence_failure(tools, path, info, cancel)
    if silent:
        failures.append(silent)
    return {"state": "完了", "meta": info, "taken_at": taken, "failures": failures}


def silence_failure(tools: media.FFmpegTools, path: str, info: dict, cancel=None) -> dict | None:
    """音声トラックがあるのに、確認したすべての区間がほぼ無音なら理由を返す。

    測れなかった区間があるとき・時間切れのときは判定しない（無音と決めつけない）。
    """
    duration = info.get("duration") or 0
    if not info.get("has_audio") or duration <= 0:
        return None
    try:
        values = media.measure_max_volume(tools, path, duration, 3, cancel)
    except (RuntimeError, TimeoutError, OSError):
        return None
    expected = 3 if duration > media.VOLUME_SEGMENT_SEC * 2 else 1
    if len(values) < expected or not all(v < SILENT_DB for v in values):
        return None
    loudest = max(values)
    shown = "測定できないほど小さい音" if loudest == float("-inf") else f"{loudest:.0f} dB"
    return {"kind": "silent", "reason": f"確認した {len(values)} か所（各 {int(media.VOLUME_SEGMENT_SEC)} 秒まで）の"
                                        f"最大音量が {shown} で、ほぼ無音です（音声を録らない設定の可能性もあります）"}


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
        report({"phase": "hash", "text": f"同じ内容のファイルを確認しています（{done[0] / 1024 ** 2:,.0f} / {total / 1024 ** 2:,.0f} MB）",
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
    mapping: dict[int, list[int]] = {}
    gid = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        hashes = {m["sha256"] for m in members}
        if len(hashes) == 1 and None not in hashes:
            continue  # すべて完全一致 → 「重複」で扱う
        gid += 1
        mapping[gid] = [m["id"] for m in members]
    db.set_similar_groups(mapping)
    return gid


def find_similar_videos(db: Database, tools: media.FFmpegTools, ctl, report) -> bool:
    """長さがほぼ同じ録画がある動画だけ、代表場面を取り出して比べる（段階的な解析）。

    代表場面はファイルが変わるまで再利用する。全ファイルの内容は読み込まない。
    """
    if not tools.available:
        return True
    rows = [r for r in db.media() if r["kind"] == "video" and not r["cloud_only"]
            and (loads(r["meta"], {}) or {}).get("duration")
            and not any(f["kind"] in ("corrupt", "read_error") for f in failures(r))]
    rows.sort(key=lambda r: loads(r["meta"])["duration"])
    durations = [loads(r["meta"])["duration"] for r in rows]
    need = set()
    for i in range(len(rows) - 1):  # 長さが近い録画が1本でもある動画だけ（隣どうしを見れば足りる）
        if similar.duration_close(durations[i], durations[i + 1]):
            need.update((i, i + 1))
    todo = [rows[i] for i in sorted(need) if (loads(rows[i]["vsig"], {}) or {}).get("v") != similar.VERSION]
    for n, row in enumerate(todo):
        if not ctl.checkpoint(report, "類似録画の確認"):
            return False
        report({"phase": "similar", "text": f"似た録画を確認しています（代表場面の取り出し {n + 1} / {len(todo)}）",
                "done": n, "total": len(todo)})
        try:
            before = media.file_state(row["path"])
            sig = similar.signature(tools, row["path"], loads(row["meta"])["duration"], ctl.cancel_event)
            media.ensure_unchanged(row["path"], before)
            db.update_video(row["id"], vsig=sig)
        except media.Cancelled:
            return False
        except (OSError, RuntimeError, TimeoutError, media.FileChanged):
            continue
    sigs = {rows[i]["id"]: loads(rows[i]["vsig"]) for i in sorted(need) if loads(rows[i]["vsig"], {})}
    label = {"prepare": "似た録画を確認しています（候補を絞り込み中）", "compare": "似た録画を確認しています（比較中）",
             "group": "似た録画を確認しています（まとめ中）"}
    try:
        groups, stats = similar.group(
            sigs, cancel=ctl.cancel_event,
            progress=lambda stage, done, total: report({"phase": "similar", "done": done, "total": total,
                                                        "text": f"{label[stage]} {done} / {total}"}))
    except similar.Cancelled:
        return False  # 途中の結果は保存しない（前回の結果のまま。完了扱いにしない）
    # すべて完全一致のグループは「重複」で扱う
    groups = [g for g in groups if len({db.video(i)["sha256"] for i in g} - {None}) != 1
              or any(db.video(i)["sha256"] is None for i in g)]
    db.set_video_groups(groups)
    return True


def run_analysis(db: Database, tools: media.FFmpegTools, ctl, report) -> bool:
    """スキャン後の解析。新規・変更されたファイルだけを対象にする。"""
    todo = [r for r in db.media() if r["light_state"] == "未解析"
            and not (r["kind"] == "video" and not tools.available and r["light_note"])]
    for i, row in enumerate(todo):
        if not ctl.checkpoint(report, "解析"):
            return False
        report({"phase": "analyze", "text": f"録画の長さや画面を確認しています（{i + 1} / {len(todo)}）",
                "done": i, "total": len(todo), "refresh": i % 50 == 0})
        try:
            light_analyze(db, row, tools, ctl.cancel_event)
        except media.Cancelled:
            return False
    if not find_exact_duplicates(db, ctl, report):
        return False
    if not find_similar_videos(db, tools, ctl, report):
        return False
    # 画像の整理（類似画像のまとめ）は今の版では行わない（録画の整理を優先する）
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
        # 類似録画（解像度違い・再圧縮など）。比較が必要な候補で、同じ内容とは保証しない
        vg = db.video_groups() if hasattr(db, "video_groups") else []
        # 完全重複のコピーは「重複」グループだけで扱い、類似グループでは残す1本にまとめる（二重計上しない）
        dup_keep_ids = {h: k for h, (k, _) in self.dup_keep.items()}

        def collapse(grp):
            out = []
            for i in grp:
                if i not in self.rows or i in self.aliases:
                    continue
                h = self.dup_of.get(i)
                rep = dup_keep_ids[h] if h else i
                if rep not in out:
                    out.append(rep)
            return out
        self.video_groups = [g for g in (collapse(grp) for grp in vg) if len(g) > 1]
        self.vgroup_of = {i: n for n, g in enumerate(self.video_groups) for i in g}
        self.vgroup_detail = {n: similar_group_detail(self.rows, g) for n, g in enumerate(self.video_groups)}
        self.vgroup_keep = {n: keep_for_similar_videos([self.rows[i] for i in g], self.vgroup_detail[n])
                            for n, g in enumerate(self.video_groups)}
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
        n = self.vgroup_of.get(row["id"])
        if n is not None:
            others = len(self.video_groups[n]) - 1
            keep_id, why = self.vgroup_keep[n]
            text = (f"類似録画: 長さがほぼ同じ録画が他に {others} 本あります。{self.vgroup_detail[n]['text']}"
                    "（同じ内容とは限りません。比較して確認してください）")
            if keep_id == row["id"]:
                text += f"。この録画を残すおすすめです（{why}）"
            elif keep_id is None:
                text += f"。{why}"
            out.append(text)
        for f in failures(row):
            out.append(f"{FAILURE_LABEL.get(f['kind'], f['kind'])}: {f['reason']}")
        if self.is_large(row):
            text = "大容量: 容量の大きいファイルです"
            pm = per_minute(row)
            if pm:
                text += f"（1分あたり {pm / 1024 ** 2:,.1f} MB）"
            out.append(text + "。容量が大きいだけでは削除をおすすめしません")
        if row.get("favorite"):
            out.append("お気に入りのため、整理のおすすめには含めません（お気に入りを外すと対象に戻ります）")
        if row["protected"]:
            out.append("保護中のため、おすすめ・ごみ箱への移動の対象になりません")
        return out

    def short_reasons(self, row) -> list[str]:
        """一覧の「理由」列に出す短い見出し。"""
        out = []
        h = self.dup_of.get(row["id"])
        if h:
            out.append("重複（残す側）" if self.dup_keep[h][0] == row["id"] else "重複")
        if self.vgroup_of.get(row["id"]) is not None:
            out.append("類似")
        for f in failures(row):
            out.append(SHORT_LABEL.get(f["kind"], f["kind"]))
        if self.is_large(row):
            out.append("大容量")
        return list(dict.fromkeys(out))

    def is_suggestable(self, row) -> bool:
        """目標容量の提案に含めてよい候補（大容量だけの理由では含めない）。"""
        if row["protected"] or row.get("favorite") or row["decision"] == "残す" or row["cloud_only"] \
                or row["id"] in self.aliases:
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
        return protected[0]["id"], "保護されています"
    favorite = [r for r in rows if r.get("favorite")]
    if favorite:
        return favorite[0]["id"], "お気に入りです"
    best = min(rows, key=lambda r: (r["mtime"], len(r["path"])))
    return best["id"], "最も古くから保存されているファイルです"


LIMITS_NOTE = "全編と音声は比較していません"


def similar_pair_detail(a_row, b_row) -> dict:
    """2本の代表場面の一致状況（一致・不一致・比較できない場面の番号）。"""
    a, b = loads(a_row["vsig"], {}) or {}, loads(b_row["vsig"], {}) or {}
    if not a.get("frames") or not b.get("frames"):
        return {"matched": [], "unmatched": [], "uncomparable": list(range(similar.N_POINTS)), "per_frame": []}
    r = similar.compare(a, b)
    per = r["per_frame"]
    return {"matched": [i for i, c in enumerate(per) if c is not None and c >= similar.MATCH_CORR],
            "unmatched": [i for i, c in enumerate(per) if c is not None and c < similar.MATCH_CORR],
            "uncomparable": [i for i, c in enumerate(per) if c is None], "per_frame": per}


def similar_group_detail(rows: dict, members: list[int]) -> dict:
    """グループ全体の場面の一致状況。どれか1組でも一致しない場面は「一致しない」として数える。"""
    unmatched, uncomparable, matched_all = set(), set(), None
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            d = similar_pair_detail(rows[a], rows[b])
            unmatched |= set(d["unmatched"])
            uncomparable |= set(d["uncomparable"])
            matched_all = set(d["matched"]) if matched_all is None else matched_all & set(d["matched"])
    uncomparable -= unmatched
    matched = (matched_all or set()) - unmatched - uncomparable
    text = (f"代表場面 {similar.N_POINTS} のうち {len(matched)} が一致、{len(unmatched)} が一致しない、"
            f"{len(uncomparable)} が比較できない（{LIMITS_NOTE}）")
    return {"matched": sorted(matched), "unmatched": sorted(unmatched), "uncomparable": sorted(uncomparable),
            "text": text}


def keep_for_similar_videos(rows, detail: dict | None = None) -> tuple[int | None, str]:
    """類似録画の「残すおすすめ」。明確な優劣がなければ決めない（None）。

    解像度だけで高画質とは断定しない。保護したものがあれば、それを残す。
    一致しない場面があるときは、内容が違う可能性があるので、おすすめを決めない。
    """
    protected = [r for r in rows if r["protected"]]
    if protected:
        return protected[0]["id"], "保護されています"
    favorite = [r for r in rows if r.get("favorite")]
    if len(favorite) == 1:
        return favorite[0]["id"], "お気に入りの録画です"
    kept = [r for r in rows if r["decision"] == "残す"]
    if len(kept) == 1:
        return kept[0]["id"], "「残す」を選んだ録画です"
    if detail and detail["unmatched"]:
        return None, (f"一致しない場面が {len(detail['unmatched'])} あり、内容が違う可能性があるため、"
                      "おすすめを決めません。比較して確認してください")
    best = max(rows, key=pixels)
    others = [r for r in rows if r is not best]
    durs = [duration(r) or 0 for r in rows]
    if pixels(best) and all(pixels(best) >= 1.5 * max(pixels(r), 1) for r in others) \
            and (duration(best) or 0) >= max(durs) - 1.5:
        meta = loads(best["meta"], {}) or {}
        return best["id"], (f"解像度が最も高い（{meta.get('width')}×{meta.get('height')}）。"
                            "画質の良し悪しは比較して確認してください")
    return None, "明確な優劣は判断できません。比較して残す1本を選んでください"


def similar_keep_confirmed(ctx: "Context", group_index: int) -> int | None:
    """利用者が比較して残す1本を確定したか（保護、または「残す」を1本選んだ）。確定した ID を返す。"""
    rows = [ctx.rows[i] for i in ctx.video_groups[group_index]]
    protected = [r for r in rows if r["protected"]]
    if protected:
        return protected[0]["id"]
    kept = [r for r in rows if r["decision"] == "残す"]
    return kept[0]["id"] if kept else None


def keep_for_similar(rows) -> tuple[int, str]:
    protected = [r for r in rows if r["protected"]]
    if protected:
        return protected[0]["id"], "「絶対に残す」が付いています"
    best = max(rows, key=lambda r: (pixels(r), r["size"]))
    meta = loads(best["meta"], {}) or {}
    return best["id"], f"解像度が最も高い（{meta.get('width')}×{meta.get('height')}）。大切さは判断していません"


CATEGORIES = ["すべて", "重複", "類似画像", "録画確認候補", "大容量", "保護", "お気に入り", "保留", "削除候補"]


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
    if category == "お気に入り":
        return [r["id"] for r in rows if r.get("favorite")]
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


def media_drives(ctx: Context, extra_paths=()) -> list[str]:
    """録画がある（または調査した場所がある）ドライブの一覧。"""
    seen: dict[str, None] = {}
    for p in list(extra_paths) + [r["path"] for r in ctx.rows.values() if r["kind"] == "video"]:
        seen.setdefault(drive_of(p), None)
    return list(seen)


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


def _review_reasons(row, now_ts: float, failures_only: bool = False) -> list[str]:
    reasons = []
    for f in failures(row):
        reasons.append(f"{FAILURE_LABEL.get(f['kind'], f['kind'])}: {f['reason']}")
    if failures_only:
        return reasons
    if row["size"] >= REVIEW_LARGE_BYTES:
        text = f"容量が大きい録画です（{_fmt(row['size'])}）"
        pm = per_minute(row)
        if pm:
            text += f"。1分あたり {_fmt(int(pm))}"
        reasons.append(text + "。容量が大きいだけでは削除をおすすめしません。内容を確認してください")
    days = (now_ts - row["mtime"]) / 86400
    if days >= REVIEW_OLD_DAYS and row["size"] >= REVIEW_OLD_MIN_BYTES:
        reasons.append(f"約 {int(days // 30)} か月更新されていません（視聴したかどうかは分かりません）")
    return reasons


def _fmt(n: int) -> str:
    from .formatting import format_size
    return format_size(n)


def _pair_text(a_row, b_row) -> str:
    d = similar_pair_detail(a_row, b_row)
    return (f"代表場面 {similar.N_POINTS} のうち {len(d['matched'])} が一致、{len(d['unmatched'])} が一致しない、"
            f"{len(d['uncomparable'])} が比較できない（{LIMITS_NOTE}）")


class _LazyExists:
    """必要になったファイルだけ存在を確認して覚えておく。"""

    def __init__(self, rows: dict) -> None:
        self.rows = rows
        self.cache: dict[int, bool] = {}

    def __getitem__(self, media_id: int) -> bool:
        if media_id not in self.cache:
            self.cache[media_id] = os.path.exists(self.rows[media_id]["path"])
        return self.cache[media_id]

    def missing(self, aliases) -> int:
        return sum(1 for i, ok in self.cache.items() if not ok and i not in aliases)


def plan_recordings(ctx: Context, target_bytes: int | None, include_images: bool = False,
                    now_ts: float | None = None, drive: str | None = None) -> dict:
    """目標容量に合わせて録画の整理候補を提案する（ルールによる提案）。

    戻り値:
      items: [{"id", "kind": "duplicate"/"review", "label", "reasons", "size", "keep_id"}]
      total（提案の合計）, target, shortage（不足分）, achieved,
      duplicate_total, review_total, review_pool, excluded（対象外の内訳）
    """
    import time as _time
    now_ts = now_ts or _time.time()
    no_target = target_bytes is None  # 目標なし: 重複と類似録画のおすすめをすべて出す（要確認の追加はしない）
    if no_target:
        target_bytes = float("inf")
    kinds = {"video", "image"} if include_images else {"video"}
    all_rows = {i: r for i, r in ctx.rows.items() if r["kind"] in kinds}
    # 空けたいドライブにあるファイルだけを候補にする（別ドライブの整理では目的のドライブは空かない）
    on_drive = {i for i, r in all_rows.items() if drive is None or drive_of(r["path"]) == drive}
    rows = {i: all_rows[i] for i in on_drive}
    excluded = {"保護": 0, "お気に入り": 0, "残す": 0, "オンライン専用": 0}

    def usable(r) -> bool:
        if r["decision"] == "保留":
            excluded["保留"] = excluded.get("保留", 0) + 1
            return False
        if r["id"] in ctx.aliases:  # 同じ実体の別表記は、提案の対象にも数にも入れない
            return False
        if r["protected"]:
            excluded["保護"] += 1
            return False
        if r.get("favorite"):  # お気に入りは「残す」に近い扱い（お気に入りを外すと対象に戻る）
            excluded["お気に入り"] += 1
            return False
        if r["decision"] == "残す":
            excluded["残す"] += 1
            return False
        if r["cloud_only"]:
            excluded["オンライン専用"] += 1
            return False
        return True

    # スキャン後に消えた・移動したファイルは候補にしない（「残す1本」も実在するものから選ぶ）
    # 存在の確認は、候補になり得るファイルだけに行う（全ファイルを毎回確認すると、大量の録画で画面が固まる）
    exists = _LazyExists(all_rows)
    usable_ids = {i for i, r in rows.items() if usable(r)}
    keep_ids = set()
    dup_items = []
    for h, members in ctx.dup_groups.items():
        # 「残す1本」は全ドライブの実在するコピーから選ぶ。ほかのドライブにコピーがあれば、そちらを残す
        alive = [m for m in members if m in all_rows and exists[m]]
        if len(alive) < 2 or not any(m in rows for m in alive):
            continue  # 実在するコピーが1本以下なら、重複分はない
        protected = [m for m in alive if all_rows[m]["protected"]]
        favorite = [m for m in alive if all_rows[m].get("favorite")]
        elsewhere = [m for m in alive if m not in on_drive]
        if protected:
            keep_id, why = protected[0], "保護されています"
        elif favorite:
            keep_id, why = favorite[0], "お気に入りです"
        elif elsewhere:
            keep_id = min(elsewhere, key=lambda m: (all_rows[m]["mtime"], len(all_rows[m]["path"])))
            why = f"別のドライブ（{drive_of(all_rows[keep_id]['path'])}）にあるコピーを残します"
        else:
            keep_id, why = keep_for_duplicates([all_rows[m] for m in alive])
        members = [m for m in alive if m in rows] + ([keep_id] if keep_id not in rows else [])
        keep_ids.add(keep_id)
        keep_name = ctx.rows[keep_id]["name"]
        for m in members:
            if m not in rows:
                continue
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
    # 類似録画（比較が必要）。比較して残す1本を確定するまで、ごみ箱へ移す対象にはできない
    similar_items = []
    for n, members in enumerate(ctx.video_groups):
        alive = [m for m in members if m in all_rows and exists[m]]
        if len(alive) < 2 or not any(m in rows for m in alive):
            continue
        confirmed = similar_keep_confirmed(ctx, n)
        keep_id, why = (confirmed, "比較して残す1本に選んだ録画") if confirmed else ctx.vgroup_keep[n]
        if keep_id is None or keep_id not in alive:  # おすすめがなければ、いちばん大きい1本を仮に残す
            keep_id = max(alive, key=lambda m: (pixels(all_rows[m]), all_rows[m]["size"]))
            why = "明確な優劣は判断できません。比較して残す1本を選んでください"
        keep_ids.add(keep_id)
        for m in alive:
            if m == keep_id or m not in rows or m not in usable_ids or m in {it["id"] for it in dup_items}:
                continue
            similar_items.append({
                "id": m, "kind": "similar", "label": "類似（要比較）", "size": rows[m]["size"], "keep_id": keep_id,
                "group": n, "confirmed": confirmed is not None,
                "reasons": [f"「{all_rows[keep_id]['name']}」と長さがほぼ同じです。"
                            f"{_pair_text(all_rows[keep_id], all_rows[m])}"
                            f"（同じ内容とは限りません）。{why}"
                            + ("" if confirmed else "。比較して残す1本を確定すると、選べるようになります")]})
    similar_items.sort(key=lambda it: -it["size"])
    for it in similar_items:
        if total >= target_bytes:
            break
        chosen.append(it)
        total += it["size"]
    in_groups = set(ctx.vgroup_of) | set(ctx.dup_of)
    review_pool = []
    if no_target or total < target_bytes:
        # 目標なし: 客観的な問題（極端に短い・黒画面・読み込み失敗・無音など）がある録画を「要確認」として示す
        # 目標あり: 重複だけで届かない場合だけ、容量が大きい・長く更新がない録画も「要確認」に加える
        # どちらも初期状態ではチェックしない（内容を確認してから選ぶ）
        for i in usable_ids - keep_ids - {it["id"] for it in dup_items} - in_groups:
            reasons = _review_reasons(rows[i], now_ts, failures_only=no_target)
            if reasons and exists[i]:
                review_pool.append({"id": i, "kind": "review", "label": "要確認", "size": rows[i]["size"],
                                    "keep_id": None, "reasons": reasons, "failure": bool(failures(rows[i]))})
        review_pool.sort(key=lambda it: (not it["failure"], -it["size"]))
        for it in review_pool:
            if total >= target_bytes:
                break  # 目標なしのときは target_bytes が無限大なので、すべて示す
            chosen.append(it)
            total += it["size"]
    dup_total = sum(it["size"] for it in chosen if it["kind"] == "duplicate")
    similar_total = sum(it["size"] for it in chosen if it["kind"] == "similar")
    missing = exists.missing(ctx.aliases)
    if missing:
        excluded["見つからない"] = missing
    # 重複の確認（ハッシュ計算）がまだの録画: サイズが同じ録画がほかにあるのに SHA-256 がないもの
    sizes: dict[int, int] = defaultdict(int)
    for r in rows.values():
        if not r["cloud_only"] and r["size"] > 0:
            sizes[r["size"]] += 1
    unchecked = sum(1 for r in rows.values() if not r["cloud_only"] and r["size"] > 0
                    and sizes[r["size"]] > 1 and not r["sha256"])
    return {"drive": drive, "unchecked": unchecked, "items": chosen, "total": total,
            "target": None if no_target else target_bytes,
            "shortage": 0 if no_target else max(target_bytes - total, 0),
            "achieved": None if no_target else total >= target_bytes, "duplicate_total": dup_total, "similar_total": similar_total,
            "review_total": total - dup_total - similar_total,
            "duplicate_pool": sum(it["size"] for it in dup_items), "review_pool": len(review_pool),
            "excluded": excluded}
