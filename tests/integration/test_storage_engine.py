"""Integration tests for StorageEngine (step 7).

Uses the real file system via pytest's tmp_path fixture.
No mocking of any storage sub-module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jsonpysql.exceptions import (
    CollectionExistsError,
    LockTimeoutError,
    StorageError,
    WALReplayError,
)
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import CollectionStats, IndexSpec
from jsonpysql.storage.wal import WAL, WALEntry


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> StorageEngine:
    """Return a fresh StorageEngine backed by a tmp directory."""
    return StorageEngine(tmp_path)


@pytest.fixture
def engine_with_users(tmp_path: Path) -> StorageEngine:
    """Engine with a 'users' collection and a name index."""
    eng = StorageEngine(tmp_path)
    eng.create_collection("users", [IndexSpec(["name"]), IndexSpec(["age"])])
    return eng


# ---------------------------------------------------------------------------
# Collection management
# ---------------------------------------------------------------------------


class TestCollectionManagement:
    def test_create_collection_creates_jsonl(self, tmp_path: Path) -> None:
        eng = StorageEngine(tmp_path)
        eng.create_collection("items", [])
        assert (tmp_path / "items.jsonl").exists()

    def test_create_duplicate_raises_collection_exists_error(
        self, engine: StorageEngine
    ) -> None:
        engine.create_collection("col", [])
        with pytest.raises(CollectionExistsError):
            engine.create_collection("col", [])

    def test_create_with_drop_if_exists_succeeds(self, engine: StorageEngine) -> None:
        engine.create_collection("col", [])
        engine.insert("col", "1", {"x": 1})
        engine.create_collection("col", [], drop_if_exists=True)
        # Data should be gone
        assert engine.get("col", "1") is None

    def test_drop_collection_removes_files(self, tmp_path: Path) -> None:
        eng = StorageEngine(tmp_path)
        eng.create_collection("col", [])
        eng.drop_collection("col")
        assert not (tmp_path / "col.jsonl").exists()

    def test_drop_unknown_collection_raises_storage_error(
        self, engine: StorageEngine
    ) -> None:
        with pytest.raises(StorageError):
            engine.drop_collection("ghost")

    def test_manifest_persists_across_reopen(self, tmp_path: Path) -> None:
        eng1 = StorageEngine(tmp_path)
        eng1.create_collection("things", [IndexSpec(["name"])])
        eng1.insert("things", "1", {"name": "widget"})

        eng2 = StorageEngine(tmp_path)
        doc = eng2.get("things", "1")
        assert doc == {"name": "widget"}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_insert_and_get(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        doc = engine_with_users.get("users", "u1")
        assert doc == {"name": "Alice", "age": 30}

    def test_get_unknown_id_returns_none(self, engine_with_users: StorageEngine) -> None:
        assert engine_with_users.get("users", "ghost") is None

    def test_update_replaces_document(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.update("users", "u1", {"name": "Alicia", "age": 31})
        assert engine_with_users.get("users", "u1") == {"name": "Alicia", "age": 31}

    def test_delete_returns_none(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.delete("users", "u1")
        assert engine_with_users.get("users", "u1") is None

    def test_insert_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            engine.insert("ghost", "1", {})

    def test_update_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            engine.update("ghost", "1", {})

    def test_delete_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            engine.delete("ghost", "1")

    def test_get_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            engine.get("ghost", "1")


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


class TestScan:
    def test_scan_returns_all_live_docs(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.insert("users", "u2", {"name": "Bob", "age": 25})
        docs = list(engine_with_users.scan("users"))
        ids = {d["_id"] for d in docs}
        assert ids == {"u1", "u2"}

    def test_scan_excludes_deleted(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.delete("users", "u1")
        assert list(engine_with_users.scan("users")) == []

    def test_scan_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            list(engine.scan("ghost"))


# ---------------------------------------------------------------------------
# Index lookup and range scan
# ---------------------------------------------------------------------------


class TestIndexLookup:
    def test_lookup_by_indexed_field(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.insert("users", "u2", {"name": "Bob", "age": 25})
        result = list(engine_with_users.lookup("users", "name", "Alice"))
        assert result == ["u1"]

    def test_lookup_unindexed_field_falls_back_to_scan(
        self, engine_with_users: StorageEngine
    ) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.create_collection("items", [])
        engine_with_users.insert("items", "i1", {"color": "red"})
        result = list(engine_with_users.lookup("items", "color", "red"))
        assert result == ["i1"]

    def test_range_scan_indexed(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.insert("users", "u2", {"name": "Bob", "age": 25})
        engine_with_users.insert("users", "u3", {"name": "Carol", "age": 35})
        results = sorted(engine_with_users.range_scan("users", "name", "Alice", "Bob"))
        assert results == ["u1", "u2"]

    def test_range_scan_unindexed_falls_back(self, engine: StorageEngine) -> None:
        engine.create_collection("items", [])
        engine.insert("items", "i1", {"price": 10})
        engine.insert("items", "i2", {"price": 50})
        engine.insert("items", "i3", {"price": 100})
        results = sorted(engine.range_scan("items", "price", 10, 50))
        assert results == ["i1", "i2"]

    def test_lookup_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            list(engine.lookup("ghost", "x", 1))

    def test_range_scan_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            list(engine.range_scan("ghost", "x", 1, 10))


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TestTransactions:
    def test_commit_applies_all_operations(self, engine_with_users: StorageEngine) -> None:
        with engine_with_users.begin_transaction() as txn:
            txn.insert("users", "u1", {"name": "Alice", "age": 30})
            txn.insert("users", "u2", {"name": "Bob", "age": 25})
        assert engine_with_users.get("users", "u1") == {"name": "Alice", "age": 30}
        assert engine_with_users.get("users", "u2") == {"name": "Bob", "age": 25}

    def test_rollback_discards_operations(self, engine_with_users: StorageEngine) -> None:
        txn = engine_with_users.begin_transaction()
        txn.insert("users", "u1", {"name": "Alice", "age": 30})
        txn.rollback()
        assert engine_with_users.get("users", "u1") is None

    def test_exception_in_with_block_triggers_rollback(
        self, engine_with_users: StorageEngine
    ) -> None:
        try:
            with engine_with_users.begin_transaction() as txn:
                txn.insert("users", "u1", {"name": "Alice", "age": 30})
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        assert engine_with_users.get("users", "u1") is None

    def test_transaction_update_and_delete(
        self, engine_with_users: StorageEngine
    ) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        with engine_with_users.begin_transaction() as txn:
            txn.update("users", "u1", {"name": "Alicia", "age": 31})
            txn.insert("users", "u2", {"name": "Bob", "age": 25})
            txn.delete("users", "u2")
        assert engine_with_users.get("users", "u1") == {"name": "Alicia", "age": 31}
        assert engine_with_users.get("users", "u2") is None

    def test_wal_file_deleted_after_commit(
        self, tmp_path: Path, engine_with_users: StorageEngine
    ) -> None:
        with engine_with_users.begin_transaction() as txn:
            txn.insert("users", "u1", {"name": "Alice", "age": 30})
        wal_files = list(tmp_path.glob("*.wal"))
        assert wal_files == []

    def test_wal_file_deleted_after_rollback(
        self, tmp_path: Path, engine_with_users: StorageEngine
    ) -> None:
        txn = engine_with_users.begin_transaction()
        txn.insert("users", "u1", {"name": "Alice", "age": 30})
        txn.rollback()
        assert list(tmp_path.glob("*.wal")) == []


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    def test_replays_wal_on_reopen(self, tmp_path: Path) -> None:
        """Simulate crash: write WAL manually, reopen engine, verify replay."""
        eng = StorageEngine(tmp_path)
        eng.create_collection("orders", [])

        # Simulate a crash mid-commit by writing a WAL file manually.
        wal_path = tmp_path / "crashed.wal"
        wal = WAL(wal_path)
        wal.append(WALEntry("insert", "orders", "o1", {"item": "widget", "qty": 5}))

        # Reopen — recovery should replay the WAL.
        eng2 = StorageEngine(tmp_path)
        doc = eng2.get("orders", "o1")
        assert doc == {"item": "widget", "qty": 5}
        assert not wal_path.exists()

    def test_corrupt_wal_is_discarded_on_reopen(self, tmp_path: Path) -> None:
        eng = StorageEngine(tmp_path)
        eng.create_collection("orders", [])

        wal_path = tmp_path / "corrupt.wal"
        wal_path.write_text("not-json\n", encoding="utf-8")

        eng2 = StorageEngine(tmp_path)  # must not raise
        assert not wal_path.exists()


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_after_insert(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        stats = engine_with_users.get_stats("users")
        assert isinstance(stats, CollectionStats)
        assert stats.document_count == 1
        assert stats.deleted_count == 0
        assert stats.index_count == 2

    def test_stats_deleted_count(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.delete("users", "u1")
        stats = engine_with_users.get_stats("users")
        assert stats.deleted_count == 1

    def test_stats_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            engine.get_stats("ghost")


# ---------------------------------------------------------------------------
# Compaction (step 8 gate folded in here)
# ---------------------------------------------------------------------------


class TestEngineInternalPaths:
    """Covers engine._load_manifest and _recover_wal edge cases."""

    def test_corrupt_manifest_raises_storage_error(self, tmp_path: Path) -> None:
        # Create a valid engine, then corrupt the manifest.
        eng = StorageEngine(tmp_path)
        eng.create_collection("col", [])
        manifest = tmp_path / "manifest.json"
        manifest.write_text("not-json", encoding="utf-8")
        with pytest.raises(StorageError):
            StorageEngine(tmp_path)

    def test_missing_idx_triggers_rebuild_on_reopen(self, tmp_path: Path) -> None:
        eng = StorageEngine(tmp_path)
        eng.create_collection("col", [IndexSpec(["name"])])
        eng.insert("col", "1", {"name": "Alice"})

        # Delete the .idx file to force rebuild.
        for f in tmp_path.glob("*.idx"):
            f.unlink()

        eng2 = StorageEngine(tmp_path)
        result = list(eng2.lookup("col", "name", "Alice"))
        assert result == ["1"]

    def test_wal_replay_skips_unknown_collection(self, tmp_path: Path) -> None:
        """WAL entries for missing collections are silently skipped."""
        eng = StorageEngine(tmp_path)
        eng.create_collection("col", [])

        wal_path = tmp_path / "bad.wal"
        wal = WAL(wal_path)
        wal.append(WALEntry("insert", "nonexistent_col", "x", {"v": 1}))

        # Reopening should not raise; the unknown-collection entry is skipped.
        StorageEngine(tmp_path)


class TestCompaction:
    def test_compact_removes_tombstones(self, engine_with_users: StorageEngine) -> None:
        for i in range(5):
            engine_with_users.insert("users", str(i), {"name": f"user{i}", "age": i})
        engine_with_users.delete("users", "0")
        engine_with_users.delete("users", "1")
        engine_with_users.compact("users")
        stats = engine_with_users.get_stats("users")
        assert stats.deleted_count == 0
        assert stats.document_count == 3

    def test_compact_preserves_live_data(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.compact("users")
        assert engine_with_users.get("users", "u1") == {"name": "Alice", "age": 30}

    def test_compact_unknown_collection_raises(self, engine: StorageEngine) -> None:
        with pytest.raises(StorageError):
            engine.compact("ghost")

    def test_compact_rebuilds_index(self, engine_with_users: StorageEngine) -> None:
        engine_with_users.insert("users", "u1", {"name": "Alice", "age": 30})
        engine_with_users.delete("users", "u1")
        engine_with_users.compact("users")
        assert list(engine_with_users.lookup("users", "name", "Alice")) == []

    def test_deleted_ratio_triggers(self, engine_with_users: StorageEngine) -> None:
        """Verify CollectionStats.deleted_ratio reflects delete state."""
        for i in range(10):
            engine_with_users.insert("users", str(i), {"name": f"u{i}", "age": i})
        for i in range(3):
            engine_with_users.delete("users", str(i))
        stats = engine_with_users.get_stats("users")
        # 3 deletes out of 13 lines = ~23 % > 20 % threshold
        assert stats.deleted_ratio > 0.2
