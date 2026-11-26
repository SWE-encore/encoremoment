"""R11 (EM-20): a dropped session reconnects and recovers without duplicates.

The real PC session against the real session server, with a TCP relay between
them that the test can cut (a network drop: no close frame either way), stop
forwarding one direction of (data lost in flight), or take down (the server is
unreachable). Nothing in the session or the server knows about the relay.
"""

import time

import pytest

from encore_client.session.panel import status_text
from encore_client.session.session import LOST_SESSION, Conn, Session
from live_harness import AUDIO_START_MS, DELAYS_MS, assert_clean, microphone, start, wait, wait_for, write_wav


@pytest.fixture
def two_laughs(tmp_path):
    """Twice: 2 s quiet, 2 s of laughter, 4 s quiet."""
    return write_wav(tmp_path / "two_laughs.wav", [(2, 0), (2, 0.5), (4, 0)] * 2)


def finals(session):
    return sum(e["phase"] == "FINAL" for e in session.view.events.values())


def outgoing_since_last_open(session):
    entries = session.log.entries
    last_open = max(i for i, e in enumerate(entries) if e["what"] == "open")
    return [e["msg"] for e in entries[last_open + 1 :] if e["what"] == "out"]


def test_a_dropped_session_reconnects_and_resumes_after_its_cursor(server, relay, two_laughs):
    app, _ = server
    session = start(app, relay, "ses-r11-resume")
    server_session = app.sessions["ses-r11-resume"]
    mic, clock = microphone(session, two_laughs)
    clock.play(mic, 8_000)  # the first laugh and its silence
    wait(session, lambda: finals(session) == 1)
    cursor = session.view.next_cursor - 1

    relay.cut()
    wait(session, lambda: session.state is Conn.DISCONNECTED)
    assert session.stale and "Reconnecting in" in status_text(session)
    clock.play(mic, 1_000)  # captured while disconnected: dropped, never sent late (R9)
    dropped = session.audio_dropped
    assert dropped > 0

    wait(session, lambda: session.state is Conn.CONNECTED)
    assert session.reconnects == 1 and "reconnected after 1 drop" in status_text(session)
    (attempt,) = [e for e in session.log.entries if e["what"] == "reconnect"]
    assert attempt["after"] == cursor  # resume after the last applied result
    assert app.sessions["ses-r11-resume"] is server_session  # the server kept the session

    clock.play(mic, 7_000)  # the second laugh
    wait(session, lambda: finals(session) == 2)
    session.end()

    first, second = sorted(session.view.events.values(), key=lambda e: e["event_id"])
    assert (first["event_id"], second["event_id"]) == ("evt-0001", "evt-0002")
    assert second["capture"]["start_ms"] > AUDIO_START_MS + 9_000  # after the gap
    assert second["playback"]["title"] == "Film A"
    # Nothing the PC already had was sent again: no duplicate cursor, revision or control.
    assert_clean(session, server_session)
    assert server_session.view.audio_gaps == dropped  # the server sees the dropped audio as a gap


def test_results_lost_in_flight_are_replayed_once(server, relay, two_laughs):
    app, _ = server
    session = start(app, relay, "ses-r11-replay")
    server_session = app.sessions["ses-r11-replay"]
    relay.drop["server->pc"] = True  # results go out but never arrive
    mic, clock = microphone(session, two_laughs)
    clock.play(mic, 8_000)
    wait_for(lambda: any(m["event"]["phase"] == "FINAL" for m in server_session.log), "the server's FINAL")
    session.poll()
    assert session.view.events == {}

    relay.cut()
    relay.drop["server->pc"] = False
    wait(session, lambda: finals(session) == 1)
    session.end()

    (attempt,) = [e for e in session.log.entries if e["what"] == "reconnect"]
    assert attempt["after"] == 0
    assert session.view.next_cursor - 1 == len(server_session.log)  # every result, each applied once
    (event,) = session.view.events.values()
    assert event == server_session.log[-1]["event"]
    assert_clean(session, server_session)


