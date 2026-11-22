"""Clip deletion and cleanup state (R7, Jira EM-16). Qt-free, so it can be unit-tested.

Specification IV-G:

    on delete_clip(clip_id):
      op = persist_deletion_intent(clip_id)
      hide_playback_link(); show(PENDING)
      cancel_exports_and_mark_tombstone(clip_id)
      unlink_clip_from_all_associated_events()
      delete_clip_files_thumbnails_and_temp_exports()
      retry_server_media_cleanup(op)
    on remote_ack(op) and local_cleanup_done(op):
      mark_completed(op); show(COMPLETED)

How this module does it:

- A deletion journal next to the clip index (``<index>.deletions.json``) is
  written first, atomically. Every clip in it is a tombstone, kept forever.
- Tombstones win over the index: ``load_clips`` shows a tombstoned clip as
  ``deletion_pending`` or ``deleted`` with no file, whatever the clip pipeline
  writes later, so a late result cannot restore media or a play link.
  ``may_commit_export`` tells the pipeline not to commit such a clip.
- Linked events see the change at once because links come from the clip
  entry itself (``event_ids``); a shared clip disappears from all its events.
- Local cleanup removes the clip file, thumbnail and temporary exports, but
  only files inside the clip folder and not used by another retained clip
  (reference counting). It is idempotent, so retries and restarts are safe.
- Remote cleanup goes through a ``RemoteCleanup`` with the persistent
  operation ID. Offline or failed requests stay pending and are retried.
  Without a remote (the server has no deletion API yet) only local cleanup
  is required.
- On restart, incomplete deletions are resumed before export commits are
  allowed.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from .model import Clip, ClipStatus, load_index
from .storage import index_lock

RETRY_MS = 2_000


class RemoteCleanup(Protocol):
    """Server side of R7: remove temporary server payloads and references for a clip.

    Returns True once the server acknowledged ``op_id`` (repeating an op_id must
    be safe), False while offline or on failure (the request is retried).
    """

    def request(self, op_id: str, session_id: str | None, clip_id: str) -> bool: ...


@dataclass(frozen=True)
class Deletion:
    op_id: str
    clip_id: str
    session_id: str | None
    files: tuple[str, ...]          # paths relative to the clip folder, captured at request time
    requested_ms: int
    local_done: bool = False
    remote_done: bool = False
    remote_required: bool = False
    completed_ms: int | None = None
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.completed_ms is not None

    def status_text(self) -> str:
        if self.completed:
            return "🗑 Deleted"
        if self.error:
            return f"🗑 Deleting — retrying: {self.error}"
        if not self.local_done:
            return "🗑 Deleting — removing local files"
        return "🗑 Deleting — waiting for server cleanup (offline or not acknowledged)"


def journal_path(index_path: Path) -> Path:
    return index_path.with_name(index_path.stem + ".deletions.json")


class DeletionManager:
    def __init__(
        self,
        index_path: Path,
        *,
        remote: RemoteCleanup | None = None,
        cancel_export: Callable[[str], None] | None = None,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),   # wall clock: stored in the journal
        retry_ms: int = RETRY_MS,
    ) -> None:
        self.index_path = Path(index_path)
        self.base_dir = self.index_path.parent
        self.journal = journal_path(self.index_path)
        self.remote = remote
        self.cancel_export = cancel_export
        self.clock = clock
        self.retry_ms = retry_ms
        self.journal_error: str | None = None
        self.ops: dict[str, Deletion] = {}
        self._missing_journal = False
        self._stamp = object()
        self.version = 0                 # bumps on every change, for the UI
        self._next_retry_ms = 0
        self.resumed = False
        self.resume()                    # restart: finish incomplete deletions first

    # ---- queries ------------------------------------------------------------------

    def is_tombstoned(self, clip_id: str) -> bool:
        return clip_id in self.ops

    def state(self, clip_id: str) -> Deletion | None:
        return self.ops.get(clip_id)

    def may_commit_export(self, clip_id: str) -> bool:
        """For the clip pipeline: commit an export only when this is True (IV-G, IV-J)."""
        self._refresh_journal()
        return self.resumed and not self.journal_error and clip_id not in self.ops

    def load_clips(self) -> list[Clip]:
        """The clip index with tombstones applied: deleted clips never regain a file."""
        self._refresh_journal()
        if self.journal_error:
            raise ValueError(self.journal_error)
        return [self._apply(c) for c in load_index(self.index_path)]

    def _apply(self, clip: Clip) -> Clip:
        op = self.ops.get(clip.clip_id)
        if op is None:
            return clip
        status = ClipStatus.DELETED if op.completed else ClipStatus.DELETION_PENDING
        return replace(clip, status=status, path=None, thumbnail=None, temp_paths=())

    # ---- delete -------------------------------------------------------------------

    def delete(self, clip_id: str) -> Deletion:
        """Request deletion. Repeating it returns the same operation."""
        with index_lock(self.index_path):
            self._refresh_journal()
            if self.journal_error:
                raise ValueError(self.journal_error)
            if clip_id not in self.ops:
                entry = next((c for c in load_index(self.index_path) if c.clip_id == clip_id), None)
                if entry is None:
                    raise KeyError(f"clip {clip_id} is not in the index")
                files = tuple(f for f in (entry.path, entry.thumbnail, *entry.temp_paths) if f)
                self.ops[clip_id] = Deletion(
                    op_id=f"del-{uuid.uuid4().hex}", clip_id=clip_id, session_id=entry.session_id,
                    files=files, requested_ms=self.clock(), remote_required=self.remote is not None)
                self._save_journal()
                self._set_index_status_locked(clip_id, ClipStatus.DELETION_PENDING)
        if self.cancel_export is not None:
            self.cancel_export(clip_id)                          # cancel_exports (tombstone already written)
        self._next_retry_ms = 0
        self.process()
        return self.ops[clip_id]

    def tick(self) -> None:
        """Call on a timer: retries pending cleanup."""
        self._refresh_journal()
        if any(not op.completed for op in self.ops.values()) and self.clock() >= self._next_retry_ms:
            self.process()

    def resume(self) -> None:
        self.process()
        self.resumed = True

    def process(self) -> None:
        self._refresh_journal()
        if self.journal_error or not any(not op.completed for op in self.ops.values()):
            return
        with index_lock(self.index_path):
            self._refresh_journal()
            if self.journal_error:
                return
            changed = False
            for clip_id, op in list(self.ops.items()):
                if op.completed:
                    continue
                new = op if op.local_done else self._local_cleanup(op)
                if new.local_done and not new.remote_required:
                    new = replace(new, completed_ms=self.clock(), error=None)
                    self._set_index_status_locked(clip_id, ClipStatus.DELETED)
                if op != new:
                    self.ops[clip_id] = new
                    changed = True
            if changed:
                self._save_journal()
            waiting = [op for op in self.ops.values() if
                       op.local_done and op.remote_required and not op.completed and self.remote is not None]
        # Network acknowledgement never holds the writer lock. Merge with the
        # latest revision so another desktop's success cannot be rolled back.
        for op in waiting:
            result = self._remote_cleanup(op)
            with index_lock(self.index_path):
                self._refresh_journal()
                current = self.ops.get(op.clip_id)
                if self.journal_error or current is None or current.op_id != op.op_id or current.completed:
                    continue
                if result.remote_done:
                    result = replace(result, completed_ms=self.clock(), error=None)
                    self._set_index_status_locked(op.clip_id, ClipStatus.DELETED)
                if result != current:
                    self.ops[op.clip_id] = result
                    self._save_journal()
        self._next_retry_ms = self.clock() + self.retry_ms

    # ---- cleanup steps ------------------------------------------------------------

    def _local_cleanup(self, op: Deletion) -> Deletion:
        try:
            keep = self._files_in_use(exclude=op.clip_id)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return replace(op, error=f"cannot verify clip file references: {exc}")
        errors = []
        for rel in op.files:
            path = self._inside(rel)
            if path is None:
                continue                       # never delete outside the clip folder
            if path in keep:
                continue                       # still used by another retained clip
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                errors.append(f"{rel}: {exc.strerror or exc}")
        if errors:
            return replace(op, error="; ".join(errors))
        return replace(op, local_done=True, error=None)

    def _remote_cleanup(self, op: Deletion) -> Deletion:
        try:
            ok = bool(self.remote.request(op.op_id, op.session_id, op.clip_id))
        except Exception as exc:  # the remote is outside our control; keep retrying
            return replace(op, error=f"server cleanup failed: {exc}")
        return replace(op, remote_done=ok, error=None)

    def _files_in_use(self, exclude: str) -> set[Path]:
        used = set()
        for c in load_index(self.index_path, required=True):
            if c.clip_id == exclude or c.clip_id in self.ops:
                continue
            for rel in (c.path, c.thumbnail, *c.temp_paths):
                p = self._inside(rel) if rel else None
                if p is not None:
                    used.add(p)
        return used

    def _inside(self, rel: str) -> Path | None:
        base = self.base_dir.resolve()
        path = (base / rel).resolve()
        return path if path != base and path.is_relative_to(base) else None

    # ---- persistence --------------------------------------------------------------

    def _journal_stamp(self):
        try:
            stat = self.journal.stat()
        except FileNotFoundError:
            # With no journal, a change to the index can reveal a lost deletion.
            try:
                stat = self.index_path.stat()
            except FileNotFoundError:
                return None
            return ("index", stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        return ("journal", stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)

    def _refresh_journal(self):
        stamp = self._journal_stamp()
        if stamp != self._stamp:
            try:
                self.ops = self._load_journal()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.journal_error = f"Deletion journal unreadable: {exc}"
            else:
                self.journal_error = None
            self._stamp = stamp
            self.version += 1

    def _load_journal(self) -> dict[str, Deletion]:
        backup = self.journal.with_name(self.journal.name + ".corrupt")
        try:
            raw = self.journal.read_bytes()
        except FileNotFoundError:
            if (self._missing_journal or self.ops or backup.exists() or
                    any(clip.status in (ClipStatus.DELETION_PENDING, ClipStatus.DELETED)
                        for clip in load_index(self.index_path))):
                self._missing_journal = True
                raise ValueError(f"restore {self.journal.name} before resuming deletion or export")
            return {}
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("operations"), list):
                raise ValueError("expected an operations list")
            ops = {}
            for row in data["operations"]:
                if not isinstance(row, dict) or not isinstance(row.get("files"), list):
                    raise ValueError("expected a deletion with a file list")
                if any(not isinstance(path, str) for path in row["files"]):
                    raise ValueError("deletion files must contain file names")
                op = Deletion(**dict(row, files=tuple(row["files"])))
                if not isinstance(op.clip_id, str) or not op.clip_id or op.clip_id in ops:
                    raise ValueError("expected a unique clip ID")
                ops[op.clip_id] = op
            self._missing_journal = False
            return ops
        except (ValueError, KeyError, TypeError):
            # Preserve what this reader observed. Never rename a file another desktop
            # could have just replaced with a repaired journal.
            try:
                with backup.open("xb") as copy:
                    copy.write(raw)
            except FileExistsError:
                pass
            raise

    def _save_journal(self) -> None:
        data = {"version": 1, "operations": [
            dict(op.__dict__, files=list(op.files)) for op in self.ops.values()]}
        _atomic_write(self.journal, json.dumps(data, indent=2))
        self._stamp = self._journal_stamp()
        self.version += 1

    def _set_index_status_locked(self, clip_id: str, status: ClipStatus) -> None:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return                              # tombstones still apply through load_clips
        entries = data.get("clips") if isinstance(data, dict) else None
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            return  # Preserve the damaged index; the journal still holds the deletion.
        for entry in entries:
            if entry.get("clip_id") == clip_id:
                entry["status"] = status.value
                if status is ClipStatus.DELETED:
                    entry["path"] = None
                    entry.pop("thumbnail", None)
                    entry.pop("temp_paths", None)
        _atomic_write(self.index_path, json.dumps(data, indent=2))
        self.version += 1


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
