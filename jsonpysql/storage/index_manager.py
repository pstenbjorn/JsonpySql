"""SortedDict-based index manager for the JsonpySql storage engine.

Manages in-memory ``SortedDict`` indexes and persists them to ``.idx``
(forward) and ``.ridx`` (reverse / FK cascade) JSON files after every
write.  On startup, if an index file is missing or corrupt, the index is
rebuilt from a full document-store scan.

Index key types:

- **Single-field**: ``field_value`` → ``[doc_id, ...]``
- **Compound**: ``(field1_value, field2_value)`` → ``[doc_id, ...]``
- **Reverse** (``.ridx``): ``parent_id`` → ``[child_doc_id, ...]``

Because JSON does not support tuple keys, compound keys are serialised as
``"val1\x1fval2"`` (ASCII Unit Separator 0x1F) and decoded on load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from sortedcontainers import SortedDict

from jsonpysql.exceptions import StorageError
from jsonpysql.storage.models import IndexSpec

# Separator used when serialising compound tuple keys to JSON-safe strings.
_COMPOUND_SEP = "\x1f"


def _encode_key(key: Any) -> str:
    """Encode an index key as a JSON-safe string.

    Args:
        key: A scalar value or a tuple for compound indexes.

    Returns:
        A string representation suitable for use as a JSON object key.
    """
    if isinstance(key, tuple):
        return _COMPOUND_SEP.join(str(part) for part in key)
    return str(key)


def _decode_key(raw: str, is_compound: bool) -> Any:
    """Decode a stored key string back to its Python representation.

    Args:
        raw: The string key as stored in the JSON file.
        is_compound: When ``True`` the key is split on the separator and
            returned as a tuple of strings.

    Returns:
        The decoded key (string for single-field, tuple for compound).
    """
    if is_compound:
        return tuple(raw.split(_COMPOUND_SEP))
    return raw


class IndexManager:
    """Manages forward and reverse indexes for one collection.

    Args:
        db_path: Path to the database directory.
        collection: Collection name — used to derive index file names.
        specs: Index specifications for this collection.
    """

    def __init__(self, db_path: Path, collection: str, specs: list[IndexSpec]) -> None:
        self._db_path = db_path
        self._collection = collection
        self._specs = specs

        # forward indexes: spec index → SortedDict{ encoded_key → [doc_id] }
        self._indexes: list[SortedDict] = [SortedDict() for _ in specs]

        # reverse index: parent_doc_id → [child_doc_id]  (FK cascade support)
        self._reverse: SortedDict = SortedDict()

    # ------------------------------------------------------------------
    # Persistence paths
    # ------------------------------------------------------------------

    def _idx_path(self, spec_index: int) -> Path:
        """Return the ``.idx`` file path for a given spec.

        Args:
            spec_index: Position of the spec in ``self._specs``.
        """
        fields = "_".join(self._specs[spec_index].fields)
        return self._db_path / f"{self._collection}.{fields}.idx"

    def _ridx_path(self) -> Path:
        """Return the ``.ridx`` file path for this collection."""
        return self._db_path / f"{self._collection}.ridx"

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load all index files from disk; rebuild any that are missing/corrupt."""
        for i in range(len(self._specs)):
            path = self._idx_path(i)
            if path.exists():
                try:
                    self._indexes[i] = self._read_idx(path, self._specs[i])
                except StorageError:
                    # Corrupt — will be rebuilt by caller via rebuild()
                    self._indexes[i] = SortedDict()
            # Missing → empty; caller is responsible for calling rebuild()

        ridx_path = self._ridx_path()
        if ridx_path.exists():
            try:
                self._reverse = self._read_ridx(ridx_path)
            except StorageError:
                self._reverse = SortedDict()

    def save(self) -> None:
        """Persist all in-memory indexes to disk.

        Raises:
            StorageError: On I/O failure.
        """
        for i, spec in enumerate(self._specs):
            self._write_idx(self._idx_path(i), self._indexes[i], spec)
        self._write_ridx(self._ridx_path(), self._reverse)

    def rebuild(self, documents: list[dict]) -> None:
        """Rebuild all indexes from *documents*.

        Args:
            documents: Full list of live documents (each must include
                ``_id``).  Typically the result of ``DocumentStore.scan()``.
        """
        self._indexes = [SortedDict() for _ in self._specs]
        self._reverse = SortedDict()
        for doc in documents:
            doc_id = doc.get("_id", "")
            self._add_to_indexes(doc_id, doc)

    def destroy(self) -> None:
        """Delete all index files for this collection.

        Raises:
            StorageError: On I/O failure.
        """
        try:
            for i in range(len(self._specs)):
                self._idx_path(i).unlink(missing_ok=True)
            self._ridx_path().unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot delete index files for {self._collection}: {exc}") from exc

    # ------------------------------------------------------------------
    # Index update operations
    # ------------------------------------------------------------------

    def on_insert(self, doc_id: str, document: dict) -> None:
        """Update indexes after inserting *document*.

        Args:
            doc_id: Document identifier.
            document: The document just inserted (must include ``_id``).
        """
        self._add_to_indexes(doc_id, document)
        self.save()

    def on_update(self, doc_id: str, old_doc: dict | None, new_doc: dict) -> None:
        """Update indexes after replacing a document.

        Args:
            doc_id: Document identifier.
            old_doc: Previous document (or ``None`` if unknown).
            new_doc: Replacement document.
        """
        if old_doc is not None:
            self._remove_from_indexes(doc_id, old_doc)
        self._add_to_indexes(doc_id, new_doc)
        self.save()

    def on_delete(self, doc_id: str, document: dict | None) -> None:
        """Update indexes after deleting a document.

        Args:
            doc_id: Document identifier.
            document: The deleted document (or ``None`` if not available).
        """
        if document is not None:
            self._remove_from_indexes(doc_id, document)
        self.save()

    # ------------------------------------------------------------------
    # Reverse index helpers (FK cascade)
    # ------------------------------------------------------------------

    def add_reverse(self, parent_id: str, child_id: str) -> None:
        """Register *child_id* as a child of *parent_id* in the reverse index.

        Args:
            parent_id: Parent document identifier.
            child_id: Child document identifier.
        """
        if parent_id not in self._reverse:
            self._reverse[parent_id] = []
        if child_id not in self._reverse[parent_id]:
            self._reverse[parent_id].append(child_id)
        self.save()

    def remove_reverse(self, parent_id: str, child_id: str) -> None:
        """Remove *child_id* from *parent_id*'s reverse-index entry.

        Args:
            parent_id: Parent document identifier.
            child_id: Child document identifier.
        """
        if parent_id in self._reverse:
            try:
                self._reverse[parent_id].remove(child_id)
            except ValueError:
                pass
            if not self._reverse[parent_id]:
                del self._reverse[parent_id]
        self.save()

    def get_children(self, parent_id: str) -> list[str]:
        """Return all child doc IDs for *parent_id*.

        Args:
            parent_id: Parent document identifier.

        Returns:
            List of child doc IDs (may be empty).
        """
        return list(self._reverse.get(parent_id, []))

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def lookup(self, field: str, value: Any) -> Iterator[str]:
        """Yield doc IDs where *field* equals *value*.

        Args:
            field: Field name to search.
            value: Value to match.

        Yields:
            Matching document IDs.
        """
        idx, _ = self._find_index_for(field)
        if idx is None:
            return
        key = _encode_key(value)
        for doc_id in idx.get(key, []):
            yield doc_id

    def range_scan(self, field: str, low: Any, high: Any) -> Iterator[str]:
        """Yield doc IDs where *low* <= *field* <= *high*.

        Args:
            field: Indexed field name.
            low: Inclusive lower bound.
            high: Inclusive upper bound.

        Yields:
            Matching document IDs.
        """
        idx, _ = self._find_index_for(field)
        if idx is None:
            return
        low_key = _encode_key(low)
        high_key = _encode_key(high)
        for key in idx.irange(low_key, high_key):
            for doc_id in idx[key]:
                yield doc_id

    def has_index_for(self, field: str) -> bool:
        """Return ``True`` if there is an index covering *field*.

        Args:
            field: Field name to check.
        """
        idx, _ = self._find_index_for(field)
        return idx is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_index_for(self, field: str) -> tuple[SortedDict | None, IndexSpec | None]:
        """Return the first index and spec that covers *field*.

        Args:
            field: Field name.

        Returns:
            Tuple of ``(SortedDict, IndexSpec)`` or ``(None, None)`` if no
            index exists.
        """
        for i, spec in enumerate(self._specs):
            if field in spec.fields:
                return self._indexes[i], spec
        return None, None

    def _make_key(self, spec: IndexSpec, document: dict) -> Any:
        """Build the index key for *document* using *spec*.

        Args:
            spec: Index specification.
            document: Document dict (with or without ``_id``).

        Returns:
            A scalar (single-field) or tuple (compound) key.
        """
        if len(spec.fields) == 1:
            return document.get(spec.fields[0])
        return tuple(document.get(f) for f in spec.fields)

    def _add_to_indexes(self, doc_id: str, document: dict) -> None:
        """Insert *doc_id* into all applicable indexes.

        Args:
            doc_id: Document identifier.
            document: Document dict.
        """
        for i, spec in enumerate(self._specs):
            key = _encode_key(self._make_key(spec, document))
            if key not in self._indexes[i]:
                self._indexes[i][key] = []
            if doc_id not in self._indexes[i][key]:
                self._indexes[i][key].append(doc_id)

    def _remove_from_indexes(self, doc_id: str, document: dict) -> None:
        """Remove *doc_id* from all applicable indexes.

        Args:
            doc_id: Document identifier.
            document: Document dict used to compute the key.
        """
        for i, spec in enumerate(self._specs):
            key = _encode_key(self._make_key(spec, document))
            if key in self._indexes[i]:
                try:
                    self._indexes[i][key].remove(doc_id)
                except ValueError:
                    pass
                if not self._indexes[i][key]:
                    del self._indexes[i][key]

    # ------------------------------------------------------------------
    # Serialisation helpers
    # ------------------------------------------------------------------

    def _write_idx(self, path: Path, idx: SortedDict, spec: IndexSpec) -> None:
        """Write a forward index to *path*.

        Args:
            path: Destination file path.
            idx: In-memory index.
            spec: Index spec (used to tag the file).

        Raises:
            StorageError: On I/O failure.
        """
        payload = {
            "fields": spec.fields,
            "unique": spec.unique,
            "data": {k: v for k, v in idx.items()},
        }
        self._write_json(path, payload)

    def _read_idx(self, path: Path, spec: IndexSpec) -> SortedDict:
        """Read a forward index from *path*.

        Args:
            path: Source file path.
            spec: Expected index spec (used to determine key decoding).

        Returns:
            Populated ``SortedDict``.

        Raises:
            StorageError: If the file is missing, corrupt, or schema mismatch.
        """
        payload = self._read_json(path)
        is_compound = len(spec.fields) > 1
        idx: SortedDict = SortedDict()
        raw_data = payload.get("data", {})
        if not isinstance(raw_data, dict):
            raise StorageError(f"Corrupt index file {path}: 'data' is not a dict")
        for raw_key, doc_ids in raw_data.items():
            decoded = _decode_key(raw_key, is_compound)
            idx[_encode_key(decoded)] = doc_ids
        return idx

    def _write_ridx(self, path: Path, ridx: SortedDict) -> None:
        """Write the reverse index to *path*.

        Args:
            path: Destination file path.
            ridx: In-memory reverse index.

        Raises:
            StorageError: On I/O failure.
        """
        self._write_json(path, dict(ridx))

    def _read_ridx(self, path: Path) -> SortedDict:
        """Read the reverse index from *path*.

        Args:
            path: Source file path.

        Returns:
            Populated ``SortedDict``.

        Raises:
            StorageError: On I/O failure or parse error.
        """
        data = self._read_json(path)
        return SortedDict(data)

    def _write_json(self, path: Path, data: Any) -> None:
        """Atomically write *data* as JSON to *path*.

        Args:
            path: Destination file path.
            data: JSON-serialisable object.

        Raises:
            StorageError: On I/O failure.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            tmp.replace(path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(f"Cannot write index {path}: {exc}") from exc

    def _read_json(self, path: Path) -> Any:
        """Read and parse a JSON file.

        Args:
            path: Source file path.

        Returns:
            Parsed Python object.

        Raises:
            StorageError: On I/O or parse failure.
        """
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise StorageError(f"Cannot read index {path}: {exc}") from exc
