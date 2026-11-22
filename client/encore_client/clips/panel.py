"""Review and delete saved audience clips, separately from the controlled content player (R7)."""
from pathlib import Path
from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QListWidget, QMessageBox, QPushButton, QVBoxLayout, QWidget
from .deletion import DeletionManager
from .model import ClipStatus, effective_label, load_index, playable_path


class ClipPanel(QWidget):
    def __init__(self, index_path: Path, parent=None, *, deletions: DeletionManager | None = None):
        super().__init__(parent)
        self.index_path = index_path
        self.deletions = deletions
        self.clips = []
        self._stamp = None
        self.list = QListWidget()
        self.detail = QLabel("Select a saved clip")
        self.detail.setWordWrap(True)
        self.play_button = QPushButton("Play saved clip")
        self.stop_button = QPushButton("Stop clip")
        self.delete_button = QPushButton("Delete clip…")
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
        self.delete_button.clicked.connect(lambda: self.delete_selected())
        self.refresh_button.clicked.connect(self.reload)
        self.delete_button.setVisible(deletions is not None)
        # Asks before deleting; tests replace it.
        self.confirm_delete = self._ask
        layout = QVBoxLayout(self)
        label = QLabel("Saved audience clips (separate from the live event list)")
        label.setToolTip(str(index_path))
        label.setWordWrap(True)
        layout.addWidget(label)
        for widget in (self.list, self.detail, self.play_button, self.stop_button,
                       self.delete_button, self.refresh_button, self.video):
            layout.addWidget(widget)
        self.reload()

    # ---- loading --------------------------------------------------------------------

    def _load(self):
        return self.deletions.load_clips() if self.deletions else load_index(self.index_path)

    def _current_stamp(self):
        try:
            st = self.index_path.stat()
            index = (st.st_mtime_ns, st.st_size)
        except OSError:
            index = None
        return index, self.deletions.version if self.deletions else 0

    def reload(self):
        """Reload from disk and stop playback (the Refresh button)."""
        self.player.stop()
        self._rebuild()

    def sync(self):
        """Call on a timer: rebuild when the index or a deletion changed, keeping playback."""
        if self._current_stamp() != self._stamp:
            self._rebuild()

    def _rebuild(self):
        selected = self.selected().clip_id if self.selected() else None
        self._stamp = self._current_stamp()
        self.list.blockSignals(True)
        self.list.clear()
        try:
            self.clips = sorted(self._load(), key=lambda c: (-c.intensity, c.timecode, c.clip_id))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.clips = []
            self.list.blockSignals(False)
            self.select(-1)
            self.detail.setText(f"Cannot load clip index: {exc}")
            return
        for clip in self.clips:
            self.list.addItem(f"{clip.event_type} · {clip.intensity:.2f} · {clip.timecode}\n"
                              f"{self._status(clip)}")
        row = next((i for i, c in enumerate(self.clips) if c.clip_id == selected), -1)
        self.list.setCurrentRow(row)
        self.list.blockSignals(False)
        self.select(row)

    def _status(self, clip):
        op = self.deletions.state(clip.clip_id) if self.deletions else None
        return op.status_text() if op else effective_label(clip, self.index_path.parent)

    # ---- selection and playback -----------------------------------------------------

    def selected(self):
        row = self.list.currentRow()
        return self.clips[row] if 0 <= row < len(self.clips) else None

    def select(self, _row):
        clip = self.selected()
        path = playable_path(clip, self.index_path.parent) if clip else None
        self.play_button.setEnabled(path is not None)
        self.delete_button.setEnabled(
            clip is not None and self.deletions is not None
            and clip.status not in (ClipStatus.DELETION_PENDING, ClipStatus.DELETED))
        text = (f"{clip.title}\n{self._status(clip)}" if clip
                else "Select a saved clip" if self.clips else "No saved clips in this index.")
        if self.deletions is not None and self.deletions.journal_error:
            text += f"\n⚠️ {self.deletions.journal_error}"
        self.detail.setText(text)

    def play_file(self, path):
        """Play a checked clip file chosen elsewhere (Tonight's Highlights)."""
        path = Path(path)
        if not (path.is_file() and path.stat().st_size > 0):
            self.detail.setText(f"Clip file is missing: {path.name}")
            return
        self.detail.setText(f"Playing {path.name}")
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    def play(self):
        clip = self.selected()
        path = playable_path(clip, self.index_path.parent) if clip else None
        if path is None:
            self.reload()
            return
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    # ---- deletion (R7) ----------------------------------------------------------------

    def _ask(self, clip) -> bool:
        answer = QMessageBox.question(
            self, "Delete clip",
            f"Delete this {clip.event_type} clip ({clip.timecode})?\n"
            "The video, its thumbnail and temporary files are removed. "
            "The reaction stays in the event list without a clip.")
        return answer == QMessageBox.StandardButton.Yes

    def delete_selected(self) -> bool:
        clip = self.selected()
        if clip is None or self.deletions is None or not self.confirm_delete(clip):
            return False
        self._release_if_playing(clip)
        try:
            self.deletions.delete(clip.clip_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._rebuild()
            self.detail.setText(f"Cannot delete clip: {exc}")
            return False
        self._rebuild()
        return True

    def _release_if_playing(self, clip):
        """Stop and release the player if it holds this clip's file (delete during playback)."""
        path = playable_path(clip, self.index_path.parent)
        source = self.player.source().toLocalFile()
        if path is not None and source and Path(source).resolve() == path:
            self.player.stop()
            self.player.setSource(QUrl())
