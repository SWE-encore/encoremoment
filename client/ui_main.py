"""Encore Moment - PC client UI (Sprint 1)

[Sprint 1] Connect clip list/playback and sensing status screen
  - List and play local clips
  - Camera off by default / active indicator / stop control / connection status
  - Sample clips for pending, ready, partial, and unavailable; no playback link for missing files

Run:
  pip install PySide6
  python scripts/make_samples.py     # create sample clips (first time only)
  python client/ui_main.py

Still mocked: server connection (WSS), real camera (GStreamer), real reaction detection.
-> Device and server events are simulated with timers and the "Developer simulation" buttons.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QApplication, QGroupBox, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QTabWidget, QVBoxLayout, QWidget)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from clips import Clip, effective_label, load_index, playable_path  # noqa: E402
from sensing_state import (CAMERA_TEXT, CONNECTION_TEXT, CameraController,  # noqa: E402
                           CameraState, ConnectionState)

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT / "samples"
INDEX_PATH = SAMPLES_DIR / "clip_index.json"

EVENT_NAME = {"laughter": "Laughter", "scream": "Scream", "gasp": "Gasp"}


class MainWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Encore Moment")
        self.session_active = False
        self.mic_on = True
        self.camera = CameraController()
        self.connection = ConnectionState.DISCONNECTED
        self.clips: list[Clip] = []

        # ---------- Top: sensing status panel (always visible across tabs, Requirement 6) ----------
        self.cam_label = QLabel()
        self.cam_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.btn_cam_stop = QPushButton("■ Stop camera")
        self.btn_cam_stop.clicked.connect(self.stop_camera)
        self.mic_label = QLabel()
        self.conn_label = QLabel()

        status_box = QGroupBox("Sensing status")
        sl = QVBoxLayout(status_box)
        cam_row = QHBoxLayout()
        cam_row.addWidget(self.cam_label)
        cam_row.addStretch()
        cam_row.addWidget(self.btn_cam_stop)
        sl.addLayout(cam_row)
        sl.addWidget(self.mic_label)
        sl.addWidget(self.conn_label)

        # ---------- Controls ----------
        self.btn_session = QPushButton()
        self.btn_cam = QPushButton("Enable camera")
        self.btn_mic = QPushButton()
        self.btn_session.clicked.connect(self.toggle_session)
        self.btn_cam.clicked.connect(self.start_camera)
        self.btn_mic.clicked.connect(self.toggle_mic)
        ctrl = QHBoxLayout()
        for b in (self.btn_session, self.btn_cam, self.btn_mic):
            ctrl.addWidget(b)

        # ---------- Developer simulation (until real devices/server are connected) ----------
        sim_box = QGroupBox("Developer simulation")
        sim = QHBoxLayout(sim_box)
        b1 = QPushButton("Unplug webcam")
        b1.clicked.connect(self.sim_device_lost)
        b2 = QPushButton("Drop server connection")
        b2.clicked.connect(self.sim_disconnect)
        sim.addWidget(b1)
        sim.addWidget(b2)

        # ---------- Tab 1: live reaction timeline ----------
        self.timeline = QListWidget()

        # ---------- Tab 2: clip list + player ----------
        self.clip_list = QListWidget()
        self.clip_list.currentItemChanged.connect(self.on_clip_selected)
        self.clip_detail = QLabel("Select a clip")
        self.clip_detail.setWordWrap(True)
        self.btn_play = QPushButton("▶ Play")
        self.btn_play.clicked.connect(self.play_selected)
        self.btn_refresh = QPushButton("Refresh list")
        self.btn_refresh.clicked.connect(self.reload_clips)

        self.video = QVideoWidget()
        self.video.setMinimumHeight(220)
        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video)
        self.player.errorOccurred.connect(
            lambda _e, msg: self.clip_detail.setText(f"Playback error: {msg}"))

        clip_tab = QWidget()
        cl = QVBoxLayout(clip_tab)
        cl.addWidget(QLabel("Tonight's Highlights (highest intensity first)"))
        cl.addWidget(self.clip_list)
        cl.addWidget(self.clip_detail)
        prow = QHBoxLayout()
        prow.addWidget(self.btn_play)
        prow.addWidget(self.btn_refresh)
        cl.addLayout(prow)
        cl.addWidget(self.video)

        tabs = QTabWidget()
        tabs.addTab(self.timeline, "Live timeline")
        tabs.addTab(clip_tab, "Clips")

        root = QVBoxLayout(self)
        root.addWidget(status_box)
        root.addLayout(ctrl)
        root.addWidget(tabs)
        root.addWidget(sim_box)

        # Fake reaction events in place of the server
        self.fake = QTimer(self)
        self.fake.timeout.connect(self.fake_event)

        self.reload_clips()
        self.render()

    # ======================= Session / microphone =======================
    def toggle_session(self) -> None:
        self.session_active = not self.session_active
        if self.session_active:
            self.timeline.clear()
            self.fake.start(3000)
            self.connection = ConnectionState.RECONNECTING
            QTimer.singleShot(600, self.sim_connected)
        else:
            self.fake.stop()
            self.stop_camera()                    # ending the session also turns the camera off
            self.connection = ConnectionState.DISCONNECTED
        self.render()

    def toggle_mic(self) -> None:
        self.mic_on = not self.mic_on
        self.render()

    def fake_event(self) -> None:
        if not self.mic_on:                       # Requirement 8: no new detections while mic is off
            return
        if self.connection != ConnectionState.CONNECTED:
            return                                # no results arrive while disconnected
        t = random.choice(list(EVENT_NAME))
        tc = f"00:{random.randint(0, 59):02d}:{random.randint(0, 59):02d}"
        self.timeline.addItem(f"{tc}  {EVENT_NAME[t]}  intensity {random.random():.2f}")

    # ======================= Camera (Requirements 5, 6) =======================
    def start_camera(self) -> None:
        if not self.camera.request_enable(self.session_active):
            return
        gen = self.camera.generation
        # In the real client, GStreamer calls this when the first frame arrives. Simulated: 0.8 s.
        QTimer.singleShot(800, lambda: self._on_first_frame(gen))
        self.render()

    def _on_first_frame(self, gen: int) -> None:
        self.camera.on_first_frame(gen)
        self.render()

    def stop_camera(self) -> None:
        if not self.camera.request_disable():
            return
        # In the real client, the pipeline reports when capture has fully stopped. Simulated: 0.5 s.
        QTimer.singleShot(500, self._on_stopped)
        self.render()

    def _on_stopped(self) -> None:
        self.camera.on_stopped()
        self.render()

    def sim_device_lost(self) -> None:
        self.camera.on_device_lost()
        self.render()

    # ======================= Connection status (Requirement 9) =======================
    def sim_connected(self) -> None:
        if self.session_active:
            self.connection = ConnectionState.CONNECTED
            self.render()

    def sim_disconnect(self) -> None:
        if not self.session_active:
            return
        self.connection = ConnectionState.DISCONNECTED
        self.render()
        QTimer.singleShot(3000, self._sim_reconnecting)

    def _sim_reconnecting(self) -> None:
        if self.session_active:
            self.connection = ConnectionState.RECONNECTING
            self.render()
            QTimer.singleShot(1000, self.sim_connected)

    # ======================= Clip list / playback =======================
    def reload_clips(self) -> None:
        self.player.stop()
        self.clips = sorted(load_index(INDEX_PATH),
                            key=lambda c: (-c.intensity, c.timecode, c.clip_id))
        self.clip_list.clear()
        if not self.clips:
            self.clip_list.addItem("No clips - run scripts/make_samples.py first")
        for c in self.clips:
            item = QListWidgetItem(
                f"{EVENT_NAME.get(c.event_type, c.event_type)}  intensity {c.intensity:.2f}  "
                f"@ {c.timecode}   [{effective_label(c, SAMPLES_DIR)}]")
            item.setData(Qt.UserRole, c.clip_id)
            self.clip_list.addItem(item)
        self.on_clip_selected(None, None)

    def selected_clip(self) -> Clip | None:
        item = self.clip_list.currentItem()
        if item is None:
            return None
        cid = item.data(Qt.UserRole)
        return next((c for c in self.clips if c.clip_id == cid), None)

    def on_clip_selected(self, *_args) -> None:
        clip = self.selected_clip()
        if clip is None:
            self.clip_detail.setText("Select a clip")
            self.btn_play.setVisible(False)
            return
        path = playable_path(clip, SAMPLES_DIR)
        self.clip_detail.setText(
            f"{clip.title}\nType: {EVENT_NAME.get(clip.event_type)}  ·  Timecode: {clip.timecode}  ·  "
            f"Status: {effective_label(clip, SAMPLES_DIR)}")
        # Key rule: show the Play button (link) only when a playable file exists
        self.btn_play.setVisible(path is not None)

    def play_selected(self) -> None:
        clip = self.selected_clip()
        if clip is None:
            return
        path = playable_path(clip, SAMPLES_DIR)   # re-check at click time (file may have been removed)
        if path is None:
            self.reload_clips()
            return
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()

    # ======================= Rendering =======================
    def render(self) -> None:
        self.btn_session.setText("End session" if self.session_active else "Start session")
        self.btn_mic.setText("Disable microphone" if self.mic_on else "Enable microphone")
        self.btn_cam.setEnabled(self.session_active and
                                self.camera.state in (CameraState.OFF, CameraState.ERROR))

        self.cam_label.setText(CAMERA_TEXT[self.camera.state])
        self.btn_cam_stop.setVisible(self.camera.show_stop_button)
        self.btn_cam_stop.setEnabled(self.camera.stop_button_enabled)

        self.mic_label.setText("🎙 Microphone sensing on" if self.mic_on else "🔇 Microphone sensing off")
        conn = CONNECTION_TEXT[self.connection] if self.session_active else "⚪ No active session (server not connected)"
        self.conn_label.setText(conn)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = MainWindow()
    w.resize(640, 820)
    w.show()
    sys.exit(app.exec())
