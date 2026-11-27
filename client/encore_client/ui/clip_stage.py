"""The player window's big video area: the film, or a saved reaction clip over it.

A clip played from Saved clips, Tonight's Highlights or the event list shows
here instead of in the small box under the clip list. The film is only covered,
not changed: its pipeline, its position and the anchors it sends to the server
stay as they are, so reaction timecodes are unaffected (R3). The workspace
pauses the film while a clip plays. While the clip shows, the window's controls
play, pause and seek the clip (PlayerWindow); at its end it pauses on the last
frame. Only "Back to film" or Stop clip (or the film playing again) shows the
film again, where it was paused.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStackedWidget, QVBoxLayout, QWidget

STOPPED = QMediaPlayer.PlaybackState.StoppedState


class VideoStage(QStackedWidget):
    def __init__(self, film_view: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.film_view = film_view
        self.addWidget(film_view)
        self.clip_page = QWidget()
        self.clip_title = QLabel()
        self.back_btn = QPushButton("■ Back to film")
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)
        bar.addWidget(self.clip_title, 1)
        bar.addWidget(self.back_btn)
        self._clip_layout = QVBoxLayout(self.clip_page)
        self._clip_layout.setContentsMargins(0, 0, 0, 0)
        self._clip_layout.addLayout(bar)
        self.addWidget(self.clip_page)
        self.clips = None

    def attach_clip_panel(self, clips) -> None:
        """Show ``clips``' (a ClipPanel's) video here instead of in the panel."""
        self.clips = clips
        clips.video.setMinimumSize(0, 0)
        self._clip_layout.insertWidget(0, clips.video, 1)
        clips.player.playbackStateChanged.connect(self._clip_state)
        self.back_btn.clicked.connect(self.show_film)
        clips.stop_button.clicked.connect(self.show_film)

    def showing_clip(self) -> bool:
        return self.currentWidget() is self.clip_page

    def show_film(self) -> None:
        """Stop the clip and show the film where it was paused."""
        if self.clips is not None:
            self.clips.player.stop()
        self.setCurrentWidget(self.film_view)

    def _clip_state(self, state) -> None:
        if state == STOPPED:
            # At the end of the file the player stops just before it reports
            # EndOfMedia, and ClipPanel then pauses it on the last frame, so look
            # again once that has happened.
            QTimer.singleShot(0, self._after_stop)
            return
        self.clip_title.setText(f"Reaction clip: {self.clips.now_playing}")
        self.setCurrentWidget(self.clip_page)

    def _after_stop(self) -> None:
        if self.clips.player.playbackState() == STOPPED:
            self.setCurrentWidget(self.film_view)
