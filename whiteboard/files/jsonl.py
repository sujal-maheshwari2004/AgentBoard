"""Multi-process-safe JSONL append and an incremental tailer (CONTRACTS §2)."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path

__all__ = ["append_jsonl", "JsonlTailer"]

log = logging.getLogger(__name__)


def append_jsonl(path: Path, record: dict) -> None:
    """Append one JSON record as a single line.

    ``O_APPEND`` plus an exclusive ``flock`` plus exactly one ``os.write`` means
    concurrent writers from other processes never interleave bytes.
    """
    line = (json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.write(fd, line)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


class JsonlTailer:
    """Read new complete records from a growing JSONL file.

    Tracks a byte offset, the file's inode (rotation) and a partial-line
    buffer (torn writes). On inode change or shrink it restarts from byte 0.
    Lines that fail to parse are skipped with a warning.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.offset = 0
        self.inode: int | None = None
        self._buffer = b""

    def _reset(self) -> None:
        self.offset = 0
        self._buffer = b""

    def read_new(self) -> list[dict]:
        """Return records appended since the last call (or since construction)."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.inode = None
            self._reset()
            return []

        if (self.inode is not None and st.st_ino != self.inode) or st.st_size < self.offset:
            self._reset()
        self.inode = st.st_ino

        if st.st_size == self.offset:
            return []

        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        self.offset += len(chunk)
        return self._consume(chunk)

    def read_all(self) -> list[dict]:
        """Re-read every record from byte 0; leaves the offset at end of file."""
        self._reset()
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self.inode = None
            return []
        self.inode = st.st_ino
        with open(self.path, "rb") as f:
            chunk = f.read()
        self.offset = len(chunk)
        return self._consume(chunk)

    def _consume(self, chunk: bytes) -> list[dict]:
        data = self._buffer + chunk
        records: list[dict] = []
        start = 0
        while True:
            nl = data.find(b"\n", start)
            if nl < 0:
                break
            line = data[start:nl]
            start = nl + 1
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                log.warning("skipping unparsable line in %s: %r", self.path, line[:120])
                continue
            if isinstance(rec, dict):
                records.append(rec)
            else:
                log.warning("skipping non-object line in %s: %r", self.path, line[:120])
        self._buffer = data[start:]
        return records
