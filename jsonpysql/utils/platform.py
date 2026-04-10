"""Platform-specific file-locking primitives.

Abstracts over ``fcntl`` (Unix) and ``msvcrt`` (Windows) so that
``storage.lock_manager`` can use a single interface on all platforms.

Each function operates on a raw integer file descriptor returned by
``fileno()``.  The caller is responsible for keeping the underlying file
object alive for the duration of the lock.
"""

import sys

_IS_WINDOWS: bool = sys.platform == "win32"

if _IS_WINDOWS:  # pragma: no cover
    import msvcrt as _msvcrt

    # msvcrt.locking() is byte-range only and always exclusive.
    # We lock one byte at the start of the file as a whole-file proxy.
    _LOCK_NBYTES: int = 1

    def try_lock_shared(fd: int) -> bool:
        """Attempt a non-blocking shared (read) lock on *fd*.

        Args:
            fd: Open file descriptor.

        Returns:
            ``True`` if the lock was acquired, ``False`` otherwise.
        """
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, _LOCK_NBYTES)
            return True
        except OSError:
            return False

    def try_lock_exclusive(fd: int) -> bool:
        """Attempt a non-blocking exclusive (write) lock on *fd*.

        Args:
            fd: Open file descriptor.

        Returns:
            ``True`` if the lock was acquired, ``False`` otherwise.
        """
        try:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, _LOCK_NBYTES)
            return True
        except OSError:
            return False

    def release_lock(fd: int) -> None:
        """Release the lock held on *fd*.

        Args:
            fd: Open file descriptor whose lock should be released.
        """
        try:
            _msvcrt.locking(fd, _msvcrt.LK_UNLCK, _LOCK_NBYTES)
        except OSError:
            pass

else:
    import fcntl as _fcntl

    def try_lock_shared(fd: int) -> bool:
        """Attempt a non-blocking shared (read) lock on *fd*.

        Args:
            fd: Open file descriptor.

        Returns:
            ``True`` if the lock was acquired, ``False`` otherwise.
        """
        try:
            _fcntl.flock(fd, _fcntl.LOCK_SH | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def try_lock_exclusive(fd: int) -> bool:
        """Attempt a non-blocking exclusive (write) lock on *fd*.

        Args:
            fd: Open file descriptor.

        Returns:
            ``True`` if the lock was acquired, ``False`` otherwise.
        """
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def release_lock(fd: int) -> None:
        """Release the lock held on *fd*.

        Args:
            fd: Open file descriptor whose lock should be released.
        """
        _fcntl.flock(fd, _fcntl.LOCK_UN)
