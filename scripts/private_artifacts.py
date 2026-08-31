"""Owner-private atomic writers for local market-data scripts."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile


def private_runtime_path(relative: str | Path) -> Path:
    """Resolve a private script default under the optional runtime root."""

    configured = os.environ.get("CODEX_SCREENER_HOME") or os.environ.get(
        "CODEX_SCREENER_PRIVATE_RUNTIME_ROOT"
    )
    return Path(configured).expanduser() / relative if configured else Path(relative)


def ensure_private_directory(path: Path) -> Path:
    """Create a private leaf directory without changing an existing parent."""

    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise NotADirectoryError(f"artifact parent is not a regular directory: {cursor}")
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        stat_result = directory.stat()
        if stat_result.st_uid != os.geteuid():
            raise PermissionError(f"refusing to change permissions on non-owned directory: {directory}")
        os.chmod(directory, 0o700)
    return path


def write_private_bytes(path: Path, content: bytes) -> Path:
    ensure_private_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def write_private_text(path: Path, content: str) -> Path:
    return write_private_bytes(path, content.encode("utf-8"))
