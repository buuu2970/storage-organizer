"""ブラウザ版（webapp.py）のテスト。

実際のユーザーの録画は使わない。一時フォルダの合成ダミーファイルだけを使う。
ごみ箱への移動は偽の移動処理（名前を変えるだけ）で確かめる。サーバーは 127.0.0.1 の空いている番号で動かす。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from storage_organizer import webapp  # noqa: E402
from storage_organizer.store import Store  # noqa: E402


class WebAppTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.videos = self.dir / "videos"
        self.videos.mkdir()
        self.db = Store(self.dir / "data")
        self.moved = []
        self.app = webapp.App(self.db, backend=self.backend)
        self.server = webapp.make_server(self.app, 0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.a = self.add("a.mp4", b"same" * 1000)
        self.b = self.add("b.mp4", b"same" * 1000)
        self.c = self.add("c.mp4", b"other" * 300)
        sha = hashlib.sha256(b"same" * 1000).hexdigest()
        for m in (self.a, self.b):
            self.db.update_video(m, sha256=sha, light_state="完了")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.db.close()
        self._tmp.cleanup()

    def backend(self, path):
        self.moved.append(path)
        os.rename(path, path + ".trashed-by-test")

    def add(self, name, data):
        p = self.videos / name
        p.write_bytes(data)
        st = p.stat()
        self.db.upsert_media([(str(p), name, str(self.videos), st.st_size, st.st_mtime, ".mp4", "video", False)], 1,
                             str(self.videos))
        return self.db.id_of(str(p))

    def request(self, path, body=None, token=True, headers=None):
        h = dict(headers or {})
        if token:
            h["X-RecSort-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                raw = res.read()
                return res.status, raw, res.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def json(self, path, body=None, **kw):
        status, raw, _ = self.request(path, body, **kw)
        return status, json.loads(raw)

    # --- 安全 -----------------------------------------------------------------
    def test_api_requires_token(self):
        status, data = self.json("/api/state", token=False)
        self.assertEqual(status, 403)
        status, _ = self.json("/api/decide", {"ids": [self.c], "decision": "削除候補"}, token=False)
        self.assertEqual(status, 403)
        self.assertIsNone(self.db.video(self.c)["decision"])  # 合言葉のない要求では何も変えない

    def test_other_host_names_are_refused(self):
        # ほかのサイトの名前でこのサーバーに届いた要求（DNS リバインディング）は断る
        status, _ = self.json("/api/state", headers={"Host": "evil.example:80"})
        self.assertEqual(status, 403)

    def test_index_carries_token_and_static_paths_stay_inside(self):
        status, raw, headers = self.request("/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.app.token.encode(), raw)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        for bad in ("/../webapp.py", "/%2e%2e/webapp.py", "/..%5cwebapp.py"):
            status, _, _ = self.request(bad, token=False)
            self.assertEqual(status, 404, bad)

    def test_media_only_for_registered_videos_and_supports_range(self):
        status, raw, headers = self.request(f"/api/media/{self.c}", headers={"Range": "bytes=0-9"})
        self.assertEqual(status, 206)
        self.assertEqual(raw, (b"other" * 300)[:10])
        self.assertEqual(headers["Content-Range"], f"bytes 0-9/{5 * 300}")
        status, _, _ = self.request("/api/media/99999")
        self.assertEqual(status, 404)
        status, _, _ = self.request(f"/api/media/{self.c}", token=False)
        self.assertEqual(status, 403)

    # --- 画面の情報 -------------------------------------------------------------
    def test_state_files_and_plan(self):
        _, s = self.json("/api/state")
        self.assertEqual(s["videos"], 3)
        self.assertEqual(s["plan_count"], 1)  # 重複の片方だけ（1本は必ず残す）
        _, f = self.json("/api/files")
        self.assertEqual({it["name"] for it in f["items"]}, {"a.mp4", "b.mp4", "c.mp4"})
        _, p = self.json("/api/plan")
        self.assertEqual(len(p["items"]), 1)
        item = p["items"][0]
        self.assertEqual(item["kind"], "duplicate")
        self.assertIn(item["keep_id"], (self.a, self.b))
        self.assertNotEqual(item["id"], item["keep_id"])
        self.assertIn(str(item["keep_id"]), p["rows"])
        _, g = self.json(f"/api/group/{self.a}")
        self.assertEqual(g["kind"], "duplicate")
        self.assertEqual({it["id"] for it in g["items"]}, {self.a, self.b})

    # --- 判断 -----------------------------------------------------------------
    def test_decide_and_protected_is_never_a_candidate(self):
        self.json("/api/protect", {"id": self.c, "value": True})
        self.json("/api/decide", {"ids": [self.a, self.c], "decision": "削除候補"})
        self.assertEqual(self.db.video(self.a)["decision"], "削除候補")
        self.assertIsNone(self.db.video(self.c)["decision"])
        status, _ = self.json("/api/decide", {"ids": [self.a], "decision": "消す"})
        self.assertEqual(status, 200)
        self.assertEqual(self.db.video(self.a)["decision"], "削除候補")  # 知らない値は記録しない

    def test_keep_one_marks_others(self):
        _, r = self.json("/api/keep", {"keep": self.a, "ids": [self.b]})
        self.assertEqual(r["count"], 1)
        self.assertEqual(self.db.video(self.a)["decision"], "残す")
        self.assertEqual(self.db.video(self.b)["decision"], "削除候補")

    # --- ごみ箱 ---------------------------------------------------------------
    def wait_trash(self):
        for _ in range(100):
            _, st = self.json("/api/trash")
            if not st["running"]:
                return st
            time.sleep(0.05)
        self.fail("ごみ箱への移動が終わりません")

    def test_trash_requires_final_confirmation(self):
        self.json("/api/decide", {"ids": [self.b], "decision": "削除候補"})
        status, _ = self.json("/api/trash", {"ids": [self.b]})
        self.assertEqual(status, 400)
        self.assertEqual(self.moved, [])

    def test_trash_moves_checked_files_and_keeps_one_copy(self):
        self.json("/api/decide", {"ids": [self.a, self.b, self.c], "decision": "削除候補"})
        _, chosen = self.json("/api/chosen")
        self.assertEqual(len(chosen["items"]), 3)
        with mock.patch.object(os, "remove", side_effect=AssertionError("完全削除")):
            _, r = self.json("/api/trash", {"ids": [self.a, self.b, self.c], "confirm": True,
                                            "generation": chosen["generation"]})
            self.assertTrue(r["ok"])
            st = self.wait_trash()
        statuses = {x["id"]: x["status"] for x in st["results"]}
        self.assertEqual(statuses[self.c], "moved")
        # 完全に同じ録画を両方選んでも、最後の1本は移動しない
        self.assertEqual(sorted([statuses[self.a], statuses[self.b]]), ["moved", "skipped"])
        self.assertTrue((self.videos / "a.mp4").exists() or (self.videos / "b.mp4").exists())

    def test_trash_stops_if_scan_result_changed(self):
        self.json("/api/decide", {"ids": [self.c], "decision": "削除候補"})
        _, chosen = self.json("/api/chosen")
        self.add("d.mp4", b"new")  # 確認した後に調査結果が変わった
        self.json("/api/trash", {"ids": [self.c], "confirm": True, "generation": chosen["generation"]})
        st = self.wait_trash()
        self.assertEqual([x["status"] for x in st["results"]], ["skipped"])
        self.assertEqual(self.moved, [])

    def test_settings_reject_relative_paths(self):
        _, r = self.json("/api/settings", {"extra_locations": ["relative\\path"]})
        self.assertFalse(r["ok"])
        _, s = self.json("/api/settings")
        self.assertEqual(s["extra_locations"], [])
        path = str(self.dir)
        _, s = self.json("/api/settings", {"extra_locations": [path], "auto_scan": False})
        self.assertEqual(s["extra_locations"], [path])
        self.assertFalse(s["auto_scan"])


if __name__ == "__main__":
    unittest.main()
