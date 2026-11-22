"""Lock the local clip index while changing media ownership or publication state."""
import json
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path


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


def _prefix(index):
    return ".encore-camera-" + uuid.uuid5(uuid.NAMESPACE_URL, index.name).hex[:12] + "-"


class CaptureDirectory:
    """The owner holds an advisory lease until capture and export are stopped."""

    def __init__(self, index):
        self.index = index
        with index_lock(index):
            self.name = tempfile.mkdtemp(prefix=_prefix(index), dir=index.parent)
            self.lock = (Path(self.name) / "owner.lock").open("a")
            _lock(self.lock)

    def cleanup(self):
        with index_lock(self.index):
            self.lock.close()
            shutil.rmtree(self.name)


def recover(index):
    """Reap only interrupted captures associated with this index. Active owners keep their lock."""
    if not index.parent.exists():
        return
    prefix = _prefix(index)
    if not index.exists() and not any(index.parent.glob(prefix + "*")):
        return
    with index_lock(index):
        active = set()
        for directory in index.parent.glob(prefix + "*"):
            if directory.is_symlink() or not directory.is_dir():
                continue
            try:
                with (directory / "owner.lock").open("a") as lock:
                    _lock(lock, wait=False)
                shutil.rmtree(directory)
            except BlockingIOError:
                active.add(directory.name)
            except FileNotFoundError:
                pass  # owner closed normally during the scan
        if not index.exists():
            return
        data = json.loads(index.read_text())
        changed = False
        for row in data["clips"]:
            owner = row.get("capture_owner", "")
            if not owner.startswith(prefix) or owner in active or row.get("status") != "pending":
                continue
            # Only a recorder-generated pending UUID can own an unindexed
            # WebM renamed just before a crash in the index commit.
            clip_id = row["clip_id"]
            if len(clip_id) == 32 and all(char in "0123456789abcdef" for char in clip_id):
                orphan = clip_id + ".webm"
                if not any(other.get("path") == orphan for other in data["clips"]):
                    (index.parent / orphan).unlink(missing_ok=True)
            row.update(status="unavailable", reason="interrupted_capture", path=None)
            changed = True
        if changed:
            atomic_index(index, data)


def atomic_index(index, data, *, media=None, destination=None):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=index.parent, delete=False) as out:
            tmp = Path(out.name)
            json.dump(data, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        if destination is not None:
            if destination.exists():
                raise OSError(f"Clip destination already exists: {destination.name}")
            os.replace(media, destination)
        try:
            os.replace(tmp, index)
        except OSError:
            if destination is not None:
                destination.unlink(missing_ok=True)
            raise
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
