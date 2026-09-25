"""Stable serialization, UTC timestamps and interprocess ownership."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: str | datetime) -> datetime:
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if value.tzinfo is None:
        raise ValueError("timezone_required")
    return value.astimezone(timezone.utc)


def iso(value: str | datetime, *, timespec: str = "microseconds") -> str:
    return timestamp(value).isoformat(timespec=timespec).replace("+00:00", "Z")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def unique_object(pairs):
    """Reject duplicate JSON keys, including in nested objects."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class CredentialError(ValueError):
    """A credential validation reason that never includes secret contents."""


def read_api_key(key: str = "", *, filename: str | Path | None = None) -> str:
    """Validate the caller-selected source; provider adapters own source priority."""
    if filename:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise CredentialError("unsafe_key_file")
            raw = stream.read(4097)
            if len(raw) > 4096:
                raise CredentialError("invalid_key")
            key = raw.decode("ascii")
    key = key.strip()
    if not key:
        raise CredentialError("missing_key")
    if len(key) > 4096 or any(c.isspace() for c in key) or not key.isascii():
        raise CredentialError("invalid_key")
    return key


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, (canonical(value) + "\n").encode())


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
        fsync_dir(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def owner_lock(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "owner.lock").open("a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("data_directory_in_use") from exc
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Budget:
    def __init__(self, seconds: float, cancelled=None):
        self.deadline = time.monotonic() + max(0, seconds)
        self.cancelled = cancelled or (lambda: False)

    def check(self) -> float:
        if self.cancelled():
            raise InterruptedError("cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline_exceeded")
        return remaining
