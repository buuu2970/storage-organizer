"""ffmpeg でテスト用動画を生成して、判定・画面API を確かめる結合テスト (ffmpeg が無ければスキップ)"""

import json
import shutil
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from threading import Thread
from unittest import mock

from storage_organizer import ffmpeg, video_cleanup
from storage_organizer.video_analyzer import (CAT_AUDIO, CAT_LONG, CAT_SHORT, CAT_VIDEO, Thresholds, analyse)
from storage_organizer.web import server as web

try:
    FF = ffmpeg.ffmpeg()
    ffmpeg.ffprobe()
except ffmpeg.FFmpegNotFound:
    FF = None

SINE = ["-f", "lavfi", "-i", "sine=f=440:d={d}"]
SAMPLES = {
    # 名前: (映像サイズ, 長さ, 音声入力, 追加オプション)
    "good":     ("1920x1080", 30, SINE, []),
    "blurry":   ("1920x1080", 30, SINE, ["-vf", "boxblur=6"]),
    "upscaled": ("1920x1080", 30, SINE, ["-vf", "scale=320:180,scale=1920:1080"]),
    "lowres":   ("640x360", 30, SINE, []),
    "short":    ("1280x720", 5, SINE, []),
    "quiet":    ("1280x720", 30, SINE, ["-af", "volume=-50dB"]),
    "clipped":  ("1280x720", 30, SINE, ["-af", "volume=30dB"]),
    "noaudio":  ("1280x720", 30, None, []),
    "silent":   ("1280x720", 30, ["-f", "lavfi", "-i", "anullsrc=r=48000"], []),
    "lowaudio": ("1280x720", 30, SINE, ["-ar", "8000", "-b:a", "16k"]),
}


def make_samples(folder: Path) -> None:
    for name, (size, d, audio, extra) in SAMPLES.items():
        cmd = [FF, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d={d}"]
        if audio:
            cmd += [a.format(d=d) for a in audio]
        cmd += extra + ["-c:v", "libx264", "-preset", "ultrafast"] + (["-c:a", "aac", "-shortest"] if audio else [])
        ffmpeg.run(cmd + [str(folder / f"{name}.mp4")])


@unittest.skipIf(FF is None, "ffmpeg が無いためスキップ")
class TestWithFFmpeg(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.videos = cls.root / "videos"
        (cls.videos / "sub").mkdir(parents=True)
        make_samples(cls.videos)
        shutil.copy2(cls.videos / "good.mp4", cls.videos / "sub" / "good_copy.mp4")
        # サムネイル・移動履歴・削除予定フォルダはテスト用の一時フォルダへ (本物のデスクトップは触らない)
        app_dir = cls.root / "app"
        cls.pending = cls.root / "desktop" / "削除予定"
        cls.patches = [
            mock.patch.object(video_cleanup, "APP_DIR", app_dir),
            mock.patch.object(video_cleanup, "THUMB_DIR", app_dir / "thumbs"),
            mock.patch.object(video_cleanup, "HISTORY_FILE", app_dir / "history.csv"),
            mock.patch.object(video_cleanup, "pending_dir", lambda: cls.pending),
            mock.patch.object(web, "THUMB_DIR", app_dir / "thumbs"),
            mock.patch.object(web, "pending_dir", lambda: cls.pending),
        ]
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    # --- 判定
    def check(self, name, expected_cats, reason_word=None, th=None):
        r = analyse(self.videos / f"{name}.mp4", th or Thresholds())
        self.assertEqual(sorted(r.categories), sorted(expected_cats), f"{name}: {r.reasons}")
        if reason_word:
            self.assertTrue(any(reason_word in x for x in r.reasons), f"{name}: {r.reasons}")

    def test_good(self):
        self.check("good", [])

    def test_video_quality(self):
        self.check("blurry", [CAT_VIDEO], "ぼやけ")
        self.check("upscaled", [CAT_VIDEO], "ぼやけ")
        self.check("lowres", [CAT_VIDEO], "低解像度")

    def test_length(self):
        self.check("short", [CAT_SHORT])
        self.check("good", [CAT_LONG], th=Thresholds(max_sec=25))

    def test_audio(self):
        self.check("quiet", [CAT_AUDIO], "無音")
        self.check("silent", [CAT_AUDIO], "無音")
        self.check("clipped", [CAT_AUDIO], "音割れ")
        self.check("noaudio", [CAT_AUDIO], "音声トラックなし")
        self.check("lowaudio", [CAT_AUDIO], "低サンプリングレート")

    # --- 画面API
    def test_web_api(self):
        app = web.App()
        srv = web.create_server(app)
        Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def call(path, body=None, token=app.token):
            req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"X-Token": token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as res:
                    return res.status, res.read()
            except urllib.error.HTTPError as e:
                with e:
                    return e.code, e.read()

        try:
            # 合言葉が無いと拒否
            self.assertEqual(call("/", token="wrong")[0], 403)
            self.assertEqual(call("/api/move", {"ids": ["x"]}, token="wrong")[0], 403)
            # 削除系の API は存在しない (削除は利用者の操作に限る)
            self.assertEqual(call("/api/delete", {"ids": ["x"]})[0], 404)
            # 画面と静的ファイル
            code, html = call("/")
            self.assertEqual(code, 200)
            self.assertIn(app.token.encode(), html)
            self.assertEqual(call("/static/app.js")[0], 200)
            self.assertEqual(call("/static/../server.py")[0], 404)

            # スキャン
            self.assertEqual(call("/api/scan", {"folders": [str(self.videos)], "blur": False})[0], 200)
            app.scanner.wait(timeout=300)
            items = json.loads(call("/api/status")[1])["items"]
            self.assertEqual(len(items), len(SAMPLES) + 1)
            by_name = {i["name"]: i for i in items}
            self.assertEqual(by_name["good_copy.mp4"]["priority"], 3)
            self.assertIn("重複", by_name["good_copy.mp4"]["categories"])
            self.assertEqual(by_name["good.mp4"]["priority"], 0)
            self.assertEqual(by_name["short.mp4"]["priority"], 3)
            self.assertTrue(by_name["good.mp4"]["thumb"])
            self.assertEqual(call(f"/thumb?id={by_name['good.mp4']['id']}&t={app.token}")[0], 200)

            # 削除予定へ移動: スキャン結果に無い ID は拒否、ある ID だけ移動 (フォルダは自動作成)
            self.assertFalse(self.pending.exists())
            src = Path(by_name["good_copy.mp4"]["path"])
            res = json.loads(call("/api/move", {"ids": ["unknown", by_name["good_copy.mp4"]["id"]]})[1])
            self.assertEqual([r["ok"] for r in res["results"]], [False, True])
            self.assertEqual(Path(res["folder"]), self.pending)
            self.assertFalse(src.exists())
            self.assertTrue((self.pending / "good_copy.mp4").is_file())
            self.assertIn("good_copy.mp4", video_cleanup.HISTORY_FILE.read_text(encoding="utf-8-sig"))
            items_after = json.loads(call("/api/status")[1])["items"]
            self.assertNotIn("good_copy.mp4", [i["name"] for i in items_after])
            self.assertEqual(call("/api/move", {"ids": []})[0], 400)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
