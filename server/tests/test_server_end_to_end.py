"""The real PC client against the real session server, over a local WebSocket (R2, R3, R8, R9, R11)."""

import json
import math
import struct
import time
import wave

import pytest

from client.tests.live_harness import microphone
from encore_client.session import ws
from encore_client.session.session import Conn, Session
from encore_server.models import LoudnessModel
from encore_server.server import EncoreServer, serve_in_thread

RATE = 16_000


def write_wav(path, parts):
    """parts: (seconds, amplitude) pairs; amplitude 0 is silence, otherwise a 440 Hz tone."""
    samples = []
    for seconds, amp in parts:
        n = int(seconds * RATE)
        samples += [round(amp * 32767 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(n)]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return sum(s for s, _ in parts)


@pytest.fixture
def server():
    app = EncoreServer(LoudnessModel(), idle_s=0.3)
    url, stop = serve_in_thread(app)
    yield app, url
    stop()


def wait(session, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.poll()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out; state {session.state.value}, summary {session.view.summary()}")


def anchor(capture_ms, position_ms):
    return {
        "schema": "encore.v1", "kind": "playback_anchor", "seq": 0, "capture_ms": capture_ms,
        "playback": {"epoch": 1, "content_id": "file:test", "title": "Test movie",
                     "position_ms": position_ms, "rate": 1.0, "state": "playing"},
    }


def play(session, wav_path, seconds, start_ms=10_000):
    """Stream the whole file as if ``seconds`` had passed since ``start_ms``."""
    mic, clock = microphone(session, wav_path, start_ms)
    clock.play(mic, round(seconds * 1000))
    assert mic.done
    return mic


def open_session(url, sid):
    session = Session(url, sid, microphone_on_start=True)
    session.start()
    session.send_anchor(anchor(9_500, 60_000))  # 60 s into the movie, 0.5 s before the audio starts
    wait(session, lambda: session.state is Conn.CONNECTED)
    return session


def test_a_laugh_in_the_audio_comes_back_as_a_mapped_event(server, tmp_path):
    app, url = server
    seconds = write_wav(tmp_path / "laugh.wav", [(2, 0), (2, 0.5), (4, 0)])
    session = open_session(url, "ses-e2e-laugh")
    mic = play(session, tmp_path / "laugh.wav", seconds)
    wait(session, lambda: any(e["phase"] == "FINAL" for e in session.view.events.values()))

    assert session.view.rejected == {} and session.bad_messages == 0
    (event,) = session.view.events.values()
    assert event["reaction"]["type"] == "laughter"
    assert event["generations"] == {"microphone": 1, "camera": None}
    assert event["source"] == {"model": "loudness-standin", "config": "detector-v3", "aec": "off"}
    cap, pb = event["capture"], event["playback"]
    assert 11_000 <= cap["start_ms"] <= 12_000 and cap["end_ms"] >= 14_000  # the tone is at 12-14 s
    assert pb["status"] == "mapped"
    assert pb["timecode_start_ms"] == 60_000 + cap["start_ms"] - 9_500
    assert pb["timecode_end_ms"] == 60_000 + cap["end_ms"] - 9_500

    server_side = app.sessions["ses-e2e-laugh"]
    # FINAL can arrive before the trailing silence has finished uploading.
    wait(session, lambda: server_side.audio_chunks == mic.sent)
    assert server_side.audio_chunks == mic.sent == 16
    assert server_side.view.sensors["microphone"].generation == 1
    assert server_side.invalid == 0
    session.end()


def test_audio_that_stops_mid_laugh_is_finished_when_idle(server, tmp_path):
    app, url = server
    seconds = write_wav(tmp_path / "cut.wav", [(2, 0), (2, 0.5)])  # the file ends while loud
    session = open_session(url, "ses-e2e-idle")
    play(session, tmp_path / "cut.wav", seconds)
    wait(session, lambda: any(e["phase"] == "FINAL" for e in session.view.events.values()))
    assert session.view.rejected == {}
    session.end()


def test_microphone_off_finishes_the_event_and_the_ack_comes_back(server, tmp_path):
    app, url = server
    seconds = write_wav(tmp_path / "loud.wav", [(2, 0), (2, 0.5)])
    session = open_session(url, "ses-e2e-micoff")
    play(session, tmp_path / "loud.wav", seconds)
    wait(session, lambda: session.view.events)
    session.set_sensor("microphone", False, capture_ms=14_000)
    wait(session, lambda: any(e["phase"] == "FINAL" for e in session.view.events.values()))
    assert session.view.rejected == {}
    assert session.view.sensors["microphone"].generation == 2
    session.end()


def test_a_reconnecting_pc_is_sent_the_results_after_its_cursor(server, tmp_path):
    app, url = server
    seconds = write_wav(tmp_path / "laugh.wav", [(2, 0), (2, 0.5), (4, 0)])
    first = open_session(url, "ses-e2e-replay")
    play(first, tmp_path / "laugh.wav", seconds)
    wait(first, lambda: any(e["phase"] == "FINAL" for e in first.view.events.values()))
    first.end()
    log = app.sessions["ses-e2e-replay"].log
    assert len(log) >= 2

    # R11: a PC that already has cursor 1 reconnects and is sent the rest, in order.
    conn = ws.connect(url + "/v1/sessions/ses-e2e-replay/stream?after=1")
    try:
        got = [json.loads(conn.recv()) for _ in range(len(log) - 1)]
    finally:
        conn.close(1001, "test done")
    assert got == log[1:]


def test_unknown_paths_are_refused(server):
    _, url = server
    with pytest.raises(ws.HandshakeError):
        ws.connect(url + "/v1/sessions/x/stream")  # session id too short
    with pytest.raises(ws.HandshakeError):
        ws.connect(url + "/health")
