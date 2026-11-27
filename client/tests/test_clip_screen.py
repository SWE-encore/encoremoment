"""Screen recording for reaction clips: the time mapping (clips/screen_map.py),
recording and joining (clips/screen.py), the Screen switch (ui/screen_panel.py),
the Screen view (ui/screen_view.py) and the Camera/Screen/side-by-side switch."""
import json
import os
import shutil
import time
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QWidget

from encore_client.__main__ import ROOT
from encore_client.clips.model import load_index
from encore_client.clips.panel import ClipPanel
from encore_client.clips.screen_map import ScreenSegment, screen_segments, segment_at
from encore_client.ui import screen_view
from encore_client.ui.clip_stage import VideoStage
from encore_client.ui.screen_panel import ScreenPanel

PLAYING = QMediaPlayer.PlaybackState.PlayingState
PAUSED = QMediaPlayer.PlaybackState.PausedState
TEST_SOURCE = "videotestsrc is-live=true pattern=ball ! video/x-raw,width=640,height=400"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "UI condition timed out"


# ---- the time mapping -------------------------------------------------------------

def test_screen_time_at_each_moment_of_the_camera_clip():
    row = {"coverage_ms": [[1_000, 3_000], [3_004, 5_000], [7_000, 9_000]],   # a 4 ms frame hole, a 2 s hole
           "screen": {"path": "c.screen.webm", "coverage_ms": [[0, 4_000], [8_000, 12_000]]}}
    segments = screen_segments(row)
    # The camera file: 1-5 s at 0-4 s (the frame hole stays in the file), 7-9 s at 4-6 s.
    # The screen file: 0-4 s at 0-4 s, 8-12 s at 4-8 s.
    assert segments == (ScreenSegment((0, 3_000), "c.screen.webm", 1_000),
                        ScreenSegment((5_000, 6_000), "c.screen.webm", 4_000))
    assert segment_at(segments, 2_000).screen_at(2_000) == 3_000
    assert segment_at(segments, 4_000) is None, "the screen was off at 4-8 s"
    assert segment_at(segments, 6_300) is segments[-1], "the file's last frame"
    assert screen_segments({"coverage_ms": [[0, 1]]}) is None
    assert screen_segments({"coverage_ms": [[0, 1]], "screen": {"path": 3}}) is None


def test_the_clip_model_reads_the_screen(tmp_path):
    index = tmp_path / "clip_index.json"
    index.write_text(json.dumps({"clips": [
        {"clip_id": "c", "event_type": "laughter", "timecode": "00:00:01", "intensity": 0.5, "status": "ready",
         "coverage_ms": [[0, 2_000]], "screen": {"path": "c.screen.webm", "coverage_ms": [[0, 2_000]]}},
        {"clip_id": "d", "event_type": "gasp", "timecode": "00:00:02", "intensity": 0.5, "status": "ready"}]}))
    clips = {c.clip_id: c for c in load_index(index)}
    assert clips["c"].screen == (ScreenSegment((0, 2_000), "c.screen.webm", 0),)
    assert clips["d"].screen is None


def test_screen_recording_needs_linux_x11():
    from encore_client.clips.screen import unavailable_reason
    assert "Linux" in unavailable_reason({"DISPLAY": ":0"}, "win32")
    assert "X11" in unavailable_reason({"DISPLAY": ":0", "XDG_SESSION_TYPE": "wayland"}, "linux")
    assert "X11" in unavailable_reason({}, "linux")


# ---- recording and joining --------------------------------------------------------

def _index(tmp_path, rows):
    index = tmp_path / "clips" / "clip_index.json"
    index.parent.mkdir(exist_ok=True)
    for row in rows:
        row.setdefault("event_type", "laughter")
        row.setdefault("timecode", "00:00:01")
        row.setdefault("intensity", 0.5)
    index.write_text(json.dumps({"clips": rows}))
    return index


