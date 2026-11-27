"""Saved clips play in the player window's big video area, over the film (ui/clip_stage.py)."""
import os
import shutil
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication, QWidget

from encore_client.__main__ import ROOT
from encore_client.clips.panel import ClipPanel
from encore_client.ui.clip_stage import VideoStage


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def index(tmp_path):
    shutil.copy(ROOT / "samples/clip_index.json", tmp_path / "clip_index.json")
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


def stage_with(app, index):
    film = QWidget()
    stage = VideoStage(film)
    clips = ClipPanel(index)
    stage.attach_clip_panel(clips)
    stage.resize(640, 400)
    stage.show()
    clips.show()
    app.processEvents()
    return stage, film, clips


def test_the_clip_video_moves_out_of_the_panel_and_the_film_shows_first(app, index):
    stage, film, clips = stage_with(app, index)
    try:
        assert clips.video.parent() is stage.clip_page
        assert clips.video not in clips.findChildren(type(clips.video))
        assert stage.currentWidget() is film and not stage.showing_clip()
    finally:
        clips.player.stop()


def test_a_playing_clip_covers_the_film_and_back_returns_to_it(app, index):
    stage, film, clips = stage_with(app, index)
    try:
        row = next(i for i, c in enumerate(clips.clips) if c.status.value == "ready")
        clips.list.setCurrentRow(row)
        clips.play_button.click()
        wait_for(app, lambda: clips.player.position() > 100)
        assert stage.showing_clip() and clips.video.isVisible()
        assert stage.clip_title.text().startswith("Reaction clip: laughter · 00:12:05 · Sample Movie")
        assert "Ready" in stage.clip_title.text()

        stage.back_btn.click()
        wait_for(app, lambda: not stage.showing_clip())
        assert clips.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
        assert stage.currentWidget() is film
    finally:
        clips.player.stop()


def test_stop_clip_and_a_paused_clip(app, index):
    stage, film, clips = stage_with(app, index)
    try:
        clips.player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.PausedState)
        assert stage.showing_clip(), "a paused clip stays on screen"
        clips.player.playbackStateChanged.emit(QMediaPlayer.PlaybackState.StoppedState)
        wait_for(app, lambda: stage.currentWidget() is film)
    finally:
        clips.player.stop()


def test_the_end_of_the_file_pauses_on_its_last_frame_until_stop_clip(app, index):
    stage, film, clips = stage_with(app, index)
    try:
        row = next(i for i, c in enumerate(clips.clips) if c.status.value == "partial")
        clips.list.setCurrentRow(row)
        clips.play_button.click()   # no trim_ms: the whole 18 s file
        wait_for(app, lambda: clips.player.duration() > 0 and clips.player.position() > 100)
        clips.seek(clips.player.duration() - 400)
        wait_for(app, lambda: clips.player.playbackState() == QMediaPlayer.PlaybackState.PausedState)
        wait_for(app, lambda: clips.player.position() > clips.player.duration() - 500)
        for _ in range(20):
            app.processEvents()
        assert stage.showing_clip(), "the clip stays on its last frame"

        clips.toggle()   # Play at the end starts again from the beginning
        wait_for(app, lambda: clips.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        assert clips.player.position() < 2_000

        clips.stop_button.click()
        assert stage.currentWidget() is film
        assert clips.player.playbackState() == QMediaPlayer.PlaybackState.StoppedState
    finally:
        clips.player.stop()
