"""Shared process locks for dual Paper writers and recovery snapshots."""
from contextlib import ExitStack, contextmanager
import fcntl
from pathlib import Path


@contextmanager
def account_locks(paths):
    # Stable ordering avoids deadlocks when callers select overlapping accounts.
    with ExitStack() as stack:
        for path in sorted({Path(path).resolve() for path in paths}):
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context(path.with_suffix(path.suffix + ".lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Paper accounts busy; retry after the active operation") from exc
        yield
