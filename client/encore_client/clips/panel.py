"""Review saved audience clips separately from the controlled content player."""
from pathlib import Path
from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QVBoxLayout, QWidget
from .model import effective_label, load_index, playable_path


class ClipPanel(QWidget):
    def __init__(self, index_path: Path, parent=None):
        super().__init__(parent)
        self.index_path = index_path
        self.clips = []
        self.list = QListWidget()
        self.detail = QLabel("Select a saved clip")
        self.detail.setWordWrap(True)
        self.play_button = QPushButton("Play saved clip")
        self.stop_button = QPushButton("Stop clip")
        self.refresh_button = QPushButton("Refresh saved clips")
        self.video = QVideoWidget()
        self.video.setMinimumSize(240, 135)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(lambda _error, message: self.detail.setText(message))
        self.list.currentRowChanged.connect(self.select)
        self.play_button.clicked.connect(self.play)
        self.stop_button.clicked.connect(self.player.stop)
        self.refresh_button.clicked.connect(self.reload)
        layout = QVBoxLayout(self)
        label = QLabel("Saved audience clips (separate from the live event list)")
        label.setToolTip(str(index_path))
        label.setWordWrap(True)
        layout.addWidget(label)
        for widget in (self.list, self.detail, self.play_button, self.stop_button, self.refresh_button, self.video):
            layout.addWidget(widget)
        self.reload()

    def reload(self):
        self.player.stop()
        self.list.clear()
        try:
            self.clips = sorted(load_index(self.index_path),
                                key=lambda c: (-c.intensity, c.timecode, c.clip_id))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.clips = []
            self.select(-1)
            self.detail.setText(f"Cannot load clip index: {exc}")
            return
        for clip in self.clips:
            self.list.addItem(f"{clip.event_type} · {clip.intensity:.2f} · {clip.timecode}\n"
                              f"{effective_label(clip, self.index_path.parent)}")
        self.select(-1)

    def selected(self):
        row = self.list.currentRow()
        return self.clips[row] if 0 <= row < len(self.clips) else None

    def select(self, _row):
        clip = self.selected()
        path = playable_path(clip, self.index_path.parent) if clip else None
        self.play_button.setEnabled(path is not None)
        self.detail.setText(f"{clip.title}\n{effective_label(clip, self.index_path.parent)}" if clip
                            else "Select a saved clip" if self.clips else "No saved clips in this index.")

    def play(self):
        clip = self.selected()
        path = playable_path(clip, self.index_path.parent) if clip else None
        if path is None:
            self.reload()
            return
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
