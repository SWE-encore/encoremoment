"""Download clip: the chosen part of a saved clip as MP4, alone, as two files with
its screen recording, or with the camera as a box on the screen
(clips/download.py, ui/download_dialog.py)."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from encore_client.__main__ import ROOT
from encore_client.clips import download as dl
from encore_client.clips.panel import ClipPanel
from encore_client.clips.screen_map import ScreenSegment
from encore_client.ui import download_dialog
from encore_client.ui.clip_stage import VideoStage

PLAYING = QMediaPlayer.PlaybackState.PlayingState
needs_ffmpeg = pytest.mark.skipif(dl.unavailable_reason() is not None,
                                  reason=f"downloads need ffmpeg: {dl.unavailable_reason()}")
TRIM = [1_000, 9_000]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "UI condition timed out"


# ---- what gets cut ----------------------------------------------------------------

def test_the_screen_track_has_black_where_the_screen_was_not_recorded():
    seg = ScreenSegment((3_000, 10_000), "c.screen.webm", 500)
    assert dl.screen_pieces([seg], 1_000, 9_000) == (dl.Piece(2_000), dl.Piece(6_000, 500))
    # The last frame holds up to a second past the end, then black.
    assert dl.screen_pieces([seg], 8_000, 13_000) == (dl.Piece(3_000, 5_500), dl.Piece(2_000))
    # Starting in that held second shows the last frame, not past the file's end.
    assert dl.screen_pieces([seg], 10_500, 11_000) == (dl.Piece(500, 500 + 7_000 - dl.LAST_FRAME_MS),)
    two = [ScreenSegment((0, 2_000), "s", 0), ScreenSegment((2_500, 4_000), "s", 2_000)]
    assert dl.screen_pieces(two, 0, 4_000) == (dl.Piece(2_500, 0), dl.Piece(1_500, 2_000))
    assert dl.screen_pieces([], 0, 1_000) == (dl.Piece(1_000),)


def test_the_camera_box_stays_inside_the_screen_and_keeps_its_shape():
    camera, screen = (640, 360), (1280, 800)
    x, y, w, h = dl.Box(0.9, 0.9, 0.5).pixels(screen, camera)
    assert (w, h) == (640, 360) and x + w == 1280 and y + h == 800
    x, y, w, h = dl.Box(-1, -1, 0.01).pixels(screen, camera)
    assert (x, y) == (0, 0) and w == 128 and h == 72, "at least a tenth of the width"
    w, h = dl.Box(0, 0, 5).pixels(screen, camera)[2:]
    assert w == 1280 and h == 720, "no wider than the screen"
    assert dl.Box(0, 0, 1).pixels((640, 480), (640, 360))[2:] == (640, 360)
    assert all(v % 2 == 0 for v in dl.Box(0.3, 0.3, 0.333).pixels(screen, camera)[2:]), "even for H.264"


def test_names_and_the_chosen_part():
    clip = type("C", (), {"event_type": "laughter", "timecode": "00:12:05"})()
    assert dl.default_name(clip) == "encore-laughter-00-12-05"
    assert dl.output_paths(Path("/d/x.mp4"), dl.OVERLAY) == (Path("/d/x.mp4"),)
    assert dl.output_paths(Path("/d/x"), dl.TWO_FILES) == (Path("/d/x-camera.mp4"), Path("/d/x-screen.mp4"))
    assert dl.chosen_range((5_000, 20_000), 25_000) == (5_000, 20_000)
    assert dl.chosen_range((5_000, 30_000), 25_000) == (5_000, 25_000)
    assert dl.chosen_range((), 25_000) == (0, 25_000)


# ---- the files ----------------------------------------------------------------------

CAMERA_FILE = ROOT / "samples/clips/clip_001_ready.mp4"   # 25 s, 640x360, with sound
SCREEN_FILE = ROOT / "samples/clips/clip_002_partial.mp4"  # stands in for a screen recording


def pixels(path, seconds):
    """RGB rows of the frame at ``seconds``."""
    media = dl.probe(path)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(seconds), "-i", str(path),
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    return [raw[r * media.width:(r + 1) * media.width] for r in range(media.height)]


def mean(rows, x0, y0, x1, y1):
    values = [v for row in rows[y0:y1] for v in row[x0:x1]]
    return sum(values) / len(values)


@needs_ffmpeg
def test_each_kind_of_download_is_cut_exactly(tmp_path):
    camera, screen = dl.probe(CAMERA_FILE), dl.probe(SCREEN_FILE)
    pieces = dl.screen_pieces([ScreenSegment((3_000, 21_000), "s", 0)], *TRIM)   # 2 s black, then the screen

    job = dl.Job(CAMERA_FILE, *TRIM, dl.output_paths(tmp_path / "a.mp4", dl.CAMERA))
    dl.run(job, camera)
    got = dl.probe(tmp_path / "a.mp4")
    assert (got.width, got.height, got.audio, got.duration_ms) == (640, 360, True, 8_000)

    job = dl.Job(CAMERA_FILE, *TRIM, dl.output_paths(tmp_path / "b.mp4", dl.TWO_FILES), dl.TWO_FILES,
                 SCREEN_FILE, pieces)
    dl.run(job, camera, screen)
    cam, scr = dl.probe(tmp_path / "b-camera.mp4"), dl.probe(tmp_path / "b-screen.mp4")
    assert cam.duration_ms == scr.duration_ms == 8_000
    assert cam.audio and not scr.audio, "the screen's sound is not recorded"
    assert mean(pixels(tmp_path / "b-screen.mp4", 1), 0, 0, 640, 360) < 20, "black before the screen started"
    assert mean(pixels(tmp_path / "b-screen.mp4", 5), 0, 0, 640, 360) > 40

    box = dl.Box(0.5, 0.5, 0.4)
    job = dl.Job(CAMERA_FILE, *TRIM, dl.output_paths(tmp_path / "c.mp4", dl.OVERLAY), dl.OVERLAY,
                 SCREEN_FILE, pieces, box)
    dl.run(job, camera, screen)
    got = dl.probe(tmp_path / "c.mp4")
    assert (got.width, got.height, got.audio, got.duration_ms) == (640, 360, True, 8_000)
    x, y, w, h = box.pixels((640, 360), (640, 360))
    frame = pixels(tmp_path / "c.mp4", 1)
    assert mean(frame, 0, 0, x - 4, y - 4) < 20, "black screen around the box"
    assert mean(frame, x + 4, y + 4, x + w - 4, y + h - 4) > 40, "the camera inside it"
    assert not list(tmp_path.glob("*.part"))


@needs_ffmpeg
def test_a_failed_download_leaves_nothing(tmp_path):
    job = dl.Job(tmp_path / "missing.mp4", 0, 1_000, (tmp_path / "x.mp4",))
    with pytest.raises(OSError):
        dl.run(job, dl.Media(640, 360, 10, False, 1_000))
    assert list(tmp_path.iterdir()) == []


# ---- the dialog and the buttons ----------------------------------------------------

@pytest.fixture
def index(tmp_path):
    """clip_001 (25 s) with borders at 1-9 s; clip_002's file stands in for its
    screen recording, which started 3 s into the camera clip."""
    data = json.loads((ROOT / "samples/clip_index.json").read_text())
    for row in data["clips"]:
        if row["clip_id"] == "clip_001":
            row["trim_ms"] = TRIM
            row["coverage_ms"] = [[100_000, 125_000]]
            row["screen"] = {"path": "clips/clip_002_partial.mp4", "coverage_ms": [[103_000, 121_000]]}
    (tmp_path / "clip_index.json").write_text(json.dumps(data))
    shutil.copytree(ROOT / "samples/clips", tmp_path / "clips")
    return tmp_path / "clip_index.json"


def dialog_for(app, index, clip_id, out_dir):
    panel = ClipPanel(index)
    panel.list.setCurrentRow(next(i for i, c in enumerate(panel.clips) if c.clip_id == clip_id))
    dialog = panel.download_selected()
    dialog.ask_path = lambda suggested: str(out_dir / "mine.mp4")
    app.processEvents()
    return panel, dialog


@needs_ffmpeg
def test_two_files_or_the_camera_on_the_screen(app, index, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    panel, dialog = dialog_for(app, index, "clip_001", out)
    try:
        assert panel.download_button.isEnabled()
        assert dialog.has_screen() and dialog.mode() == dl.TWO_FILES
        assert (dialog.start_ms, dialog.end_ms) == tuple(TRIM)
        assert not dialog.placer.isVisible(), "the box only matters on the screen"
        assert dialog.save()
        wait_for(app, lambda: dialog.process is None)
        assert dialog.saved_paths == (out / "mine-camera.mp4", out / "mine-screen.mp4"), dialog.status.text()
        assert dl.probe(out / "mine-screen.mp4").duration_ms == 8_000

        dialog.overlay.setChecked(True)
        assert dialog.placer.isVisible() and dialog.mode() == dl.OVERLAY
        assert dialog.save()
        wait_for(app, lambda: dialog.process is None)
        assert dialog.saved_paths == (out / "mine.mp4",) and dl.probe(out / "mine.mp4").duration_ms == 8_000
        assert download_dialog.last_box == dialog.placer.box, "the next download starts from here"
        assert sorted(p.name for p in out.iterdir()) == ["mine-camera.mp4", "mine-screen.mp4", "mine.mp4"]
    finally:
        dialog.reject()


@needs_ffmpeg
def test_the_camera_box_is_dragged_and_resized(app, index, tmp_path):
    panel, dialog = dialog_for(app, index, "clip_001", tmp_path)
    try:
        dialog.overlay.setChecked(True)
        app.processEvents()   # the dialog fits itself first
        placer = dialog.placer
        placer.resize(640, 360)
        placer.set_box(dl.Box(0.1, 0.1, 0.25))
        start = placer.box_rect().center().toPoint()
        QTest.mousePress(placer, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(placer, start + QPoint(160, 90))
        QTest.mouseRelease(placer, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start + QPoint(160, 90))
        assert placer.box.x == pytest.approx(0.35, abs=0.01) and placer.box.y == pytest.approx(0.35, abs=0.01)

        corner = placer.handle_rect().center().toPoint()
        QTest.mousePress(placer, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, corner)
        QTest.mouseMove(placer, corner + QPoint(64, 0))
        QTest.mouseRelease(placer, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, corner + QPoint(64, 0))
        assert placer.box.width == pytest.approx(0.35, abs=0.01)
        assert dialog.size_slider.value() == 35, "the slider follows"

        dialog.size_slider.setValue(100)
        assert placer.box.width == 1.0 and (placer.box.x, placer.box.y) == (0.0, 0.0), "kept inside"
    finally:
        dialog.reject()


@needs_ffmpeg
def test_a_clip_without_screen_downloads_as_one_camera_video(app, index, tmp_path):
    panel, dialog = dialog_for(app, index, "clip_002", tmp_path)
    try:
        assert not dialog.has_screen() and dialog.mode() == dl.CAMERA
        assert "no screen recording" in dialog.note.text()
        assert dialog.save()
        wait_for(app, lambda: dialog.process is None)
        assert dialog.saved_paths == (tmp_path / "mine.mp4",)
        assert dl.probe(tmp_path / "mine.mp4").duration_ms == 18_000, "no borders: the whole file"
    finally:
        dialog.reject()


@needs_ffmpeg
def test_existing_files_are_only_replaced_when_confirmed_and_cancel_leaves_nothing(app, index, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    panel, dialog = dialog_for(app, index, "clip_001", out)
    try:
        (out / "mine-screen.mp4").write_bytes(b"older")
        asked = []
        dialog.confirm_replace = lambda paths: asked.append([p.name for p in paths]) or False
        assert not dialog.save() and asked == [["mine-screen.mp4"]]
        assert (out / "mine-screen.mp4").read_bytes() == b"older"

        (out / "mine-screen.mp4").unlink()
        assert dialog.save()
        dialog.cancel()
        assert dialog.process is None and list(out.iterdir()) == []
    finally:
        dialog.reject()


def test_the_buttons(app, index, monkeypatch):
    panel = ClipPanel(index)
    panel.list.setCurrentRow(next(i for i, c in enumerate(panel.clips) if c.status.value == "pending"))
    assert not panel.download_button.isEnabled(), "nothing to download yet"

    monkeypatch.setattr("encore_client.clips.panel.download_unavailable_reason", lambda: "no ffmpeg here")
    without = ClipPanel(index)
    without.list.setCurrentRow(next(i for i, c in enumerate(without.clips) if c.clip_id == "clip_001"))
    assert not without.download_button.isEnabled() and "no ffmpeg here" in without.download_button.toolTip()


@needs_ffmpeg
def test_the_playing_clip_downloads_with_its_borders_as_they_are(app, index):
    stage = VideoStage(QWidget())
    clips = ClipPanel(index)
    stage.attach_clip_panel(clips)
    stage.show()
    try:
        clips.list.setCurrentRow(next(i for i, c in enumerate(clips.clips) if c.clip_id == "clip_001"))
        clips.play_button.click()
        wait_for(app, lambda: clips.player.playbackState() == PLAYING and clips.player.duration() > 0)
        assert stage.download_btn.isEnabled()
        clips.move_border("end", 6_000)          # not saved to the index
        stage.download_btn.click()
        dialog = clips.download_dialog
        assert (dialog.start_ms, dialog.end_ms) == (TRIM[0], 6_000)
        dialog.reject()
    finally:
        stage.show_film()
