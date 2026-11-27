"""The Screen view: the screen as it was at each moment of a reaction clip.

It plays the clip's screen recording (clips/screen.py), silently, following the
clip player: the same moment while the clip plays, the same frame while it is
paused or scrubbed. Which screen time matches which moment of the clip comes
from the clip's ``screen`` segments (clips/screen_map.py).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QStackedWidget, QWidget

from ..clips.screen_map import segment_at

PLAYING = QMediaPlayer.PlaybackState.PlayingState
PAUSED = QMediaPlayer.PlaybackState.PausedState
DRIFT_MS = 250   # while both play, re-seek the screen only when it is this far off
STILL_MS = 40    # while the clip is paused, show the screen's frame this close

NOT_RECORDED = ("No screen recording for this clip: Screen recording was off when it was saved. "
                "Turn on Record screen under the camera control before the reactions.")
NOT_HERE = "The screen was not being recorded at this moment of the clip."
MISSING = "Screen recording file not found: {name}"


class ScreenView(QStackedWidget):
    """Follows ``clips`` (a ClipPanel) while ``active``."""

    def __init__(self, clips, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.clips = clips
        self.active = False
        self.video = QVideoWidget()
        self.message = QLabel()
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("background: black; color: #bbb; padding: 24px")
        self.addWidget(self.video)
        self.addWidget(self.message)
        self.player = QMediaPlayer(self)   # no audio output: the clip's own sound plays
        self.player.setVideoOutput(self.video)
        self.source: Path | None = None
        self._exists: dict[Path, bool] = {}
        self.player.mediaStatusChanged.connect(lambda _status: self.sync())
        clips.player.positionChanged.connect(lambda _position: self.sync())
        clips.player.playbackStateChanged.connect(lambda _state: self.sync())

    def start(self) -> None:
        self.active = True
        self._exists.clear()   # a screen file may have been restored meanwhile
        self.sync()

    def stop(self) -> None:
        self.active = False
        self.player.stop()

    def showing_screen(self) -> bool:
        return self.currentWidget() is self.video

    def _say(self, text: str) -> None:
        self.message.setText(text)
        self.setCurrentWidget(self.message)
        if self.player.playbackState() == PLAYING:
            self.player.pause()

    def sync(self) -> None:
        if not self.active:
            return
        clip = self.clips.playing_clip
        screen = clip.screen if clip is not None else None
        if not screen:
            self._say(NOT_RECORDED)
            return
        position = self.clips.player.position()
        segment = segment_at(screen, position)
        if segment is None:
            self._say(NOT_HERE)
            return
        path = self.clips.index_path.parent / segment.path
        if path not in self._exists:
            self._exists[path] = path.is_file()
        if not self._exists[path]:
            self._say(MISSING.format(name=path.name))
            return
        self.setCurrentWidget(self.video)
        if self.source != path:
            self.source = path
            self.player.setSource(QUrl.fromLocalFile(str(path)))
        target = segment.screen_at(position)
        duration = self.player.duration()
        at_end = duration > 0 and target >= duration - 100
        if at_end:  # a recording that ends before the clip rests on its last frame
            target = duration - 100
        state = self.player.playbackState()
        if self.clips.player.playbackState() == PLAYING and not at_end:
            if state != PLAYING:
                self.player.setPosition(target)
                self.player.play()
            elif abs(self.player.position() - target) > DRIFT_MS:
                self.player.setPosition(target)
        else:
            if state != PAUSED:
                self.player.pause()
            if abs(self.player.position() - target) > STILL_MS:
                self.player.setPosition(target)
