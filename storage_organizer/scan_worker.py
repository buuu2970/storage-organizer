"""スキャンを GUI とは別のスレッドで実行するためのワーカー。"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal, Slot

from .scanner import scan


class ScanWorker(QObject):
    """QThread に移して使う。結果はシグナルで GUI スレッドへ送る。"""

    batch_ready = Signal(object)  # list[FileRecord]
    progress = Signal(object)  # ScanProgress
    scan_finished = Signal(object)  # ScanResult
    scan_failed = Signal(str)

    def __init__(self, root: str) -> None:
        super().__init__()
        self._root = root
        self._cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = scan(
                self._root,
                self._cancel_event,
                on_batch=self.batch_ready.emit,
                on_progress=self.progress.emit,
            )
        except Exception as error:  # 想定外のエラーでもスレッドを確実に終わらせる
            self.scan_failed.emit(f"{type(error).__name__}: {error}")
            return
        self.scan_finished.emit(result)

    def cancel(self) -> None:
        """GUI スレッドから呼んでよい（threading.Event はスレッド安全）。"""
        self._cancel_event.set()
