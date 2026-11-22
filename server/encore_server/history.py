"""Archive finalized event metadata. Live cursors belong to the WebSocket session."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager, closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakValueDictionary

if TYPE_CHECKING:
    from .session import ServerSession


class HistoryUnavailable(Exception):
    """A database operation failed. Keep connection details out of client responses."""


class PostgresHistory:
    def __init__(self, dsn: str) -> None:
        import psycopg

        self._driver = psycopg
        self._dsn = dsn

    @asynccontextmanager
    async def _connection(self):
        try:
            conn = await self._driver.AsyncConnection.connect(
                self._dsn, connect_timeout=4, options="-c statement_timeout=5000"
            )
            async with conn:
                yield conn
        except self._driver.Error as exc:
            raise HistoryUnavailable from exc

    async def check(self) -> None:
        async with self._connection() as conn:
            await conn.execute("SELECT session_id, event_id, event FROM reaction_events LIMIT 0")

    async def save(self, session_id: str, events: list[dict]) -> None:
        from psycopg.types.json import Jsonb

        async with self._connection() as conn, conn.cursor() as cursor:
            await cursor.executemany(
                "INSERT INTO reaction_events (session_id, event_id, event) VALUES (%s, %s, %s) "
                "ON CONFLICT (session_id, event_id) DO NOTHING",
                [(session_id, event["event_id"], Jsonb(event)) for event in events],
            )

    async def for_session(self, session_id: str) -> list[dict]:
        async with self._connection() as conn:
            rows = await conn.execute(
                "SELECT event FROM reaction_events WHERE session_id = %s ORDER BY recorded_at, event_id",
                (session_id,),
            )
            return [row[0] async for row in rows]


class OutboxUnavailable(HistoryUnavailable):
    """A FINAL cannot safely be delivered until the local queue works again."""


class FinalOutbox:
    """Durable handoff of FINAL metadata before a live client can observe it."""

    def __init__(self, path: Path):
        self.path = path
        self._needs_write_probe = False
        try:
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
            finally:
                os.close(fd)
        except OSError as exc:
            raise OutboxUnavailable("cannot open history outbox") from exc
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS pending (session_id TEXT NOT NULL, event_id TEXT NOT NULL, "
                       "payload TEXT NOT NULL, PRIMARY KEY (session_id, event_id))")

    @contextmanager
    def _connection(self):
        try:
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("PRAGMA synchronous=EXTRA")
                yield db
        except sqlite3.Error as exc:
            self._needs_write_probe = True
            raise OutboxUnavailable("local history outbox is unavailable") from exc

    def check(self) -> None:
        if self._needs_write_probe:
            # Commit a real write after a disk error. This reserved empty session
            # never becomes visible to a reader (both statements share a transaction).
            with self._connection() as db:
                db.execute("INSERT INTO pending VALUES ('', '', '')")
                db.execute("DELETE FROM pending WHERE session_id = '' AND event_id = ''")
            self._needs_write_probe = False
        else:
            with self._connection() as db:
                db.execute("SELECT 1 FROM pending LIMIT 1").fetchone()

    def put(self, sid: str, events: list[dict]) -> None:
        with self._connection() as db:
            db.executemany("INSERT INTO pending VALUES (?, ?, ?) ON CONFLICT(session_id, event_id) DO NOTHING",
                           [(sid, event["event_id"], json.dumps(event, separators=(",", ":"))) for event in events])

    def sessions(self) -> list[str]:
        with self._connection() as db:
            return [row[0] for row in db.execute("SELECT session_id FROM pending GROUP BY session_id ORDER BY min(rowid)")]

    def batch(self, sid: str) -> list[dict]:
        with self._connection() as db:
            rows = db.execute("SELECT payload FROM pending WHERE session_id = ? ORDER BY rowid LIMIT 128", (sid,))
            return [json.loads(row[0]) for row in rows]

    def acknowledge(self, sid: str, events: list[dict]) -> None:
        with self._connection() as db:
            db.executemany("DELETE FROM pending WHERE session_id = ? AND event_id = ?",
                           [(sid, event["event_id"]) for event in events])


def outbox_path() -> Path:
    configured = os.environ.get("ENCORE_HISTORY_OUTBOX")
    if configured:
        return Path(configured).expanduser()
    state = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(state) / "encore-server" / "history-outbox.sqlite3"


class SessionArchive:
    """Commit FINAL metadata locally before delivery, drain it independently to PostgreSQL."""

    def __init__(self, store: PostgresHistory, say=lambda line: None) -> None:
        self.store = store
        self.say = say
        self.outbox = FinalOutbox(outbox_path())
        self._cursors: dict[str, int] = {}
        self._stage_locks: dict[str, asyncio.Lock] = {}
        self._drain_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._writer: asyncio.Task | None = None

    async def check(self) -> None:
        await asyncio.to_thread(self.outbox.check)
        await self.store.check()

    def start(self) -> None:
        if self._writer is None:
            self._writer = asyncio.create_task(self._retry())

    async def stage(self, session: ServerSession) -> bool:
        sid = session.session_id
        async with self._stage_locks.setdefault(sid, asyncio.Lock()):
            pending = session.results_after(self._cursors.get(sid, 0))
            if not pending:
                return False
            finals = [msg["event"] for msg in pending if msg["event"]["phase"] == "FINAL"]
            if finals:
                await asyncio.to_thread(self.outbox.put, sid, finals)
                self._wake.set()
            self._cursors[sid] = pending[-1]["cursor"]
            return bool(finals)

    async def notify(self, session: ServerSession) -> None:
        if await self.stage(session):
            self.start()

    async def _retry(self) -> None:
        delay = 1
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                for sid in await asyncio.to_thread(self.outbox.sessions):
                    await self.drain(sid)
            except HistoryUnavailable as exc:
                source = "local history outbox" if isinstance(exc, OutboxUnavailable) else "PostgreSQL history"
                self.say(f"{source} unavailable; retrying in {delay}s")
                try:
                    await asyncio.wait_for(self._stopping.wait(), delay)
                except asyncio.TimeoutError:
                    pass
                delay = min(30, delay * 2)
            else:
                delay = 1
                await self._wake.wait()

    async def stop(self) -> None:
        if self._writer is not None:
            self._stopping.set()
            self._wake.set()
            await self._writer
            self._writer = None

    async def drain(self, sid: str) -> None:
        async with self._drain_locks.setdefault(sid, asyncio.Lock()):
            while events := await asyncio.to_thread(self.outbox.batch, sid):
                await self.store.save(sid, events)
                # A restart between the SQL commit and this deletion reuses the same PostgreSQL key.
                await asyncio.to_thread(self.outbox.acknowledge, sid, events)

    async def flush(self, session: ServerSession) -> None:
        await self.stage(session)
        await self.drain(session.session_id)
