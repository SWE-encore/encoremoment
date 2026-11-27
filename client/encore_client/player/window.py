"""PySide6 window for the controlled player.

While a saved reaction clip shows over the film (ui/clip_stage.py), the
progress bar, the time, Play/Pause and Space/Left/Right control the clip
instead, and speed is off. The bar then covers the whole clip file: grey for the
hidden editing margins, yellow borders for the chosen part, which can be
dragged by their grips under the bar (ui/trim_bar.py) and is saved to the clip
index when let go. The film keeps its position and anchors meanwhile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .pipeline import Frame, PlayerPipeline
from ..ui.clip_stage import VideoStage
from ..ui.formatting import format_ms
from ..ui.trim_bar import make_trim_bar

POLL_MS = 15
RATES = (0.5, 1.0, 1.5, 2.0)
SEEK_STEP_MS = 5_000
CLIP_HINT = ("Reaction clip: grey on the bar is its hidden editing margin. "
             "Drag the yellow borders under the bar to keep more or less of it.")


def clip_time(position: int, chosen: tuple[int, int] | None) -> str:
    """Where a clip is, from its start border, and how long its chosen part is."""
    if chosen is None:
        return "--:-- / --:--"
    start, end = chosen
    since = position - start
    return f"{'−' if since < 0 else ''}{format_ms(abs(since))} / {format_ms(end - start)}"


class VideoView(QWidget):
    """Paints the newest frame, letterboxed, on black."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.image: QImage | None = None
        self.frames_shown = 0
        self.setMinimumSize(320, 180)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)

    def show_frame(self, frame: Frame) -> None:
        # copy() detaches the QImage from the Python bytes it was built on.
        self.image = QImage(
            frame.data, frame.width, frame.height, frame.stride, QImage.Format.Format_RGBX8888
        ).copy()
        self.frames_shown += 1
        self.update()

    def clear(self) -> None:
        self.image = None
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("black"))
        if self.image is None:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No video")
            return
        scaled = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(QRect(x, y, scaled.width(), scaled.height()), self.image)


