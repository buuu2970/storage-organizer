"""ブラウザ画面用のローカル Web サーバー

127.0.0.1 のみで待ち受け、起動ごとに作る合言葉 (トークン) が無いリクエストは拒否する。
(他の Web ページからローカルの API を叩かれてファイルを動かされるのを防ぐため)

アプリに許可している書き込み操作は「デスクトップに削除予定フォルダを作る」「選ばれた動画をそこへ移動する」のみ。
ごみ箱への移動・削除の機能は持たない (利用者が自分の手で行う)。
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import __version__, ffmpeg
from ..files import default_folders
from ..video_analyzer import Thresholds
from ..pending_folder import pending_dir
from ..video_cleanup import HISTORY_FILE, THUMB_DIR, CleanupScanner

STATIC_DIR = Path(__file__).parent / "static"
# Windows の mimetypes はレジストリ次第で .js を text/plain にすることがあるので固定する
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
MAX_BODY = 1 << 20  # POST の上限 1MB


class App:
    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        self.scanner = CleanupScanner()


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # アクセスログは出さない
            pass

        # --- 応答
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _authorized(self, query: dict) -> bool:
            given = self.headers.get("X-Token") or query.get("t", [""])[0]
            return secrets.compare_digest(given, app.token)

        # --- GET
        def do_GET(self) -> None:
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path.startswith("/static/"):  # CSS/JS はトークン不要 (中身に秘密は無い)
                return self._static(u.path[len("/static/"):])
            if not self._authorized(q):
                return self._send(403, "アクセスが拒否されました。アプリが表示したURLから開いてください。".encode(),
                                  "text/plain; charset=utf-8")
            if u.path == "/":
                html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
                html = html.replace("__TOKEN__", app.token).replace("__VERSION__", __version__)
                return self._send(200, html.encode(), "text/html; charset=utf-8")
            if u.path == "/api/defaults":
                return self._json({"folders": default_folders(), "history": str(HISTORY_FILE),
                                   "pending": str(pending_dir())})
            if u.path == "/api/status":
                return self._json(app.scanner.status())
            if u.path == "/thumb":
                f = THUMB_DIR / f"{q.get('id', [''])[0]}.jpg"
                if f.parent == THUMB_DIR and f.is_file():
                    return self._send(200, f.read_bytes(), "image/jpeg")
            self._send(404, b"not found", "text/plain")

        def _static(self, name: str) -> None:
            f = (STATIC_DIR / name).resolve()
            if f.parent != STATIC_DIR.resolve() or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = STATIC_TYPES.get(f.suffix, "application/octet-stream")
            self._send(200, f.read_bytes(), ctype)

        # --- POST
        def do_POST(self) -> None:
            u = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True
                return self._json({"error": "too large"}, 413)
            raw = self.rfile.read(length)  # 拒否する場合も先に読み切る (読まずに返すと接続がリセットされる)
            if not self._authorized({}):
                return self._json({"error": "forbidden"}, 403)
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad request"}, 400)

            if u.path == "/api/scan":
                folders = [f for f in body.get("folders", []) if isinstance(f, str) and Path(f).is_dir()]
                if not folders:
                    return self._json({"error": "存在するフォルダが指定されていません"}, 400)
                ok = app.scanner.start(folders, Thresholds(check_blur=bool(body.get("blur", True))))
                return self._json({"ok": ok})
            if u.path == "/api/open":
                item = app.scanner.get(str(body.get("id", "")))
                if item is None or not Path(item["path"]).exists():
                    return self._json({"error": "ファイルが見つかりません"}, 404)
                if body.get("mode") == "folder":
                    subprocess.Popen(["explorer", "/select,", item["path"]])
                else:
                    os.startfile(item["path"])  # 既定の動画プレーヤーで再生
                return self._json({"ok": True})
            if u.path == "/api/move":
                # 許可しているのは「削除予定フォルダの作成」と「そこへの移動」だけ。削除の API は持たない
                ids = [str(i) for i in body.get("ids", [])]
                if not ids:
                    return self._json({"error": "動画が選択されていません"}, 400)
                return self._json(app.scanner.move_to_pending(ids))
            if u.path == "/api/open-pending":
                folder = pending_dir()
                if not folder.is_dir():
                    return self._json({"error": "削除予定フォルダはまだありません"}, 404)
                subprocess.Popen(["explorer", str(folder)])
                return self._json({"ok": True})
            if u.path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._json({"error": "not found"}, 404)

    return Handler


def create_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))  # 自分のPCからだけアクセス可能


def main() -> None:
    if sys.platform != "win32":
        sys.exit("このアプリは Windows 専用です。")
    try:
        ffmpeg.ffmpeg(), ffmpeg.ffprobe()
    except ffmpeg.FFmpegNotFound as e:
        sys.exit(f"[エラー] {e}")
    app = App()
    server = create_server(app)
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={app.token}"
    print("ストレージ整理アプリを起動しました。ブラウザが開かない場合は次のURLを開いてください:")
    print(f"  {url}")
    print("終了するには画面右上の「アプリを終了」か、この画面で Ctrl+C を押してください。")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    print("終了しました。")
