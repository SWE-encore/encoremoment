"""Lock the local clip index while changing media ownership or publication state."""
import os
from contextlib import contextmanager


def _lock(file, *, wait=True):
    if os.name == "nt":
        import msvcrt
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK if wait else msvcrt.LK_NBLCK, 1)
        except PermissionError as exc:
            if not wait:
                raise BlockingIOError from exc
            raise
    else:
        import fcntl
        fcntl.flock(file, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))


@contextmanager
def index_lock(index):
    index.parent.mkdir(parents=True, exist_ok=True)
    with index.with_suffix(index.suffix + ".lock").open("a") as lock:
        _lock(lock)
        yield
