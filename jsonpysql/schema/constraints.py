"""Unique-constraint and referential-integrity checkers for the schema engine.

Both checkers work against a ``StorageEngine`` instance to perform indexed
lookups.  They raise the appropriate ``jsonpysql`` exceptions rather than
returning boolean results so that callers can propagate errors without any
extra logic.
"""

from __future__ import annotations

from typing import Any

from jsonpysql.exceptions import ReferentialIntegrityError, UniqueConstraintError
from jsonpysql.storage.engine import StorageEngine


class UniqueConstraintChecker:
    """Checks that a field value does not already exist in the collection.

    Args:
        engine: The live ``StorageEngine`` instance.
        collection: Collection name to check.
        unique_fields: Field names that carry a unique constraint.
    """

    def __init__(
        self,
        engine: StorageEngine,
        collection: str,
        unique_fields: list[str],
    ) -> None:
        self._engine = engine
        self._collection = collection
        self._unique_fields = unique_fields

    def check_insert(self, document: dict[str, Any]) -> None:
        """Raise ``UniqueConstraintError`` if any unique field is already taken.

        Args:
            document: The document about to be inserted.

        Raises:
            UniqueConstraintError: When a unique field value already exists.
        """
        for field_name in self._unique_fields:
            value = document.get(field_name)
            if value is None:
                continue
            existing = list(
                self._engine.lookup(self._collection, field_name, value)
            )
            if existing:
                raise UniqueConstraintError(
                    f"Unique constraint violated on {self._collection!r}.{field_name!r}: "
                    f"value {value!r} already exists."
                )

    def check_update(
        self,
        doc_id: str,
        document: dict[str, Any],
    ) -> None:
        """Raise ``UniqueConstraintError`` if an updated value conflicts.

        Conflicts with the document's *own* current ``doc_id`` are
        ignored — a document may be updated to keep the same unique value.

        Args:
            doc_id: The ID of the document being updated.
            document: The new document data.

        Raises:
            UniqueConstraintError: When a unique field value is already
                owned by a *different* document.
        """
        for field_name in self._unique_fields:
            value = document.get(field_name)
            if value is None:
                continue
            existing = list(
                self._engine.lookup(self._collection, field_name, value)
            )
            conflicts = [eid for eid in existing if eid != doc_id]
            if conflicts:
                raise UniqueConstraintError(
                    f"Unique constraint violated on {self._collection!r}.{field_name!r}: "
                    f"value {value!r} is owned by document {conflicts[0]!r}."
                )


class ReferentialIntegrityChecker:
    """Checks foreign-key constraints using the storage-layer index.

    Args:
        engine: The live ``StorageEngine`` instance.
        parent_collection: Name of the collection that holds the parent docs.
        parent_pk_field: Primary-key field name of the parent model.
        child_collection: Name of the collection that holds the child docs.
        fk_field: The FK field name on the child document.
        on_delete: Action when the parent is deleted.
    """

    def __init__(
        self,
        engine: StorageEngine,
        parent_collection: str,
        parent_pk_field: str,
        child_collection: str,
        fk_field: str,
        on_delete: str,
    ) -> None:
        self._engine = engine
        self._parent_collection = parent_collection
        self._parent_pk = parent_pk_field
        self._child_collection = child_collection
        self._fk_field = fk_field
        self._on_delete = on_delete

    def check_child_insert(self, child_document: dict[str, Any]) -> None:
        """Verify the referenced parent exists when inserting a child.

        Args:
            child_document: The child document about to be inserted.

        Raises:
            ReferentialIntegrityError: When the referenced parent ID is not
                found in the parent collection.
        """
        parent_id = child_document.get(self._fk_field)
        if parent_id is None:
            return  # nullable FK — skip check
        self._assert_parent_exists(parent_id)

    def check_parent_delete(self, parent_id: str) -> None:
        """Enforce the ``on_delete`` rule before the parent is removed.

        Args:
            parent_id: The ID of the parent document about to be deleted.

        Raises:
            ReferentialIntegrityError: When ``on_delete="restrict"`` and at
                least one child exists.
        """
        im = self._engine._indexes.get(self._child_collection)
        children = im.get_children(parent_id) if im is not None else []

        if not children:
            return

        if self._on_delete == "restrict":
            raise ReferentialIntegrityError(
                f"Cannot delete {self._parent_collection!r} document {parent_id!r}: "
                f"{len(children)} child document(s) exist in "
                f"{self._child_collection!r} (on_delete='restrict')."
            )
        # cascade and set_null are handled by SchemaCollection after this check.

    def _assert_parent_exists(self, parent_id: str) -> None:
        """Raise ``ReferentialIntegrityError`` if *parent_id* is not found.

        Args:
            parent_id: The parent document identifier to verify.

        Raises:
            ReferentialIntegrityError: When the parent is not found.
        """
        parent_doc = self._engine.get(self._parent_collection, str(parent_id))
        if parent_doc is None:
            raise ReferentialIntegrityError(
                f"Foreign-key constraint failed on "
                f"{self._child_collection!r}.{self._fk_field!r}: "
                f"parent {self._parent_collection!r} document {parent_id!r} not found."
            )
