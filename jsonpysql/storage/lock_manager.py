"""File-level locking for the JsonpySql storage engine.

Writers acquire an exclusive lock; readers acquire a shared lock.
Lock acquisition is non-blocking internally and retried until the
configurable timeout expires, at which point ``LockTimeoutError`` is raised.
"""

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from jsonpysql.exceptions import LockTimeoutError, StorageError
from jsonpysql.utils.platform import release_lock, try_lock_exclusive, try_lock_shared

# Seconds to sleep between non-blocking lock attempts.
_POLL_INTERVAL: float = 0.05


class LockManager:
    """Acquires and releases shared/exclusive file locks with timeout.

    Each context manager call opens a fresh file descriptor so that
    separate callers (including different threads in the same process)
    contend correctly via the OS lock table.

    Args:
        timeout: Maximum seconds to wait for a lock before raising
            ``LockTimeoutError``. Default is ``5.0``.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self._timeout = timeout

    @contextmanager
    def shared(self, path: Path) -> Iterator[None]:
        """Hold a shared (read) lock on *path* for the duration of the block.

        Args:
            path: Path to the file to lock. The file must exist.

        Yields:
            Nothing; the lock is held until the ``with`` block exits.

        Raises:
            StorageError: If *path* cannot be opened.
            LockTimeoutError: If the lock cannot be acquired within
                ``self._timeout`` seconds.
        """
        fp = self._open(path)
        try:
            self._acquire(fp.fileno(), exclusive=False)
            yield
        finally:
            release_lock(fp.fileno())
            fp.close()

    @contextmanager
    def exclusive(self, path: Path) -> Iterator[None]:
        """Hold an exclusive (write) lock on *path* for the duration of the block.

        Args:
            path: Path to the file to lock. The file must exist.

        Yields:
            Nothing; the lock is held until the ``with`` block exits.

        Raises:
            StorageError: If *path* cannot be opened.
            LockTimeoutError: If the lock cannot be acquired within
                ``self._timeout`` seconds.
        """
        fp = self._open(path)
        try:
            self._acquire(fp.fileno(), exclusive=True)
            yield
        finally:
            release_lock(fp.fileno())
            fp.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _open(self, path: Path):  # type: ignore[return]
        """Open *path* in binary-read mode, wrapping OSError as StorageError.

        Args:
            path: Path to open.

        Returns:
            A binary file object.

        Raises:
            StorageError: If the file cannot be opened.
        """
        try:
            return open(path, "rb")
        except OSError as exc:
            raise StorageError(f"Cannot open {path} for locking: {exc}") from exc

    def _acquire(self, fd: int, exclusive: bool) -> None:
        """Poll until the requested lock is granted or the timeout expires.

        Args:
            fd: File descriptor to lock.
            exclusive: ``True`` for an exclusive lock; ``False`` for shared.

        Raises:
            LockTimeoutError: When the timeout expires before the lock is
                granted.
        """
        lock_fn = try_lock_exclusive if exclusive else try_lock_shared
        kind = "exclusive" if exclusive else "shared"
        deadline = time.monotonic() + self._timeout

        while True:
            if lock_fn(fd):
                return
            if time.monotonic() >= deadline:
                raise LockTimeoutError(
                    f"Could not acquire {kind} lock within {self._timeout}s"
                )
            time.sleep(_POLL_INTERVAL)
