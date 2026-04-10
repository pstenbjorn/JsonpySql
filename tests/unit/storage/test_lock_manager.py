"""Tests for storage/lock_manager.py."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from jsonpysql.exceptions import LockTimeoutError, StorageError
from jsonpysql.storage.lock_manager import LockManager


@pytest.fixture
def lock_file(tmp_path: Path) -> Path:
    """Return a pre-created file suitable for locking."""
    p = tmp_path / "test.jsonl"
    p.touch()
    return p


# ---------------------------------------------------------------------------
# Shared lock — basic behaviour
# ---------------------------------------------------------------------------


class TestSharedLock:
    def test_acquires_and_releases(self, lock_file: Path) -> None:
        lm = LockManager()
        with lm.shared(lock_file):
            pass  # must not raise

    def test_sequential_shared_locks_succeed(self, lock_file: Path) -> None:
        lm = LockManager()
        with lm.shared(lock_file):
            pass
        with lm.shared(lock_file):
            pass

    def test_missing_file_raises_storage_error(self, tmp_path: Path) -> None:
        lm = LockManager()
        with pytest.raises(StorageError):
            with lm.shared(tmp_path / "nonexistent.jsonl"):
                pass


# ---------------------------------------------------------------------------
# Exclusive lock — basic behaviour
# ---------------------------------------------------------------------------


class TestExclusiveLock:
    def test_acquires_and_releases(self, lock_file: Path) -> None:
        lm = LockManager()
        with lm.exclusive(lock_file):
            pass  # must not raise

    def test_sequential_exclusive_locks_succeed(self, lock_file: Path) -> None:
        lm = LockManager()
        with lm.exclusive(lock_file):
            pass
        with lm.exclusive(lock_file):
            pass

    def test_missing_file_raises_storage_error(self, tmp_path: Path) -> None:
        lm = LockManager()
        with pytest.raises(StorageError):
            with lm.exclusive(tmp_path / "nonexistent.jsonl"):
                pass


# ---------------------------------------------------------------------------
# Timeout — contention between threads
#
# flock() locks are tied to open file descriptions, not to processes or
# threads.  Because each LockManager context manager opens a fresh fd, two
# threads contend correctly even within the same process.
# ---------------------------------------------------------------------------


def _hold_exclusive(
    path: Path,
    ready: threading.Event,
    release: threading.Event,
    errors: list[Exception],
) -> None:
    """Thread target: acquire exclusive lock, signal ready, wait for release."""
    lm = LockManager(timeout=5.0)
    try:
        with lm.exclusive(path):
            ready.set()
            release.wait(timeout=10.0)
    except Exception as exc:
        errors.append(exc)
        ready.set()


class TestTimeout:
    def test_exclusive_times_out_when_exclusive_held(self, lock_file: Path) -> None:
        ready = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        t = threading.Thread(
            target=_hold_exclusive,
            args=(lock_file, ready, release, errors),
            daemon=True,
        )
        t.start()
        ready.wait(timeout=5.0)
        assert not errors, f"Holder thread failed: {errors}"

        lm = LockManager(timeout=0.2)
        with pytest.raises(LockTimeoutError):
            with lm.exclusive(lock_file):
                pass

        release.set()
        t.join(timeout=5.0)
        assert not errors

    def test_shared_times_out_when_exclusive_held(self, lock_file: Path) -> None:
        ready = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        t = threading.Thread(
            target=_hold_exclusive,
            args=(lock_file, ready, release, errors),
            daemon=True,
        )
        t.start()
        ready.wait(timeout=5.0)
        assert not errors, f"Holder thread failed: {errors}"

        lm = LockManager(timeout=0.2)
        with pytest.raises(LockTimeoutError):
            with lm.shared(lock_file):
                pass

        release.set()
        t.join(timeout=5.0)
        assert not errors

    def test_lock_succeeds_after_holder_releases(self, lock_file: Path) -> None:
        """A lock unavailable during contention is acquirable once released."""
        ready = threading.Event()
        release = threading.Event()
        errors: list[Exception] = []

        t = threading.Thread(
            target=_hold_exclusive,
            args=(lock_file, ready, release, errors),
            daemon=True,
        )
        t.start()
        ready.wait(timeout=5.0)

        # Release the holder, then immediately acquire with generous timeout.
        release.set()
        t.join(timeout=5.0)

        lm = LockManager(timeout=2.0)
        with lm.exclusive(lock_file):
            pass  # must succeed

        assert not errors
