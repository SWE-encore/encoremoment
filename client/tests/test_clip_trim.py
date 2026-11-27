"""A clip's chosen part between its hidden editing margins: playback, the
borders under the progress bar, and saving them (clips/panel.py, ui/trim_bar.py)."""
import json
import os
import shutil
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from encore_client.__main__ import ROOT
from encore_client.clips.model import load_index
from encore_client.clips.panel import MAX_CLIP_MS, MIN_CLIP_MS, ClipPanel
from encore_client.ui.clip_stage import VideoStage
from encore_client.ui.trim_bar import make_trim_bar

PLAYING = QMediaPlayer.PlaybackState.PlayingState
PAUSED = QMediaPlayer.PlaybackState.PausedState
TRIM = [5_000, 20_000]   # the 25 s sample file: 5 s margin, 15 s chosen, 5 s margin


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def index(tmp_path):
    data = json.loads((ROOT / "samples/clip_index.json").read_text())
    for row in data["clips"]:
        if row["clip_id"] == "clip_001":
            row["trim_ms"] = TRIM
    (tmp_path / "clip_index.json").write_text(json.dumps(data))
    shutil.copytree(ROOT / "samples/clips", tmp_path / "clips")
    return tmp_path / "clip_index.json"


def wait_for(app, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "UI condition timed out"


def play(app, clips, clip_id="clip_001"):
    clips.list.setCurrentRow(next(i for i, c in enumerate(clips.clips) if c.clip_id == clip_id))
    clips.play_button.click()
    wait_for(app, lambda: clips.player.duration() > 0 and clips.player.playbackState() == PLAYING)


def row_of(index, clip_id="clip_001"):
    return next(r for r in json.loads(index.read_text())["clips"] if r["clip_id"] == clip_id)


def test_playback_covers_the_chosen_part_and_pauses_at_its_end(app, index):
    clips = ClipPanel(index)
    try:
        play(app, clips)
        assert clips.play_range() == tuple(TRIM)
        wait_for(app, lambda: clips.player.position() >= TRIM[0])
        clips.seek(12_000)   # a seek is not pulled back to the start border
        wait_for(app, lambda: clips.player.position() >= 12_000)
        app.processEvents()
        assert clips.player.position() >= 12_000

        clips.seek(TRIM[1] - 300)
        wait_for(app, lambda: clips.player.playbackState() == PAUSED)
        assert TRIM[1] <= clips.player.position() < TRIM[1] + 500

        clips.seek(23_000)   # the margin after the end border can be looked at...
        wait_for(app, lambda: clips.player.position() >= 23_000)
        clips.toggle()       # ...and Play from there starts at the start border
        wait_for(app, lambda: clips.player.playbackState() == PLAYING)
        assert TRIM[0] <= clips.player.position() < TRIM[0] + 1_000
        clips.toggle()
        assert clips.player.playbackState() == PAUSED
    finally:
        clips.player.stop()


def test_borders_move_into_the_margins_within_limits_and_are_saved(app, index):
    clips = ClipPanel(index)
    try:
        play(app, clips)
        duration = clips.player.duration()
        assert clips.move_border("start", 1_000) == (1_000, TRIM[1])
        assert clips.player.playbackState() == PAUSED, "dragging a border pauses on its frame"
        wait_for(app, lambda: abs(clips.player.position() - 1_000) < 100)
        assert clips.move_border("start", -500) == (0, TRIM[1])
        assert clips.move_border("start", TRIM[1]) == (TRIM[1] - MIN_CLIP_MS, TRIM[1])
        assert clips.move_border("start", 0) == (0, TRIM[1])
        assert clips.move_border("end", 99_999) == (0, duration)
        assert clips.move_border("end", 0) == (0, MIN_CLIP_MS)
        assert clips.move_border("end", 22_000) == (0, 22_000)

        assert clips.save_range()
        assert row_of(index)["trim_ms"] == [0, 22_000]
        assert row_of(index, "clip_002") == next(
            r for r in json.loads((ROOT / "samples/clip_index.json").read_text())["clips"]
            if r["clip_id"] == "clip_002"), "other rows are unchanged"
        assert next(c for c in load_index(index) if c.clip_id == "clip_001").trim_ms == (0, 22_000)
        assert row_of(index)["status"] == "ready", "the status stays as recorded"

        clips.player.stop()
        play(app, clips)     # the next play uses the saved part
        assert clips.play_range() == (0, 22_000)
    finally:
        clips.player.stop()


class FakePlayer:
    """A paused player of a 90 s file: longer than any sample clip."""
    def __init__(self):
        self.position = 0

    def duration(self):
        return 90_000

    def playbackState(self):  # noqa: N802 (Qt API)
        return PAUSED

    def setPosition(self, ms):  # noqa: N802 (Qt API)
        self.position = ms

    def stop(self):
        pass


def test_no_clip_is_longer_than_60_s(app, index):
    clips = ClipPanel(index)
    real, clips.player = clips.player, FakePlayer()
    try:
        clips._play_range = (10_000, 40_000)
        assert clips.move_border("end", 85_000) == (10_000, 10_000 + MAX_CLIP_MS)
        assert clips.player.position == 10_000 + MAX_CLIP_MS
        assert clips.move_border("start", 0) == (10_000, 10_000 + MAX_CLIP_MS)
        clips._play_range = (70_000, 80_000)
        assert clips.move_border("start", 0) == (80_000 - MAX_CLIP_MS, 80_000)
    finally:
        clips.player = real


def test_a_clip_being_deleted_or_not_in_the_index_is_not_saved(app, index):
    clips = ClipPanel(index)
    try:
        play(app, clips)
        clips.move_border("end", 22_000)
        data = json.loads(index.read_text())
        next(r for r in data["clips"] if r["clip_id"] == "clip_001")["status"] = "deletion_pending"
        index.write_text(json.dumps(data))
        assert not clips.save_range()
        assert row_of(index)["trim_ms"] == TRIM
        clips.playing_id = None
        assert not clips.save_range()
    finally:
        clips.player.stop()


def test_the_bar_greys_the_margins_and_the_lane_drags_a_border(app):
    host = QWidget()
    slider, lane, box = make_trim_bar(host)
    box.resize(400, 40)
    host.resize(400, 40)
    host.show()
    lane.show()
    app.processEvents()
    slider.setRange(0, 25_000)
    slider.set_chosen((5_000, 20_000))
    for value in (0, 5_000, 12_500, 25_000):
        assert abs(slider.value_for(slider.x_for(value)) - value) <= 25_000 / 300
    assert slider.x_for(0) < slider.x_for(5_000) < slider.x_for(20_000) < slider.x_for(25_000)
    assert not slider.grab().isNull() and not lane.grab().isNull()   # paints without errors

    moved, released = [], []
    lane.border_moved.connect(lambda border, value: moved.append((border, value)))
    lane.border_released.connect(lambda: released.append(True))
    y = lane.height() // 2
    start_x = round(lane.x_for(5_000))
    QTest.mousePress(lane, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(start_x, y))
    assert lane.dragging == "start"
    QTest.mouseMove(lane, QPoint(start_x - 40, y))
    QTest.mouseRelease(lane, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(start_x - 40, y))
    assert moved and all(border == "start" for border, _ in moved)
    assert moved[-1][1] < 5_000 and released == [True] and lane.dragging is None

    moved.clear()
    middle = round(lane.x_for(12_500))
    QTest.mousePress(lane, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(middle, y))
    QTest.mouseRelease(lane, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(middle, y))
    assert lane.dragging is None and not moved, "only the grips move a border"

    pressed = []
    slider.sliderPressed.connect(lambda: pressed.append(slider.value()))
    x = round(slider.x_for(18_000))
    QTest.mousePress(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(x, slider.height() // 2))
    QTest.mouseRelease(slider, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                       QPoint(x, slider.height() // 2))
    assert pressed and abs(pressed[0] - 18_000) < 25_000 / 100, "a click on the bar jumps there"
    host.close()


def test_player_window_controls_the_clip_while_it_shows(app, index):
    pytest.importorskip("gi")
    from encore_client.player.window import PlayerWindow
    win = PlayerWindow("ses-trim-test", "fakesink sync=true")
    clips = ClipPanel(index)
    win.stage.attach_clip_panel(clips)
    win.show()
    try:
        play(app, clips)
        wait_for(app, lambda: win.showing_clip() and win.slider.maximum() == clips.player.duration())
        assert win.slider.chosen == tuple(TRIM) and win.trim_lane.isVisible()
        assert not win.rate_box.isEnabled()
        assert win.time_label.text().endswith("/ 00:15")
        assert win.statusBar().currentMessage().startswith("Reaction clip:")

        win.toggle()     # Play/Pause (and Space) pause the clip, not the film
        assert clips.player.playbackState() == PAUSED and win.showing_clip()
        before = clips.player.position()
        win.step(5_000)  # and Right seeks it
        wait_for(app, lambda: clips.player.position() >= before + 4_000)

        win.trim_lane.border_moved.emit("end", 22_000)
        assert clips.play_range() == (TRIM[0], 22_000) and win.slider.chosen == (TRIM[0], 22_000)
        win.trim_lane.border_released.emit()
        assert row_of(index)["trim_ms"] == [TRIM[0], 22_000]
        assert win.statusBar().currentMessage() == "Clip length 17.0 s, saved."
        win.tick()
        assert win.time_label.text().endswith("/ 00:17")

        win.stage.back_btn.click()
        win.tick()
        assert not win.showing_clip() and clips.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
        assert win.rate_box.isEnabled() and win.slider.chosen is None and not win.trim_lane.isVisible()
        assert win.play_btn.text() == "Play"
    finally:
        clips.player.stop()
        win.close()
