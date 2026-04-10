"""StorageEngine — the unified storage-layer facade.

Combines ``DocumentStore``, ``IndexManager``, ``LockManager``, and ``WAL``
behind the interface defined in ``storage/models.py``.  Higher layers
(schema, query, public API) interact only with this class.

Startup protocol
----------------
On ``__init__``:

1. Create the database directory if it does not exist.
2. Load ``manifest.json`` (collection registry).
3. For each registered collection, open its ``DocumentStore`` and
   ``IndexManager`` (loading index files).
4. Scan for any leftover ``.wal`` files and replay them (crash recovery).

Concurrency
-----------
Every mutating operation acquires an exclusive lock on the collection's
``.jsonl`` file for the duration of the write.  Reads acquire a shared
lock.  The ``LockManager`` timeout defaults to 5 seconds.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterator

from jsonpysql.exceptions import (
    CollectionExistsError,
    StorageError,
    WALReplayError,
)
from jsonpysql.storage.document_store import DocumentStore
from jsonpysql.storage.index_manager import IndexManager
from jsonpysql.storage.lock_manager import LockManager
from jsonpysql.storage.models import CollectionStats, IndexSpec, Transaction
from jsonpysql.storage.wal import WAL, WALEntry, find_wal_files

_MANIFEST = "manifest.json"
_WAL_NAME = "transaction.wal"


# ---------------------------------------------------------------------------
# Concrete Transaction
# ---------------------------------------------------------------------------


class _Transaction(Transaction):
    """Concrete WAL-backed transaction returned by ``StorageEngine.begin_transaction()``.

    Args:
        engine: The owning ``StorageEngine``.
        wal: WAL instance for this transaction.
    """

    def __init__(self, engine: StorageEngine, wal: WAL) -> None:
        self._engine = engine
        self._wal = wal

    def insert(self, collection: str, doc_id: str, document: dict) -> None:
        """Buffer an insert in the WAL.

        Args:
            collection: Target collection name.
            doc_id: Document identifier.
            document: Document data.
        """
        self._wal.append(WALEntry("insert", collection, doc_id, document))

    def update(self, collection: str, doc_id: str, document: dict) -> None:
        """Buffer an update in the WAL.

        Args:
            collection: Target collection name.
            doc_id: Document identifier.
            document: New document data.
        """
        self._wal.append(WALEntry("update", collection, doc_id, document))

    def delete(self, collection: str, doc_id: str) -> None:
        """Buffer a delete in the WAL.

        Args:
            collection: Target collection name.
            doc_id: Document identifier.
        """
        self._wal.append(WALEntry("delete", collection, doc_id, None))

    def commit(self) -> None:
        """Apply all buffered operations and remove the WAL file."""
        entries = self._wal.read_entries()
        for entry in entries:
            self._engine._apply_entry(entry)
        self._wal.delete()

    def rollback(self) -> None:
        """Discard all buffered operations and remove the WAL file."""
        self._wal.delete()


# ---------------------------------------------------------------------------
# StorageEngine
# ---------------------------------------------------------------------------


class StorageEngine:
    """Unified storage-layer facade.

    Args:
        db_path: Path to the database directory.  Created if absent.
        lock_timeout: Seconds to wait when acquiring a file lock.
    """

    def __init__(self, db_path: Path, lock_timeout: float = 5.0) -> None:
        self._db_path = db_path
        self._lock = LockManager(timeout=lock_timeout)
        self._stores: dict[str, DocumentStore] = {}
        self._indexes: dict[str, IndexManager] = {}
        self._specs: dict[str, list[IndexSpec]] = {}

        self._db_path.mkdir(parents=True, exist_ok=True)
        self._load_manifest()
        self._recover_wal()

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    def create_collection(
        self,
        name: str,
        indexes: list[IndexSpec],
        drop_if_exists: bool = False,
    ) -> None:
        """Register a new collection.

        Args:
            name: Collection name.
            indexes: Index specifications.
            drop_if_exists: When ``True``, silently drop and recreate if
                the collection already exists.

        Raises:
            CollectionExistsError: If the collection exists and
                ``drop_if_exists`` is ``False``.
            StorageError: On I/O failure.
        """
        if name in self._stores:
            if not drop_if_exists:
                raise CollectionExistsError(
                    f"Collection {name!r} already exists. "
                    "Pass drop_if_exists=True to replace it."
                )
            self.drop_collection(name)

        store = DocumentStore(self._jsonl_path(name))
        store.create()
        im = IndexManager(self._db_path, name, indexes)
        im.save()

        self._stores[name] = store
        self._indexes[name] = im
        self._specs[name] = indexes
        self._save_manifest()

    def drop_collection(self, name: str) -> None:
        """Remove a collection and all its data files.

        Args:
            name: Collection name.

        Raises:
            StorageError: If the collection does not exist.
        """
        self._require(name)
        self._stores[name].destroy()
        self._indexes[name].destroy()
        del self._stores[name]
        del self._indexes[name]
        del self._specs[name]
        self._save_manifest()

    # ------------------------------------------------------------------
    # Single-document writes (bypass WAL)
    # ------------------------------------------------------------------

    def insert(self, collection: str, doc_id: str, document: dict) -> None:
        """Insert a document into *collection*.

        Args:
            collection: Target collection name.
            doc_id: Unique document identifier.
            document: JSON-serialisable mapping.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        jsonl = self._jsonl_path(collection)
        with self._lock.exclusive(jsonl):
            self._stores[collection].append(doc_id, document)
            self._indexes[collection].on_insert(doc_id, {**document, "_id": doc_id})

    def update(self, collection: str, doc_id: str, document: dict) -> None:
        """Replace the document identified by *doc_id*.

        Args:
            collection: Target collection name.
            doc_id: Identifier of the document to replace.
            document: New document data.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        jsonl = self._jsonl_path(collection)
        with self._lock.exclusive(jsonl):
            old = self._stores[collection].get(doc_id)
            self._stores[collection].append(doc_id, document)
            self._indexes[collection].on_update(
                doc_id,
                {**old, "_id": doc_id} if old is not None else None,
                {**document, "_id": doc_id},
            )

    def delete(self, collection: str, doc_id: str) -> None:
        """Delete the document identified by *doc_id*.

        Args:
            collection: Target collection name.
            doc_id: Identifier of the document to delete.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        jsonl = self._jsonl_path(collection)
        with self._lock.exclusive(jsonl):
            old = self._stores[collection].get(doc_id)
            self._stores[collection].tombstone(doc_id)
            self._indexes[collection].on_delete(
                doc_id,
                {**old, "_id": doc_id} if old is not None else None,
            )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, collection: str, doc_id: str) -> dict | None:
        """Return the document identified by *doc_id*, or ``None``.

        Args:
            collection: Collection name.
            doc_id: Document identifier.

        Returns:
            Document dict (without ``_id``) or ``None``.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        jsonl = self._jsonl_path(collection)
        with self._lock.shared(jsonl):
            return self._stores[collection].get(doc_id)

    def scan(self, collection: str) -> Iterator[dict]:
        """Yield every live document in *collection*.

        Args:
            collection: Collection name.

        Yields:
            Live document dicts (each includes ``_id``).

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        jsonl = self._jsonl_path(collection)
        with self._lock.shared(jsonl):
            yield from self._stores[collection].scan()

    def lookup(self, collection: str, field: str, value: Any) -> Iterator[str]:
        """Yield doc IDs where *field* equals *value* via index.

        Falls back to full scan if no index covers *field*.

        Args:
            collection: Collection name.
            field: Field to match.
            value: Value to match.

        Yields:
            Matching document IDs.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        im = self._indexes[collection]
        if im.has_index_for(field):
            yield from im.lookup(field, value)
        else:
            jsonl = self._jsonl_path(collection)
            with self._lock.shared(jsonl):
                for doc in self._stores[collection].scan():
                    if doc.get(field) == value:
                        yield doc["_id"]

    def range_scan(
        self, collection: str, field: str, low: Any, high: Any
    ) -> Iterator[str]:
        """Yield doc IDs where *low* <= *field* <= *high*.

        Uses index when available; otherwise falls back to full scan.

        Args:
            collection: Collection name.
            field: Field to range-match.
            low: Inclusive lower bound.
            high: Inclusive upper bound.

        Yields:
            Matching document IDs.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        im = self._indexes[collection]
        if im.has_index_for(field):
            yield from im.range_scan(field, low, high)
        else:
            jsonl = self._jsonl_path(collection)
            with self._lock.shared(jsonl):
                for doc in self._stores[collection].scan():
                    v = doc.get(field)
                    if v is not None and low <= v <= high:
                        yield doc["_id"]

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def begin_transaction(self) -> Transaction:
        """Return a new WAL-backed transaction.

        Returns:
            A ``Transaction`` instance ready to buffer operations.
        """
        wal_path = self._db_path / f"{uuid.uuid4().hex}.wal"
        return _Transaction(self, WAL(wal_path))

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def compact(self, collection: str) -> None:
        """Rewrite *collection*'s JSONL file, dropping tombstones.

        Acquires an exclusive lock for the entire duration.

        Args:
            collection: Collection name to compact.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        jsonl = self._jsonl_path(collection)
        with self._lock.exclusive(jsonl):
            store = self._stores[collection]
            live = list(store.scan())
            store.compact(live)
            self._indexes[collection].rebuild(live)
            self._indexes[collection].save()

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self, collection: str) -> CollectionStats:
        """Return runtime statistics for *collection*.

        Args:
            collection: Collection name.

        Returns:
            A ``CollectionStats`` instance.

        Raises:
            StorageError: On I/O failure or unknown collection.
        """
        self._require(collection)
        store = self._stores[collection]
        total, deleted = store.count_lines()
        live = total - deleted
        return CollectionStats(
            name=collection,
            document_count=live,
            deleted_count=deleted,
            total_lines=total,
            index_count=len(self._specs[collection]),
            file_size_bytes=store.file_size(),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require(self, collection: str) -> None:
        """Raise ``StorageError`` if *collection* is not registered.

        Args:
            collection: Collection name to check.

        Raises:
            StorageError: When the collection does not exist.
        """
        if collection not in self._stores:
            raise StorageError(f"Unknown collection: {collection!r}")

    def _jsonl_path(self, collection: str) -> Path:
        """Return the ``.jsonl`` file path for *collection*.

        Args:
            collection: Collection name.
        """
        return self._db_path / f"{collection}.jsonl"

    def _manifest_path(self) -> Path:
        """Return the manifest file path."""
        return self._db_path / _MANIFEST

    def _save_manifest(self) -> None:
        """Persist the collection registry to ``manifest.json``.

        Raises:
            StorageError: On I/O failure.
        """
        data = {
            name: [{"fields": s.fields, "unique": s.unique} for s in specs]
            for name, specs in self._specs.items()
        }
        tmp = self._manifest_path().with_suffix(".json.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            tmp.replace(self._manifest_path())
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(f"Cannot save manifest: {exc}") from exc

    def _load_manifest(self) -> None:
        """Load the collection registry from ``manifest.json``.

        Silently starts with an empty registry when the file is absent.

        Raises:
            StorageError: If the manifest exists but is corrupt.
        """
        path = self._manifest_path()
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"Corrupt manifest: {exc}") from exc

        for name, raw_specs in data.items():
            specs = [IndexSpec(fields=s["fields"], unique=s["unique"]) for s in raw_specs]
            self._specs[name] = specs
            self._stores[name] = DocumentStore(self._jsonl_path(name))
            im = IndexManager(self._db_path, name, specs)
            im.load()
            # Rebuild any index that failed to load (missing / corrupt)
            idx_missing = any(
                not (self._db_path / f"{name}.{('_'.join(s.fields))}.idx").exists()
                for s in specs
            )
            if idx_missing:
                live = list(self._stores[name].scan())
                im.rebuild(live)
                im.save()
            self._indexes[name] = im

    def _recover_wal(self) -> None:
        """Replay any leftover WAL files found in the database directory.

        Called once during ``__init__``.  Any ``.wal`` file found implies
        the previous process crashed mid-commit; we replay and clean up.

        Raises:
            WALReplayError: If a WAL file is corrupt and cannot be replayed.
        """
        for wal_path in find_wal_files(self._db_path):
            wal = WAL(wal_path)
            try:
                entries = wal.read_entries()
            except WALReplayError:
                # Corrupt WAL — discard it; we cannot determine intent.
                wal.delete()
                continue
            for entry in entries:
                try:
                    self._apply_entry(entry)
                except StorageError:
                    pass  # collection may have been dropped; skip
            wal.delete()

    def _apply_entry(self, entry: WALEntry) -> None:
        """Apply a single WAL entry to the live stores.

        Args:
            entry: The WAL entry to apply.

        Raises:
            StorageError: If the collection is not registered.
        """
        col = entry.collection
        self._require(col)
        if entry.op == "insert":
            self._stores[col].append(entry.doc_id, entry.document or {})
            self._indexes[col].on_insert(
                entry.doc_id,
                {**(entry.document or {}), "_id": entry.doc_id},
            )
        elif entry.op == "update":
            old = self._stores[col].get(entry.doc_id)
            self._stores[col].append(entry.doc_id, entry.document or {})
            self._indexes[col].on_update(
                entry.doc_id,
                {**old, "_id": entry.doc_id} if old else None,
                {**(entry.document or {}), "_id": entry.doc_id},
            )
        elif entry.op == "delete":
            old = self._stores[col].get(entry.doc_id)
            self._stores[col].tombstone(entry.doc_id)
            self._indexes[col].on_delete(
                entry.doc_id,
                {**old, "_id": entry.doc_id} if old else None,
            )
