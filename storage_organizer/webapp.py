"""ブラウザで使う RecSort（このパソコンの中だけで動く小さなサーバー）。

- 画面はブラウザ（web_ui/）。調査・候補・保存・ごみ箱への移動は、デスクトップ版と同じ処理をそのまま使う
- 127.0.0.1 だけで待ち受ける（ほかのパソコンからは開けない）。起動ごとに作る合言葉（トークン）がない要求は断る。
  ほかのサイトのページから、こっそり API を呼ばれないようにするため（Host ヘッダーも確認する）
- 録画の内容を外部へ送信しない。標準ライブラリだけで動く（Flask などは使わない）
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import APP_NAME, __version__, autoscan, candidates, media, trash
from .store import Store, StoreError

UI_DIR = Path(__file__).resolve().parent / "web_ui"
DECISIONS = {"残す", "保留", "削除候補", None}


class App:
    """サーバー全体で共有する状態（保存データ・調査・ごみ箱への移動）。"""

    def __init__(self, db: Store, backend=None) -> None:
        self.db = db
        self.backend = backend  # テストでは偽の移動処理を渡す（None ならWindows のごみ箱）
        self.token = secrets.token_urlsafe(24)
        self.scan_thread: threading.Thread | None = None
        self.scan_ctl: autoscan.Controller | None = None
        self.scan_progress: dict = {}
        self.scan_result: dict | None = None
        self.trash_job: dict | None = None
        self._lock = threading.Lock()
        self._thumb_lock = threading.Semaphore(2)

    # --- 共通 -----------------------------------------------------------------
    def tools(self) -> media.FFmpegTools:
        return media.find_ffmpeg(self.db.get_setting("ffmpeg_dir"))

    def ctx(self) -> candidates.Context:
        return candidates.Context(self.db)

    def row_json(self, r: dict, ctx: candidates.Context) -> dict:
        meta = candidates.loads(r["meta"], {}) or {} if r.get("meta") else {}
        return {"id": r["id"], "name": r["name"], "path": r["path"], "folder": r["folder"], "size": r["size"],
                "mtime": r["mtime"], "taken_at": r["taken_at"], "ext": r["ext"] or "",
                "duration": candidates.duration(r), "width": meta.get("width"), "height": meta.get("height"),
                "cloud_only": bool(r["cloud_only"]), "protected": bool(r["protected"]),
                "favorite": bool(r.get("favorite")), "decision": r["decision"], "missing": bool(r.get("missing")),
                "reasons": ctx.short_reasons(r), "detail": ctx.reasons(r)}

    def videos(self, ctx: candidates.Context) -> list[dict]:
        return [r for r in ctx.rows.values() if r["kind"] == "video" and not r.get("missing")]

    # --- 状態 -----------------------------------------------------------------
    def state(self) -> dict:
        ctx = self.ctx()
        vids = self.videos(ctx)
        plan = candidates.plan_recordings(ctx, None)
        chosen = [r for r in vids if r["decision"] == "削除候補" and not r["protected"]]
        tools = self.tools()
        paths = [loc["path"] for loc in self.db.locations()] or [os.environ.get("SystemDrive", "C:") + "\\"]
        last = self.db.get_setting("scan_finished")
        return {
            "app": APP_NAME, "version": __version__,
            "videos": len(vids), "video_bytes": sum(r["size"] for r in vids),
            "plan_count": len(plan["items"]), "plan_bytes": plan["total"],
            "duplicate_bytes": plan["duplicate_total"],
            "chosen_count": len(chosen), "chosen_bytes": sum(r["size"] for r in chosen),
            "drives": candidates.drive_space(paths),
            "ffmpeg": tools.available, "ffmpeg_source": tools.source,
            "scanning": self.scan_thread is not None and self.scan_thread.is_alive(),
            "progress": self.scan_progress, "scan_result": self.scan_result,
            "analysis_state": self.db.get_setting("analysis_state"),
            "last_scan": float(last) if last else None,
            "protection_ok": self.db.protection_ok, "problems": list(self.db.load_problems),
            "trashing": bool(self.trash_job and not self.trash_job.get("done")),
            "not_scanned": self.db.json_list("not_scanned_drives"),
            "data_dir": str(self.db.data_dir),
        }

    # --- 調査 -----------------------------------------------------------------
    def start_scan(self) -> dict:
        with self._lock:
            if self.scan_thread is not None and self.scan_thread.is_alive():
                return {"ok": False, "error": "調査中です"}
            self.scan_ctl = ctl = autoscan.Controller()
            self.scan_progress = {"text": "調査を始めています"}
            self.scan_result = None
            tools = self.tools()

            def run():
                try:
                    self.scan_result = autoscan.task_autoscan(self.db, tools, ctl, self._report)
                except Exception as e:  # noqa: BLE001  画面を「調査中」のままにしない
                    self.scan_result = {"error": str(e) or type(e).__name__}
                finally:
                    self.scan_progress = {}
            self.scan_thread = threading.Thread(target=run, name="recsort-scan", daemon=True)
            self.scan_thread.start()
        return {"ok": True}

    def _report(self, info: dict) -> None:
        self.scan_progress = {k: v for k, v in info.items() if isinstance(v, (str, int, float, bool)) or v is None}

    def cancel_scan(self) -> dict:
        if self.scan_ctl:
            self.scan_ctl.cancel()
        return {"ok": True}

    # --- 一覧・候補 -------------------------------------------------------------
    def files(self) -> dict:
        ctx = self.ctx()
        return {"items": [self.row_json(r, ctx) for r in self.videos(ctx)]}

    def plan(self, target_gb: float | None, drive: str | None) -> dict:
        ctx = self.ctx()
        target = int(target_gb * 1024 ** 3) if target_gb else None
        plan = candidates.plan_recordings(ctx, target, drive=drive or None)
        rows = {}
        for it in plan["items"]:
            for mid in (it["id"], it.get("keep_id")):
                if mid is not None and mid in ctx.rows:
                    rows[mid] = self.row_json(ctx.rows[mid], ctx)
        groups = []
        for n, members in enumerate(ctx.video_groups):
            groups.append({"index": n, "members": members,
                           "confirmed": candidates.similar_keep_confirmed(ctx, n)})
        plan["rows"] = rows
        plan["similar_groups"] = groups
        plan["drives"] = sorted({candidates.drive_of(r["path"]) for r in self.videos(ctx)})
        if plan["target"] == float("inf"):
            plan["target"] = None
        return plan

    def group(self, mid: int) -> dict:
        """比較の対象: 完全重複のグループ、または類似録画のグループ。"""
        ctx = self.ctx()
        if mid in ctx.dup_of:
            kind, members = "duplicate", ctx.dup_groups[ctx.dup_of[mid]]
            keep = ctx.dup_keep[ctx.dup_of[mid]]
        elif mid in ctx.vgroup_of:
            n = ctx.vgroup_of[mid]
            kind, members = "similar", ctx.video_groups[n]
            confirmed = candidates.similar_keep_confirmed(ctx, n)
            keep = (confirmed, "比較して残す1本に選んだ録画") if confirmed else ctx.vgroup_keep[n]
        else:
            kind, members, keep = "single", [mid], (mid, "")
        return {"kind": kind, "keep_id": keep[0], "keep_reason": keep[1],
                "items": [self.row_json(ctx.rows[m], ctx) for m in members if m in ctx.rows]}

    # --- 判断 -----------------------------------------------------------------
    def decide(self, ids: list[int], decision) -> dict:
        if decision not in DECISIONS:
            return {"ok": False, "error": "判断の値が正しくありません"}
        if decision == "削除候補":  # 保護中は削除候補にしない（デスクトップ版と同じ）
            ids = [m for m in ids if self.db.video(m) and not self.db.video(m)["protected"]]
        skipped = self.db.set_decisions(ids, decision)  # 戻り値は記録しなかった（保護中の）ID
        return {"ok": True, "skipped": len(skipped)}

    def keep_one(self, keep: int, others: list[int]) -> dict:
        """この1本を残し、ほかを削除候補にする（まだ移動しない）。保護中は削除候補にしない。"""
        others = [m for m in others if m != keep and self.db.video(m) and not self.db.video(m)["protected"]]
        self.db.set_decision(keep, "残す")
        if others:
            self.db.set_decisions(others, "削除候補")
        return {"ok": True, "count": len(others)}

    # --- 削除候補・ごみ箱 ---------------------------------------------------------
    def chosen(self) -> dict:
        ctx = self.ctx()
        items = []
        for r in self.videos(ctx):
            if r["decision"] != "削除候補":
                continue
            item = self.row_json(r, ctx)
            item["blocked"] = trash.check(self.db, r["id"], ctx)
            keep = ctx.dup_keep.get(ctx.dup_of.get(r["id"])) if r["id"] in ctx.dup_of else None
            item["keeper"] = ctx.rows[keep[0]]["path"] if keep and keep[0] != r["id"] else None
            items.append(item)
        return {"items": items, "generation": self.db.generation}

    def start_trash(self, ids: list[int], generation: int | None) -> dict:
        with self._lock:
            if self.trash_job and not self.trash_job.get("done"):
                return {"ok": False, "error": "ごみ箱への移動を実行中です"}
            job = {"done": False, "current": 0, "total": len(ids), "name": "", "results": [],
                   "cancel": threading.Event(), "error": None}
            self.trash_job = job

        def progress(n, total, name):
            job.update(current=n + 1, total=total, name=name)

        def run():
            try:
                with trash.windows_com_thread():  # Windows のごみ箱 API は、呼び出すスレッドで COM の初期化が必要
                    results = trash.move_to_trash(self.db, ids, self.backend, progress=progress,
                                                  cancel=job["cancel"], expected_generation=generation)
                job["results"] = [{"id": r.media_id, "path": r.path, "size": r.size, "status": r.status,
                                   "message": r.message, "record_error": r.record_error} for r in results]
            except Exception as e:  # noqa: BLE001  画面を「実行中」のままにしない
                job["error"] = str(e) or type(e).__name__
            finally:
                job["done"] = True
        threading.Thread(target=run, name="recsort-trash", daemon=True).start()
        return {"ok": True}

    def trash_status(self) -> dict:
        job = self.trash_job
        if not job:
            return {"running": False}
        return {"running": not job["done"], **{k: v for k, v in job.items() if k != "cancel"}}

    # --- 設定 -----------------------------------------------------------------
    def settings(self) -> dict:
        tools = self.tools()
        return {"ffmpeg_dir": self.db.get_setting("ffmpeg_dir") or "", "ffmpeg": tools.available,
                "ffmpeg_source": tools.source, "auto_scan": self.db.get_setting("auto_scan_on_start", "1") != "0",
                "locations": self.db.locations(), "extra_locations": self.db.json_list("extra_locations"),
                "excluded_folders": self.db.json_list("excluded_folders"),
                "not_scanned": self.db.json_list("not_scanned_drives"), "data_dir": str(self.db.data_dir)}

    def save_settings(self, data: dict) -> dict:
        if "ffmpeg_dir" in data:
            self.db.set_setting("ffmpeg_dir", str(data["ffmpeg_dir"]).strip() or None)
        if "auto_scan" in data:
            self.db.set_setting("auto_scan_on_start", "1" if data["auto_scan"] else "0")
        for key in ("extra_locations", "excluded_folders"):
            if key in data:
                values = [str(v).strip() for v in data[key] if str(v).strip()]
                bad = [v for v in values if not os.path.isabs(v)]
                if bad:
                    return {"ok": False, "error": f"場所はフルパスで指定してください: {bad[0]}"}
                self.db.set_json_list(key, values)
        return {"ok": True, **self.settings()}

    # --- 録画の再生・サムネイル -----------------------------------------------------
    def media_path(self, mid: int) -> str | None:
        row = self.db.video(mid)
        if row is None or row["missing"] or row["cloud_only"] or row["kind"] != "video":
            return None  # 登録した録画だけを返す（任意のファイルは読ませない）。オンライン専用は勝手にダウンロードしない
        return row["path"]

    def thumbnail(self, mid: int) -> str | None:
        row = self.db.video(mid)
        if row is None or row["cloud_only"] or row["missing"]:
            return None
        out = str(self.db.thumb_dir / f"m{mid}_{int(row['mtime'])}_{row['size']}.jpg")
        if os.path.exists(out):
            return out
        tools = self.tools()
        if not tools.available:
            return None
        with self._thumb_lock:  # 同時に作るのは2本まで（ほかの操作を重くしない）
            if os.path.exists(out):
                return out
            try:
                info = media.probe(tools, row["path"])
                paths = media.make_thumbnails(tools, row["path"], info.get("duration", 0), out[:-4], None)
            except Exception:  # noqa: BLE001  作れなければ「プレビューなし」と表示する
                return None
            if not paths:
                return None
            os.replace(paths[min(1, len(paths) - 1)], out)
            for p in paths:
                if os.path.exists(p):
                    os.remove(p)  # アプリのキャッシュ内の一時画像だけを消す
        return out

    def reveal(self, mid: int) -> dict:
        row = self.db.video(mid)
        if row is None:
            return {"ok": False, "error": "見つかりません"}
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(row["path"])])
            return {"ok": True}
        return {"ok": False, "error": "この操作は Windows でだけ使えます"}


class Handler(BaseHTTPRequestHandler):
    app: App = None  # type: ignore[assignment]
    server_version = "RecSort"

    def log_message(self, fmt, *args):  # 録画のパスをコンソールに出さない
        pass

    # --- 応答 -----------------------------------------------------------------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    # --- 安全確認 ---------------------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost")

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-RecSort-Token") or (query.get("t") or [""])[0]
        return secrets.compare_digest(given, self.app.token)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1024 * 1024:
            raise ValueError("要求が大きすぎます")
        raw = self.rfile.read(n) if n > 0 else b""
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("要求の形式が正しくありません")
        return data

    # --- 振り分け ---------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "このアドレスからは開けません")
        url = urlparse(self.path)
        q = parse_qs(url.query)
        path = url.path
        if not path.startswith("/api/"):
            return self._static(path)
        if not self._token_ok(q):
            return self._error(HTTPStatus.FORBIDDEN, "ページを開き直してください（合言葉が一致しません）")
        try:
            if path == "/api/state":
                return self._json(self.app.state())
            if path == "/api/files":
                return self._json(self.app.files())
            if path == "/api/plan":
                target = float(q["target"][0]) if q.get("target") and q["target"][0] else None
                return self._json(self.app.plan(target, (q.get("drive") or [None])[0]))
            if path.startswith("/api/group/"):
                return self._json(self.app.group(int(path.rsplit("/", 1)[1])))
            if path == "/api/chosen":
                return self._json(self.app.chosen())
            if path == "/api/trash":
                return self._json(self.app.trash_status())
            if path == "/api/settings":
                return self._json(self.app.settings())
            if path.startswith("/api/media/"):
                return self._file(self.app.media_path(int(path.rsplit("/", 1)[1])))
            if path.startswith("/api/thumb/"):
                return self._file(self.app.thumbnail(int(path.rsplit("/", 1)[1])), "image/jpeg")
        except ValueError:
            return self._error(HTTPStatus.BAD_REQUEST, "要求の形式が正しくありません")
        return self._error(HTTPStatus.NOT_FOUND, "見つかりません")

    def do_POST(self):
        # 断る場合も、送られてきた中身を先に読み切る（読まずに応答すると Windows では接続が切れて見える）
        try:
            data = self._body()
        except ValueError:
            data = None
        if not self._host_ok():
            return self._error(HTTPStatus.FORBIDDEN, "このアドレスからは開けません")
        url = urlparse(self.path)
        if not self._token_ok({}):  # 書き込みはヘッダーの合言葉だけを受け付ける（URL に載せない）
            return self._error(HTTPStatus.FORBIDDEN, "ページを開き直してください（合言葉が一致しません）")
        try:
            if data is None:
                raise ValueError("要求の形式が正しくありません")
            ids = [int(i) for i in data.get("ids", [])]
            p = url.path
            if p == "/api/scan":
                return self._json(self.app.start_scan())
            if p == "/api/scan/cancel":
                return self._json(self.app.cancel_scan())
            if p == "/api/decide":
                return self._json(self.app.decide(ids, data.get("decision")))
            if p == "/api/keep":
                return self._json(self.app.keep_one(int(data["keep"]), ids))
            if p == "/api/protect":
                self.app.db.set_protected(int(data["id"]), bool(data["value"]))
                return self._json({"ok": True})
            if p == "/api/favorite":
                self.app.db.set_favorite(int(data["id"]), bool(data["value"]))
                return self._json({"ok": True})
            if p == "/api/viewed":
                self.app.db.mark_viewed(int(data["id"]))
                return self._json({"ok": True})
            if p == "/api/trash":
                if data.get("confirm") is not True:  # 最終確認の承認がない要求では移動しない
                    return self._error(HTTPStatus.BAD_REQUEST, "最終確認がありません")
                gen = data.get("generation")
                return self._json(self.app.start_trash(ids, int(gen) if gen is not None else None))
            if p == "/api/trash/cancel":
                job = self.app.trash_job
                if job:
                    job["cancel"].set()
                return self._json({"ok": True})
            if p == "/api/settings":
                return self._json(self.app.save_settings(data))
            if p == "/api/reveal":
                return self._json(self.app.reveal(int(data["id"])))
        except StoreError as e:  # 保存できなかったことを、成功したように見せずに伝える
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"保存できませんでした: {e}")
        except (ValueError, KeyError, TypeError):
            return self._error(HTTPStatus.BAD_REQUEST, "要求の形式が正しくありません")
        return self._error(HTTPStatus.NOT_FOUND, "見つかりません")

    # --- ファイル -------------------------------------------------------------
    def _static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else unquote(path.lstrip("/"))
        target = (UI_DIR / name).resolve()
        if UI_DIR not in target.parents or not target.is_file():
            return self._error(HTTPStatus.NOT_FOUND, "見つかりません")
        body = target.read_bytes()
        if name == "index.html":  # 合言葉は、このパソコンで開いたページにだけ渡す
            body = body.replace(b"__RECSORT_TOKEN__", self.app.token.encode())
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, body, ctype, {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self'; img-src 'self' data:"})

    def _file(self, path: str | None, ctype: str | None = None) -> None:
        """録画・サムネイルを返す。動画のシークのため Range（一部分だけ）にも対応する。読み取りだけ。"""
        if not path or not os.path.isfile(path):
            return self._error(HTTPStatus.NOT_FOUND, "見つかりません")
        size = os.path.getsize(path)
        ctype = ctype or mimetypes.guess_type(path)[0] or "application/octet-stream"
        start, end, status = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start, end = int(a), (int(b) if b else size - 1)
                else:
                    start, end = max(size - int(b), 0), size - 1
            except ValueError:
                start, end = 0, size - 1
            if start >= size or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end, status = min(end, size - 1), 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionError, OSError):
            pass  # 再生位置を変えるとブラウザが途中で接続を切る（正常）


def make_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    from .instance_lock import AlreadyRunning, InstanceLock
    from .store import default_data_dir
    argv = sys.argv[1:] if argv is None else argv
    data_dir = default_data_dir()
    try:  # 保存データを読み書きする前に、二重起動を防ぐロックを取る（デスクトップ版とも共通）
        lock = InstanceLock(data_dir).acquire()
    except AlreadyRunning:
        print("RecSort はすでに起動しています。保護や判断の記録が食い違わないよう、同時に2つは起動できません。")
        return 1
    try:
        db = Store(data_dir)
        app = App(db)
        server = None
        for port in (8765, 8766, 8767, 0):  # 決まった番号が使われていれば、空いている番号を使う
            try:
                server = make_server(app, port)
                break
            except OSError:
                continue
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        print(f"RecSort {__version__} をブラウザで開きます: {url}")
        print("このウィンドウを閉じるか Ctrl+C で終了します。")
        if "--no-browser" not in argv:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        if db.get_setting("auto_scan_on_start", "1") != "0" and "--no-scan" not in argv:
            app.start_scan()
        try:
            server.serve_forever(poll_interval=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            if app.scan_ctl:
                app.scan_ctl.cancel()
            if app.scan_thread:
                app.scan_thread.join(15)
            db.flush_user_state()
            db.save_scan_state()
        return 0
    finally:
        lock.release()
