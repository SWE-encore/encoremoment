"""Review and delete saved audience clips, separately from the controlled content player (R7).

A clip file keeps hidden editing margins (5 s before and after, ``HANDLE_MS``
in recorder.py); its index row's ``trim_ms`` is the chosen part between them,
in ms of the file. Playback covers the chosen part and pauses on its last frame.
``move_border`` and ``save_range`` change the chosen part (the borders under the
player window's progress bar, ui/trim_bar.py); the status and gaps stay as
recorded for the original part.
"""
import json
from pathlib import Path
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QLabel, QListWidget, QMessageBox, QPushButton, QVBoxLayout, QWidget
from .deletion import DeletionManager
from .model import ClipStatus, effective_label, gap_detail, load_index, playable_path
from .storage import atomic_index, index_lock

MIN_CLIP_MS = 1_000    # the borders stay at least this far apart
MAX_CLIP_MS = 60_000   # no clip over 60 s (R10, as the clip plan)
LAST_FRAME_MS = 50     # where a clip that played to the end of its file rests
END_SLACK_MS = 100     # Play this close to the end border starts again from the start border
PLAYING = QMediaPlayer.PlaybackState.PlayingState


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
        # Shown under the buttons, unless the player window takes it for its big
        # video area (ui/clip_stage.py).
        self.video = QVideoWidget()
        self.video.setMinimumSize(240, 135)
        self.now_playing = ""
        self.audio = QAudioOutput(self)
        # Qt 6.8's FFmpeg backend rebuilds its audio renderer on every seek. Its
        # own thread destroys the old one, calling this object's disconnectNotify
        # while it holds a Qt connection lock. The first such call makes PySide
        # take the GIL to look for a Python override, while the main thread holds
        # the GIL in setPosition, waiting for that lock to connect the new
        # renderer: a deadlock (dragging a border froze the tests). One disconnect
        # here, on the main thread, does that lookup once; PySide caches it.
        warm_up = QTimer()
        self.audio.mutedChanged.connect(warm_up.stop)
        self.audio.mutedChanged.disconnect(warm_up.stop)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self._play_range = ()     # (start, end) ms of the file: the chosen part, if the index has it
        self._start_on_load = None
        self.playing_id = None    # clip_id of the clip in the player, if it is in the index
        self.player.mediaStatusChanged.connect(self._loaded)
        self.player.positionChanged.connect(self._limit_playback)
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
        if clip is not None and clip.status is ClipStatus.PARTIAL and clip.gaps:
            text += f"\n{gap_detail(clip.gaps)}"
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
        target = path.resolve()
        selected = next((c for c in self.clips if playable_path(c, self.index_path.parent) == target), None)
        trim = selected.trim_ms if selected else ()
        self._play_range = tuple(int(t) for t in trim) if len(trim) == 2 and trim[0] < trim[1] else ()
        self.playing_id = selected.clip_id if selected else None
        self.now_playing = (" · ".join(filter(None, (selected.event_type, selected.timecode, selected.title)))
                            + f" — {self._status(selected)}" if selected else path.name)
        self.player.setSource(QUrl.fromLocalFile(str(target)))
        self._start_on_load = self._play_range[0] if self._play_range else None
        if self._play_range:
            self.player.setPosition(self._play_range[0])
        self.player.play()

    def _loaded(self, state):
        if state == QMediaPlayer.MediaStatus.LoadedMedia and self._start_on_load is not None:
            # Once: LoadedMedia also follows every seek.
            self.player.setPosition(self._start_on_load)
            self._start_on_load = None
        elif state == QMediaPlayer.MediaStatus.EndOfMedia and self.player.duration() > 0:
            # The player stops at the end of the file; pause on the last frame
            # instead (pause() rewinds a stopped player, so seek after it).
            self.player.pause()
            self.player.setPosition(max(0, self.player.duration() - LAST_FRAME_MS))

    def _limit_playback(self, position):
        chosen = self.play_range()
        if self.player.playbackState() == PLAYING and chosen and position >= chosen[1]:
            self.player.pause()  # on the end border's frame; the margin after it stays hidden

    def play_range(self):
        """The chosen part of the file in the player, ``(start, end)`` ms, or None
        before its length is known. Without ``trim_ms``, the whole file."""
        duration = self.player.duration()
        if self._play_range:
            start, end = self._play_range
            return (start, min(end, duration)) if duration > 0 else (start, end)
        return (0, duration) if duration > 0 else None

    def toggle(self):
        """Play or pause the clip. Play outside the chosen part, or at its end,
        starts again from the start border."""
        if self.player.playbackState() == PLAYING:
            self.player.pause()
            return
        chosen = self.play_range()
        if chosen and not chosen[0] <= self.player.position() < chosen[1] - END_SLACK_MS:
            self.player.setPosition(chosen[0])
        self.player.play()

    def seek(self, ms):
        """Show the clip at ``ms`` of its file, margins included."""
        duration = self.player.duration()
        self.player.setPosition(max(0, min(int(ms), duration) if duration > 0 else int(ms)))

    def move_border(self, border, ms):
        """Move the chosen part's ``"start"`` or ``"end"`` border to ``ms`` of the
        file and show that frame, paused. The part stays within the file, at least
        ``MIN_CLIP_MS`` and at most ``MAX_CLIP_MS`` long. Returns the new part."""
        chosen, duration = self.play_range(), self.player.duration()
        if chosen is None or duration <= 0:
            return None
        start, end = chosen
        if border == "start":
            start = shown = max(0, end - MAX_CLIP_MS, min(int(ms), end - MIN_CLIP_MS))
        else:
            end = shown = min(duration, start + MAX_CLIP_MS, max(int(ms), start + MIN_CLIP_MS))
        self._play_range = (start, end)
        if self.player.playbackState() == PLAYING:
            self.player.pause()
        self.player.setPosition(shown)
        return self._play_range

    def save_range(self):
        """Keep the chosen part in the clip's index row (``trim_ms``) for the next
        play. Returns False if the clip is not in the index or is being deleted."""
        if self.playing_id is None or not self._play_range:
            return False
        try:
            with index_lock(self.index_path):
                data = json.loads(self.index_path.read_text())
                row = next((r for r in data["clips"] if r.get("clip_id") == self.playing_id), None)
                if row is None or row.get("status") in (ClipStatus.DELETION_PENDING.value,
                                                        ClipStatus.DELETED.value):
                    return False
                row["trim_ms"] = list(self._play_range)
                atomic_index(self.index_path, data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.detail.setText(f"Cannot save the clip's new length: {exc}")
            return False
        self._rebuild()  # the list now has the new part; playback goes on
        return True

    def play(self):
        clip = self.selected()
        path = playable_path(clip, self.index_path.parent) if clip else None
        if path is None:
            self.reload()
            return
        self.play_file(path)

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
