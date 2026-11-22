"""R3 (EM-12): an event's content timecode is where the player was at capture time.

The real PC session and the player's own anchor tracker, against the real session
server over a local WebSocket. The viewer laughs from 12 s to 14 s of capture time;
at 13 s, in the middle of the laugh, playback changes (pause, 2x speed, seek, another
title). The event must report what the PC's anchors say the player was showing at
the event's start and end, never the position when a message happened to arrive.
"""

import math
import struct
import time
import wave

import pytest

from encore_client.player.anchors import AnchorTracker
from encore_client.sensing.wav_mic import WavMicrophone
from encore_client.session.session import Conn, Session, event_rows
from encore_server.models import LoudnessModel
from encore_server.server import EncoreServer, serve_in_thread

RATE = 16_000
AUDIO_START_MS = 10_000  # capture time of the first sample
ANCHOR_MS, ANCHOR_POS = 9_500, 60_000  # the film is at 60 s, 0.5 s before the audio starts
CHANGE_MS = 13_000  # in the middle of the laugh (12-14 s)
FILM = ("file:film-a", "Film A")
OTHER = ("file:film-b", "Film B")


def at_change(tracker):
    return tracker.last.timecode_at(CHANGE_MS)  # 63.5 s


def pause(tracker):
    tracker.report(at_change(tracker), 1.0, False, capture_ms=CHANGE_MS)


def double_speed(tracker):
    tracker.report(at_change(tracker), 2.0, True, capture_ms=CHANGE_MS)


def seek(tracker):
    tracker.seeked()
    tracker.report(600_000, 1.0, True, capture_ms=CHANGE_MS)


def other_title(tracker):
    tracker.new_content(*OTHER)
    tracker.report(0, 1.0, True, capture_ms=CHANGE_MS)


# change during the laugh -> expected end timecode for capture end time `end`
CASES = {
    "steady": (lambda tracker: None, lambda end: ANCHOR_POS + end - ANCHOR_MS),
    "pause": (pause, lambda end: 63_500),
    "double_speed": (double_speed, lambda end: 63_500 + 2 * (end - CHANGE_MS)),
    "seek": (seek, lambda end: None),  # another epoch: the end is not on the same timeline
    "other_title": (other_title, lambda end: None),
}


@pytest.fixture
def server():
    app = EncoreServer(LoudnessModel(), idle_s=0.3)
    url, stop = serve_in_thread(app)
    yield app, url
    stop()


@pytest.fixture
def laugh_wav(tmp_path):
    """2 s quiet, 2 s of a loud tone (the stand-in model's laughter), 4 s quiet."""
    samples = []
    for seconds, amp in [(2, 0), (2, 0.5), (4, 0)]:
        samples += [round(amp * 32767 * math.sin(2 * math.pi * 440 * i / RATE)) for i in range(seconds * RATE)]
    path = tmp_path / "laugh.wav"
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


def wait(session, until, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.poll()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out; state {session.state.value}, summary {session.view.summary()}")


def connect(url, sid):
    session = Session(url, sid, microphone_on_start=True)
    session.start()
    wait(session, lambda: session.state is Conn.CONNECTED)
    tracker = AnchorTracker(sid, emit=session.send_anchor)
    tracker.new_content(*FILM)
    tracker.report(ANCHOR_POS, 1.0, True, capture_ms=ANCHOR_MS)
    return session, tracker


def upload(session, wav):
    """Send the whole file as if 8 s had passed. Results are not read here."""
    clock = Clock(AUDIO_START_MS)
    mic = WavMicrophone(session, wav, clock=clock)
    mic.pump()
    clock.t = AUDIO_START_MS + 8_000
    mic.pump()
    assert mic.done


def final_event(session):
    wait(session, lambda: any(e["phase"] == "FINAL" for e in session.view.events.values()))
    assert session.view.rejected == {} and session.bad_messages == 0
    (event,) = session.view.events.values()
    return event


def server_anchors(app, sid):
    """The anchors the server has applied so far (read from the test thread)."""
    session = app.sessions.get(sid)
    return list(session.view.anchors) if session else []


def pc_view(tracker, capture):
    """What the PC's own anchors say about the event's capture interval."""
    start, end = capture["start_ms"], capture["end_ms"]
    a, b = tracker.anchor_for(start), tracker.anchor_for(end)
    same = (a.epoch, a.content_id) == (b.epoch, b.content_id)
    return {
        "status": "mapped", "epoch": a.epoch, "content_id": a.content_id, "title": a.title,
        "timecode_start_ms": a.timecode_at(start),
        "timecode_end_ms": b.timecode_at(end) if same else None,
    }


@pytest.mark.parametrize("case", list(CASES))
def test_timecode_is_where_the_player_was_at_capture_time(server, laugh_wav, case):
    _, url = server
    change, expected_end = CASES[case]
    session, tracker = connect(url, f"ses-r3-{case.replace('_', '-')}")
    change(tracker)
    upload(session, laugh_wav)
    event = final_event(session)
    session.end()

    cap, pb = event["capture"], event["playback"]
    assert cap["start_ms"] < CHANGE_MS < cap["end_ms"]  # the change really was mid-laugh
    assert pb["title"] == "Film A" and pb["epoch"] == 1
    assert pb["timecode_start_ms"] == ANCHOR_POS + cap["start_ms"] - ANCHOR_MS
    assert pb["timecode_end_ms"] == expected_end(cap["end_ms"])
    # The server's mapping and the PC's anchors agree.
    assert pb == pc_view(tracker, cap)


def test_a_result_read_late_keeps_the_title_and_timecode_of_its_capture(server, laugh_wav):
    """Delayed reception: the viewer has moved on before the PC reads the result."""
    app, url = server
    session, tracker = connect(url, "ses-r3-late")
    upload(session, laugh_wav)
    # Before any result is read: another film, then a seek in it.
    tracker.new_content(*OTHER)
    tracker.report(5_000, 1.0, True, capture_ms=18_500)
    tracker.seeked()
    tracker.report(90_000, 1.0, True, capture_ms=19_000)
    # The server has the later anchors before the PC reads anything. (The event was
    # finalized during the trailing silence, before they arrived: results are not remapped.)
    deadline = time.monotonic() + 5.0
    while len(server_anchors(app, "ses-r3-late")) < 3:
        assert time.monotonic() < deadline, f"server anchors: {server_anchors(app, 'ses-r3-late')}"
        time.sleep(0.01)
    assert [a["title"] for a in server_anchors(app, "ses-r3-late")] == ["Film A", "Film B", "Film B"]
    event = final_event(session)
    session.end()

    (row,) = event_rows(session.view)
    assert row["title"] == "Film A"
    assert row["timecode_ms"] == ANCHOR_POS + event["capture"]["start_ms"] - ANCHOR_MS
    assert event["playback"] == pc_view(tracker, event["capture"])
