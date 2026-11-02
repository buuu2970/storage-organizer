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


class TaskWorker(QObject):
    """解析・重複検出・再学習・圧縮などを別スレッドで実行する汎用ワーカー。

    fn(cancel_event, report) を呼び、report(dict) で途中経過を GUI に送る。
    """

    progress = Signal(object)
    task_finished = Signal(object)
    task_failed = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn
        self._cancel_event = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(self._cancel_event, self.progress.emit)
        except Exception as error:
            self.task_failed.emit(str(error) or type(error).__name__)
            return
        self.task_finished.emit(result)

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()
