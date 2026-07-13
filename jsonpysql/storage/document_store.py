"""JSONL-based append-only document store.

Each collection is backed by a single ``.jsonl`` file: one JSON object per
line.  Writes are append-only; deletes write a tombstone line.  The file is
only rewritten during compaction.

Locking is the caller's responsibility — ``StorageEngine`` acquires the
appropriate ``LockManager`` context before calling into this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from jsonpysql.exceptions import StorageError
from jsonpysql.utils.serialization import json_default

# Sentinel key written on a delete operation.
_DELETED_KEY = "_deleted"
_ID_KEY = "_id"


class DocumentStore:
    """Manages a single JSONL document store file.

    Args:
        path: Path to the ``.jsonl`` file.  The file is created when
            ``create()`` is called; it must exist before any other method
            is used.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create(self) -> None:
        """Create an empty ``.jsonl`` file.

        Raises:
            StorageError: If the file cannot be created.
        """
        try:
            self._path.touch(exist_ok=False)
        except FileExistsError as exc:
            raise StorageError(f"Document store already exists: {self._path}") from exc
        except OSError as exc:
            raise StorageError(f"Cannot create document store {self._path}: {exc}") from exc

    def destroy(self) -> None:
        """Delete the ``.jsonl`` file.

        Raises:
            StorageError: If the file cannot be deleted.
        """
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot delete document store {self._path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def append(self, doc_id: str, document: dict) -> None:
        """Append a document line to the store.

        Used for both inserts and updates.  For updates the old line
        remains in the file; the latest entry for a given ``_id`` wins
        during a full scan.

        Args:
            doc_id: Unique identifier for the document.
            document: JSON-serialisable mapping.  ``_id`` is injected
                automatically; any existing ``_id`` key is overwritten.

        Raises:
            StorageError: On I/O failure.
        """
        record = dict(document)
        record[_ID_KEY] = doc_id
        self._write_line(record)

    def tombstone(self, doc_id: str) -> None:
        """Append a tombstone line marking *doc_id* as deleted.

        Args:
            doc_id: Identifier of the document to delete.

        Raises:
            StorageError: On I/O failure.
        """
        self._write_line({_DELETED_KEY: True, _ID_KEY: doc_id})

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, doc_id: str) -> dict | None:
        """Return the latest live document for *doc_id*, or ``None``.

        Performs a full linear scan and returns the last non-tombstoned
        record matching *doc_id*.

        Args:
            doc_id: Document identifier to retrieve.

        Returns:
            The document dict (without ``_id``) or ``None`` if not found
            or deleted.

        Raises:
            StorageError: On I/O failure or parse error.
        """
        result: dict | None = None
        for line in self._iter_lines():
            rec = self._parse(line)
            if rec.get(_ID_KEY) != doc_id:
                continue
            if rec.get(_DELETED_KEY):
                result = None
            else:
                result = self._strip_id(rec)
        return result

    def scan(self) -> Iterator[dict]:
        """Yield every live document in insertion order (latest write wins).

        Tombstoned documents and superseded versions are excluded.

        Yields:
            Each live document dict including its ``_id`` field.

        Raises:
            StorageError: On I/O failure or parse error.
        """
        # Build a dict keyed by doc_id; later lines overwrite earlier ones.
        seen: dict[str, dict | None] = {}
        for line in self._iter_lines():
            rec = self._parse(line)
            doc_id = rec.get(_ID_KEY)
            if doc_id is None:
                continue
            if rec.get(_DELETED_KEY):
                seen[doc_id] = None
            else:
                seen[doc_id] = rec

        for doc in seen.values():
            if doc is not None:
                yield dict(doc)

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def compact(self, live_docs: list[dict]) -> None:
        """Rewrite the file retaining only *live_docs*.

        ``live_docs`` must already contain the ``_id`` field for each
        document (as returned by ``scan()``).

        Args:
            live_docs: Ordered list of live document dicts to keep.

        Raises:
            StorageError: On I/O failure.
        """
        tmp = self._path.with_suffix(".jsonl.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                for doc in live_docs:
                    fh.write(
                        json.dumps(doc, ensure_ascii=False, default=json_default)
                        + "\n"
                    )
            tmp.replace(self._path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StorageError(f"Compaction failed for {self._path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Stats helpers
    # ------------------------------------------------------------------

    def count_lines(self) -> tuple[int, int]:
        """Return ``(total_lines, tombstone_lines)`` for this store.

        Raises:
            StorageError: On I/O failure.
        """
        total = 0
        tombstones = 0
        for line in self._iter_lines():
            total += 1
            rec = self._parse(line)
            if rec.get(_DELETED_KEY):
                tombstones += 1
        return total, tombstones

    def file_size(self) -> int:
        """Return the current size of the ``.jsonl`` file in bytes.

        Raises:
            StorageError: If the file cannot be stat'd.
        """
        try:
            return self._path.stat().st_size
        except OSError as exc:
            raise StorageError(f"Cannot stat {self._path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_line(self, record: dict) -> None:
        """Append *record* as a single JSON line.

        Args:
            record: Mapping to serialise.

        Raises:
            StorageError: On I/O failure.
        """
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(record, ensure_ascii=False, default=json_default)
                    + "\n"
                )
        except OSError as exc:
            raise StorageError(f"Write failed for {self._path}: {exc}") from exc

    def _iter_lines(self) -> Iterator[str]:
        """Yield non-empty lines from the store file.

        Raises:
            StorageError: On I/O failure.
        """
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if line:
                        yield line
        except OSError as exc:
            raise StorageError(f"Read failed for {self._path}: {exc}") from exc

    def _parse(self, line: str) -> dict:
        """Parse a JSON line, wrapping errors as StorageError.

        Args:
            line: Raw JSON text.

        Returns:
            Parsed dict.

        Raises:
            StorageError: If the line is not valid JSON.
        """
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise StorageError(f"Corrupt line in {self._path}: {exc}") from exc

    @staticmethod
    def _strip_id(record: dict) -> dict:
        """Return *record* without the internal ``_id`` key removed.

        Args:
            record: Raw record from the store.

        Returns:
            A copy of *record* with ``_id`` removed.
        """
        return {k: v for k, v in record.items() if k != _ID_KEY}