def test_controls_are_synced_first_with_their_original_keys(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-controls")
    server_session = app.sessions["ses-r11-controls"]
    relay.drop["pc->server"] = True
    lost = session.set_sensor("microphone", False)  # sent, lost in flight: never acknowledged
    relay.cut()
    relay.drop["pc->server"] = False
    wait(session, lambda: session.state is Conn.DISCONNECTED)
    made_offline = session.set_sensor("microphone", True)  # made while disconnected

    wait(session, lambda: session.acks.get(made_offline["idempotency_key"]) is not None)
    session.end()

    first = session.controls[0]
    assert session.acks == {first["idempotency_key"]: "duplicate", lost["idempotency_key"]: "applied",
                            made_offline["idempotency_key"]: "applied"}
    # On the new connection the controls go first, unchanged, then the newest anchor.
    sent = outgoing_since_last_open(session)
    assert sent[:3] == [first, lost, made_offline]
    assert sent[3]["kind"] == "playback_anchor"
    mic = server_session.view.sensors["microphone"]
    assert (mic.enabled, mic.generation) == (True, 3) == (session.view.sensors["microphone"].enabled,
                                                          session.view.sensors["microphone"].generation)
    assert server_session.view.sensors["camera"].enabled is False  # never enabled
    assert_clean(session, server_session)  # a changed message under a used key would be rejected


def test_a_server_that_lost_the_session_is_not_shown_as_recovered(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-lost")
    relay.cut()
    wait(session, lambda: session.state is Conn.DISCONNECTED)
    del app.sessions["ses-r11-lost"]  # as after a server restart

    wait(session, lambda: session.lost)
    assert session.state is Conn.DISCONNECTED and session.reason == LOST_SESSION
    assert session.retry_in_ms is None and "no longer has this session" in status_text(session)
    attempts = session.attempt
    time.sleep(2 * DELAYS_MS[-1] / 1000)
    session.poll()
    assert session.state is Conn.DISCONNECTED and session.attempt == attempts  # no more attempts
    session.end()


def test_retries_back_off_until_the_server_is_reachable(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-backoff", delays=(100, 200, 400))
    relay.down()
    relay.cut()
    wait(session, lambda: session.attempt >= 3 and session.state is Conn.DISCONNECTED)
    relay.up()
    wait(session, lambda: session.state is Conn.CONNECTED)
    session.end()

    entries = session.log.entries
    waits = [entries[i]["t_ms"] - entries[i - 1]["t_ms"]
             for i, e in enumerate(entries) if e["what"] == "reconnect"]
    assert all(entries[i - 1]["what"] == "closed" for i, e in enumerate(entries) if e["what"] == "reconnect")
    assert [w >= d for w, d in zip(waits, (100, 200, 400, 400))] == [True] * len(waits)
    assert len(waits) >= 4 and session.reconnects == 1 and session.attempt == 0


def test_a_server_unreachable_at_start_is_retried_without_counting_a_reconnect(relay):
    relay.down()
    session = Session(f"ws://127.0.0.1:{relay.port}", "ses-r11-late-server", microphone_on_start=True,
                      reconnect_delays_ms=(100,))
    session.start()
    wait(session, lambda: session.attempt >= 1)
    relay.up()
    wait(session, lambda: session.acks.get("ctl-microphone-1") == "applied")
    assert status_text(session) == "Connected"  # not "reconnected after a drop": there was none
    session.end()
    assert session.reconnects == 0 and session.attempt == 0


def test_end_while_waiting_to_reconnect_stops_the_attempts(server, relay):
    app, _ = server
    session = start(app, relay, "ses-r11-end", delays=(200,))
    relay.down()
    relay.cut()
    wait(session, lambda: session.retry_in_ms is not None)
    session.end()
    time.sleep(0.4)
    session.poll()
    assert session.state is Conn.ENDED and session.retry_in_ms is None
    assert not any(e["what"] == "reconnect" for e in session.log.entries)


def test_a_session_whose_pc_never_returns_is_ended_after_the_lease(server, relay, tmp_path):
    """End while offline: the server cannot hear End, so its lease closes what was open."""
    app, _ = server
    app.idle_s, app.lease_s = 30, 0.5  # only the lease may close the reaction here
    session = start(app, relay, "ses-r11-lease")
    server_session = app.sessions["ses-r11-lease"]
    mic, clock = microphone(session, write_wav(tmp_path / "laugh.wav", [(2, 0), (2, 0.5)]))
    clock.play(mic, 4_000)
    wait(session, lambda: session.open_events == ["evt-0001"])

    relay.down()
    relay.cut()
    wait(session, lambda: session.state is Conn.DISCONNECTED)
    session.finish()  # ends at once on the PC; the server never hears it
    assert session.state is Conn.ENDED
    assert server_session.log[-1]["event"]["phase"] != "FINAL"

    wait_for(lambda: "ses-r11-lease" not in app.sessions, "the lease to end the session")
    phases = [(m["event"]["event_id"], m["event"]["phase"]) for m in server_session.log]
    assert phases[-1] == ("evt-0001", "FINAL") and [p for _, p in phases].count("FINAL") == 1
    assert "ses-r11-lease" not in app._leases and "ses-r11-lease" not in app._locks


def test_a_reconnect_within_the_lease_keeps_the_session(server, relay):
    app, _ = server
    app.lease_s = 1.0
    session = start(app, relay, "ses-r11-lease-back")
    server_session = app.sessions["ses-r11-lease-back"]
    relay.cut()  # the relay stays up: the PC is back after its first delay (300 ms)
    wait(session, lambda: session.state is Conn.CONNECTED and session.reconnects == 1)
    time.sleep(1.3)  # past the lease of the first drop
    session.poll()
    assert app.sessions["ses-r11-lease-back"] is server_session and not session.lost
    assert session.state is Conn.CONNECTED and "ses-r11-lease-back" not in app._leases
    session.end()


def test_a_pc_back_after_the_lease_is_told_the_session_is_lost(server, relay):
    app, _ = server
    app.lease_s = 0.3
    session = start(app, relay, "ses-r11-lease-late")
    relay.down()
    relay.cut()
    wait(session, lambda: session.state is Conn.DISCONNECTED)
    wait_for(lambda: "ses-r11-lease-late" not in app.sessions, "the lease to end the session")

    relay.up()
    wait(session, lambda: session.lost)
    assert session.state is Conn.DISCONNECTED and session.reason == LOST_SESSION
    session.end()
