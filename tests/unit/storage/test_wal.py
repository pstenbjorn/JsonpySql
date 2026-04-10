"""Tests for storage/wal.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonpysql.exceptions import StorageError, WALReplayError
from jsonpysql.storage.wal import WAL, WALEntry, find_wal_files


# ---------------------------------------------------------------------------
# WALEntry
# ---------------------------------------------------------------------------


class TestWALEntry:
    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        entry = WALEntry(op="insert", collection="orders", doc_id="o1", document={"x": 1})
        result = WALEntry.from_dict(entry.to_dict())
        assert result == entry

    def test_from_dict_delete_has_null_document(self) -> None:
        data = {"op": "delete", "collection": "orders", "doc_id": "o1", "document": None}
        entry = WALEntry.from_dict(data)
        assert entry.document is None

    def test_from_dict_missing_key_raises_storage_error(self) -> None:
        with pytest.raises(StorageError):
            WALEntry.from_dict({"op": "insert", "collection": "x"})  # missing doc_id

    def test_from_dict_invalid_op_raises_storage_error(self) -> None:
        with pytest.raises(StorageError):
            WALEntry.from_dict(
                {"op": "upsert", "collection": "x", "doc_id": "1", "document": {}}
            )


# ---------------------------------------------------------------------------
# WAL append / read_entries
# ---------------------------------------------------------------------------


@pytest.fixture
def wal(tmp_path: Path) -> WAL:
    return WAL(tmp_path / "transaction.wal")


class TestWALAppendAndRead:
    def test_append_and_read_single_entry(self, wal: WAL) -> None:
        entry = WALEntry(op="insert", collection="orders", doc_id="o1", document={"v": 1})
        wal.append(entry)
        entries = wal.read_entries()
        assert len(entries) == 1
        assert entries[0] == entry

    def test_read_entries_deduplicates_by_doc_id(self, wal: WAL) -> None:
        wal.append(WALEntry("insert", "col", "1", {"v": 1}))
        wal.append(WALEntry("update", "col", "1", {"v": 2}))
        entries = wal.read_entries()
        assert len(entries) == 1
        assert entries[0].op == "update"
        assert entries[0].document == {"v": 2}

    def test_read_entries_dedup_across_collections(self, wal: WAL) -> None:
        """Same doc_id in different collections are treated independently."""
        wal.append(WALEntry("insert", "col_a", "1", {"v": 1}))
        wal.append(WALEntry("insert", "col_b", "1", {"v": 2}))
        entries = wal.read_entries()
        assert len(entries) == 2

    def test_read_empty_wal_returns_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.wal"
        path.touch()
        entries = WAL(path).read_entries()
        assert entries == []

    def test_multiple_different_docs(self, wal: WAL) -> None:
        wal.append(WALEntry("insert", "col", "1", {"x": 1}))
        wal.append(WALEntry("insert", "col", "2", {"x": 2}))
        assert len(wal.read_entries()) == 2

    def test_append_missing_file_creates_it(self, tmp_path: Path) -> None:
        path = tmp_path / "new.wal"
        w = WAL(path)
        w.append(WALEntry("insert", "c", "1", {}))
        assert path.exists()


# ---------------------------------------------------------------------------
# iter_raw
# ---------------------------------------------------------------------------


class TestIterRaw:
    def test_iter_raw_preserves_duplicates(self, wal: WAL) -> None:
        wal.append(WALEntry("insert", "col", "1", {"v": 1}))
        wal.append(WALEntry("update", "col", "1", {"v": 2}))
        entries = list(wal.iter_raw())
        assert len(entries) == 2
        assert entries[0].op == "insert"
        assert entries[1].op == "update"

    def test_iter_raw_corrupt_line_raises_wal_replay_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.wal"
        path.write_text("not-json\n", encoding="utf-8")
        with pytest.raises(WALReplayError):
            list(WAL(path).iter_raw())


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestWALLifecycle:
    def test_exists_false_when_file_absent(self, tmp_path: Path) -> None:
        assert WAL(tmp_path / "nope.wal").exists() is False

    def test_exists_true_after_append(self, wal: WAL) -> None:
        wal.append(WALEntry("insert", "c", "1", {}))
        assert wal.exists() is True

    def test_delete_removes_file(self, wal: WAL) -> None:
        wal.append(WALEntry("insert", "c", "1", {}))
        wal.delete()
        assert not wal._path.exists()

    def test_delete_nonexistent_is_noop(self, tmp_path: Path) -> None:
        WAL(tmp_path / "ghost.wal").delete()  # must not raise

    def test_read_missing_file_raises_wal_replay_error(self, tmp_path: Path) -> None:
        with pytest.raises(WALReplayError):
            WAL(tmp_path / "missing.wal").read_entries()

    def test_corrupt_wal_raises_wal_replay_error(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.wal"
        path.write_text("not-json\n", encoding="utf-8")
        with pytest.raises(WALReplayError):
            WAL(path).read_entries()

    def test_corrupt_entry_missing_key_raises(self, tmp_path: Path) -> None:
        import json

        path = tmp_path / "bad.wal"
        path.write_text(json.dumps({"op": "insert"}) + "\n", encoding="utf-8")
        with pytest.raises(WALReplayError):
            WAL(path).read_entries()


# ---------------------------------------------------------------------------
# find_wal_files
# ---------------------------------------------------------------------------


class TestFindWalFiles:
    def test_empty_directory_returns_empty(self, tmp_path: Path) -> None:
        assert find_wal_files(tmp_path) == []

    def test_finds_wal_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.wal").touch()
        (tmp_path / "b.wal").touch()
        (tmp_path / "other.jsonl").touch()
        paths = {p.name for p in find_wal_files(tmp_path)}
        assert paths == {"a.wal", "b.wal"}
