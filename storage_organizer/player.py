"""動画のプレビュー（Qt Multimedia）。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget


class VideoPlayer(QWidget):
    """Qt Multimedia による再生・停止・シーク。

    映像の場所が白いままにならないよう、黒い背景の上に状態（読み込み中・再生できます・再生できません・
    利用できません）とサムネイルを表示し、再生中だけ映像に切り替える。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QStackedWidget
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #b00020;")
        self.error_label.setWordWrap(True)
        self.screen = QStackedWidget()
        self.screen.setMinimumSize(240, 135)
        self.status = QLabel()  # 再生前・読み込み中・失敗のときに映像の代わりに見せる
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("background: #1f1f1f; color: #f0f0f0; border-radius: 6px; padding: 8px;")
        self._poster: QPixmap | None = None
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
        except ImportError as e:  # Qt Multimedia が使えない環境でも他の機能は動かす
            self.player = None
            self.screen.addWidget(self.status)
            self._show_status("動画プレビューを利用できません（Qt Multimedia がありません）。"
                              "「既定のアプリで開く」で確認できます。")
            self.error_label.setText(f"詳細: {e}")
            layout.addWidget(self.screen, 1)
            layout.addWidget(self.error_label)
            return
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setStyleSheet("background: #000000;")
        self.player.setVideoOutput(self.video)
        self.screen.addWidget(self.status)
        self.screen.addWidget(self.video)
        self.play_button = QPushButton("再生")
        self.stop_button = QPushButton("停止")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.time_label = QLabel("0:00 / 0:00")
        controls = QHBoxLayout()
        for w in (self.play_button, self.stop_button, self.slider, self.time_label):
            controls.addWidget(w)
        layout.addWidget(self.screen, 1)
        layout.addLayout(controls)
        layout.addWidget(self.error_label)
        self.play_button.clicked.connect(self.toggle)
        self.stop_button.clicked.connect(self.player.stop)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(lambda d: self.slider.setRange(0, d))
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.mediaStatusChanged.connect(self._on_media_status)
        self.player.errorOccurred.connect(self._on_error)
        self._failed = False
        self._show_status("録画を選ぶと、ここでプレビューできます。")

    @property
    def state_text(self) -> str:
        return getattr(self, "_status_text", "") if self.screen.currentWidget() is self.status else "再生中"

    def _show_status(self, text: str) -> None:
        self._status_text = text
        if self._poster is not None and not self._poster.isNull() and not getattr(self, "_failed", False):
            # 再生前はサムネイルを見せ、下部に状態の文字を重ねる（パネルの幅に収める）
            from PySide6.QtGui import QColor, QPainter
            width = max(min(self.status.width() - 16, 480), 200)
            pm = self._poster.scaled(width, int(width * 9 / 16), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            painter = QPainter(pm)
            band = pm.rect().adjusted(0, pm.height() - 28, 0, 0)
            painter.fillRect(band, QColor(0, 0, 0, 170))
            painter.setPen(QColor("#ffffff"))
            painter.drawText(band, Qt.AlignmentFlag.AlignCenter, text)
            painter.end()
            self.status.setPixmap(pm)
            self.status.setToolTip(text)
            self.status.setAccessibleName(text)
        else:
            self.status.setPixmap(QPixmap())
            self.status.setText(text)
        self.screen.setCurrentWidget(self.status)

    def set_poster(self, path: str | None) -> None:
        pm = QPixmap(path) if path and os.path.exists(path) else None
        self._poster = pm if pm and not pm.isNull() else None

    def set_source(self, path: str | None, poster: str | None = None) -> None:
        if self.player is None:
            return
        self.player.stop()
        self.error_label.clear()
        self._failed = False
        self.set_poster(poster)
        if path:
            self._show_status("読み込み中…")
        else:
            self._poster = None
            self._show_status("録画を選ぶと、ここでプレビューできます。")
        self.player.setSource(QUrl.fromLocalFile(path) if path else QUrl())

    def toggle(self) -> None:
        if self.player.playbackState() == self.player.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()

    def _on_position(self, pos: int) -> None:
        if not self.slider.isSliderDown():
            self.slider.setValue(pos)
        fmt = lambda ms: f"{ms // 60000}:{ms // 1000 % 60:02d}"  # noqa: E731
        self.time_label.setText(f"{fmt(pos)} / {fmt(self.player.duration())}")

    def _on_media_status(self, status) -> None:
        S = self.player.MediaStatus
        if self._failed:
            return
        if status == S.LoadingMedia:
            self._show_status("読み込み中…")
        elif status in (S.LoadedMedia, S.BufferedMedia) and \
                self.player.playbackState() == self.player.PlaybackState.StoppedState:
            self._show_status("再生ボタンで再生できます。")
        elif status == S.EndOfMedia:
            self._show_status("最後まで再生しました。もう一度見るときは「再生」を押してください。")
        elif status == S.InvalidMedia:
            self._on_error(None, "")

    def _on_state(self, state) -> None:
        playing = state == self.player.PlaybackState.PlayingState
        self.play_button.setText("一時停止" if playing else "再生")
        if playing or state == self.player.PlaybackState.PausedState:
            self.screen.setCurrentWidget(self.video)
        elif not self._failed and self.player.mediaStatus() != self.player.MediaStatus.EndOfMedia:
            self._show_status("再生ボタンで再生できます。")

    def _on_error(self, error, message: str = "") -> None:
        self._failed = True
        self._show_status("再生できません。形式・コーデックが未対応か、ファイルを読み込めません。"
                          "ファイル情報と理由は下で確認できます。「既定のアプリで開く」も試せます。")
        self.error_label.setText(f"再生できません（詳細: {message or '不明なエラー'}）")
