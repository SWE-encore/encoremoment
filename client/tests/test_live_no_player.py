"""Live microphone without the player (--no-player --mic): the microphone-clock anchor.

The server maps each event through the anchor the PC sends at Start, so the
timeline shows seconds since Start and says it is not a film position.
"""

import json
import math
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from encore_client.live import (MIC_CLOCK_CONTENT_ID, MIC_CLOCK_TITLE, mic_clock_anchor,  # noqa: E402
                                parse_triggers, send_mic_clock_on_start)

SID = "ses-live-mic-test"


def test_anchor_matches_the_contract_schema():
    from encore_server.contract import schema_errors

    msg = mic_clock_anchor(12_345) | {"session_id": SID, "seq": 0}
    assert schema_errors(msg) == []
    assert msg["playback"]["position_ms"] == 0 and msg["playback"]["state"] == "playing"


def test_start_sets_the_anchor_before_connecting():
    calls = []

    class FakeSession:
        def send_anchor(self, msg):
            calls.append(("anchor", msg["capture_ms"]))

        def start(self):
            calls.append(("start", None))

    s = FakeSession()
    send_mic_clock_on_start(s, clock=lambda: 7_000)
    s.start()
    assert calls == [("anchor", 7_000), ("start", None)]


def test_parse_triggers():
    assert parse_triggers(None) == {}
    assert parse_triggers(["laughter=0.6", "gasp=0.25"]) == {"laughter": 0.6, "gasp": 0.25}
    for bad in (["laughter"], ["clap=0.5"], ["scream=0"], ["scream=1.5"]):
        with pytest.raises(ValueError):
            parse_triggers(bad)


def _audio(seq, gen, chunk, cap, pcm):
    from encore_server.contract import _contract as contract

    header = {"schema": "encore.v1", "kind": "audio_chunk", "session_id": SID, "seq": seq,
              "capture_ms": cap, "generation": gen, "chunk": chunk, "sample_rate": 16_000,
              "channels": 1, "encoding": "pcm_s16le", "samples": len(pcm) // 2, "aec": "off"}
    return contract.encode_audio_frame(header, pcm)


def test_events_are_mapped_to_time_since_start():
    from encore_server.models import LoudnessModel
    from encore_server.session import ServerSession

    quiet = bytes(16_000)
    loud = struct.pack("<8000h", *(round(0.5 * 32767 * math.sin(2 * math.pi * 440 * i / 16_000))
                                   for i in range(8000)))
    start_ms = 50_000
    s = ServerSession(SID, LoudnessModel())
    s.on_text(json.dumps({"schema": "encore.v1", "kind": "control_update", "session_id": SID, "seq": 0,
                          "capture_ms": start_ms, "idempotency_key": "ctl-microphone-1",
                          "sensor": "microphone", "enabled": True, "generation": 1}))
    s.on_text(json.dumps(mic_clock_anchor(start_ms) | {"session_id": SID, "seq": 1}))
    out = []
    for i, pcm in enumerate([quiet] * 6 + [loud] * 4 + [quiet] * 8):
        out += s.on_audio(_audio(2 + i, 1, i, start_ms + 500 * i, pcm))
    evs = [m["event"] for m in out if m["kind"] == "event_update"]
    assert evs and evs[-1]["phase"] == "FINAL"
    pb = evs[-1]["playback"]
    assert pb["status"] == "mapped" and pb["content_id"] == MIC_CLOCK_CONTENT_ID
    assert pb["title"] == MIC_CLOCK_TITLE
    # the loud part starts 3 s after Start; the 0.975 s window may open slightly earlier
    assert 2_000 <= pb["timecode_start_ms"] <= 3_500
