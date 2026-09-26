"""Durable receipts and a process-level single-writer lock (Windows and POSIX)."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator

_local = threading.local()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def file_identity(path: str | Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.is_file():
        return None
    sha = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(block)
    return {'size': path.stat().st_size, 'sha256': sha.hexdigest()}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


@contextmanager
def output_lock(directory: str | Path) -> Iterator[None]:
    """OS releases the lock on process exit; the persistent lock file is not stale state."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = str(directory)
    held = getattr(_local, 'held', {})
    if key in held:
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    stream = (directory / '.optimizer.lock').open('a+b')
    if stream.seek(0, os.SEEK_END) == 0:
        stream.write(b'0')
        stream.flush()
    stream.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        raise RuntimeError(f'Another optimizer is writing to {directory}; wait for it to finish.') from exc
    held[key] = 1
    _local.held = held
    try:
        yield
    finally:
        del held[key]
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def case_reservations(directory: str | Path) -> dict[str, Any]:
    path=Path(directory)/'case_reservations.json'
    return json.loads(path.read_text()) if path.exists() else {}


def reserve_case(directory: str | Path, run_id: str, owner: dict[str, Any]) -> None:
    # Callers hold the common output lock. Persist before a separate plan checkpoint.
    reservations=case_reservations(directory)
    if run_id in reservations and reservations[run_id] != owner:
        raise ValueError(f'Case ID {run_id} is reserved by another experiment.')
    reservations[run_id]=owner
    atomic_json(Path(directory)/'case_reservations.json',reservations)
