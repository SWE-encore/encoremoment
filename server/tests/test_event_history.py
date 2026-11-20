"""FINAL archival, retry and PostgreSQL integration (dedicated test database)."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import Mock
from urllib.request import urlopen
from uuid import uuid4

import pytest

from encore_server.history import HistoryUnavailable, PostgresHistory, SessionArchive
from encore_server.models import LoudnessModel
from encore_server.session import ServerSession
from test_server_session import LOUD, Pc, stream


def test_archive_retries_failed_final_without_rearchiving_live_replay():
    session = ServerSession("ses-history", LoudnessModel())
    pc = Pc(session.session_id)
    store = Mock(spec=PostgresHistory)
    archive = SessionArchive(store)
    session.on_text(pc.control("microphone", True, 1))
    stream(session, pc, 1, 0, [LOUD] * 4)
    assert session.log[0]["event"]["phase"] == "OPEN"
    asyncio.run(archive.flush(session))
    store.save.assert_not_called()

    final, = session.end()
    store.save.side_effect = HistoryUnavailable
    with pytest.raises(HistoryUnavailable):
        asyncio.run(archive.flush(session))
    store.save.side_effect = None
    asyncio.run(archive.flush(session))
    assert store.save.call_count == 2
    store.save.assert_called_with(session.session_id, [final["event"]])

    assert session.results_after(1)[-1] == final
    asyncio.run(archive.flush(session))
    assert store.save.call_count == 2


@pytest.mark.skipif(not os.environ.get("ENCORE_TEST_DATABASE_URL"), reason="needs a dedicated PostgreSQL test DB")
def test_websocket_final_survives_server_restart_and_session_id_reuse():
    import psycopg
    from websockets.sync.client import connect
    from encore_server.server import EncoreServer, serve_in_thread

    dsn = os.environ["ENCORE_TEST_DATABASE_URL"]
    sid = "ses-history-" + uuid4().hex
    migration = Path(__file__).resolve().parents[1] / "sql" / "001_reaction_events.sql"
    with psycopg.connect(dsn) as conn:
        conn.execute(migration.read_text())

    def get_history(url):
        with urlopen(url.replace("ws://", "http://") + f"/v1/sessions/{sid}/events", timeout=5) as response:
            assert response.headers.get_content_type() == "application/json"
            return json.load(response)["events"]

    def trigger(url, *, end_by_close):
        pc = Pc(sid)
        with connect(url + f"/v1/sessions/{sid}/stream", open_timeout=5) as ws:
            ws.send(pc.control("microphone", True, 1))
            assert json.loads(ws.recv(timeout=5))["status"] == "applied"
            ws.send(pc.anchor(0, 5000))
            for i in range(3):
                ws.send(pc.audio(1, i * 500, LOUD))
            opened = json.loads(ws.recv(timeout=5))["event"]
            assert opened["phase"] == "OPEN"
            if not end_by_close:
                ws.send(pc.control("microphone", False, 2, cap=1500))
                assert json.loads(ws.recv(timeout=5))["kind"] == "control_ack"
                assert json.loads(ws.recv(timeout=5))["event"]["phase"] == "FINAL"
        return opened["event_id"]

    def start():
        return serve_in_thread(EncoreServer(LoudnessModel(), history=PostgresHistory(dsn), idle_s=60))

    try:
        url, stop = start()
        try:
            first = trigger(url, end_by_close=True)
        finally:
            stop()  # includes the normal-close finalizer

        url, stop = start()
        try:
            saved, = get_history(url)
            assert saved["event_id"] == first and saved["phase"] == "FINAL"
            assert saved["playback"]["timecode_start_ms"] == 5000 + saved["capture"]["start_ms"]
            asyncio.run(PostgresHistory(dsn).save(sid, [saved]))  # commit retried after an uncertain acknowledgement
            second = trigger(url, end_by_close=False)
            assert second != first
            with connect(url + f"/v1/sessions/{sid}/stream?after=1") as ws:
                assert json.loads(ws.recv(timeout=5))["event"]["event_id"] == second
            assert [ev["event_id"] for ev in get_history(url)] == [first, second]
            assert asyncio.run(PostgresHistory(dsn).for_session(sid + "-other")) == []
        finally:
            stop()
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM reaction_events WHERE session_id = %s", (sid,))
