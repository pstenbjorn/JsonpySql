"""Supplemental tests targeting specific uncovered lines in the storage layer.

These tests are focused on error paths and edge cases that could not be
conveniently expressed in the primary per-module test files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jsonpysql.exceptions import StorageError, WALReplayError
from jsonpysql.storage.document_store import DocumentStore
from jsonpysql.storage.index_manager import IndexManager, _decode_key, _encode_key
from jsonpysql.storage.models import IndexSpec
from jsonpysql.storage.wal import WAL, WALEntry


# ---------------------------------------------------------------------------
# _encode_key / _decode_key — compound branch (lines 43, 59)
# ---------------------------------------------------------------------------


class TestKeyEncoding:
    def test_encode_tuple_key(self) -> None:
        result = _encode_key(("Alice", "Oslo"))
        assert "\x1f" in result
        assert "Alice" in result
        assert "Oslo" in result

    def test_decode_compound_key(self) -> None:
        encoded = _encode_key(("Alice", "Oslo"))
        decoded = _decode_key(encoded, is_compound=True)
        assert decoded == ("Alice", "Oslo")

    def test_decode_scalar_key(self) -> None:
        decoded = _decode_key("Alice", is_compound=False)
        assert decoded == "Alice"


# ---------------------------------------------------------------------------
# IndexManager — compound index end-to-end (lines 43, 59, 322)
# ---------------------------------------------------------------------------


class TestCompoundIndex:
    def test_compound_insert_persists_to_disk(self, tmp_path: Path) -> None:
        """Compound indexes are written to .idx files on every insert."""
        spec = IndexSpec(["name", "city"])
        im = IndexManager(tmp_path, "users", [spec])
        doc = {"_id": "a", "name": "Alice", "city": "Oslo"}
        im.on_insert("a", doc)
        # The .idx file must exist after insert.
        assert (tmp_path / "users.name_city.idx").exists()

    def test_compound_persist_and_reload(self, tmp_path: Path) -> None:
        """Compound index survives a save/load round-trip."""
        spec = IndexSpec(["name", "city"])
        im = IndexManager(tmp_path, "users", [spec])
        doc = {"_id": "a", "name": "Alice", "city": "Oslo"}
        im.on_insert("a", doc)

        im2 = IndexManager(tmp_path, "users", [spec])
        im2.load()
        # has_index_for returns False for compound (single-field only)
        assert im2.has_index_for("name") is False

    def test_compound_delete_updates_index_file(self, tmp_path: Path) -> None:
        """Deleting a doc through a compound-indexed collection re-saves the .idx."""
        spec = IndexSpec(["name", "city"])
        im = IndexManager(tmp_path, "users", [spec])
        doc = {"_id": "a", "name": "Alice", "city": "Oslo"}
        im.on_insert("a", doc)
        mtime_after_insert = (tmp_path / "users.name_city.idx").stat().st_mtime_ns
        im.on_delete("a", doc)
        mtime_after_delete = (tmp_path / "users.name_city.idx").stat().st_mtime_ns
        assert mtime_after_delete >= mtime_after_insert


# ---------------------------------------------------------------------------
# IndexManager — remove_reverse ValueError branch (lines 224-225)
# ---------------------------------------------------------------------------


class TestRemoveReverseEdgeCases:
    def test_remove_nonexistent_child_is_noop(self, tmp_path: Path) -> None:
        im = IndexManager(tmp_path, "col", [])
        im.add_reverse("parent", "child_a")
        im.remove_reverse("parent", "nonexistent_child")  # ValueError branch
        assert im.get_children("parent") == ["child_a"]


# ---------------------------------------------------------------------------
# IndexManager — _remove_from_indexes ValueError branch (lines 350-351)
# ---------------------------------------------------------------------------


class TestRemoveFromIndexesEdgeCases:
    def test_double_delete_does_not_raise(self, tmp_path: Path) -> None:
        """Removing a doc_id that has already been removed must not raise."""
        spec = IndexSpec(["name"])
        im = IndexManager(tmp_path, "col", [spec])
        doc = {"_id": "a", "name": "Alice"}
        im.on_insert("a", doc)
        im.on_delete("a", doc)
        # Second delete on same doc — key is already gone; must not raise
        im.on_delete("a", doc)


# ---------------------------------------------------------------------------
# IndexManager — _read_idx non-dict data branch (line 395)
# ---------------------------------------------------------------------------


class TestReadIdxCorrupt:
    def test_non_dict_data_falls_back_to_empty(self, tmp_path: Path) -> None:
        """load() swallows StorageError from corrupt idx; falls back to empty."""
        spec = IndexSpec(["name"])
        idx_path = tmp_path / "col.name.idx"
        idx_path.write_text(
            json.dumps({"fields": ["name"], "unique": False, "data": "BAD"}),
            encoding="utf-8",
        )
        im = IndexManager(tmp_path, "col", [spec])
        im.load()  # must not raise; falls back to empty index
        assert list(im.lookup("name", "Alice")) == []


# ---------------------------------------------------------------------------
# IndexManager — corrupt ridx falls back to empty (lines 120-121)
# ---------------------------------------------------------------------------


class TestCorruptRidx:
    def test_corrupt_ridx_loads_empty(self, tmp_path: Path) -> None:
        im = IndexManager(tmp_path, "col", [])
        im.add_reverse("p", "c")

        ridx_path = tmp_path / "col.ridx"
        ridx_path.write_text("not-json", encoding="utf-8")

        im2 = IndexManager(tmp_path, "col", [])
        im2.load()  # should not raise; falls back to empty
        assert im2.get_children("p") == []


# ---------------------------------------------------------------------------
# DocumentStore — scan() skips line with no _id (line 146)
# ---------------------------------------------------------------------------


class TestScanSkipsNoId:
    def test_line_without_id_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "col.jsonl"
        path.write_text(
            json.dumps({"name": "orphan"}) + "\n"
            + json.dumps({"_id": "1", "name": "Alice"}) + "\n",
            encoding="utf-8",
        )
        store = DocumentStore(path)
        docs = list(store.scan())
        assert len(docs) == 1
        assert docs[0]["_id"] == "1"


# ---------------------------------------------------------------------------
# DocumentStore — create() OSError in non-existent parent (lines 50-51)
# ---------------------------------------------------------------------------


class TestDocumentStoreCreateOSError:
    def test_create_in_missing_directory_raises_storage_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "missing_dir" / "col.jsonl"
        store = DocumentStore(path)
        with pytest.raises(StorageError):
            store.create()


# ---------------------------------------------------------------------------
# DocumentStore — file_size() on missing file (lines 212-213)
# ---------------------------------------------------------------------------


class TestDocumentStoreFileSizeError:
    def test_file_size_missing_file_raises_storage_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "gone.jsonl"
        store = DocumentStore(path)
        with pytest.raises(StorageError):
            store.file_size()


# ---------------------------------------------------------------------------
# DocumentStore — _write_line() permission error (lines 231-232)
# ---------------------------------------------------------------------------


class TestDocumentStoreWriteError:
    @pytest.mark.skipif(os.getuid() == 0, reason="root bypasses file permissions")
    def test_append_to_readonly_file_raises_storage_error(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "col.jsonl"
        store = DocumentStore(path)
        store.create()
        os.chmod(path, 0o444)
        try:
            with pytest.raises(StorageError):
                store.append("1", {"x": 1})
        finally:
            os.chmod(path, 0o644)


# ---------------------------------------------------------------------------
# WAL — read_entries() with blank line (line 144)
# ---------------------------------------------------------------------------


class TestWALBlankLines:
    def test_read_entries_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "t.wal"
        # Write a blank line followed by a valid entry.
        path.write_text(
            "\n" + json.dumps({"op": "insert", "collection": "c", "doc_id": "1", "document": {}}) + "\n",
            encoding="utf-8",
        )
        entries = WAL(path).read_entries()
        assert len(entries) == 1

    def test_iter_raw_skips_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "t.wal"
        path.write_text(
            "\n" + json.dumps({"op": "insert", "collection": "c", "doc_id": "1", "document": {}}) + "\n",
            encoding="utf-8",
        )
        entries = list(WAL(path).iter_raw())
        assert len(entries) == 1
