"""The one clock every capture timestamp uses (contracts/README.md)."""

import time


def monotonic_ms() -> int:
    """PC monotonic clock in milliseconds. Never wall-clock time."""
    return time.monotonic_ns() // 1_000_000
