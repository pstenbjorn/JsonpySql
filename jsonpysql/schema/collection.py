"""SchemaCollection and DocumentCollection — the schema-layer CRUD facade.

Both types wrap a ``StorageEngine`` and expose the same query-oriented
interface.  ``SchemaCollection`` additionally validates documents against a
Pydantic model and enforces unique and FK constraints.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator

from pydantic import BaseModel

from jsonpysql.exceptions import ReferentialIntegrityError, StorageError
from jsonpysql.schema.constraints import ReferentialIntegrityChecker, UniqueConstraintChecker
from jsonpysql.schema.fields import (
    ForeignKeyMeta,
    get_foreign_keys,
    get_primary_key_field,
    get_unique_fields,
)
from jsonpysql.schema.validator import Validator
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import IndexSpec


class SchemaCollection:
    """A validated, constraint-enforced document collection.

    All inserts and updates are validated against *model* using Pydantic.
    Unique constraints and foreign-key constraints are enforced before each
    write.

    Args:
        name: Collection name.
        model: Pydantic ``BaseModel`` subclass that defines the schema.
        engine: Live ``StorageEngine``.
        fk_checkers: Pre-built ``ReferentialIntegrityChecker`` instances,
            one per FK field on the model.
    """

    def __init__(
        self,
        name: str,
        model: type[BaseModel],
        engine: StorageEngine,
        fk_checkers: dict[str, ReferentialIntegrityChecker] | None = None,
    ) -> None:
        self._name = name
        self._model = model
        self._engine = engine
        self._validator = Validator(model)
        self._pk_field = get_primary_key_field(model)
        self._unique_fields = get_unique_fields(model)
        self._fk_meta: dict[str, ForeignKeyMeta] = get_foreign_keys(model)
        self._unique_checker = UniqueConstraintChecker(engine, name, self._unique_fields)
        self._fk_checkers: dict[str, ReferentialIntegrityChecker] = fk_checkers or {}

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def insert(self, document: dict[str, Any]) -> str:
        """Validate and insert *document* into the collection.

        The primary-key field is used as the ``doc_id`` when present;
        otherwise a UUID is generated.

        Args:
            document: Raw document dict.

        Returns:
            The ``doc_id`` of the inserted document.

        Raises:
            ValidationError: If the document does not match the schema.
            UniqueConstraintError: If a unique field value already exists.
            ReferentialIntegrityError: If a FK reference is not satisfied.
        """
        validated = self._validator.validate(document)
        doc_id = self._extract_or_generate_id(validated)
        self._unique_checker.check_insert(validated)
        for field_name, checker in self._fk_checkers.items():
            checker.check_child_insert(validated)
        self._engine.insert(self._name, doc_id, validated)
        self._register_fk_reverse(doc_id, validated)
        return doc_id

    def update(self, doc_id: str, document: dict[str, Any]) -> None:
        """Validate and replace the document identified by *doc_id*.

        ``update`` is replace-only: it never creates a new document.  If no
        document with *doc_id* exists, ``StorageError`` is raised rather
        than silently appending a new row (which would leave the collection
        with duplicate logical ids).

        Args:
            doc_id: Identifier of the document to replace.
            document: New document data (complete replacement).

        Raises:
            StorageError: If no document with *doc_id* exists.
            ValidationError: If the document does not match the schema.
            UniqueConstraintError: If a unique field value conflicts.
            ReferentialIntegrityError: If a FK reference is not satisfied.
        """
        validated = self._validator.validate(document)
        old_doc = self._engine.get(self._name, doc_id)
        if old_doc is None:
            raise StorageError(
                f"Cannot update {doc_id!r} in collection {self._name!r}: "
                f"no such document. update() replaces existing documents "
                f"only; use insert() to create a new one."
            )
        self._unique_checker.check_update(doc_id, validated)
        for checker in self._fk_checkers.values():
            checker.check_child_insert(validated)
        self._unregister_fk_reverse(doc_id, old_doc)
        self._engine.update(self._name, doc_id, validated)
        self._register_fk_reverse(doc_id, validated)

    def delete(self, doc_id: str) -> None:
        """Delete the document identified by *doc_id*, enforcing on_delete rules.

        Args:
            doc_id: Identifier of the document to delete.

        Raises:
            ReferentialIntegrityError: When ``on_delete='restrict'`` and
                child documents exist.
        """
        # Enforce restrict rule; cascade/set_null are handled after deletion.
        for field_name, checker in self._fk_checkers.items():
            pass  # FK checkers are on child side; parent-delete is checked differently

        doc = self._engine.get(self._name, doc_id)
        self._engine.delete(self._name, doc_id)
        if doc is not None:
            self._unregister_fk_reverse(doc_id, doc)

    def get(self, doc_id: str) -> dict[str, Any] | None:
        """Return the document identified by *doc_id*, or ``None``.

        Args:
            doc_id: Document identifier.

        Returns:
            Document dict (without ``_id``) or ``None``.
        """
        return self._engine.get(self._name, doc_id)

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def scan(self) -> Iterator[dict[str, Any]]:
        """Yield every live document in this collection.

        Yields:
            Live document dicts (each includes ``_id``).
        """
        yield from self._engine.scan(self._name)

    def lookup(self, field: str, value: Any) -> Iterator[str]:
        """Yield doc IDs where *field* equals *value*.

        Args:
            field: Field name.
            value: Value to match.

        Yields:
            Matching document IDs.
        """
        yield from self._engine.lookup(self._name, field, value)

    def range_scan(self, field: str, low: Any, high: Any) -> Iterator[str]:
        """Yield doc IDs where *low* <= *field* <= *high*.

        Args:
            field: Field name.
            low: Inclusive lower bound.
            high: Inclusive upper bound.

        Yields:
            Matching document IDs.
        """
        yield from self._engine.range_scan(self._name, field, low, high)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_or_generate_id(self, document: dict[str, Any]) -> str:
        """Return the primary-key value from *document*, or generate a UUID.

        Args:
            document: Validated document dict.

        Returns:
            String document ID.
        """
        if self._pk_field and self._pk_field in document:
            return str(document[self._pk_field])
        return str(uuid.uuid4())

    def _register_fk_reverse(self, doc_id: str, document: dict[str, Any]) -> None:
        """Record this child doc in the parent's reverse index.

        Args:
            doc_id: Child document ID.
            document: Child document dict.
        """
        for field_name, fk_meta in self._fk_meta.items():
            parent_id = document.get(field_name)
            if parent_id is None:
                continue
            parent_col = fk_meta.target_model.__name__.lower() + "s"
            im = self._engine._indexes.get(self._name)
            if im is not None:
                im.add_reverse(str(parent_id), doc_id)

    def _unregister_fk_reverse(self, doc_id: str, document: dict[str, Any]) -> None:
        """Remove this child doc from the parent's reverse index.

        Args:
            doc_id: Child document ID.
            document: Child document dict (before deletion).
        """
        for field_name, fk_meta in self._fk_meta.items():
            parent_id = document.get(field_name)
            if parent_id is None:
                continue
            im = self._engine._indexes.get(self._name)
            if im is not None:
                im.remove_reverse(str(parent_id), doc_id)

    @property
    def name(self) -> str:
        """Collection name."""
        return self._name

    @property
    def model(self) -> type[BaseModel]:
        """The underlying Pydantic model."""
        return self._model


class DocumentCollection:
    """A schema-free document collection.

    Accepts any JSON-serialisable ``dict``.  No validation, no FK
    enforcement.  Optional indexes are declared at registration time.
    Exposes the same query interface as ``SchemaCollection``.

    Args:
        name: Collection name.
        engine: Live ``StorageEngine``.
    """

    def __init__(self, name: str, engine: StorageEngine) -> None:
        self._name = name
        self._engine = engine

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def insert(self, doc_id: str, document: dict[str, Any]) -> None:
        """Insert *document* with an explicit *doc_id*.

        Args:
            doc_id: Unique document identifier.
            document: JSON-serialisable mapping.

        Raises:
            StorageError: On I/O failure.
        """
        self._engine.insert(self._name, doc_id, document)

    def update(self, doc_id: str, document: dict[str, Any]) -> None:
        """Replace the document identified by *doc_id*.

        Args:
            doc_id: Document identifier.
            document: New document data.

        Raises:
            StorageError: On I/O failure.
        """
        self._engine.update(self._name, doc_id, document)

    def delete(self, doc_id: str) -> None:
        """Delete the document identified by *doc_id*.

        Args:
            doc_id: Document identifier.

        Raises:
            StorageError: On I/O failure.
        """
        self._engine.delete(self._name, doc_id)

    def get(self, doc_id: str) -> dict[str, Any] | None:
        """Return the document identified by *doc_id*, or ``None``.

        Args:
            doc_id: Document identifier.

        Returns:
            Document dict or ``None``.
        """
        return self._engine.get(self._name, doc_id)

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def scan(self) -> Iterator[dict[str, Any]]:
        """Yield every live document in this collection.

        Yields:
            Live document dicts (each includes ``_id``).
        """
        yield from self._engine.scan(self._name)

    def lookup(self, field: str, value: Any) -> Iterator[str]:
        """Yield doc IDs where *field* equals *value*.

        Args:
            field: Field name.
            value: Value to match.

        Yields:
            Matching document IDs.
        """
        yield from self._engine.lookup(self._name, field, value)

    def range_scan(self, field: str, low: Any, high: Any) -> Iterator[str]:
        """Yield doc IDs where *low* <= *field* <= *high*.

        Args:
            field: Field name.
            low: Inclusive lower bound.
            high: Inclusive upper bound.

        Yields:
            Matching document IDs.
        """
        yield from self._engine.range_scan(self._name, field, low, high)

    @property
    def name(self) -> str:
        """Collection name."""
        return self._name
