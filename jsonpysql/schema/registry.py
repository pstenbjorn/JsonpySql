"""CollectionManager — schema-layer collection registry.

Acts as the bridge between the public ``Database`` API and the storage
engine.  It keeps track of which collections are schema-backed
(``SchemaCollection``) and which are schema-free (``DocumentCollection``),
builds ``IndexSpec`` lists from Pydantic model metadata, and wires up
``ReferentialIntegrityChecker`` instances.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from jsonpysql.exceptions import CollectionExistsError, StorageError
from jsonpysql.schema.collection import DocumentCollection, SchemaCollection
from jsonpysql.schema.constraints import ReferentialIntegrityChecker
from jsonpysql.schema.fields import (
    ForeignKeyMeta,
    get_foreign_keys,
    get_indexed_fields,
    get_primary_key_field,
    get_unique_fields,
)
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import IndexSpec


class CollectionManager:
    """Registers, retrieves, and drops schema and document collections.

    Args:
        engine: The live ``StorageEngine`` instance.
    """

    def __init__(self, engine: StorageEngine) -> None:
        self._engine = engine
        self._schema_cols: dict[str, SchemaCollection] = {}
        self._doc_cols: dict[str, DocumentCollection] = {}
        # Mapping of (parent_collection, child_collection) → FK meta list
        self._fk_registry: list[tuple[str, str, str, ForeignKeyMeta]] = []

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_schema_collection(
        self,
        name: str,
        model: type[BaseModel],
        drop_if_exists: bool = False,
    ) -> SchemaCollection:
        """Register a schema-backed collection.

        Derives ``IndexSpec`` entries from the model's field metadata and
        creates the collection in the storage engine.

        Args:
            name: Collection name.
            model: Pydantic ``BaseModel`` subclass.
            drop_if_exists: When ``True``, silently replace an existing
                collection of the same name.

        Returns:
            The new ``SchemaCollection``.

        Raises:
            CollectionExistsError: If the collection exists and
                ``drop_if_exists`` is ``False``.
        """
        if name in self._schema_cols or name in self._doc_cols:
            if not drop_if_exists:
                raise CollectionExistsError(
                    f"Collection {name!r} is already registered."
                )
            self.drop_collection(name)

        indexed = get_indexed_fields(model)
        unique = get_unique_fields(model)
        specs = [
            IndexSpec(fields=[f], unique=(f in unique))
            for f in indexed
        ]
        # When reopening an existing database the collection already exists
        # in the storage engine but is absent from this manager's in-memory
        # registry.  In that case, skip create_collection to preserve data.
        engine_has_it = name in self._engine._stores
        if not engine_has_it:
            self._engine.create_collection(name, specs)
        elif drop_if_exists:
            # User explicitly requested replacement — drop then recreate.
            self._engine.drop_collection(name)
            self._engine.create_collection(name, specs)

        fk_checkers = self._build_fk_checkers(name, model)
        col = SchemaCollection(name, model, self._engine, fk_checkers=fk_checkers)
        self._schema_cols[name] = col
        return col

    def register_document_collection(
        self,
        name: str,
        indexes: list[str] | None = None,
        drop_if_exists: bool = False,
    ) -> DocumentCollection:
        """Register a schema-free document collection.

        Args:
            name: Collection name.
            indexes: Optional list of field names to index.
            drop_if_exists: When ``True``, silently replace an existing
                collection.

        Returns:
            The new ``DocumentCollection``.

        Raises:
            CollectionExistsError: If the collection exists and
                ``drop_if_exists`` is ``False``.
        """
        if name in self._schema_cols or name in self._doc_cols:
            if not drop_if_exists:
                raise CollectionExistsError(
                    f"Collection {name!r} is already registered."
                )
            self.drop_collection(name)

        specs = [IndexSpec(fields=[f]) for f in (indexes or [])]
        engine_has_it = name in self._engine._stores
        if not engine_has_it:
            self._engine.create_collection(name, specs)
        elif drop_if_exists:
            self._engine.drop_collection(name)
            self._engine.create_collection(name, specs)

        col = DocumentCollection(name, self._engine)
        self._doc_cols[name] = col
        return col

    def drop_collection(self, name: str) -> None:
        """Drop a collection from both the registry and the storage engine.

        Args:
            name: Collection name.

        Raises:
            StorageError: If the collection is not registered.
        """
        if name not in self._schema_cols and name not in self._doc_cols:
            raise StorageError(f"Collection {name!r} is not registered.")
        self._schema_cols.pop(name, None)
        self._doc_cols.pop(name, None)
        self._engine.drop_collection(name)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_collection(self, name: str) -> SchemaCollection | DocumentCollection:
        """Return the collection named *name*.

        Args:
            name: Collection name.

        Returns:
            The ``SchemaCollection`` or ``DocumentCollection``.

        Raises:
            StorageError: If no collection with *name* is registered.
        """
        if name in self._schema_cols:
            return self._schema_cols[name]
        if name in self._doc_cols:
            return self._doc_cols[name]
        raise StorageError(f"No collection registered with name {name!r}.")

    def has_collection(self, name: str) -> bool:
        """Return ``True`` if *name* is a registered collection.

        Args:
            name: Collection name.
        """
        return name in self._schema_cols or name in self._doc_cols

    def list_collections(self) -> list[str]:
        """Return names of all registered collections (sorted).

        Returns:
            Sorted list of collection names.
        """
        return sorted(set(self._schema_cols) | set(self._doc_cols))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_fk_checkers(
        self,
        child_col_name: str,
        child_model: type[BaseModel],
    ) -> dict[str, ReferentialIntegrityChecker]:
        """Build ``ReferentialIntegrityChecker`` instances for every FK field.

        The parent collection name is derived from the target model's class
        name, lower-cased and pluralised with ``'s'``.  For example, a FK
        to ``Customer`` targets the ``"customers"`` collection.

        Args:
            child_col_name: Name of the child collection being registered.
            child_model: The child Pydantic model.

        Returns:
            Dict mapping FK field name → checker.
        """
        checkers: dict[str, ReferentialIntegrityChecker] = {}
        for fk_field, fk_meta in get_foreign_keys(child_model).items():
            parent_col_name = fk_meta.target_model.__name__.lower() + "s"
            parent_pk = get_primary_key_field(fk_meta.target_model) or "id"
            checker = ReferentialIntegrityChecker(
                engine=self._engine,
                parent_collection=parent_col_name,
                parent_pk_field=parent_pk,
                child_collection=child_col_name,
                fk_field=fk_field,
                on_delete=fk_meta.on_delete,
            )
            checkers[fk_field] = checker
        return checkers
