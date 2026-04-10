"""Data models for the JsonpySql storage layer.

Contains the ``IndexSpec``, ``CollectionStats``, and ``Transaction`` types
shared across the storage sub-modules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class IndexSpec:
    """Specification for a single index on a collection.

    Attributes:
        fields: Ordered list of field names covered by this index.
            A single-element list creates a simple field index; a
            multi-element list creates a compound index keyed on the
            tuple of values.
        unique: When ``True`` the index enforces uniqueness. The schema
            layer uses this flag to perform existence checks before
            delegating to the storage layer.
    """

    fields: list[str]
    unique: bool = False


@dataclass
class CollectionStats:
    """Runtime statistics for a single collection.

    Attributes:
        name: Collection name.
        document_count: Number of live (non-tombstoned) documents.
        deleted_count: Number of tombstone entries in the ``.jsonl`` file.
        total_lines: Total line count of the ``.jsonl`` file
            (``document_count + deleted_count``).
        index_count: Number of indexes registered for the collection.
        file_size_bytes: Current size of the ``.jsonl`` file in bytes.
    """

    name: str
    document_count: int
    deleted_count: int
    total_lines: int
    index_count: int
    file_size_bytes: int

    @property
    def deleted_ratio(self) -> float:
        """Fraction of JSONL lines that are tombstones.

        Returns:
            A float in ``[0.0, 1.0]``. Returns ``0.0`` when the file is
            empty to avoid division by zero. Compaction is triggered when
            this value exceeds ``0.2``.
        """
        if self.total_lines == 0:
            return 0.0
        return self.deleted_count / self.total_lines


class Transaction(ABC):
    """Abstract base class for a storage-layer write transaction.

    Concrete implementations are provided by ``StorageEngine`` in
    ``storage/engine.py``.  Multi-document transactions buffer operations
    in the WAL; single-document writes bypass the WAL entirely.

    Typical usage::

        with db.transaction() as txn:
            txn.insert("orders", order_id, order_doc)
            txn.update("inventory", item_id, updated_doc)
        # commit is called automatically on clean exit;
        # rollback is called automatically if an exception propagates.
    """

    @abstractmethod
    def insert(self, collection: str, doc_id: str, document: dict) -> None:
        """Buffer an insert operation.

        Args:
            collection: Target collection name.
            doc_id: Unique document identifier.
            document: JSON-serialisable mapping to store.
        """

    @abstractmethod
    def update(self, collection: str, doc_id: str, document: dict) -> None:
        """Buffer an update operation, replacing the existing document.

        Args:
            collection: Target collection name.
            doc_id: Identifier of the document to replace.
            document: New JSON-serialisable mapping.
        """

    @abstractmethod
    def delete(self, collection: str, doc_id: str) -> None:
        """Buffer a delete operation, writing a tombstone on commit.

        Args:
            collection: Target collection name.
            doc_id: Identifier of the document to tombstone.
        """

    @abstractmethod
    def commit(self) -> None:
        """Apply all buffered operations and delete the WAL file."""

    @abstractmethod
    def rollback(self) -> None:
        """Discard all buffered operations and delete the WAL file."""

    def __enter__(self) -> Transaction:
        """Return self to support use as a context manager."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Commit on clean exit; rollback if an exception propagated.

        Args:
            exc_type: Exception class, or ``None`` on clean exit.
            exc_val: Exception instance, or ``None`` on clean exit.
            exc_tb: Traceback object, or ``None`` on clean exit.
        """
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
