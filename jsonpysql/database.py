"""Public ``Database`` API.

``Database`` is the single entry point for application code.  It owns
a ``StorageEngine``, a ``CollectionManager``, and a ``FunctionRegistry``
and exposes the integrated, user-facing API.

Usage::

    from jsonpysql import Database
    from pydantic import BaseModel
    from jsonpysql import field

    db = Database("my_db")

    class Customer(BaseModel):
        id: str = field(primary_key=True)
        email: str = field(unique=True, index=True)
        name: str = field()

    db.register_collection("customers", Customer)
    db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})

    results = db.customers.where(lambda c: c["email"] == "a@b.com").to_list()
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel

from jsonpysql.exceptions import SchemaError, StorageError
from jsonpysql.query.builder import QueryBuilder
from jsonpysql.schema.collection import DocumentCollection, SchemaCollection
from jsonpysql.schema.functions import FunctionRegistry
from jsonpysql.schema.registry import CollectionManager
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import CollectionStats, Transaction


class DatabaseStats:
    """Aggregated statistics for all collections in the database.

    Args:
        collection_stats: Per-collection statistics.
    """

    def __init__(self, collection_stats: list[CollectionStats]) -> None:
        self._stats = collection_stats

    @property
    def collections(self) -> list[CollectionStats]:
        """Per-collection statistics."""
        return list(self._stats)

    @property
    def total_documents(self) -> int:
        """Sum of live document counts across all collections."""
        return sum(s.document_count for s in self._stats)

    def __repr__(self) -> str:
        return (
            f"DatabaseStats(collections={len(self._stats)}, "
            f"total_documents={self.total_documents})"
        )


class _QueryableCollection:
    """Thin wrapper that makes a collection iterable via ``QueryBuilder``.

    Returned by ``Database.__getattr__`` so that ``db.customers.where(...)``
    works seamlessly.

    Args:
        collection: The underlying ``SchemaCollection`` or
            ``DocumentCollection``.
        engine: The live storage engine (for query execution).
        indexed_fields: Set of field names with a single-field index.
        collection_size: Estimated document count.
    """

    def __init__(
        self,
        collection: SchemaCollection | DocumentCollection,
        engine: StorageEngine,
        indexed_fields: set[str],
        collection_size: int,
    ) -> None:
        self._col = collection
        self._engine = engine
        self._indexed_fields = indexed_fields
        self._collection_size = collection_size

    # Delegate CRUD to the underlying collection
    def insert(self, *args: Any, **kwargs: Any) -> Any:
        """Insert a document into the collection."""
        return self._col.insert(*args, **kwargs)  # type: ignore[arg-type]

    def update(self, *args: Any, **kwargs: Any) -> None:
        """Update a document in the collection."""
        self._col.update(*args, **kwargs)  # type: ignore[arg-type]

    def delete(self, *args: Any, **kwargs: Any) -> None:
        """Delete a document from the collection."""
        self._col.delete(*args, **kwargs)  # type: ignore[arg-type]

    def get(self, doc_id: str) -> dict[str, Any] | None:
        """Return the document with *doc_id*, or ``None``."""
        return self._col.get(doc_id)

    # Fluent query API
    def where(self, predicate: Callable[[dict[str, Any]], bool]) -> QueryBuilder:
        """Start a filtered query on this collection.

        Args:
            predicate: A callable that receives a document dict and
                returns ``bool``.

        Returns:
            A ``QueryBuilder`` with the predicate applied.
        """
        return self._builder().where(predicate)

    def join(
        self,
        collection: str,
        on: Callable[[dict[str, Any], dict[str, Any]], bool],
        collection_size: int = 0,
    ) -> QueryBuilder:
        """Start a join query on this collection.

        Args:
            collection: Right-side collection name.
            on: Join predicate.
            collection_size: Estimated doc count on the right side.

        Returns:
            A ``QueryBuilder`` with the join configured.
        """
        return self._builder().join(collection, on, collection_size=collection_size)

    def order_by(self, field: str, descending: bool = False) -> QueryBuilder:
        """Start an ordered query on this collection."""
        return self._builder().order_by(field, descending=descending)

    def limit(self, n: int) -> QueryBuilder:
        """Start a limited query on this collection."""
        return self._builder().limit(n)

    def group_by(self, *fields: str) -> QueryBuilder:
        """Start a group-by query on this collection."""
        return self._builder().group_by(*fields)

    def all(self) -> list[dict[str, Any]]:
        """Return all documents in the collection as a list.

        Returns:
            List of all document dicts.
        """
        return self._builder().to_list()

    def _builder(self) -> QueryBuilder:
        return QueryBuilder(
            self._col.name,
            self._engine,
            indexed_fields=self._indexed_fields,
            collection_size=self._collection_size,
        )

    @property
    def name(self) -> str:
        """Collection name."""
        return self._col.name


class _FunctionAccessor:
    """Attribute-style accessor for registered functions and procedures.

    Returned by ``Database.fn``.  Registered **functions** are returned
    unchanged.  Registered **procedures** are returned with the owning
    ``Database`` bound as their first argument (the database context), so
    ``db.fn.my_procedure(args)`` matches ``Database.procedure``'s
    documented contract.  Any other attribute (registry methods such as
    ``list_functions``) is delegated to the underlying registry.

    Args:
        db: The owning ``Database`` instance, injected into procedures.
        registry: The live ``FunctionRegistry``.
    """

    def __init__(self, db: Database, registry: FunctionRegistry) -> None:
        self._db = db
        self._registry = registry

    def __getattr__(self, name: str) -> Any:
        """Resolve *name* to a function, a db-bound procedure, or a
        registry attribute.

        Args:
            name: Function name, procedure name, or registry attribute.

        Returns:
            The callable (procedures are bound to the database context).

        Raises:
            AttributeError: For private/dunder attribute names.
            FunctionError: If *name* is not a registered function,
                procedure, or registry attribute.
        """
        # Guard private/dunder to avoid recursion during attribute setup.
        if name.startswith("_"):
            raise AttributeError(name)
        registry = self._registry
        if name in registry._procedures:
            # Inject the database context as the first positional argument.
            return functools.partial(registry._procedures[name], self._db)
        # Functions and registry methods pass through unchanged.
        return getattr(registry, name)


class Database:
    """Public database interface.

    One ``Database`` instance corresponds to one directory on the file
    system.  On construction, the database directory is created if
    absent, the storage engine is initialised (with WAL crash recovery),
    and the function registry is loaded from ``functions.pkl`` if it
    exists.

    Args:
        path: File-system path for the database directory.
        lock_timeout: Seconds to wait for a file lock before raising
            ``LockTimeoutError``.  Defaults to 5.0.

    Example::

        db = Database("/tmp/mydb")
        db.register_collection("logs", indexes=["level"])
        db.logs.insert("id1", {"level": "INFO", "msg": "hello"})
    """

    def __init__(self, path: str | Path, lock_timeout: float = 5.0) -> None:
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)
        self._engine = StorageEngine(self._path, lock_timeout=lock_timeout)
        self._manager = CollectionManager(self._engine)
        self._fn_registry = FunctionRegistry(self._path)
        self._fn_registry.load()

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    def register_collection(
        self,
        name: str,
        model: type[BaseModel] | None = None,
        indexes: list[str] | None = None,
        drop_if_exists: bool = False,
    ) -> None:
        """Register a new collection.

        When *model* is provided, a schema-backed ``SchemaCollection`` is
        created and its indexes are derived from the model's
        ``field(index=True)`` / ``field(unique=True)`` metadata.  Otherwise
        a schema-free ``DocumentCollection`` is created with the optional
        *indexes* list.

        Args:
            name: Collection name.  Must be a valid Python identifier.
            model: Optional Pydantic ``BaseModel`` subclass.
            indexes: Optional list of field names to index.  Only valid for
                document collections (when *model* is ``None``); schema
                collections declare indexes via ``field(index=True)``.
            drop_if_exists: When ``True``, silently replace an existing
                collection of the same name.

        Raises:
            SchemaError: If both *model* and *indexes* are supplied.  A
                schema collection's indexes come from its model metadata,
                so passing *indexes* alongside a model would be silently
                ignored — this is rejected rather than dropped.
        """
        if model is not None:
            if indexes is not None:
                raise SchemaError(
                    f"Cannot pass indexes={indexes!r} when registering schema "
                    f"collection {name!r} with a model. Declare indexes on the "
                    f"model with field(index=True) instead."
                )
            self._manager.register_schema_collection(
                name, model, drop_if_exists=drop_if_exists
            )
        else:
            self._manager.register_document_collection(
                name, indexes=indexes, drop_if_exists=drop_if_exists
            )

    def drop_collection(self, name: str) -> None:
        """Drop the collection named *name*.

        Args:
            name: Collection name.

        Raises:
            StorageError: If no collection with *name* is registered.
        """
        self._manager.drop_collection(name)

    def has_collection(self, name: str) -> bool:
        """Return ``True`` if *name* is a registered collection.

        Args:
            name: Collection name.
        """
        return self._manager.has_collection(name)

    def list_collections(self) -> list[str]:
        """Return a sorted list of registered collection names.

        Returns:
            Sorted list of collection names.
        """
        return self._manager.list_collections()

    # ------------------------------------------------------------------
    # Collection access via attribute syntax
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> _QueryableCollection:
        """Return the collection named *name* wrapped for fluent queries.

        Args:
            name: Collection name.

        Returns:
            A ``_QueryableCollection`` wrapping the underlying collection.

        Raises:
            StorageError: If no collection with *name* is registered.
            AttributeError: For private/dunder attributes.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            col = self._manager.get_collection(name)
        except StorageError as exc:
            raise StorageError(str(exc)) from exc
        indexed = self._get_indexed_fields(name)
        size = self._engine.get_stats(name).document_count
        return _QueryableCollection(col, self._engine, indexed, size)

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def transaction(self) -> Transaction:
        """Return a ``Transaction`` context manager.

        Usage::

            with db.transaction() as txn:
                txn.insert("customers", "c1", {...})
                txn.insert("orders", "o1", {...})

        Returns:
            An active ``Transaction``.
        """
        return self._engine.begin_transaction()

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------

    def compact(self, collection: str | None = None) -> None:
        """Compact one or all collections.

        Rewrites the JSONL file(s) without tombstoned documents.

        Args:
            collection: Collection name, or ``None`` to compact all.
        """
        if collection is not None:
            self._engine.compact(collection)
        else:
            for name in self._manager.list_collections():
                self._engine.compact(name)

    # ------------------------------------------------------------------
    # Function registry
    # ------------------------------------------------------------------

    def function(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register *fn* as a named database function.

        Usable as a decorator.

        Args:
            fn: A pure callable with a ``__name__`` attribute.

        Returns:
            The same callable.
        """
        return self._fn_registry.register_function(fn)

    def procedure(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register *fn* as a named database procedure.

        Usable as a decorator.  The first argument of *fn* will receive
        a reference to this ``Database`` when called.

        Args:
            fn: A callable whose first parameter is the database context.

        Returns:
            The same callable.
        """
        return self._fn_registry.register_procedure(fn)

    @property
    def fn(self) -> _FunctionAccessor:
        """Attribute-style access to registered functions and procedures.

        Functions are returned unchanged; procedures are returned with
        this ``Database`` bound as their first argument (the database
        context), so ``db.fn.my_procedure(args)`` invokes the procedure
        as ``my_procedure(db, args)`` — matching ``Database.procedure``'s
        documented contract.
        """
        return _FunctionAccessor(self, self._fn_registry)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def stats(self) -> DatabaseStats:
        """Return aggregated statistics for all registered collections.

        Returns:
            A ``DatabaseStats`` object.
        """
        col_stats = [
            self._engine.get_stats(name)
            for name in self._manager.list_collections()
        ]
        return DatabaseStats(col_stats)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """No-op lifecycle hook — included for API completeness.

        The storage engine is file-based with no persistent connection,
        so there is nothing to close.
        """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_indexed_fields(self, collection: str) -> set[str]:
        """Return field names that have a single-field index.

        Args:
            collection: Collection name.

        Returns:
            Set of indexed field names.
        """
        im = self._engine._indexes.get(collection)
        if im is None:
            return set()
        return {
            spec.fields[0]
            for spec in im._specs
            if len(spec.fields) == 1
        }
