"""Sprint 1 acceptance tests.  Run: python -m pytest client/tests -v"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clips import Clip, ClipStatus, effective_label, playable_path  # noqa: E402
from sensing_state import CameraController, CameraState  # noqa: E402


def make(status, path=None, gaps=None):
    return Clip("c1", "laughter", "00:01:00", 0.5, ClipStatus(status), path, gaps or [])


# ---------- Clips: no playback link for a file that does not exist ----------
def test_ready_with_file_is_playable(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"data")
    assert playable_path(make("ready", "a.mp4"), tmp_path) is not None


def test_partial_with_file_is_playable_and_shows_gap(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"data")
    clip = make("partial", "a.mp4", [[0, 7]])
    assert playable_path(clip, tmp_path) is not None
    assert "missing" in effective_label(clip, tmp_path)


def test_pending_is_not_playable_even_if_file_exists(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"data")       # temporary file still being assembled
    assert playable_path(make("pending", "a.mp4"), tmp_path) is None


def test_unavailable_is_not_playable(tmp_path):
    assert playable_path(make("unavailable"), tmp_path) is None


def test_ready_but_missing_file_is_not_playable(tmp_path):
    clip = make("ready", "missing.mp4")
    assert playable_path(clip, tmp_path) is None
    assert effective_label(clip, tmp_path) == "❌ File missing"


def test_empty_file_is_not_playable(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"")
    assert playable_path(make("ready", "a.mp4"), tmp_path) is None


# ---------- Camera: off by default, indicator follows device, stop control ----------
def test_camera_off_by_default():
    assert CameraController().state == CameraState.OFF


def test_camera_cannot_start_without_session():
    cam = CameraController()
    assert cam.request_enable(session_active=False) is False
    assert cam.state == CameraState.OFF


def test_indicator_follows_device_not_button():
    cam = CameraController()
    cam.request_enable(True)
    assert cam.state == CameraState.STARTING        # pressing the button alone is not ON
    cam.on_first_frame(cam.generation)
    assert cam.state == CameraState.ON


def test_stop_button_visible_until_fully_stopped():
    cam = CameraController()
    cam.request_enable(True)
    cam.on_first_frame(cam.generation)
    assert cam.show_stop_button
    cam.request_disable()
    assert cam.state == CameraState.STOPPING and cam.show_stop_button
    cam.on_stopped()
    assert cam.state == CameraState.OFF and not cam.show_stop_button


def test_late_frame_from_old_generation_ignored():
    cam = CameraController()
    cam.request_enable(True)
    old = cam.generation
    cam.request_disable()
    cam.on_stopped()
    cam.request_enable(True)
    cam.on_first_frame(old)                          # late callback from the previous generation
    assert cam.state == CameraState.STARTING


def test_device_lost_shows_error():
    cam = CameraController()
    cam.request_enable(True)
    cam.on_first_frame(cam.generation)
    cam.on_device_lost()
    assert cam.state == CameraState.ERROR and not cam.show_stop_button
