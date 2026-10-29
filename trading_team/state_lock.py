"""Local shared-state exclusion and crash-safe single-file replacement."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from threading import local


class StateBusy(RuntimeError):
    """A cooperating writer owns the state; retry from fresh inputs later."""


_held = local()


@contextmanager
def state_lock(team: Path, name: str = "shared-state"):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("invalid state lock name")
    path = Path(team).resolve() / ".runs" / "locks" / f"{name}.lock"
    key = (os.getpid(), path)
    owned = getattr(_held, "paths", None)
    if owned is None:
        owned = _held.paths = set()
    if key in owned:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StateBusy("shared state busy; retry after the active operation") from exc
        owned.add(key)
        try:
            yield
        finally:
            owned.remove(key)
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            if path.exists():
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, value: object) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))
