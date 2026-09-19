"""Atomic file writes and self-write echo suppression (CONTRACTS §2)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["atomic_write", "read_bytes", "SelfWriteRegistry"]


def atomic_write(path: Path, data: bytes, *, fsync: bool = True) -> None:
    """Write ``data`` to ``path`` atomically.

    Writes to ``.<name>.tmp.<pid>`` in the same directory (so the watcher's
    ``*.md`` pattern never matches the temp file and ``os.replace`` stays on one
    filesystem), fsyncs the file, renames over the target, then fsyncs the
    directory so the rename itself is durable.
    """
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            if fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    if fsync:
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def read_bytes(path: Path) -> bytes | None:
    """Return the file's bytes, or ``None`` if it does not exist."""
    try:
        return Path(path).read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _key(path: Path) -> str:
    return os.path.realpath(os.fspath(path))


class SelfWriteRegistry:
    """path -> sha256 of bytes we wrote; used to swallow our own watcher echo once.

    Keys are resolved paths so ``/var/...`` and ``/private/var/...`` (and any
    symlinked project dir) collapse to the same entry.
    """

    def __init__(self) -> None:
        self._digests: dict[str, str] = {}

    def note(self, path: Path, data: bytes) -> None:
        self._digests[_key(path)] = _digest(data)

    def is_echo(self, path: Path, data: bytes) -> bool:
        """True (and forgets) if ``data`` matches what we last wrote to ``path``.

        A non-matching digest means an external edit happened after our write;
        the entry is forgotten too, and False is returned.
        """
        expected = self._digests.pop(_key(path), None)
        if expected is None:
            return False
        return expected == _digest(data)

    def forget(self, path: Path) -> None:
        self._digests.pop(_key(path), None)

    def __len__(self) -> int:
        return len(self._digests)

    def __contains__(self, path: object) -> bool:
        if not isinstance(path, (str, Path)):
            return False
        return _key(Path(path)) in self._digests
