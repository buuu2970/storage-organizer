"""同じ保存先を使うアプリの二重起動を防ぐ（OS のファイルロック）。

ロックはプロセスが終わると OS が解放するので、異常終了後も再起動できる。
保存データ（settings.json など）を読み書きする前に取得する。
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(Exception):
    """同じ保存先を使う RecSort がすでに動いている。"""


class InstanceLock:
    def __init__(self, data_dir: Path | str) -> None:
        self.path = Path(data_dir) / "instance.lock"
        self._fh = None

    def acquire(self, wait: float = 2.0) -> "InstanceLock":
        """ロックを取る。取れなければ wait 秒まで待ってから AlreadyRunning。

        Windows では、異常終了したプロセスのロックが OS に解放されるまで少し時間がかかるため、短く待ち直す。
        """
        import time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait
        while True:
            fh = open(self.path, "a+")
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                fh.close()
                if time.monotonic() >= deadline:
                    raise AlreadyRunning(str(e)) from e
                time.sleep(0.1)
                continue
            self._fh = fh
            return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None