class PlayerWindow(QMainWindow):
    """Video, transport controls, and a log of the playback anchors sent."""

    anchor_sent = Signal(dict)

    def __init__(
        self,
        session_id: str = "ses-local-player",
        audio_sink: str = "autoaudiosink",
        on_anchor: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("Encore Moment Player")
        self.resize(1100, 720)
        self._external_anchor = on_anchor
        self.player = PlayerPipeline(session_id, self._on_anchor, audio_sink=audio_sink)
        self._dragging = False
        self._clip_mode = False
        self._last_error: str | None = None

        self.video = VideoView()
        # The film, or a saved reaction clip over it (VideoStage.attach_clip_panel).
        self.stage = VideoStage(self.video)
        self.play_btn = QPushButton("Play")
        self.open_btn = QPushButton("Open…")
        self.slider, self.trim_lane, slider_box = make_trim_bar()
        self.slider.setRange(0, 0)
        self.time_label = QLabel("--:-- / --:--")
        self.rate_box = QComboBox()
        for r in RATES:
            self.rate_box.addItem(f"{r:g}×", r)
        self.rate_box.setCurrentIndex(RATES.index(1.0))
        self.anchor_log = QPlainTextEdit()
        self.anchor_log.setReadOnly(True)
        self.anchor_log.setMaximumBlockCount(500)
        self.anchor_log.setPlaceholderText("Playback anchors appear here as they are sent.")

        controls = QHBoxLayout()
        controls.addWidget(self.open_btn)
        controls.addWidget(self.play_btn)
        controls.addWidget(slider_box, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(self.rate_box)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.addWidget(self.stage, 1)
        top_layout.addLayout(controls)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(top)
        split.addWidget(self.anchor_log)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        self.setCentralWidget(split)

        self.echo_label = QLabel()
        self.statusBar().addPermanentWidget(self.echo_label)
        self._update_echo_label()

        self.open_btn.clicked.connect(self._choose_file)
        self.play_btn.clicked.connect(self.toggle)
        self.slider.sliderPressed.connect(self._drag_start)
        self.slider.sliderReleased.connect(self._drag_end)
        self.slider.sliderMoved.connect(self._scrub)
        self.trim_lane.border_moved.connect(self._move_border)
        self.trim_lane.border_released.connect(self._save_border)
        self.rate_box.currentIndexChanged.connect(self._rate_changed)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self.toggle)
        QShortcut(QKeySequence(Qt.Key.Key_Left), self, lambda: self.step(-SEEK_STEP_MS))
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, lambda: self.step(SEEK_STEP_MS))

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    # ---- actions -----------------------------------------------------------

    def open_file(self, path: str | Path, title: str | None = None) -> None:
        self.video.clear()
        self.rate_box.blockSignals(True)
        self.rate_box.setCurrentIndex(RATES.index(1.0))
        self.rate_box.blockSignals(False)
        self.player.open(path, title)
        self.setWindowTitle(f"{self.player.title} — Encore Moment Player")
        self.statusBar().showMessage(f"Opened {Path(path).name}", 4000)

    def showing_clip(self) -> bool:
        return self.stage.showing_clip()

    def toggle(self) -> None:
        if self.showing_clip():
            self.stage.clips.toggle()
            return
        self.player.toggle()

    def step(self, delta_ms: int) -> None:
        if self.showing_clip():
            clips = self.stage.clips
            clips.seek(clips.player.position() + delta_ms)
            return
        pos = self.player.position_ms()
        if pos is not None:
            self.player.seek(pos + delta_ms)

    def tick(self) -> None:
        frame = self.player.poll()
        if frame is not None:
            self.video.show_frame(frame)
        if self.showing_clip():
            self._tick_clip()
        else:
            self._tick_film()
        if self.player.error and self.player.error != self._last_error:
            self._last_error = self.player.error
            self.statusBar().showMessage(f"Playback error: {self.player.error}")

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.timer.stop()
        self.player.close()
        super().closeEvent(event)

    # ---- internals ---------------------------------------------------------

    def _tick_film(self) -> None:
        if self._clip_mode:
            self._clip_mode = False
            self.slider.set_chosen(None)
            self.slider.setRange(0, 0)
            self.trim_lane.hide()
            self.rate_box.setEnabled(True)
            if self.statusBar().currentMessage() == CLIP_HINT:
                self.statusBar().clearMessage()
        pos, dur = self.player.position_ms(), self.player.duration_ms()
        if dur and self.slider.maximum() != dur:
            self.slider.setRange(0, dur)
        if pos is not None and not self._dragging:
            self.slider.setValue(pos)
        self.time_label.setText(f"{format_ms(pos)} / {format_ms(dur)}")
        self.play_btn.setText("Pause" if self.player.want_playing else "Play")

    def _tick_clip(self) -> None:
        if not self._clip_mode:
            self._clip_mode = True
            self.trim_lane.show()
            self.rate_box.setEnabled(False)
            self.statusBar().showMessage(CLIP_HINT)
        clips = self.stage.clips
        pos, dur = clips.player.position(), clips.player.duration()
        chosen = clips.play_range()
        if dur > 0 and self.slider.maximum() != dur:
            self.slider.setRange(0, dur)
        if not self._dragging:
            self.slider.setValue(pos)
        self.slider.set_chosen(chosen)
        self.trim_lane.update()
        self.time_label.setText(clip_time(pos, chosen))
        playing = clips.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setText("Pause" if playing else "Play")

    def _scrub(self, value: int) -> None:
        if self.showing_clip():
            self.stage.clips.seek(value)

    def _move_border(self, border: str, value: int) -> None:
        if not self.showing_clip():
            return
        chosen = self.stage.clips.move_border(border, value)
        if chosen is not None:
            self.slider.set_chosen(chosen)
            self.slider.setValue(chosen[0] if border == "start" else chosen[1])
            self.time_label.setText(clip_time(self.slider.value(), chosen))

    def _save_border(self) -> None:
        if not self.showing_clip():
            return
        clips = self.stage.clips
        chosen = clips.play_range()
        if chosen is None:
            return
        length = f"Clip length {(chosen[1] - chosen[0]) / 1000:.1f} s"
        self.statusBar().showMessage(f"{length}, saved." if clips.save_range()
                                     else f"{length} for now: not saved to the clip index.", 6000)

    def _on_anchor(self, msg: dict[str, Any]) -> None:
        pb = msg["playback"]
        self.anchor_log.appendPlainText(
            f"#{msg['seq']:<3} t={msg['capture_ms']} ms  epoch {pb['epoch']}  {pb['state']:<7}  "
            f"pos {format_ms(pb['position_ms'])} ({pb['position_ms']} ms)  {pb['rate']:g}×"
        )
        if self._external_anchor is not None:
            self._external_anchor(msg)
        self.anchor_sent.emit(msg)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open video", "", "Video (*.mp4 *.mkv *.webm *.mov *.avi);;All files (*)"
        )
        if path:
            self.open_file(path)

    def _drag_start(self) -> None:
        self._dragging = True
        self._scrub(self.slider.value())

    def _drag_end(self) -> None:
        self._dragging = False
        if self.showing_clip():
            self.stage.clips.seek(self.slider.value())
            return
        self.player.seek(self.slider.value())

    def _rate_changed(self, index: int) -> None:
        rate = self.rate_box.itemData(index)
        if not self.player.set_rate(rate):
            self.statusBar().showMessage(f"Could not change speed to {rate:g}×", 4000)

    def _update_echo_label(self) -> None:
        if self.player.echo_reference == "available":
            self.echo_label.setText("Echo reference: ready")
        elif self.player.echo_reference == "linear":
            self.echo_label.setText("Echo reference: ready (linear canceller; webrtcdsp not installed)")
        else:
            self.echo_label.setText("Echo reference: unavailable")


def jsonl_writer(path: str | Path) -> Callable[[dict[str, Any]], None]:
    """Append each anchor to a JSON Lines file (one message per line)."""
    fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 (lives as long as the app)

    def write(msg: dict[str, Any]) -> None:
        fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        fh.flush()

    return write
