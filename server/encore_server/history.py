"""Archive finalized event metadata. Live cursors belong to the WebSocket session."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

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


class SessionArchive:
    """Archive immutable live-log entries without making device controls wait for SQL."""
    def __init__(self, store: PostgresHistory, say=lambda line: None) -> None:
        self.store = store
        self.say = say
        self._cursors: dict[str, int] = {}
        self._writers: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def notify(self, session: ServerSession, messages: list[dict]) -> None:
        sid = session.session_id
        if sid not in self._writers and any(
            msg.get("event", {}).get("phase") == "FINAL" for msg in messages
        ):
            self._writers[sid] = asyncio.create_task(self._retry(session))

    async def _retry(self, session: ServerSession) -> None:
        sid = session.session_id
        delay = 1
        try:
            while self._cursors.get(sid, 0) < len(session.log):
                try:
                    await self.flush(session)
                    delay = 1
                except HistoryUnavailable:
                    self.say(f"{sid}: history unavailable; retrying in {delay}s")
                    await asyncio.sleep(delay)
                    delay = min(30, delay * 2)
        finally:
            self._writers.pop(sid, None)

    async def stop(self) -> None:
        writers = list(self._writers.values())
        for task in writers:
            task.cancel()
        await asyncio.gather(*writers, return_exceptions=True)

    async def flush(self, session: ServerSession) -> None:
        sid = session.session_id
        async with self._locks.setdefault(sid, asyncio.Lock()):
            pending = session.results_after(self._cursors.get(sid, 0))
            if pending:
                finals = [msg["event"] for msg in pending if msg["event"]["phase"] == "FINAL"]
                if finals:
                    await self.store.save(sid, finals)
                self._cursors[sid] = pending[-1]["cursor"]