def test_each_finished_clip_gets_the_screen_of_its_moments(app, tmp_path):
    pytest.importorskip("gi")
    from encore_client.clips.screen import ScreenCapture, ScreenRecorder
    index = _index(tmp_path, [])
    deleting = set()
    recorder = ScreenRecorder(index, may_commit=lambda clip_id: clip_id not in deleting,
                              capture_factory=lambda d: ScreenCapture(d, source=TEST_SOURCE))
    recorder.sessions.add("ses-now")
    assert recorder.turn_on() and recorder.on
    buffer = Path(recorder.directory.name)
    try:
        wait_for(app, lambda: (recorder.poll(), len(recorder.video) >= 3)[1], timeout=10)
        first = recorder.video[0].start
        span = [[first + 300, first + 1_200], [first + 1_250, first + 2_400]]
        index.write_text(json.dumps({"clips": [
            {"clip_id": "mine", "session_id": "ses-now", "status": "ready", "coverage_ms": span,
             "path": "mine.webm", "temp_paths": ["mine.thumb.jpg"], "event_type": "gasp",
             "timecode": "00:00:01", "intensity": 0.5},
            {"clip_id": "deleting", "session_id": "ses-now", "status": "ready", "coverage_ms": span,
             "event_type": "gasp", "timecode": "00:00:01", "intensity": 0.5},
            {"clip_id": "other-run", "session_id": "ses-before", "status": "ready", "coverage_ms": span,
             "event_type": "gasp", "timecode": "00:00:01", "intensity": 0.5},
            {"clip_id": "waiting", "session_id": "ses-now", "status": "pending",
             "window_ms": [first, first + 1_000], "event_type": "gasp", "timecode": "00:00:01", "intensity": 0.5},
        ]}))
        deleting.add("deleting")

        def screen_of(clip_id):
            recorder.poll()
            return next(r for r in json.loads(index.read_text())["clips"] if r["clip_id"] == clip_id).get("screen")
        wait_for(app, lambda: screen_of("mine") is not None, timeout=20)
        rows = {r["clip_id"]: r for r in json.loads(index.read_text())["clips"]}
        screen = rows["mine"]["screen"]
        assert screen["path"] == "mine.screen.webm"
        assert screen["coverage_ms"][0][0] <= span[0][0] and screen["coverage_ms"][-1][1] >= span[-1][1]
        assert rows["mine"]["temp_paths"] == ["mine.thumb.jpg", "mine.screen.webm"], "deleted with the clip"
        assert (index.parent / "mine.screen.webm").stat().st_size > 0
        assert "screen" not in rows["deleting"] and "screen" not in rows["other-run"]
        assert "screen" not in rows["waiting"]
        assert next(c for c in load_index(index) if c.clip_id == "mine").screen
    finally:
        recorder.turn_off()
    assert not recorder.on and not buffer.exists(), "turning it off deletes the buffer"
    assert (index.parent / "mine.screen.webm").exists(), "screen files already cut stay"


def test_the_buffer_keeps_only_recent_screen_and_what_clips_need(tmp_path):
    from encore_client.clips.capture import Segment
    from encore_client.clips.screen import ScreenRecorder
    now = [100_000]
    recorder = ScreenRecorder(_index(tmp_path, []), clock=lambda: now[0])
    recorder._rows = [{"clip_id": "w", "session_id": "s", "status": "pending", "window_ms": [20_000, 21_000]}]
    for start in range(0, 100_000, 1_000):
        path = tmp_path / f"f{start}.webm"
        path.write_bytes(b"x")
        recorder.video.append(Segment(path, start, start + 1_000, 1))
    recorder._evict()
    kept = sorted(s.start for s in recorder.video)
    # the pending clip with its 5 s margins (15-26 s), and the last 35 s
    assert kept == [*range(15_000, 26_000, 1_000), *range(64_000, 100_000, 1_000)]
    assert not (tmp_path / "f0.webm").exists() and (tmp_path / "f15000.webm").exists()


# ---- the Screen switch ------------------------------------------------------------

class FakeRecorder:
    def __init__(self):
        self.on = False
        self.error = None
        self.sessions = set()
        self.polls = 0

    def turn_on(self):
        self.on = True
        return True

    def turn_off(self):
        self.on = False

    def poll(self):
        self.polls += 1


def test_the_switch_is_off_by_default_and_only_on_during_a_session(app):
    recorder = FakeRecorder()
    panel = ScreenPanel(recorder)
    assert panel.indicator.text() == "Screen: Off" and not panel.toggle_btn.isEnabled()
    panel.refresh(True)
    panel.toggle_btn.click()
    assert recorder.on and panel.indicator.text().startswith("● Screen: Recording")
    assert panel.toggle_btn.text() == "■ Stop screen recording"
    panel.toggle_btn.click()
    assert not recorder.on

    unavailable = ScreenPanel(FakeRecorder(), "screen recording needs an X11 desktop session")
    unavailable.refresh(True)
    assert not unavailable.toggle_btn.isEnabled() and "X11" in unavailable.detail.text()


def test_the_workspace_polls_it_and_end_turns_it_off(app, tmp_path):
    from encore_client.session.session import Session
    from encore_client.ui.workspace import Workspace
    session = Session("ws://127.0.0.1:1", "ses-screen")
    workspace = Workspace(session, None, tmp_path / "index.json")
    recorder = FakeRecorder()
    try:
        workspace.add_screen_recording(recorder)
        workspace.refresh()
        assert recorder.polls == 1 and recorder.sessions == {"ses-screen"}
        assert not workspace.screen_panel.toggle_btn.isEnabled(), "no session running"
        recorder.on = True
        workspace.refresh()
        assert not recorder.on, "off whenever no session runs"
        recorder.on = True
        workspace.end_session()
        assert not recorder.on
    finally:
        workspace.shutdown()
        workspace.close()


