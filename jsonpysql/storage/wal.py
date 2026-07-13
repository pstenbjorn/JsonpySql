"""Write-Ahead Log (WAL) for multi-document transactions.

Single-document writes bypass the WAL entirely; the OS provides
sufficient atomicity for append operations.  The WAL is activated only
when a caller explicitly starts a transaction via ``StorageEngine.begin_transaction()``.

WAL file format (one JSON line per operation)::

    {"op": "insert", "collection": "orders", "doc_id": "o1", "document": {...}}
    {"op": "update", "collection": "orders", "doc_id": "o1", "document": {...}}
    {"op": "delete", "collection": "orders", "doc_id": "o1", "document": null}

On commit  → apply all entries → delete the WAL file.
On rollback → delete the WAL file without applying.
On open     → if any ``.wal`` file exists, replay it before accepting ops.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal

from jsonpysql.exceptions import StorageError, WALReplayError
from jsonpysql.utils.serialization import json_default

Op = Literal["insert", "update", "delete"]


@dataclass
class WALEntry:
    """A single WAL operation record.

    Attributes:
        op: One of ``"insert"``, ``"update"``, or ``"delete"``.
        collection: Target collection name.
        doc_id: Document identifier.
        document: The full document dict for insert/update, or ``None``
            for delete.
    """

    op: Op
    collection: str
    doc_id: str
    document: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        """Serialise this entry to a plain dict for JSON encoding.

        Returns:
            A dict with keys ``op``, ``collection``, ``doc_id``, ``document``.
        """
        return {
            "op": self.op,
            "collection": self.collection,
            "doc_id": self.doc_id,
            "document": self.document,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WALEntry:
        """Deserialise a WAL entry from a plain dict.

        Args:
            data: Dict with keys ``op``, ``collection``, ``doc_id``,
                ``document``.

        Returns:
            A ``WALEntry`` instance.

        Raises:
            StorageError: If any required key is missing or ``op`` is invalid.
        """
        try:
            op = data["op"]
            collection = data["collection"]
            doc_id = data["doc_id"]
            document = data.get("document")
        except KeyError as exc:
            raise StorageError(f"Malformed WAL entry (missing key {exc}): {data!r}") from exc
        if op not in ("insert", "update", "delete"):
            raise StorageError(f"Unknown WAL op {op!r}")
        return cls(op=op, collection=collection, doc_id=doc_id, document=document)


class WAL:
    """Write-Ahead Log for a single in-flight transaction.

    Each ``WAL`` instance is tied to one ``.wal`` file in the database
    directory.  The file name is ``{collection}.wal`` for the primary
    collection of the transaction; in practice ``StorageEngine`` uses a
    single ``transaction.wal`` file per transaction.

    Args:
        path: Path to the ``.wal`` file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(self, entry: WALEntry) -> None:
        """Append one operation to the WAL file.

        Args:
            entry: The operation to record.

        Raises:
            StorageError: On I/O failure.
        """
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        entry.to_dict(), ensure_ascii=False, default=json_default
                    )
                    + "\n"
                )
        except OSError as exc:
            raise StorageError(f"WAL write failed ({self._path}): {exc}") from exc

    # ------------------------------------------------------------------
    # Read / replay
    # ------------------------------------------------------------------

    def read_entries(self) -> list[WALEntry]:
        """Read and parse all entries from the WAL file.

        Deduplicates by ``doc_id`` within each collection — the **last**
        entry for a given ``(collection, doc_id)`` pair wins, making
        replay idempotent.

        Returns:
            Ordered list of deduplicated ``WALEntry`` objects.

        Raises:
            WALReplayError: If the file cannot be read or contains corrupt
                JSON.
        """
        entries_map: dict[tuple[str, str], WALEntry] = {}
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entry = WALEntry.from_dict(data)
                    except (json.JSONDecodeError, StorageError) as exc:
                        raise WALReplayError(
                            f"Corrupt WAL entry at line {lineno} in {self._path}: {exc}"
                        ) from exc
                    entries_map[(entry.collection, entry.doc_id)] = entry
        except OSError as exc:
            raise WALReplayError(f"Cannot read WAL {self._path}: {exc}") from exc
        return list(entries_map.values())

    def iter_raw(self) -> Iterator[WALEntry]:
        """Yield all raw (non-deduplicated) WAL entries in file order.

        Yields:
            Each ``WALEntry`` from the file.

        Raises:
            WALReplayError: On I/O or parse failure.
        """
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, start=1):
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        yield WALEntry.from_dict(data)
                    except (json.JSONDecodeError, StorageError) as exc:
                        raise WALReplayError(
                            f"Corrupt WAL entry at line {lineno} in {self._path}: {exc}"
                        ) from exc
        except OSError as exc:
            raise WALReplayError(f"Cannot read WAL {self._path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        """Return ``True`` if the WAL file exists on disk."""
        return self._path.exists()

    def delete(self) -> None:
        """Delete the WAL file (commit or rollback cleanup).

        Raises:
            StorageError: If the file exists but cannot be deleted.
        """
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(f"Cannot delete WAL {self._path}: {exc}") from exc


def find_wal_files(db_path: Path) -> list[Path]:
    """Return paths of all ``.wal`` files in *db_path*.

    Args:
        db_path: Database directory to scan.

    Returns:
        List of ``.wal`` file paths (may be empty).
    """
    return list(db_path.glob("*.wal"))
