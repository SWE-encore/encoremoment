"""The player window's big video area: the film, or a saved reaction clip over it.

A clip played from Saved clips, Tonight's Highlights or the event list shows
here instead of in the small box under the clip list. The film is only covered,
not changed: its pipeline, its position and the anchors it sends to the server
stay as they are, so reaction timecodes are unaffected (R3). The workspace
pauses the film while a clip plays. While the clip shows, the window's controls
play, pause and seek the clip (PlayerWindow); at its end it pauses on the last
frame. Only "Back to film" or Stop clip (or the film playing again) shows the
film again, where it was paused.

A clip has two pictures: the audience camera, and the screen as it was at the
same moments, if Screen recording was on (clips/screen.py, ui/screen_view.py).
They show as Camera and Screen tabs, or side by side; the choice stays for the
next clip.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QStackedWidget, QTabBar,
                               QVBoxLayout, QWidget)

from .screen_view import ScreenView

STOPPED = QMediaPlayer.PlaybackState.StoppedState
CAMERA, SCREEN = 0, 1


class VideoStage(QStackedWidget):
    def __init__(self, film_view: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.film_view = film_view
        self.addWidget(film_view)
        self.clip_page = QWidget()
        self.clip_title = QLabel()
        self.view_tabs = QTabBar()
        self.view_tabs.addTab("Camera")
        self.view_tabs.addTab("Screen")
        self.view_tabs.setToolTip("Camera: the audience. Screen: the screen at the same moment.")
        self.side_by_side = QPushButton("Side by side")
        self.side_by_side.setCheckable(True)
        self.side_by_side.setToolTip("Show the camera and the screen next to each other")
        self.back_btn = QPushButton("■ Back to film")
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 4)
        bar.addWidget(self.clip_title, 1)
        bar.addWidget(self.view_tabs)
        bar.addWidget(self.side_by_side)
        bar.addWidget(self.back_btn)
        self.pictures = QHBoxLayout()
        self.pictures.setContentsMargins(0, 0, 0, 0)
        self.pictures.setSpacing(4)
        self._clip_layout = QVBoxLayout(self.clip_page)
        self._clip_layout.setContentsMargins(0, 0, 0, 0)
        self._clip_layout.addLayout(bar)
        self._clip_layout.addLayout(self.pictures, 1)
        self.addWidget(self.clip_page)
        self.clips = None
        self.screen = None
        self.view_tabs.currentChanged.connect(lambda _index: self._show_pictures())
        self.side_by_side.toggled.connect(lambda _on: self._show_pictures())

    def attach_clip_panel(self, clips) -> None:
        """Show ``clips``' (a ClipPanel's) video here instead of in the panel."""
        self.clips = clips
        clips.video.setMinimumSize(0, 0)
        self.screen = ScreenView(clips)
        self.pictures.addWidget(clips.video, 1)
        self.pictures.addWidget(self.screen, 1)
        self._show_pictures()
        clips.player.playbackStateChanged.connect(self._clip_state)
        self.back_btn.clicked.connect(self.show_film)
        clips.stop_button.clicked.connect(self.show_film)

    def showing_clip(self) -> bool:
        return self.currentWidget() is self.clip_page

    def view(self) -> str:
        """"side by side", "camera" or "screen"."""
        if self.side_by_side.isChecked():
            return "side by side"
        return "screen" if self.view_tabs.currentIndex() == SCREEN else "camera"

    def show_film(self) -> None:
        """Stop the clip and show the film where it was paused."""
        if self.clips is not None:
            self.clips.player.stop()
            self.screen.stop()
        self.setCurrentWidget(self.film_view)

    def _show_pictures(self) -> None:
        if self.clips is None:
            return
        view = self.view()
        side = view == "side by side"
        self.side_by_side.setText("Tabs" if side else "Side by side")
        self.side_by_side.setToolTip("Show the camera and the screen as tabs" if side
                                     else "Show the camera and the screen next to each other")
        self.view_tabs.setVisible(not side)
        self.clips.video.setVisible(view in ("camera", "side by side"))
        self.screen.setVisible(view in ("screen", "side by side"))

    def _clip_state(self, state) -> None:
        if state == STOPPED:
            # At the end of the file the player stops just before it reports
            # EndOfMedia, and ClipPanel then pauses it on the last frame, so look
            # again once that has happened.
            QTimer.singleShot(0, self._after_stop)
            return
        self.clip_title.setText(f"Reaction clip: {self.clips.now_playing}")
        if not self.showing_clip():
            self.setCurrentWidget(self.clip_page)
            self.screen.start()

    def _after_stop(self) -> None:
        if self.clips.player.playbackState() == STOPPED:
            self.screen.stop()
            self.setCurrentWidget(self.film_view)
