"""Microphone audio upload (contracts/README.md, "Audio frames"; R1, R2, R8, R9).

- The WebSocket carries binary frames.
- Session sends audio only while the microphone is on, numbered per
  generation, and drops audio captured while disconnected instead of
  queueing it.
- The WAV stand-in microphone releases chunks in real time with their
  capture times.
- Audio from a WAV file reaches the fake server byte for byte.
"""

import json
import math
import socket
import struct
import threading
import time
import wave

import pytest
from jsonschema import Draft202012Validator

from encore_client.__main__ import main
from encore_client.sensing.wav_mic import WavMicrophone
from encore_client.session import ws
from encore_client.session.contract import SAMPLES_DIR, decode_audio_frame
from encore_client.session.fake_server import FakeServer
from encore_client.session.session import Conn, ScenarioDriver, Session, load_scenario

SCHEMA = json.loads((SAMPLES_DIR.parent / "schema" / "messages.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
SAMPLE = load_scenario(SAMPLES_DIR / "01_single_event.json")
SID = SAMPLE["session_id"]


def tone(seconds, rate=16000):
    """A 440 Hz tone as PCM16: not silence, so a byte comparison means something."""
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n)))


def write_wav(path, pcm, rate=16000, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


class FakeTransport:
    def __init__(self, url):
        self.pending, self.sent = [], []

    def start(self):
        pass

    def send(self, data):
        self.sent.append(data)
        return True

    def poll(self):
        out, self.pending = self.pending, []
        return out

    def close(self):
        pass


def make_session(*, connect=True, **kw):
    transports = []

    def factory(url):
        transports.append(FakeTransport(url))
        return transports[-1]

    session = Session("ws://test", SID, transport_factory=factory, **kw)
    session.start()
    if connect:
        transports[0].pending.append(("open", None))
        session.poll()
    return session, transports[0]


def audio_frames(transport):
    return [decode_audio_frame(d) for d in transport.sent if isinstance(d, bytes)]


# ---- WebSocket ---------------------------------------------------------------------


def test_binary_messages_round_trip():
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def echo():
        sock, _ = listener.accept()
        conn, _ = ws.accept(sock)
        for _ in range(2):
            conn.send(conn.recv())
        conn.close()

    threading.Thread(target=echo, daemon=True).start()
    client = ws.connect(f"ws://127.0.0.1:{port}/")
    big = bytes(range(256)) * 200  # needs the 16-bit length form, and is masked by the client
    client.send(big)
    assert client.recv() == big
    client.send("text stays text")
    assert client.recv() == "text stays text"
    client.close()
    listener.close()


# ---- Session -----------------------------------------------------------------------


def test_audio_flows_only_while_the_microphone_is_on():
    session, transport = make_session()
    pcm = tone(0.5)
    assert session.send_audio(pcm, 1_000) is False  # microphone off: nothing captured or sent
    assert audio_frames(transport) == []

    session.set_sensor("microphone", True)
    assert session.send_audio(pcm, 1_000)
    assert session.send_audio(pcm, 1_500)
    session.set_sensor("microphone", False)
    assert session.send_audio(pcm, 2_000) is False
    session.set_sensor("microphone", True)
    assert session.send_audio(pcm, 3_000)

    frames = audio_frames(transport)
    assert [(h["generation"], h["chunk"]) for h, _ in frames] == [(1, 0), (1, 1), (3, 0)]
    assert all(p == pcm for _, p in frames)
    assert [h["capture_ms"] for h, _ in frames] == [1_000, 1_500, 3_000]
    for header, _ in frames:
        assert [e.message for e in VALIDATOR.iter_errors(header)] == []
    # One seq counter for everything the PC sends.
    seqs = [json.loads(d)["seq"] if isinstance(d, str) else decode_audio_frame(d)[0]["seq"]
            for d in transport.sent]
    assert seqs == list(range(len(seqs)))
    assert session.view.rejected == {}
    assert (session.view.audio_chunks, session.audio_sent) == (3, 3)


def test_audio_captured_while_disconnected_is_dropped_not_queued():
    session, transport = make_session(connect=False, microphone_on_start=True)
    assert session.send_audio(tone(0.5), 1_000) is False
    assert session.audio_dropped == 1
    assert [m["kind"] for m in session.unsent] == ["control_update"]  # controls wait, audio does not
    transport.pending.append(("open", None))
    session.poll()
    assert session.send_audio(tone(0.5), 1_500)
    (header, _), = audio_frames(transport)
    assert header["chunk"] == 1  # chunk 0 was lost: the server sees the gap
    assert session.view.audio_gaps == 1


def test_audio_cannot_go_as_a_text_message_or_in_bad_sizes():
    session, _ = make_session(microphone_on_start=True)
    with pytest.raises(ValueError):
        session.send({"kind": "audio_chunk", "session_id": SID})
    for bad in (b"", b"\x00", bytes(32_002)):
        with pytest.raises(ValueError):
            session.send_audio(bad, 1_000)


def test_a_binary_message_from_the_server_is_counted_as_bad():
    session, transport = make_session()
    transport.pending.append(("message", b"\x00\x01"))
    session.poll()
    assert session.bad_messages == 1


# ---- WAV stand-in microphone ------------------------------------------------------


def test_wav_microphone_releases_chunks_in_real_time(tmp_path):
    pcm = tone(1.3)
    now = [10_000]
    session, transport = make_session()
    mic = WavMicrophone(session, write_wav(tmp_path / "in.wav", pcm), clock=lambda: now[0])

    assert mic.pump() == 0  # microphone off: the file does not advance
    assert mic.pos == 0
    session.set_sensor("microphone", True)
    assert mic.pump() == 0  # capture starts now; nothing complete yet
    now[0] += 499
    assert mic.pump() == 0
    now[0] += 1
    assert mic.pump() == 1  # first 0.5 s chunk complete
    now[0] += 2_000
    assert mic.pump() == 2  # 0.5 s, then the last 0.3 s
    assert mic.done

    frames = audio_frames(transport)
    assert [h["capture_ms"] for h, _ in frames] == [10_000, 10_500, 11_000]
    assert [h["samples"] for h, _ in frames] == [8_000, 8_000, 4_800]
    assert all(h["aec"] == "off" for h, _ in frames)
    assert b"".join(p for _, p in frames) == pcm


def test_wav_microphone_pauses_while_the_microphone_is_off(tmp_path):
    now = [0]
    session, transport = make_session(microphone_on_start=True)
    mic = WavMicrophone(session, write_wav(tmp_path / "in.wav", tone(1.0)), clock=lambda: now[0])
    mic.pump()
    now[0] += 500
    mic.pump()
    session.set_sensor("microphone", False)
    now[0] += 5_000
    assert mic.pump() == 0
    session.set_sensor("microphone", True)
    mic.pump()
    now[0] += 500
    mic.pump()
    frames = audio_frames(transport)
    assert [(h["generation"], h["chunk"], h["capture_ms"]) for h, _ in frames] == [
        (1, 0, 0), (3, 0, 5_500)]


def test_wav_microphone_needs_the_contract_format(tmp_path):
    session, _ = make_session()
    stereo = write_wav(tmp_path / "stereo.wav", tone(0.5, rate=48_000) * 2, rate=48_000, channels=2)
    with pytest.raises(ValueError, match="ffmpeg"):
        WavMicrophone(session, stereo)


# ---- end to end ----------------------------------------------------------------------


def test_wav_audio_reaches_the_fake_server_intact(tmp_path):
    pcm = tone(1.3)
    out = tmp_path / "received.wav"
    server = FakeServer(SAMPLE, port=0, interval_s=0, save_audio=out).start()
    session = Session(server.url, SID)
    driver = ScenarioDriver(session, SAMPLE)  # turns the microphone on, as the sample does
    now = [0]
    mic = WavMicrophone(session, write_wav(tmp_path / "in.wav", pcm), clock=lambda: now[0])
    session.start()
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline and not (driver.done and mic.done and server.audio_chunks == 3):
            session.poll()
            driver.pump()
            mic.pump()
            now[0] += 100
            time.sleep(0.005)
    finally:
        session.end()
        server.stop()
    assert session.state is Conn.ENDED
    assert (server.audio_chunks, server.bad_frames, session.audio_dropped) == (3, 0, 0)
    assert session.view.summary() == SAMPLE["expected"]
    with wave.open(str(out), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.readframes(w.getnframes()) == pcm


def test_cli_streams_a_wav_in_demo_mode(tmp_path):
    log = tmp_path / "delivery.jsonl"
    wav = write_wav(tmp_path / "in.wav", tone(1.0))
    code = main(["--demo", "--headless", "--scenario", str(SAMPLES_DIR / "01_single_event.json"),
                 "--mic-wav", str(wav), "--log", str(log), "--timeout", "10"])
    assert code == 0
    sent = [json.loads(line)["msg"] for line in log.read_text().splitlines()
            if json.loads(line)["what"] == "out"]
    assert [m["chunk"] for m in sent if m["kind"] == "audio_chunk"] == [0, 1]


def test_cli_refuses_a_file_that_is_not_a_wav(tmp_path):
    bad = tmp_path / "notes.wav"
    bad.write_text("not audio")
    with pytest.raises(SystemExit) as exc:
        main(["--demo", "--headless", "--mic-wav", str(bad)])
    assert exc.value.code == 2
