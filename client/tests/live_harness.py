"""Shared by the R11 tests: the real session server behind a TCP relay the test controls."""

import math
import socket
import struct
import threading
import time
import wave

from encore_client.player.anchors import AnchorTracker
from encore_client.sensing.wav_mic import CHUNK_MS, WavMicrophone
from encore_client.session.session import Session

RATE = 16_000
AUDIO_START_MS = 10_000
DELAYS_MS = (300, 600, 1_200)


class Relay:
    """A TCP hop between the PC and the server."""

    def __init__(self, upstream_port):
        self.upstream_port = upstream_port
        self.drop = {"pc->server": False, "server->pc": False}  # discard bytes going that way
        self.pairs = []
        self.port = None
        self.up()

    def up(self):
        self.listener = socket.create_server(("127.0.0.1", self.port or 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, args=(self.listener,), daemon=True).start()

    def down(self):
        """New connections are refused until ``up()``. Open ones are kept: see ``cut()``."""
        try:
            self.listener.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept(); close() alone does not
        except OSError:
            pass
        self.listener.close()

    def cut(self):
        """Drop every open connection, as a network failure would."""
        for pair in self.pairs:
            for sock in pair:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
        self.pairs = []

    def _accept(self, listener):
        while True:
            try:
                pc, _ = listener.accept()
            except OSError:
                return
            server = socket.create_connection(("127.0.0.1", self.upstream_port))
            self.pairs.append((pc, server))
            for src, dst, way in ((pc, server, "pc->server"), (server, pc, "server->pc")):
                threading.Thread(target=self._pump, args=(src, dst, way), daemon=True).start()

    def _pump(self, src, dst, way):
        while True:
            try:
                data = src.recv(65536)
            except OSError:
                data = b""
            if not data:
                try:
                    dst.shutdown(socket.SHUT_WR)  # pass the close on, as a direct connection would
                except OSError:
                    pass
                return
            if self.drop[way]:
                continue
            try:
                dst.sendall(data)
            except OSError:
                return


def write_wav(path, segments):
    """``segments``: [(seconds, amplitude)]; amplitude 0.5 is a loud 440 Hz tone (the stand-in model's laughter)."""
    samples = []
    for seconds, amp in segments:
        samples += [round(amp * 32767 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(seconds * RATE)]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def play(self, mic, duration_ms):
        """Advance WAV capture one chunk at a time without consuming server responses."""
        end = self.t + duration_ms
        while self.t < end:
            self.t = min(self.t + CHUNK_MS, end)
            mic.pump()
            wait_for(lambda: not mic.session.media_pending, "the audio writer")


def wait(session, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.poll()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out; state {session.state.value} ({session.reason}), {session.summary()}")


def wait_for(until, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not until():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


def start(app, relay, sid, delays=DELAYS_MS):
    """Start a live session through the relay, with the film at 60 s at capture time 9.5 s.

    Returns once the server has applied the microphone-on control and the anchor.
    """
    session = Session(f"ws://127.0.0.1:{relay.port}", sid, microphone_on_start=True,
                      reconnect_delays_ms=delays)
    session.start()
    tracker = AnchorTracker(sid, emit=session.send_anchor)
    tracker.new_content("file:film-a", "Film A")
    tracker.report(60_000, 1.0, True, capture_ms=9_500)
    wait(session, lambda: session.acks.get("ctl-microphone-1") == "applied")
    wait_for(lambda: app.sessions[sid].view.anchors, "the server's anchor")
    return session


def microphone(session, wav, start_ms=AUDIO_START_MS):
    """Start the WAV microphone at a known capture time, ready for clock.play()."""
    clock = Clock(start_ms)
    mic = WavMicrophone(session, wav, clock=clock)
    mic.pump()  # fixes the capture time of the first sample
    return mic, clock


def assert_clean(session, server_session):
    assert session.view.ignored == {} and session.view.rejected == {} and session.bad_messages == 0
    assert server_session.view.rejected == {} and server_session.invalid == 0
