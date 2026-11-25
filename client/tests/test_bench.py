"""Test bench window: file and server choices, live scores, events through the real contract path."""

import importlib.util
import math
import shutil
import struct
import subprocess
import time
import wave
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from encore_client.bench.local import prepare_audio
from encore_client.bench.window import BenchWindow
from encore_client.sensing.wav_mic import WavMicrophone
from encore_client.session.session import Conn, Session, Transport, WsTransport

RATE = 16_000


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "bench condition timed out"


def write_wav(path, parts, rate=RATE, channels=1):
    """parts: (seconds, amplitude); amplitude 0 is silence, otherwise a 440 Hz tone."""
    samples = []
    for seconds, amp in parts:
        for i in range(int(seconds * rate)):
            v = round(amp * 32767 * math.sin(2 * math.pi * 440 * i / rate))
            samples += [v] * channels
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def finals(win):
    return [e for e in win.session.view.events.values() if e["phase"] == "FINAL"]


def run_file(app, win, path, speed=16):
    assert win.open_file(path)
    win.speed_box.setCurrentIndex(win.speed_box.findData(speed))
    wait_for(app, lambda: win.start_btn.isEnabled())  # the model is loaded
    assert win.start()
    wait_for(app, lambda: win.session.state is Conn.ENDED)


def test_a_file_with_a_loud_burst_gives_scores_and_a_mapped_event(app, tmp_path):
    win = BenchWindow(model="loudness")
    try:
        run_file(app, win, write_wav(tmp_path / "burst.wav", [(2, 0), (2, 0.5), (3, 0)]))
        (event,) = finals(win)
        assert event["reaction"]["type"] == "laughter"
        assert event["source"]["model"] == "loudness-standin" and event["source"]["config"] == "detector-v2"
        start = event["capture"]["start_ms"] - win._origin
        assert 1_000 <= start <= 2_000  # the burst is at 2-4 s in the file
        assert event["playback"]["timecode_start_ms"] == start  # content time = position in the file
        assert win.session.view.rejected == {}
        assert len(win.plot.frames) >= 10  # scores came from the built-in server
        assert max(s["laughter"] for _, s, _ in win.plot.frames) == 1.0
        assert win.table.rowCount() == 1 and win.table.item(0, 1).text() == "FINAL"
        assert "PC rejected nothing" in win.counts.text()
        assert win.start_btn.isEnabled() and not win.stop_btn.isEnabled()
    finally:
        win.close()


def test_file_end_waits_for_the_last_audio_write(app, tmp_path):
    win = BenchWindow()
    try:
        win.timer.stop()
        session = Session("ws://unused")
        session.set_sensor("microphone", True)
        session.state = Conn.CONNECTED
        transport = Mock(spec_set=WsTransport)
        transport.take_dropped.return_value = 0
        transport.poll.return_value = []
        transport.send.return_value = True
        transport.media_pending = True
        session._transport = transport
        win.session = session
        win._began = True
        now = [10_000]
        wav = write_wav(tmp_path / "tail.wav", [(0.5, 0.5)])
        win.source = WavMicrophone(session, wav, clock=lambda: now[0])
        win.tick()
        now[0] += 500
        win.tick()
        assert win.source.done and win.source.sent == 1
        assert session.view.sensors["microphone"].enabled

        transport.media_pending = False
        win.tick()
        assert not session.view.sensors["microphone"].enabled
    finally:
        win.close()


def test_changed_trigger_levels_are_named_in_the_results(app, tmp_path):
    win = BenchWindow(model="loudness")
    try:
        win.trigger_boxes["laughter"].setValue(0.9)
        run_file(app, win, write_wav(tmp_path / "burst.wav", [(2, 0), (2, 0.5), (3, 0)]))
        (event,) = finals(win)
        assert event["source"]["config"] == "bench gasp=0.30 laughter=0.90 scream=0.40"
        assert win.plot.triggers["laughter"] == 0.9
    finally:
        win.close()


def test_stop_finishes_an_open_event(app, tmp_path):
    win = BenchWindow(model="loudness")
    try:
        assert win.open_file(write_wav(tmp_path / "long.wav", [(2, 0), (30, 0.5)]))
        wait_for(app, lambda: win.start_btn.isEnabled())
        assert win.start()
        wait_for(app, lambda: win.session.view.events)  # OPEN while the tone goes on
        win.stop()
        wait_for(app, lambda: win.session.state is Conn.ENDED)
        assert len(finals(win)) == 1 and win.session.view.rejected == {}
    finally:
        win.close()


