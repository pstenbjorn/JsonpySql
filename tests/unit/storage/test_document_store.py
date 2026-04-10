"""Tests for storage/document_store.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jsonpysql.exceptions import StorageError
from jsonpysql.storage.document_store import DocumentStore


@pytest.fixture
def store(tmp_path: Path) -> DocumentStore:
    """Return a freshly created DocumentStore."""
    s = DocumentStore(tmp_path / "col.jsonl")
    s.create()
    return s


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_create_makes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "col.jsonl"
        s = DocumentStore(path)
        s.create()
        assert path.exists()

    def test_create_raises_if_already_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "col.jsonl"
        s = DocumentStore(path)
        s.create()
        with pytest.raises(StorageError):
            s.create()

    def test_destroy_removes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "col.jsonl"
        s = DocumentStore(path)
        s.create()
        s.destroy()
        assert not path.exists()

    def test_destroy_nonexistent_is_noop(self, tmp_path: Path) -> None:
        s = DocumentStore(tmp_path / "nope.jsonl")
        s.destroy()  # must not raise


# ---------------------------------------------------------------------------
# Insert / get
# ---------------------------------------------------------------------------


class TestAppendAndGet:
    def test_get_returns_none_for_missing(self, store: DocumentStore) -> None:
        assert store.get("missing") is None

    def test_append_and_get_roundtrip(self, store: DocumentStore) -> None:
        store.append("1", {"name": "Alice", "age": 30})
        doc = store.get("1")
        assert doc == {"name": "Alice", "age": 30}

    def test_get_does_not_return_id_key(self, store: DocumentStore) -> None:
        store.append("1", {"x": 1})
        assert "_id" not in store.get("1")  # type: ignore[operator]

    def test_update_via_append_returns_latest(self, store: DocumentStore) -> None:
        store.append("1", {"v": 1})
        store.append("1", {"v": 2})
        assert store.get("1") == {"v": 2}

    def test_multiple_documents_independent(self, store: DocumentStore) -> None:
        store.append("a", {"x": 1})
        store.append("b", {"x": 2})
        assert store.get("a") == {"x": 1}
        assert store.get("b") == {"x": 2}


# ---------------------------------------------------------------------------
# Tombstone / delete
# ---------------------------------------------------------------------------


class TestTombstone:
    def test_get_after_tombstone_returns_none(self, store: DocumentStore) -> None:
        store.append("1", {"x": 1})
        store.tombstone("1")
        assert store.get("1") is None

    def test_reinsert_after_tombstone(self, store: DocumentStore) -> None:
        store.append("1", {"x": 1})
        store.tombstone("1")
        store.append("1", {"x": 99})
        assert store.get("1") == {"x": 99}

    def test_tombstone_unknown_id_does_not_raise(self, store: DocumentStore) -> None:
        store.tombstone("ghost")  # must not raise


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


class TestScan:
    def test_scan_empty_store(self, store: DocumentStore) -> None:
        assert list(store.scan()) == []

    def test_scan_returns_live_documents(self, store: DocumentStore) -> None:
        store.append("1", {"x": 1})
        store.append("2", {"x": 2})
        docs = list(store.scan())
        ids = {d["_id"] for d in docs}
        assert ids == {"1", "2"}

    def test_scan_excludes_tombstoned(self, store: DocumentStore) -> None:
        store.append("1", {"x": 1})
        store.append("2", {"x": 2})
        store.tombstone("1")
        docs = list(store.scan())
        assert all(d["_id"] != "1" for d in docs)

    def test_scan_returns_latest_version(self, store: DocumentStore) -> None:
        store.append("1", {"v": 1})
        store.append("1", {"v": 2})
        docs = list(store.scan())
        assert len(docs) == 1
        assert docs[0]["v"] == 2

    def test_scan_includes_id_field(self, store: DocumentStore) -> None:
        store.append("abc", {"y": 7})
        docs = list(store.scan())
        assert docs[0]["_id"] == "abc"


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompaction:
    def test_compact_removes_tombstoned(self, store: DocumentStore) -> None:
        store.append("1", {"x": 1})
        store.append("2", {"x": 2})
        store.tombstone("1")
        live = [d for d in store.scan()]
        store.compact(live)
        assert store.get("1") is None
        assert store.get("2") == {"x": 2}

    def test_compact_reduces_line_count(self, store: DocumentStore) -> None:
        for i in range(5):
            store.append(str(i), {"v": i})
        store.tombstone("0")
        store.tombstone("1")
        live = list(store.scan())
        store.compact(live)
        total, tombstones = store.count_lines()
        assert tombstones == 0
        assert total == 3

    def test_compact_empty_store(self, store: DocumentStore) -> None:
        store.compact([])
        assert list(store.scan()) == []


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------


class TestStats:
    def test_count_lines_empty(self, store: DocumentStore) -> None:
        total, tombstones = store.count_lines()
        assert total == 0
        assert tombstones == 0

    def test_count_lines_with_tombstone(self, store: DocumentStore) -> None:
        store.append("1", {"x": 1})
        store.tombstone("1")
        total, tombstones = store.count_lines()
        assert total == 2
        assert tombstones == 1

    def test_file_size_grows_after_append(self, store: DocumentStore) -> None:
        before = store.file_size()
        store.append("1", {"x": 1})
        assert store.file_size() > before


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:
    def test_get_on_missing_file_raises_storage_error(self, tmp_path: Path) -> None:
        s = DocumentStore(tmp_path / "missing.jsonl")
        with pytest.raises(StorageError):
            s.get("x")

    def test_scan_corrupt_line_raises_storage_error(self, tmp_path: Path) -> None:
        path = tmp_path / "col.jsonl"
        path.write_text("not-json\n", encoding="utf-8")
        s = DocumentStore(path)
        with pytest.raises(StorageError):
            list(s.scan())
