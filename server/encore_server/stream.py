"""Yield complete WebSocket messages, or None on an idle polling tick."""
import asyncio


async def messages_with_idle(recv, interval):
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(recv())
            ready, _ = await asyncio.wait({pending}, timeout=interval)
            if ready:
                data = pending.result()
                pending = None
                yield data
            else:
                # Preserve partial frames across ticks until recv() finishes.
                yield None
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
