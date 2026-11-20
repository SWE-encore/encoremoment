import asyncio
import unittest

from encore_server.stream import messages_with_idle


class SlowReceive(unittest.IsolatedAsyncioTestCase):
    async def test_delayed_message_survives_multiple_idle_ticks(self):
        complete = asyncio.Event()
        reads = 0

        async def recv():
            nonlocal reads
            reads += 1
            await complete.wait()
            return b"complete frame"

        messages = messages_with_idle(recv, .005)
        self.assertIsNone(await anext(messages))
        self.assertIsNone(await anext(messages))
        complete.set()
        self.assertEqual(await anext(messages), b"complete frame")
        self.assertEqual(reads, 1)
        await messages.aclose()

    async def test_leaving_session_cancels_pending_read(self):
        cancelled = asyncio.Event()

        async def recv():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        messages = messages_with_idle(recv, .005)
        self.assertIsNone(await anext(messages))
        await messages.aclose()
        self.assertTrue(cancelled.is_set())
