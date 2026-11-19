"""Development stand-in for the result server (EM-23).

Plays the server side of a contracts/samples scenario over a WebSocket, so
the PC client can be developed and shown before the real server (EM-3) exists.

    python -m encore_client.session.fake_server ../contracts/samples/04_duplicate_delivery.json

It accepts ``/v1/sessions/<session_id>/stream`` for the scenario's session and
walks the messages in order: at a ``pc->server`` step it waits for the PC to
send that message, at a ``server->pc`` step it sends it. Extra playback
anchors from a live player are accepted and logged. ``--drop-after N`` cuts the
connection without a close frame after N sent messages, to show a disconnect.
Audio frames are checked against the contract and counted; ``--save-audio
FILE.wav`` writes the received audio out, to hear what the server would get.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable

from . import ws
from .contract import AUDIO_RATE, decode_audio_frame
from .session import STREAM_PATH, load_scenario


class FakeServer:
    def __init__(
        self,
        scenario: dict[str, Any],
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        interval_s: float = 0.5,
        drop_after: int | None = None,
        save_audio: str | Path | None = None,
        say: Callable[[str], None] = lambda line: None,
    ) -> None:
        self.scenario = scenario
        self.path = STREAM_PATH.format(session_id=scenario["session_id"])
        self.interval_s = interval_s
        self.drop_after = drop_after
        self.say = say
        self.sent = 0
        self.extra_anchors = 0
        self.mismatches = 0
        self.audio_chunks = 0
        self.bad_frames = 0
        self._save_audio = save_audio
        self._wav: wave.Wave_write | None = None
        self.finished = threading.Event()
        self._listener = socket.create_server((host, port))
        self.port = self._listener.getsockname()[1]
        self.url = f"ws://{host}:{self.port}"
        self._conns: list[ws.WebSocket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-server", daemon=True)

    def start(self) -> FakeServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._listener.close()
        for conn in list(self._conns):
            conn.abort()
        if self._wav is not None:
            self._wav.close()
            self._wav = None

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                sock, addr = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(sock, addr), daemon=True).start()

    def _handle(self, sock: socket.socket, addr) -> None:
        try:
            conn, path = ws.accept(sock, allow_path=lambda p: p == self.path)
        except OSError as exc:
            self.say(f"handshake failed from {addr[0]}: {exc}")
            return
        if conn is None:
            self.say(f"refused {path!r} (this server only serves {self.path})")
            return
        self._conns.append(conn)
        self.say(f"PC connected: {path}")
        try:
            self._play(conn)
            self.finished.set()
            self.say("scenario finished; holding the connection open (Ctrl+C to stop)")
            while True:
                self._note_extra(self._read(conn))
        except ws.WsClosed as exc:
            self.say(f"connection closed ({exc})")

    def _play(self, conn: ws.WebSocket) -> None:
        for n, step in enumerate(self.scenario["messages"], 1):
            msg = step["msg"]
            if step["dir"] == "pc->server":
                self._await(conn, msg, n)
                continue
            if self.drop_after is not None and self.sent >= self.drop_after:
                self.say(f"dropping the connection after {self.sent} messages (--drop-after)")
                conn.abort()
                raise ws.WsClosed(1006, "dropped on purpose")
            time.sleep(self.interval_s)
            conn.send(json.dumps(msg, ensure_ascii=False))
            self.sent += 1
            self.say(f"step {n}: sent {_describe(msg)}")

    def _read(self, conn: ws.WebSocket) -> dict[str, Any]:
        """The next PC message. For an audio frame, its header, after taking the audio."""
        while True:
            data = conn.recv()
            if isinstance(data, str):
                return json.loads(data)
            try:
                header, pcm = decode_audio_frame(data)
            except ValueError as exc:
                self.bad_frames += 1
                self.say(f"bad audio frame ({exc})")
                continue
            self._on_audio(header, pcm)
            return header

    def _on_audio(self, header: dict[str, Any], pcm: bytes) -> None:
        self.audio_chunks += 1
        if header["chunk"] == 0 or self.audio_chunks % 20 == 0:
            self.say(f"audio: {_describe(header)}, {self.audio_chunks} chunk(s) so far")
        if self._save_audio is not None:
            if self._wav is None:
                self._wav = wave.open(str(self._save_audio), "wb")
                self._wav.setnchannels(1)
                self._wav.setsampwidth(2)
                self._wav.setframerate(AUDIO_RATE)
            self._wav.writeframes(pcm)

    def _await(self, conn: ws.WebSocket, expected: dict[str, Any], n: int) -> None:
        while True:
            got = self._read(conn)
            if got == expected:
                self.say(f"step {n}: got {_describe(got)}")
                return
            if got.get("kind") == "playback_anchor" and expected.get("kind") != "playback_anchor":
                self._note_extra(got)
                continue
            if got.get("kind") == expected.get("kind"):
                self.mismatches += 1
                self.say(f"step {n}: got a different {got.get('kind')} than the sample; continuing")
                return
            self._note_extra(got)

    def _note_extra(self, msg: dict[str, Any]) -> None:
        if msg.get("kind") == "playback_anchor":
            self.extra_anchors += 1
            pb = msg.get("playback", {})
            self.say(f"live anchor: {pb.get('title')!r} epoch {pb.get('epoch')} {pb.get('state')}")
        elif msg.get("kind") == "audio_chunk":
            pass  # counted in _on_audio
        else:
            self.say(f"unexpected {msg.get('kind')} (ignored)")


def _describe(msg: dict[str, Any]) -> str:
    kind = msg.get("kind")
    if kind == "event_update":
        ev = msg["event"]
        return f"event_update cursor {msg['cursor']} {ev['event_id']} rev {ev['revision']} {ev['phase']}"
    if kind == "control_update":
        state = "on" if msg.get("enabled") else "off"
        return f"control_update {msg.get('sensor')} {state} gen {msg.get('generation')}"
    if kind == "audio_chunk":
        return f"audio_chunk gen {msg.get('generation')} chunk {msg.get('chunk')}"
    return str(kind)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m encore_client.session.fake_server", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", type=Path, help="a contracts/samples/*.json file")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--interval", type=float, default=0.5, help="seconds before each server message")
    ap.add_argument("--drop-after", type=int, metavar="N", help="cut the connection after N messages")
    ap.add_argument("--save-audio", type=Path, metavar="FILE.wav", help="write received audio to a WAV file")
    args = ap.parse_args(argv)

    scenario = load_scenario(args.scenario)
    server = FakeServer(scenario, args.host, args.port, interval_s=args.interval,
                        drop_after=args.drop_after, save_audio=args.save_audio,
                        say=lambda s: print(f"[fake-server] {s}", flush=True))
    print(f"[fake-server] {scenario['name']}: {server.url}{server.path}", flush=True)
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
