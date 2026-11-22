"""FINAL archival, retry and PostgreSQL integration (dedicated test database)."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import urlopen
from uuid import uuid4

import pytest

from encore_server.history import HistoryUnavailable, PostgresHistory, SessionArchive
from encore_server.models import LoudnessModel
from encore_server.session import ServerSession
from test_server_session import LOUD, Pc, stream


@pytest.fixture(autouse=True)
def isolated_outbox(tmp_path, monkeypatch):
    path = tmp_path / "state" / "outbox.sqlite3"
    monkeypatch.setenv("ENCORE_HISTORY_OUTBOX", str(path))
    return path


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


@pytest.mark.parametrize("close_with_open_event", [False, True])
def test_outage_keeps_live_controls_and_retries_after_pc_end(close_with_open_event):
    from websockets.exceptions import ConnectionClosedOK
    from websockets.frames import Close
    from encore_server.server import EncoreServer

    async def scenario():
        pc = Pc("ses-history-outage")
        messages = [pc.control("microphone", True, 1)]
        messages.extend(pc.audio(1, i * 500, LOUD) for i in range(3))
        if not close_with_open_event:
            messages.append(pc.control("microphone", False, 2, cap=1500))
        ws = Mock()
        ws.request = SimpleNamespace(path=f"/v1/sessions/{pc.sid}/stream")
        ws.close_code = 1000
        sent = []

        async def receive():
            if messages:
                return messages.pop(0)
            close = Close(1000, "")
            raise ConnectionClosedOK(close, close, True)

        async def send(data):
            sent.append(json.loads(data))

        ws.recv, ws.send = receive, send
        failed, archived = asyncio.Event(), asyncio.Event()
        store = Mock(spec=PostgresHistory)

        async def save(sid, events):
            if not failed.is_set():
                failed.set()
                raise HistoryUnavailable
            assert sid == pc.sid
            assert len(events) == 1 and events[0]["phase"] == "FINAL"
            archived.set()

        store.save.side_effect = save
        app = EncoreServer(LoudnessModel(), history=store)

        async def inline_model(fn, *args):
            return fn(*args)

        try:
            # Isolate database and transport scheduling from the audio executor.
            with patch("encore_server.server.asyncio.to_thread", inline_model):
                await asyncio.wait_for(app.handler(ws), timeout=4)
            assert any(m.get("event", {}).get("phase") == "OPEN" for m in sent)
            if not close_with_open_event:
                assert any(m.get("kind") == "control_ack" and m["generation"] == 2 for m in sent)
                assert sent[-1]["event"]["phase"] == "FINAL"
            await asyncio.wait_for(archived.wait(), timeout=4)
            assert store.save.call_count == 2
        finally:
            await app.finish_history()
        assert store.save.call_count == 2

    asyncio.run(scenario())


def test_orderly_stop_archives_an_open_reaction():
    from encore_server.server import EncoreServer

    store = Mock(spec=PostgresHistory)
    app = EncoreServer(LoudnessModel(), history=store)
    session = ServerSession("ses-history-stop", LoudnessModel())
    app.sessions[session.session_id] = session
    pc = Pc(session.session_id)
    session.on_text(pc.control("microphone", True, 1))
    stream(session, pc, 1, 0, [LOUD] * 3)
    asyncio.run(app.finish_history())
    final, = store.save.await_args.args[1]
    assert final["phase"] == "FINAL" and final["event_id"] == session.log[0]["event"]["event_id"]


def test_shutdown_stages_all_open_sessions_before_waiting_for_postgres():
    from encore_server.server import EncoreServer

    async def scenario():
        saving, release = asyncio.Event(), asyncio.Event()
        store = Mock(spec=PostgresHistory)

        async def blocked_write(sid, events):
            saving.set()
            await release.wait()

        store.save.side_effect = blocked_write
        app = EncoreServer(LoudnessModel(), history=store)
        for sid in ("ses-one", "ses-two", "ses-three"):
            session = app.sessions[sid] = ServerSession(sid, LoudnessModel())
            pc = Pc(sid)
            session.on_text(pc.control("microphone", True, 1))
            stream(session, pc, 1, 0, [LOUD] * 4)
        task = asyncio.create_task(app.finish_history())
        try:
            await asyncio.wait_for(saving.wait(), 4)
            assert set(app.archive.outbox.sessions()) == set(app.sessions)
            assert all(app.archive.outbox.batch(sid)[0]["phase"] == "FINAL" for sid in app.sessions)
        finally:
            release.set()
            await task
        assert not app.archive.outbox.sessions()

    asyncio.run(scenario())


def test_restart_drains_durably_delivered_final_without_a_new_pc(isolated_outbox):
    from encore_server.server import EncoreServer

    async def scenario():
        store = Mock(spec=PostgresHistory)
        store.save.side_effect = HistoryUnavailable
        app = EncoreServer(LoudnessModel(), history=store)
        session = ServerSession("ses-crash", LoudnessModel())
        pc = Pc(session.session_id)
        session.on_text(pc.control("microphone", True, 1))
        stream(session, pc, 1, 0, [LOUD] * 4)
        final, = session.end()
        await app.archive.notify(session)
        assert isolated_outbox.stat().st_mode & 0o777 == 0o600
        assert app.archive.outbox.batch(pc.sid) == [final["event"]]
        # Lose all live state without the server's orderly end/flush path.
        app.archive._writer.cancel()
        await asyncio.gather(app.archive._writer, return_exceptions=True)
        restored = EncoreServer(LoudnessModel(), history=store)
        store.save.side_effect = None
        restored.archive.start()
        try:
            async with asyncio.timeout(5):
                while restored.archive.outbox.sessions():
                    await asyncio.sleep(.01)
        finally:
            await restored.finish_history()
        store.save.assert_awaited_with(pc.sid, [final["event"]])
        assert not restored.sessions

    asyncio.run(scenario())


def test_replay_cannot_emit_a_final_while_the_outbox_cannot_commit():
    from unittest.mock import AsyncMock
    from encore_server.history import OutboxUnavailable
    from encore_server.server import EncoreServer

    async def scenario():
        app = EncoreServer(LoudnessModel(), history=Mock(spec=PostgresHistory))
        session = ServerSession("ses-disk-full", LoudnessModel())
        pc = Pc(session.session_id)
        app.sessions[pc.sid] = session
        app._locks[pc.sid] = asyncio.Lock()
        session.on_text(pc.control("microphone", True, 1))
        stream(session, pc, 1, 0, [LOUD] * 4)
        final, = session.end()
        ws = Mock()
        ws.request = SimpleNamespace(path=f"/v1/sessions/{pc.sid}/stream")
        ws.send, ws.close = AsyncMock(), AsyncMock()
        ws.close_code = 1011
        with patch.object(app.archive.outbox, "put", side_effect=OutboxUnavailable):
            await app.handler(ws)
        ws.close.assert_awaited_once_with(1011, "local history outbox unavailable")
        ws.send.assert_not_awaited()
        await app.finish_history()
        app.archive.store.save.assert_awaited_once_with(pc.sid, [final["event"]])

    asyncio.run(scenario())


def test_outbox_readiness_requires_a_new_write_after_disk_failure(isolated_outbox):
    import sqlite3
    from encore_server.history import OutboxUnavailable

    store = Mock(spec=PostgresHistory)
    archive = SessionArchive(store)
    connect = sqlite3.connect

    def readonly(path):
        db = connect(path)
        db.execute("PRAGMA query_only=ON")
        return db

    with patch("encore_server.history.sqlite3.connect", readonly):
        with pytest.raises(OutboxUnavailable):
            archive.outbox.put("ses-probe", [{"event_id": "evt-1", "phase": "FINAL"}])
        assert archive.outbox.sessions() == []  # reads still succeed
        with pytest.raises(OutboxUnavailable):
            asyncio.run(archive.check())
        store.check.assert_not_called()
    asyncio.run(archive.check())
    store.check.assert_awaited_once()
    assert not archive.outbox.sessions()


def test_uncertain_postgres_ack_replays_the_same_id_after_restart():
    from encore_server.history import OutboxUnavailable

    async def scenario():
        store = Mock(spec=PostgresHistory)
        archive = SessionArchive(store)
        entry = {"event_id": "evt-0001", "phase": "FINAL"}
        archive.outbox.put("ses-uncertain", [entry, entry])
        with patch.object(archive.outbox, "acknowledge", side_effect=OutboxUnavailable):
            with pytest.raises(OutboxUnavailable):
                await archive.drain("ses-uncertain")
        restarted = SessionArchive(store)
        await restarted.drain("ses-uncertain")
        assert not restarted.outbox.sessions()
        assert len(store.save.await_args_list) == 2
        for call in store.save.await_args_list:
            assert call.args == ("ses-uncertain", [entry])

    asyncio.run(scenario())


def test_disabled_archive_does_not_touch_outbox(isolated_outbox):
    from encore_server.server import EncoreServer

    app = EncoreServer(LoudnessModel())
    asyncio.run(app.finish_history())
    assert not isolated_outbox.parent.exists()


@pytest.mark.parametrize("has_pending", [False, True])
def test_cli_restart_checks_for_pending_history_during_postgres_outage(isolated_outbox, monkeypatch, has_pending):
    from unittest.mock import AsyncMock
    from encore_server.__main__ import main
    from encore_server.history import FinalOutbox
    from encore_server.server import EncoreServer

    monkeypatch.setenv("DATABASE_URL", "postgresql://test.invalid/events")
    store = Mock(spec=PostgresHistory)
    store.check.side_effect = HistoryUnavailable
    if has_pending:
        FinalOutbox(isolated_outbox).put("ses-recover", [{"event_id": "evt-001", "phase": "FINAL"}])
    with patch("encore_server.history.PostgresHistory", return_value=store), \
            patch.object(EncoreServer, "run", new_callable=AsyncMock) as serve:
        if has_pending:
            assert main(["--record-history", "--model", "loudness"]) == 0
            serve.assert_awaited_once()
        else:
            with pytest.raises(SystemExit) as err:
                main(["--record-history", "--model", "loudness"])
            assert err.value.code == 2 and not isolated_outbox.exists()
            serve.assert_not_called()


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
