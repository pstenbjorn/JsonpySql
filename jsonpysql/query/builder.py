"""Fluent query builder.

``QueryBuilder`` accumulates query clauses (``where``, ``join``,
``order_by``, ``limit``, ``skip``, ``select``, ``group_by``,
``aggregate``) and lazily executes via ``QueryExecutor`` when the
result is materialised.

Materialization methods:

- ``__iter__`` — lazy streaming (default)
- ``to_list()`` — eager list
- ``to_dict(key)`` — dict keyed on a field
- ``first()`` — first result or ``None``
- ``count()`` — result count
- ``explain()`` — ``QueryPlan`` for debugging
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from jsonpysql.query.executor import QueryExecutor
from jsonpysql.query.models import AggSpec, QueryPlan
from jsonpysql.query.planner import QueryPlanner
from jsonpysql.storage.engine import StorageEngine


class QueryBuilder:
    """Immutable-style fluent query builder.

    Each method returns a *new* ``QueryBuilder`` with the clause added,
    so chains can be branched and re-used.

    Args:
        collection: Name of the primary collection.
        engine: Live storage engine.
        indexed_fields: Set of field names that have a single-field
            index on *collection*.
        collection_size: Estimated document count for join planning.
        hash_join_threshold: Hash-join size threshold (default 10,000).
    """

    def __init__(
        self,
        collection: str,
        engine: StorageEngine,
        indexed_fields: set[str] | None = None,
        collection_size: int = 0,
        hash_join_threshold: int = 10_000,
    ) -> None:
        self._collection = collection
        self._engine = engine
        self._indexed_fields: set[str] = indexed_fields or set()
        self._collection_size = collection_size
        self._hash_join_threshold = hash_join_threshold

        self._predicates: list[Callable[[dict[str, Any]], bool]] = []
        self._join_collection: str | None = None
        self._join_predicate: Callable[..., bool] | None = None
        self._join_collection_size: int = 0
        self._order_by_field: str | None = None
        self._order_descending: bool = False
        self._limit: int | None = None
        self._skip: int = 0
        self._projection: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self._group_fields: tuple[str, ...] = ()
        self._aggregates: dict[str, AggSpec] = {}

    def _copy(self) -> QueryBuilder:
        """Return a shallow copy of this builder."""
        other = QueryBuilder(
            self._collection,
            self._engine,
            indexed_fields=set(self._indexed_fields),
            collection_size=self._collection_size,
            hash_join_threshold=self._hash_join_threshold,
        )
        other._predicates = list(self._predicates)
        other._join_collection = self._join_collection
        other._join_predicate = self._join_predicate
        other._join_collection_size = self._join_collection_size
        other._order_by_field = self._order_by_field
        other._order_descending = self._order_descending
        other._limit = self._limit
        other._skip = self._skip
        other._projection = self._projection
        other._group_fields = self._group_fields
        other._aggregates = dict(self._aggregates)
        return other

    # ------------------------------------------------------------------
    # Clause methods
    # ------------------------------------------------------------------

    def where(self, predicate: Callable[[dict[str, Any]], bool]) -> QueryBuilder:
        """Add a filter predicate (AND semantics with existing predicates).

        Args:
            predicate: A callable that receives a document dict and
                returns ``bool``.

        Returns:
            New ``QueryBuilder`` with the predicate appended.
        """
        q = self._copy()
        q._predicates.append(predicate)
        return q

    def join(
        self,
        collection: str,
        on: Callable[[dict[str, Any], dict[str, Any]], bool],
        collection_size: int = 0,
    ) -> QueryBuilder:
        """Add a two-collection join.

        Args:
            collection: Right-side collection name.
            on: Two-argument callable ``(left_doc, right_doc) → bool``.
            collection_size: Estimated doc count in *collection* for
                join strategy selection.

        Returns:
            New ``QueryBuilder`` with the join configured.
        """
        q = self._copy()
        q._join_collection = collection
        q._join_predicate = on
        q._join_collection_size = collection_size
        return q

    def order_by(self, field: str, descending: bool = False) -> QueryBuilder:
        """Set the result ordering field.

        Args:
            field: Field name to sort by.
            descending: When ``True``, sort highest first.

        Returns:
            New ``QueryBuilder`` with ordering set.
        """
        q = self._copy()
        q._order_by_field = field
        q._order_descending = descending
        return q

    def limit(self, n: int) -> QueryBuilder:
        """Cap the number of results.

        Args:
            n: Maximum result count.

        Returns:
            New ``QueryBuilder`` with limit set.
        """
        q = self._copy()
        q._limit = n
        return q

    def skip(self, n: int) -> QueryBuilder:
        """Skip the first *n* results.

        Args:
            n: Number of results to skip.

        Returns:
            New ``QueryBuilder`` with skip set.
        """
        q = self._copy()
        q._skip = n
        return q

    def select(
        self, projection: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> QueryBuilder:
        """Apply a projection (transformation) to each result document.

        Args:
            projection: Callable that receives a document and returns
                the projected dict.

        Returns:
            New ``QueryBuilder`` with projection set.
        """
        q = self._copy()
        q._projection = projection
        return q

    def group_by(self, *fields: str) -> QueryBuilder:
        """Set the group-by key fields for aggregation.

        Args:
            *fields: Field names to group by.

        Returns:
            New ``QueryBuilder`` with group-by fields set.
        """
        q = self._copy()
        q._group_fields = fields
        return q

    def aggregate(self, **named_specs: AggSpec) -> QueryBuilder:
        """Add named aggregate specifications.

        Args:
            **named_specs: Mapping of output field name → ``AggSpec``.

        Returns:
            New ``QueryBuilder`` with aggregates merged.
        """
        q = self._copy()
        q._aggregates = {**self._aggregates, **named_specs}
        return q

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    def _build_plan(self) -> QueryPlan:
        """Produce a ``QueryPlan`` for the current builder state."""
        planner = QueryPlanner(
            indexed_fields=self._indexed_fields,
            collection_size=self._collection_size,
            hash_join_threshold=self._hash_join_threshold,
        )
        return planner.plan(
            collection=self._collection,
            predicates=self._predicates,
            join_collection=self._join_collection,
            join_predicate=self._join_predicate,
            join_collection_size=self._join_collection_size,
            has_order_by=self._order_by_field is not None,
            has_limit=self._limit is not None,
            has_projection=self._projection is not None,
            has_aggregation=bool(self._aggregates),
        )

    def _execute(self) -> Iterator[dict[str, Any]]:
        """Run the query and return a lazy iterator of result docs."""
        plan = self._build_plan()
        executor = QueryExecutor(self._engine)
        return executor.execute(
            plan=plan,
            predicates=self._predicates,
            join_collection=self._join_collection,
            join_predicate=self._join_predicate,
            order_by_field=self._order_by_field,
            order_descending=self._order_descending,
            limit=self._limit,
            skip=self._skip,
            projection=self._projection,
            group_fields=self._group_fields,
            aggregates=self._aggregates if self._aggregates else None,
        )

    # ------------------------------------------------------------------
    # Materialization
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[dict[str, Any]]:
        """Lazily yield result documents.

        Yields:
            Result document dicts.
        """
        yield from self._execute()

    def to_list(self) -> list[dict[str, Any]]:
        """Eagerly collect and return all results as a list.

        Returns:
            List of result document dicts.
        """
        return list(self._execute())

    def to_dict(self, key: str) -> dict[str, dict[str, Any]]:
        """Return results as a dict keyed on *key*.

        Args:
            key: Field name to use as the dict key.

        Returns:
            Dict mapping field value → document dict.
        """
        return {doc[key]: doc for doc in self._execute()}

    def first(self) -> dict[str, Any] | None:
        """Return the first result document, or ``None`` if empty.

        Returns:
            First result dict, or ``None``.
        """
        for doc in self._execute():
            return doc
        return None

    def count(self) -> int:
        """Return the total number of result documents.

        Returns:
            Result count.
        """
        return sum(1 for _ in self._execute())

    def explain(self) -> QueryPlan:
        """Return the ``QueryPlan`` without executing the query.

        Returns:
            The planned ``QueryPlan``.
        """
        return self._build_plan()
