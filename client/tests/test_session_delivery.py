"""EM-23: session, sample result delivery to the PC, duplicates, disconnects.

Standard library only (no Qt, no GStreamer). Every test runs the real
WebSocket client against the fake server on a free local port.

    cd client && python -m unittest tests.test_session_delivery -v
"""

import json
import socket
import time
import unittest

from encore_client.session import ws
from encore_client.session.contract import SAMPLES_DIR
from encore_client.session.fake_server import FakeServer
from encore_client.session.session import (
    Conn,
    ScenarioDriver,
    Session,
    WsTransport,
    event_rows,
    load_scenario,
    new_session_id,
)

SAMPLES = sorted(SAMPLES_DIR.glob("*.json"))
TIMEOUT_S = 10


def run_until(session, driver, done, timeout=TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.poll()
        if driver is not None:
            driver.pump()
        if done():
            return True
        time.sleep(0.01)
    return False


def anchor(session_id, title="Test movie"):
    return {
        "schema": "encore.v1",
        "kind": "playback_anchor",
        "session_id": session_id,
        "seq": 0,
        "capture_ms": 1000,
        "playback": {"epoch": 1, "content_id": "file:test", "title": title,
                     "position_ms": 0, "rate": 1.0, "state": "paused"},
    }


class WebSocketTest(unittest.TestCase):
    def test_round_trip_ping_and_close(self):
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server_side = {}

        def serve():
            sock, _ = listener.accept()
            conn, path = ws.accept(sock, allow_path=lambda p: p == "/x")
            server_side["path"] = path
            text = conn.recv()
            conn.ping()
            # Send ping first so client.recv() consumes it before returning the
            # reply. Closing with an unread ping can reset TCP on Windows and
            # this round-trip test would depend on thread scheduling.
            conn.send(text[::-1])
            try:
                conn.recv()
            except ws.WsClosed as exc:
                server_side["close"] = exc.code

        import threading

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        client = ws.connect(f"ws://127.0.0.1:{port}/x")
        long_text = "반응" * 40_000  # > 64 KiB once encoded: 8-byte length path
        client.send(long_text)
        self.assertEqual(client.recv(), long_text[::-1])
        client.close()
        t.join(TIMEOUT_S)
        listener.close()
        self.assertEqual(server_side, {"path": "/x", "close": 1000})

    def test_wrong_path_is_refused(self):
        server = FakeServer(load_scenario(SAMPLES[0]), port=0, interval_s=0).start()
        try:
            with self.assertRaises(ws.HandshakeError):
                ws.connect(server.url + "/v1/sessions/ses-other/stream")
        finally:
            server.stop()


class DeliveryTest(unittest.TestCase):
    def play(self, path, **server_kw):
        scenario = load_scenario(path)
        server = FakeServer(scenario, port=0, interval_s=0, **server_kw).start()
        self.addCleanup(server.stop)
        session = Session(server.url, scenario["session_id"])
        self.addCleanup(session.end)
        driver = ScenarioDriver(session, scenario)
        return scenario, server, session, driver

    def test_every_sample_reaches_its_expected_result(self):
        self.assertGreaterEqual(len(SAMPLES), 4)
        for path in SAMPLES:
            with self.subTest(sample=path.name):
                scenario, _, session, driver = self.play(path)
                session.start()
                self.assertTrue(run_until(session, driver, lambda d=driver: d.done))
                self.assertIs(session.state, Conn.CONNECTED)
                self.assertEqual(session.view.summary(), scenario["expected"])
                session.end()

    def test_same_result_twice_leaves_one_event(self):
        _, server, session, driver = self.play(SAMPLES_DIR / "04_duplicate_delivery.json")
        session.start()
        self.assertTrue(run_until(session, driver, lambda: driver.done))
        rows = event_rows(session.view)
        self.assertEqual([r["event_id"] for r in rows], ["evt-0004"])
        self.assertEqual((rows[0]["phase"], rows[0]["revision"]), ("FINAL", 3))
        # The server really did send the event more than once.
        self.assertGreater(server.sent, len(rows) + 2)

    def test_player_anchor_is_sent_on_connect(self):
        scenario, server, session, driver = self.play(SAMPLES_DIR / "01_single_event.json")
        session.send_anchor(anchor("ses-player", "My movie"))  # player still has its own id
        self.assertEqual(session.sent, 0)  # not connected yet: kept, not queued
        session.start()
        self.assertTrue(run_until(session, driver, lambda: driver.done))
        sent_anchors = [e["msg"] for e in session.log.entries
                        if e["what"] == "out" and e["msg"]["kind"] == "playback_anchor"]
        self.assertEqual(sent_anchors[0]["session_id"], scenario["session_id"])
        self.assertEqual(sent_anchors[0]["playback"]["title"], "My movie")
        self.assertTrue(run_until(session, None, lambda: server.extra_anchors == 1))
        self.assertEqual(session.view.summary(), scenario["expected"])

    def test_dropped_connection_is_reported(self):
        _, _, session, driver = self.play(SAMPLES_DIR / "01_single_event.json", drop_after=2)
        session.start()
        self.assertTrue(run_until(session, driver, lambda: session.state is Conn.DISCONNECTED))
        self.assertTrue(session.stale)
        self.assertIn("connection lost", session.reason)
        self.assertEqual(session.received, 2)
        self.assertFalse(driver.done)
        self.assertTrue(any(e["what"] == "closed" for e in session.log.entries))
        # Sending while disconnected keeps the message instead of losing it.
        self.assertFalse(session.send(anchor(session.session_id)))
        self.assertEqual(len(session.unsent), 1)

    def test_silent_server_counts_as_dropped(self):
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        held = []

        import threading

        def serve():
            sock, _ = listener.accept()
            conn, _ = ws.accept(sock)
            held.append(conn)  # never reads: pings go unanswered

        threading.Thread(target=serve, daemon=True).start()
        session = Session(
            f"ws://127.0.0.1:{port}", "ses-silent",
            transport_factory=lambda url: WsTransport(url, ping_s=0.1, silence_s=0.5),
        )
        session.start()
        self.assertTrue(run_until(session, None, lambda: session.state is Conn.DISCONNECTED))
        self.assertIn("no response", session.reason)
        listener.close()
        for conn in held:
            conn.abort()

    def test_unreachable_server_is_reported(self):
        with socket.create_server(("127.0.0.1", 0)) as s:
            port = s.getsockname()[1]  # closed again before connecting
        session = Session(f"ws://127.0.0.1:{port}", "ses-unreachable")
        session.start()
        self.assertTrue(run_until(session, None, lambda: session.state is Conn.DISCONNECTED))
        self.assertIn("could not connect", session.reason)

    def test_end_is_not_a_disconnect(self):
        _, _, session, driver = self.play(SAMPLES_DIR / "01_single_event.json")
        session.start()
        self.assertTrue(run_until(session, driver, lambda: driver.done))
        session.end()
        run_until(session, None, lambda: any(e["what"] == "closed" for e in session.log.entries))
        self.assertIs(session.state, Conn.ENDED)
        self.assertFalse(session.stale)
        with self.assertRaises(RuntimeError):
            session.start()


class SessionRulesTest(unittest.TestCase):
    def test_new_session_ids_match_the_contract(self):
        ids = {new_session_id() for _ in range(50)}
        self.assertEqual(len(ids), 50)
        for sid in ids:
            Session("ws://127.0.0.1:1", sid)  # raises if the pattern does not match

    def test_rejects_bad_ids_and_wrong_direction(self):
        with self.assertRaises(ValueError):
            Session("ws://127.0.0.1:1", "bad id!")
        session = Session("ws://127.0.0.1:1", "ses-rules")
        with self.assertRaises(ValueError):
            session.send({"kind": "event_update", "session_id": "ses-rules"})
        with self.assertRaises(ValueError):
            session.send(anchor("ses-other"))

    def test_bad_json_is_counted_not_fatal(self):
        session = Session("ws://127.0.0.1:1", "ses-json")
        session._on_message("not json")
        session._on_message(json.dumps([1, 2]))
        self.assertEqual(session.bad_messages, 2)
        self.assertEqual(event_rows(session.view), [])


if __name__ == "__main__":
    unittest.main()
