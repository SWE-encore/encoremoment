"""The test bench window."""

from __future__ import annotations

import math
import tempfile
import time
import wave
from array import array
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..clock import monotonic_ms
from ..sensing.live_mic import LiveMicrophone, QtAudioInput
from ..sensing.wav_mic import WavMicrophone
from ..session.contract import AUDIO_RATE
from ..session.session import Conn, Session
from ..ui.formatting import format_ms
from .local import MODELS, LocalServer, Models, default_triggers, preload_tensorflow, prepare_audio
from .plot import COLORS, ScorePlot

TICK_MS = 50
STOP_GRACE_S = 1.0  # after the microphone goes off, time for the server's FINALs to arrive
SPEEDS = (1, 4, 16)
COLUMNS = ("Reaction", "Phase", "Rev", "Confidence", "Intensity", "Start", "End", "Content time", "Event ID")
STATUS = {
    Conn.IDLE: "#888888", Conn.CONNECTING: "#c28a00", Conn.CONNECTED: "#1a8f3c",
    Conn.DISCONNECTED: "#c62828", Conn.ENDED: "#888888",
}


class BenchWindow(QMainWindow):
    def __init__(self, *, model: str = "loudness", server_url: str | None = None,
                 audio_file: Path | None = None, speed: int = 1) -> None:
        preload_tensorflow()  # before _build() lists the audio devices
        super().__init__()
        self.setWindowTitle("Encore Moment — test bench")
        self.models = Models()
        self.session: Session | None = None
        self.local: LocalServer | None = None
        self.source: WavMicrophone | LiveMicrophone | None = None
        self.qt_input: QtAudioInput | None = None
        self.recording: bytes = b""
        self.audio_path: Path | None = None
        self.error = ""
        self._clock = monotonic_ms
        self._origin = 0
        self._began = False  # audio flows once the connection is open
        self._title = ""
        self._stop_at: float | None = None
        self._rows = None
        self._workdir = tempfile.TemporaryDirectory(prefix="encore-bench-")
        self._triggers_default = default_triggers()
        self._build()

        self.model_box.setCurrentIndex(max(self.model_box.findData(model), 0))
        if server_url:
            self.remote_radio.setChecked(True)
            self.url_edit.setText(server_url)
        if speed in SPEEDS:
            self.speed_box.setCurrentIndex(SPEEDS.index(speed))
        if audio_file is not None:
            self.open_file(Path(audio_file))
        self.models.request(self.model_box.currentData())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(TICK_MS)
        self.refresh()

    # ---- layout ------------------------------------------------------------------

    def _build(self) -> None:
        from PySide6.QtMultimedia import QMediaDevices

        # Audio source
        self.mic_radio = QRadioButton("Microphone")
        self.file_radio = QRadioButton("Audio file")
        self.source_group = QButtonGroup(self)
        for b in (self.mic_radio, self.file_radio):
            self.source_group.addButton(b)
        self.device_box = QComboBox()
        default = QMediaDevices.defaultAudioInput()
        for dev in QMediaDevices.audioInputs():
            self.device_box.addItem(dev.description(), dev)
            if dev.id() == default.id():
                self.device_box.setCurrentIndex(self.device_box.count() - 1)
        has_mic = self.device_box.count() > 0
        self.mic_radio.setEnabled(has_mic)
        self.mic_radio.setToolTip("" if has_mic else "No microphone found")
        (self.mic_radio if has_mic else self.file_radio).setChecked(True)
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText("WAV, MP3, a video… (converted with ffmpeg)")
        self.open_btn = QPushButton("Open…")
        self.speed_box = QComboBox()
        for s in SPEEDS:
            self.speed_box.addItem(f"{s}× speed", s)
        file_row = QHBoxLayout()
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(self.open_btn)
        file_row.addWidget(self.speed_box)
        source = QGroupBox("Audio in")
        form = QFormLayout(source)
        form.addRow(self.mic_radio, self.device_box)
        form.addRow(self.file_radio, file_row)
        hint = QLabel("No echo cancellation here: use headphones if anything else is playing.")
        hint.setWordWrap(True)
        form.addRow(hint)

        # Server
        self.builtin_radio = QRadioButton("Built-in")
        self.remote_radio = QRadioButton("URL")
        self.server_group = QButtonGroup(self)
        for b in (self.builtin_radio, self.remote_radio):
            self.server_group.addButton(b)
        self.builtin_radio.setChecked(True)
        self.model_box = QComboBox()
        for key, label in MODELS.items():
            self.model_box.addItem(label, key)
        self.url_edit = QLineEdit("ws://127.0.0.1:8765")
        server = QGroupBox("Server")
        sform = QFormLayout(server)
        sform.addRow(self.builtin_radio, self.model_box)
        sform.addRow(self.remote_radio, self.url_edit)
        self.model_state = QLabel()
        self.model_state.setWordWrap(True)
        sform.addRow(self.model_state)

        # Detector trigger levels
        self.trigger_boxes: dict[str, QDoubleSpinBox] = {}
        levels = QGroupBox("Trigger levels")
        lform = QFormLayout(levels)
        for reaction, value in self._triggers_default.items():
            box = QDoubleSpinBox()
            box.setRange(0.05, 0.95)
            box.setSingleStep(0.05)
            box.setDecimals(2)
            box.setValue(value)
            label = QLabel(reaction)
            label.setStyleSheet(f"color: {COLORS.get(reaction, '#888')}; font-weight: 600")
            lform.addRow(label, box)
            self.trigger_boxes[reaction] = box
        self.defaults_btn = QPushButton("Defaults")
        lform.addRow(QLabel("Built-in server; applied at Start."), self.defaults_btn)

        top = QHBoxLayout()
        top.addWidget(source, 3)
        top.addWidget(server, 2)
        top.addWidget(levels, 1)

        # Controls
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.save_btn = QPushButton("Save recording…")
        self.save_btn.setToolTip("Save the microphone audio of the last run as a WAV, to replay it later")
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.RichText)
        self.status.setWordWrap(True)
        self.level = QLabel()
        controls = QHBoxLayout()
        for b in (self.start_btn, self.stop_btn, self.save_btn):
            controls.addWidget(b)
        controls.addWidget(self.status, 1)
        controls.addWidget(self.level)

        # Plot and events
        self.plot = ScorePlot()
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.plot)
        split.addWidget(self.table)
        split.setSizes([420, 220])
        self.counts = QLabel()
        self.counts.setWordWrap(True)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addLayout(top)
        layout.addLayout(controls)
        layout.addWidget(split, 1)
        layout.addWidget(self.counts)
        self.setCentralWidget(central)
        self.resize(1200, 860)

        self.open_btn.clicked.connect(self._choose_file)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        self.save_btn.clicked.connect(self._save_recording)
        self.defaults_btn.clicked.connect(self._reset_triggers)
        self.model_box.currentIndexChanged.connect(lambda _: self.models.request(self.model_box.currentData()))
        for b in (self.mic_radio, self.file_radio, self.builtin_radio, self.remote_radio):
            b.toggled.connect(lambda _: self.refresh())

    # ---- user actions ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self.session is not None and self.session.state in (Conn.CONNECTING, Conn.CONNECTED)

    def open_file(self, path: Path) -> bool:
        try:
            self.audio_path = prepare_audio(path, Path(self._workdir.name))
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            self.audio_path = None
            self.refresh()
            return False
        self.error = ""
        self.file_edit.setText(str(path))
        self.file_radio.setChecked(True)
        self.refresh()
        return True

    def start(self) -> bool:
        if self.running or not self._can_start():
            return False
        self._shutdown_run()
        self.error = ""
        use_file = self.file_radio.isChecked()
        speed = self.speed_box.currentData() if use_file else 1
        if speed == 1:
            self._clock = monotonic_ms
        else:
            base = monotonic_ms()
            self._clock = lambda: base + (monotonic_ms() - base) * speed
        triggers = {r: round(b.value(), 2) for r, b in self.trigger_boxes.items()}
        note = ""
        if self.builtin_radio.isChecked():
            _, model = self.models.state(self.model_box.currentData())
            try:
                self.local = LocalServer(model, triggers)
            except (OSError, RuntimeError) as exc:
                self.error = f"Could not start the built-in server: {exc}"
                self.refresh()
                return False
            url = self.local.url
        else:
            url = self.url_edit.text().strip()
            note = "Scores are shown only with the built-in server; events still appear."
            triggers = {}
        try:
            self.session = Session(url, clock=self._clock, microphone_on_start=True)
        except ValueError as exc:
            self.error = str(exc)
            self._shutdown_run()
            self.refresh()
            return False
        self._title = Path(self.file_edit.text()).name if use_file else "Microphone test"
        if use_file:
            self.source = WavMicrophone(self.session, self.audio_path, clock=self._clock)
        else:
            self.source = LiveMicrophone(self.session, clock=self._clock)
            try:  # check the device now; it starts recording once connected
                self.qt_input = QtAudioInput(self.source, self.device_box.currentData())
            except RuntimeError as exc:
                self.error = str(exc)
                self.session = None
                self._shutdown_run()
                self.refresh()
                return False
        self._began = False
        self.recording = b""
        self._rows = None
        self._origin = self._clock()
        self.plot.reset(self._origin, triggers, note)
        self.session.start()
        self.refresh()
        return True

    def _begin(self) -> None:
        """Connected: tell the server what is "playing", then let audio flow."""
        self._began = True
        self._origin = self._clock()
        self.plot.reset(self._origin, self.plot.triggers, self.plot.note)
        self.session.send_anchor({
            "schema": "encore.v1", "kind": "playback_anchor", "seq": 0, "capture_ms": self._origin,
            # Content time on the bench is time since the audio started: for a file, the position in it.
            "playback": {"epoch": 1, "content_id": "bench", "title": self._title, "position_ms": 0,
                         "rate": 1.0, "state": "playing"},
        })
        if self.qt_input is not None:
            try:
                self.qt_input.start()
            except RuntimeError as exc:
                self.error = str(exc)
                self.stop()

    def stop(self) -> None:
        """Turn the microphone off first, so the server finishes open events (R8), then end."""
        if self._stop_at is not None:
            return
        self._stop_capture()
        if not self.running:
            self.refresh()
            return
        if self.session.state is Conn.CONNECTED:
            self._stop_at = time.monotonic() + STOP_GRACE_S
        else:
            self._finish()
        self.refresh()

    def _stop_capture(self) -> None:
        if self.qt_input is not None:
            self.qt_input.stop()
            self.qt_input = None
        if self.session is not None and self.session.view.sensors["microphone"].enabled:
            self.session.set_sensor("microphone", False)
        if isinstance(self.source, LiveMicrophone):
            self.recording = bytes(self.source.recording)

    def _finish(self) -> None:
        self._stop_at = None
        self._stop_capture()
        if self.session is not None:
            self.session.end()

    def _shutdown_run(self) -> None:
        self._finish()
        if self.local is not None:
            self.local.stop()
            self.local = None

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self._shutdown_run()
        self._workdir.cleanup()
        super().closeEvent(event)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open audio", str(Path.home()),
                                              "Audio or video (*.wav *.mp3 *.m4a *.ogg *.flac *.mp4 *.mkv *.webm);;All files (*)")
        if path:
            self.open_file(Path(path))

    def _save_recording(self) -> None:
        name = time.strftime("bench-%Y%m%d-%H%M%S.wav")
        path, _ = QFileDialog.getSaveFileName(self, "Save recording", str(Path.home() / name), "WAV (*.wav)")
        if path:
            self.save_recording(Path(path))

    def save_recording(self, path: Path) -> None:
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(AUDIO_RATE)
            w.writeframes(self.recording)

    def _reset_triggers(self) -> None:
        for reaction, box in self.trigger_boxes.items():
            box.setValue(self._triggers_default[reaction])

    # ---- updates -----------------------------------------------------------------

    def tick(self) -> None:
        s = self.session
        if s is not None:
            s.poll()
            if s.state in (Conn.DISCONNECTED, Conn.ENDED) and (
                    self.qt_input is not None or s.view.sensors["microphone"].enabled):
                self._stop_at = None
                self._stop_capture()
            if not self._began and s.state is Conn.CONNECTED and self._stop_at is None:
                self._begin()
            if isinstance(self.source, WavMicrophone) and self._began and self.running:
                self.source.pump()
                # A mic-off control discards queued audio. Let the final file chunk
                # reach the writer before automatically stopping at EOF.
                if self.source.done and not s.media_pending:
                    self.stop()
            if self._stop_at is not None and time.monotonic() >= self._stop_at:
                self._finish()
            if self.local is not None:
                self.plot.add_frames(self.local.frames(s.session_id))
            now = self._clock() if self.running else None
            if now is not None and isinstance(self.source, WavMicrophone):  # nothing after the file's end
                now = min(now, self._origin + len(self.source.pcm) * 1000 // (2 * AUDIO_RATE))
            self.plot.set_events(s.view.events.values(), now)
        self.refresh()

    def _can_start(self) -> bool:
        if self.file_radio.isChecked() and self.audio_path is None:
            return False
        if self.builtin_radio.isChecked():
            return self.models.state(self.model_box.currentData())[0] == "ready"
        return bool(self.url_edit.text().strip())

    def refresh(self) -> None:
        running = self.running
        for w in (self.mic_radio, self.file_radio, self.device_box, self.file_edit, self.open_btn, self.speed_box,
                  self.builtin_radio, self.remote_radio, self.model_box, self.url_edit, self.defaults_btn,
                  *self.trigger_boxes.values()):
            w.setEnabled(not running)
        if not running:
            self.mic_radio.setEnabled(self.device_box.count() > 0)
        self.start_btn.setEnabled(not running and self._can_start())
        self.stop_btn.setEnabled(running and self._stop_at is None)
        self.save_btn.setEnabled(not running and bool(self.recording))

        state, value = self.models.state(self.model_box.currentData())
        name = self.model_box.currentText()
        self.model_state.setText({"ready": f"{name} ready.",
                                  "loading": f"Loading {name}… (YAMNet downloads once, then starts in seconds)",
                                  "error": value}[state] if self.builtin_radio.isChecked() else "")
        self.model_state.setStyleSheet("color: #c62828" if state == "error" else "")

        s = self.session
        if self.error:
            self.status.setText(f'<span style="color:#c62828">{_escape(self.error)}</span>')
        elif s is None:
            self.status.setText("Choose an input and press Start.")
        else:
            text = s.state.value.capitalize()
            if self._stop_at is not None:
                text = "Finishing open events…"
            elif s.state is Conn.DISCONNECTED:
                text += f": {s.reason}"
            where = f"built-in server, {self.local.model_name}, {self.local.config.name}" if self.local else s.url
            self.status.setText(f'<span style="color:{STATUS[s.state]}; font-weight:600">● {_escape(text)}</span>'
                                f" · {_escape(where)} · {s.session_id}")
        live = self.source if isinstance(self.source, LiveMicrophone) and running else None
        muted = live is not None and live.muted
        self.level.setText("" if live is None else
                           "No sound at all: is this microphone muted in the system settings?" if muted else
                           f"Input {_db(live.last_pcm):.0f} dBFS")
        self.level.setStyleSheet("color: #c62828; font-weight: 600" if muted else "")

        if s is None:
            self.counts.setText("")
            return
        rows = _rows(s.view.events.values(), self._origin)
        if rows != self._rows:
            self._rows = rows
            self.table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    self.table.setItem(r, c, QTableWidgetItem(value))
        summary = s.view.summary()
        parts = [f"{len(rows)} event(s)", f"audio chunks sent {s.audio_sent}"]
        if s.audio_dropped:
            parts.append(f"dropped {s.audio_dropped}")
        if summary["ignored"]:
            parts.append(f"PC ignored {summary['ignored']}")
        parts.append(f"PC rejected {summary['rejected'] or 'nothing'}")
        server_side = self.local.session(s.session_id) if self.local else None
        if server_side is not None:
            parts.append(f"server analysed {server_side.audio_chunks} chunk(s), invalid {server_side.invalid}, "
                         f"gaps {server_side.view.audio_gaps}")
        self.counts.setText(" · ".join(parts))


def _rows(events, origin_ms: int) -> list[tuple[str, ...]]:
    def sec(t):
        return "open" if t is None else f"{(t - origin_ms) / 1000:.1f} s"

    out = []
    for ev in sorted(events, key=lambda e: (e["capture"]["triggers_ms"][0], e["event_id"])):
        cap, pb = ev["capture"], ev.get("playback") or {}
        when = format_ms(pb["timecode_start_ms"]) if pb.get("status") == "mapped" else "pending"
        out.append((ev["reaction"]["type"], ev["phase"], str(ev["revision"]),
                    f"{ev['reaction']['confidence']:.2f}", f"{ev['intensity']['combined']:.2f}",
                    sec(cap["start_ms"]), sec(cap["end_ms"]), when, ev["event_id"]))
    return out


def _db(pcm: bytes) -> float:
    """Level without the DC offset, like the server's rms_db: an offset is not sound."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return -120.0
    n = len(samples)
    mean = sum(samples) / n
    variance = max(sum(v * v for v in samples) / n - mean * mean, 0.0)
    return 20 * math.log10(max(math.sqrt(variance) / 32768, 1e-6))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
