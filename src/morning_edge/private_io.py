"""Owner-private filesystem helpers for licensed market-data artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


OWNER_DIRECTORY_MODE = 0o700
OWNER_FILE_MODE = 0o600


def ensure_private_directory(path: str | Path) -> Path:
    """Create or tighten an owner-controlled directory."""

    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True, mode=OWNER_DIRECTORY_MODE)
    stat_result = directory.stat()
    if stat_result.st_uid != os.geteuid():
        raise PermissionError(f"refusing to change permissions on non-owned directory: {directory}")
    os.chmod(directory, OWNER_DIRECTORY_MODE)
    return directory


def harden_private_file(path: str | Path) -> None:
    """Set an existing owner-controlled file to mode 0600."""

    destination = Path(path)
    try:
        stat_result = destination.stat()
    except FileNotFoundError:
        return
    if stat_result.st_uid != os.geteuid():
        raise PermissionError(f"refusing to change permissions on non-owned file: {destination}")
    os.chmod(destination, OWNER_FILE_MODE)


def harden_sqlite_files(path: str | Path) -> None:
    """Harden a SQLite database and any WAL or shared-memory sidecars."""

    database = Path(path)
    for candidate in (
        database,
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        harden_private_file(candidate)


def write_private_bytes(path: str | Path, content: bytes) -> Path:
    """Atomically write bytes with a private parent and mode 0600."""

    destination = Path(path)
    ensure_private_directory(destination.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, OWNER_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        harden_private_file(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def write_private_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
    """Atomically write text through the owner-private byte boundary."""

    return write_private_bytes(path, content.encode(encoding))