def test_an_external_server_shows_events_without_scores(app, tmp_path):
    from encore_server.models import LoudnessModel
    from encore_server.server import EncoreServer, serve_in_thread

    url, stop = serve_in_thread(EncoreServer(LoudnessModel()))
    win = BenchWindow(server_url=url)
    try:
        assert win.remote_radio.isChecked()
        run_file(app, win, write_wav(tmp_path / "burst.wav", [(2, 0), (2, 0.5), (3, 0)]))
        assert len(finals(win)) == 1
        assert len(win.plot.frames) == 0 and "only with the built-in server" in win.plot.note
    finally:
        win.close()
        stop()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_other_audio_formats_are_converted(tmp_path):
    src = write_wav(tmp_path / "cd.wav", [(1, 0.5)], rate=44_100, channels=2)
    out = prepare_audio(src, tmp_path)
    with wave.open(str(out)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (RATE, 1, 2)
        assert abs(w.getnframes() - RATE) < 200
    mp3 = tmp_path / "clip.mp3"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src), str(mp3)], check=True)
    assert prepare_audio(mp3, tmp_path).suffix == ".wav"


def test_files_already_in_the_contract_format_are_used_as_they_are(tmp_path):
    src = write_wav(tmp_path / "ok.wav", [(1, 0)])
    assert prepare_audio(src, tmp_path) == src


def test_unreadable_files_are_reported_in_the_window(app, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not audio")
    win = BenchWindow()
    try:
        assert not win.open_file(bad)
        assert "notes.txt" in win.status.text()
        win.file_radio.setChecked(True)
        win.refresh()
        assert not win.start_btn.isEnabled()
    finally:
        win.close()


def test_the_level_meter_ignores_a_dc_offset():
    from encore_client.bench.window import _db

    tone = [round(8_000 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(RATE)]
    assert abs(_db(struct.pack(f"<{RATE}h", *tone)) - _db(struct.pack(f"<{RATE}h", *(v + 6_700 for v in tone)))) < 0.01


def test_a_recording_is_saved_as_contract_format_wav(app, tmp_path):
    win = BenchWindow()
    try:
        win.recording = bytes(RATE * 2)  # one second
        win.save_recording(tmp_path / "rec.wav")
        with wave.open(str(tmp_path / "rec.wav")) as w:
            assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (RATE, 1, RATE)
    finally:
        win.close()


@pytest.mark.skipif(importlib.util.find_spec("tensorflow_hub") is not None, reason="TensorFlow is installed")
def test_yamnet_without_tensorflow_explains_what_to_install(app):
    win = BenchWindow(model="yamnet")
    try:
        wait_for(app, lambda: win.models.state("yamnet")[0] == "error")
        win.refresh()
        assert "requirements-yamnet.txt" in win.model_state.text()
        assert not win.start_btn.isEnabled()
    finally:
        win.close()


@pytest.mark.parametrize("disconnect", [True, False])
def test_capture_stops_on_disconnect_or_stop_and_recording_is_preserved(app, disconnect):
    from encore_client.sensing.live_mic import LiveMicrophone

    win = BenchWindow()
    try:
        win.timer.stop()
        session = Session("ws://127.0.0.1:1")
        session.set_sensor("microphone", True)
        session.state = Conn.CONNECTED
        session._transport = Mock(spec_set=Transport)
        session._transport.poll.return_value = [("closed", "connection lost")] if disconnect else []
        win.session = session
        win.source = LiveMicrophone(session)
        win.source.recording.extend(b"\x01\x00" * 8000)
        win._began = True
        device = win.qt_input = Mock()
        if disconnect:
            win.tick()
            assert session.state is Conn.DISCONNECTED  # keep the failure visible
        else:
            win.stop()
            assert win._stop_at is not None  # FINAL grace period continues without capturing
        device.stop.assert_called_once()
        assert win.qt_input is None
        assert not session.view.sensors["microphone"].enabled
        before = bytes(win.source.recording)
        win.source.feed(bytes(16000))  # an already queued device callback
        assert win.recording == before == bytes(win.source.recording)
        win.stop()  # safe even after a disconnect / repeated Stop
        device.stop.assert_called_once()
        if disconnect:
            assert win.save_btn.isEnabled()
            assert "connection lost" in win.status.text()
    finally:
        win.close()