# ---- the Screen view and the switch -------------------------------------------------

@pytest.fixture
def index(tmp_path):
    """clip_001 (25 s) is the camera clip; the 18 s clip_002 file stands in for its
    screen recording, which started 3 s into the camera clip."""
    data = json.loads((ROOT / "samples/clip_index.json").read_text())
    for row in data["clips"]:
        if row["clip_id"] == "clip_001":
            row["coverage_ms"] = [[100_000, 125_000]]
            row["screen"] = {"path": "clips/clip_002_partial.mp4", "coverage_ms": [[103_000, 121_000]]}
    (tmp_path / "clip_index.json").write_text(json.dumps(data))
    shutil.copytree(ROOT / "samples/clips", tmp_path / "clips")
    return tmp_path / "clip_index.json"


def stage_with(app, index):
    stage = VideoStage(QWidget())
    clips = ClipPanel(index)
    stage.attach_clip_panel(clips)
    stage.resize(800, 400)
    stage.show()
    app.processEvents()
    return stage, clips


def play(app, clips, clip_id):
    clips.list.setCurrentRow(next(i for i, c in enumerate(clips.clips) if c.clip_id == clip_id))
    clips.play_button.click()
    wait_for(app, lambda: clips.player.playbackState() == PLAYING and clips.player.duration() > 0)


def test_tabs_or_side_by_side(app, index):
    stage, clips = stage_with(app, index)
    try:
        play(app, clips, "clip_001")
        assert stage.view() == "camera"
        assert clips.video.isVisible() and not stage.screen.isVisible()
        stage.view_tabs.setCurrentIndex(1)
        assert stage.view() == "screen"
        assert stage.screen.isVisible() and not clips.video.isVisible()
        stage.side_by_side.setChecked(True)
        assert stage.view() == "side by side"
        assert stage.screen.isVisible() and clips.video.isVisible() and not stage.view_tabs.isVisible()
        assert stage.side_by_side.text() == "Tabs", "the button says how to go back"
        stage.side_by_side.setChecked(False)
        assert stage.view() == "screen" and stage.view_tabs.isVisible()

        stage.back_btn.click()
        assert not stage.showing_clip() and not stage.screen.active
        assert stage.screen.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
        play(app, clips, "clip_001")
        assert stage.view() == "screen", "the choice stays for the next clip"
    finally:
        stage.show_film()


def test_the_screen_follows_the_clip(app, index):
    stage, clips = stage_with(app, index)
    screen = stage.screen
    try:
        play(app, clips, "clip_001")
        assert screen.active
        assert screen.message.text() == screen_view.NOT_HERE, "the screen was not recorded in the first 3 s"
        clips.seek(5_000)
        wait_for(app, lambda: screen.showing_screen() and screen.player.playbackState() == PLAYING)
        wait_for(app, lambda: abs(screen.player.position() - (clips.player.position() - 3_000)) < 400)

        clips.toggle()   # pausing the clip pauses the screen on the same moment
        wait_for(app, lambda: screen.player.playbackState() == PAUSED)
        clips.seek(10_000)
        wait_for(app, lambda: abs(screen.player.position() - 7_000) < 100)
    finally:
        stage.show_film()


def test_the_screen_says_why_it_has_nothing_to_show(app, index):
    stage, clips = stage_with(app, index)
    screen = stage.screen
    try:
        play(app, clips, "clip_002")   # recorded with the Screen switch off
        assert not screen.showing_screen() and screen.message.text() == screen_view.NOT_RECORDED

        play(app, clips, "clip_001")
        clips.playing_clip = replace(clips.playing_clip, screen=(ScreenSegment((0, 25_000), "gone.webm", 0),))
        clips.seek(4_000)
        wait_for(app, lambda: screen.message.text() == screen_view.MISSING.format(name="gone.webm"))
    finally:
        stage.show_film()


def test_screen_files_are_deleted_with_their_clip(tmp_path):
    from encore_client.clips.deletion import DeletionManager
    index = _index(tmp_path, [{"clip_id": "c", "status": "ready", "path": "c.webm",
                               "temp_paths": ["c.screen.webm"], "coverage_ms": [[0, 1_000]],
                               "screen": {"path": "c.screen.webm", "coverage_ms": [[0, 1_000]]}}])
    for name in ("c.webm", "c.screen.webm"):
        (index.parent / name).write_bytes(b"x")
    DeletionManager(index).delete("c")
    assert not (index.parent / "c.screen.webm").exists() and not (index.parent / "c.webm").exists()
