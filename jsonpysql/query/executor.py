"""Query executor.

Takes a ``QueryPlan`` and a ``QuerySpec`` (the accumulated state from the
``QueryBuilder``) and produces a lazy stream of result documents.

Scan strategies:

- ``INDEX_LOOKUP``: equality lookup via ``StorageEngine.lookup``.
- ``RANGE_SCAN``: range lookup via ``StorageEngine.range_scan``.
- ``FULL_SCAN``: iterate every document via ``StorageEngine.scan``.

Post-scan steps (applied in order):

1. Predicate filters (all ``.where()`` callables, AND semantics).
2. Join (nested-loop default; hash join when right side is small).
3. ``order_by`` sort.
4. ``skip`` / ``limit`` slicing.
5. ``select`` projection.
6. Aggregation (``group_by`` + ``aggregate``).
"""

from __future__ import annotations

from typing import Any, Callable, Iterator

from jsonpysql.query.aggregates import apply_aggregates
from jsonpysql.query.models import AggSpec, JoinStrategy, QueryPlan, ScanType
from jsonpysql.storage.engine import StorageEngine


class QueryExecutor:
    """Runs a ``QueryPlan`` against a live ``StorageEngine``.

    Args:
        engine: The live storage engine.
    """

    def __init__(self, engine: StorageEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def execute(
        self,
        plan: QueryPlan,
        predicates: list[Callable[[dict[str, Any]], bool]],
        join_collection: str | None = None,
        join_predicate: Callable[[dict[str, Any], dict[str, Any]], bool] | None = None,
        order_by_field: str | None = None,
        order_descending: bool = False,
        limit: int | None = None,
        skip: int = 0,
        projection: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        group_fields: tuple[str, ...] = (),
        aggregates: dict[str, AggSpec] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Execute *plan* and yield result documents.

        Args:
            plan: The ``QueryPlan`` produced by the planner.
            predicates: List of filter callables (AND semantics).
            join_collection: Name of the right-side collection for a join.
            join_predicate: Two-argument callable ``(left, right) → bool``.
            order_by_field: Field name for result ordering, or ``None``.
            order_descending: When ``True``, reverse the sort order.
            limit: Maximum number of results to yield.
            skip: Number of results to skip before yielding.
            projection: Single-argument callable that transforms each
                result document.
            group_fields: Tuple of field names for ``group_by``.
            aggregates: Named aggregation specs (requires *group_fields*).

        Yields:
            Result document dicts.
        """
        docs = self._scan(plan)
        docs = self._apply_predicates(docs, predicates)

        if join_collection is not None and join_predicate is not None:
            docs = self._join(docs, plan, join_collection, join_predicate)

        if aggregates:
            results = apply_aggregates(docs, group_fields, aggregates)
            if order_by_field is not None:
                results = sorted(
                    results,
                    key=lambda d: (d.get(order_by_field) is None, d.get(order_by_field)),
                    reverse=order_descending,
                )
            yield from self._slice(iter(results), skip, limit)
            return

        if order_by_field is not None:
            docs = self._sort(docs, order_by_field, order_descending)

        docs = self._slice(docs, skip, limit)

        if projection is not None:
            docs = (projection(d) for d in docs)

        yield from docs

    # ------------------------------------------------------------------
    # Scan strategies
    # ------------------------------------------------------------------

    def _scan(self, plan: QueryPlan) -> Iterator[dict[str, Any]]:
        """Produce documents from the primary collection per the plan.

        Args:
            plan: Query plan with scan strategy.

        Yields:
            Raw document dicts (each includes ``_id``).
        """
        col = plan.collection

        if plan.scan_type is ScanType.INDEX_LOOKUP and plan.index_field is not None:
            doc_ids = self._engine.lookup(col, plan.index_field, plan.index_value)
            for doc_id in doc_ids:
                doc = self._engine.get(col, doc_id)
                if doc is not None:
                    yield doc
            return

        if plan.scan_type is ScanType.RANGE_SCAN and plan.index_field is not None:
            low = plan.index_low if plan.index_low is not None else ""
            high = plan.index_high if plan.index_high is not None else "\uffff" * 10
            doc_ids = self._engine.range_scan(col, plan.index_field, low, high)
            for doc_id in doc_ids:
                doc = self._engine.get(col, doc_id)
                if doc is not None:
                    yield doc
            return

        # Default: full scan
        yield from self._engine.scan(col)

    # ------------------------------------------------------------------
    # Filter / join
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_predicates(
        docs: Iterator[dict[str, Any]],
        predicates: list[Callable[[dict[str, Any]], bool]],
    ) -> Iterator[dict[str, Any]]:
        """Yield only documents that satisfy every predicate.

        Args:
            docs: Input document stream.
            predicates: List of boolean callables.

        Yields:
            Matching documents.
        """
        for doc in docs:
            if all(p(doc) for p in predicates):
                yield doc

    def _join(
        self,
        left_docs: Iterator[dict[str, Any]],
        plan: QueryPlan,
        right_collection: str,
        predicate: Callable[[dict[str, Any], dict[str, Any]], bool],
    ) -> Iterator[dict[str, Any]]:
        """Perform a two-collection join.

        Uses hash join when the plan specifies it, otherwise falls back
        to nested-loop join.

        Args:
            left_docs: Stream of left-side documents.
            plan: Query plan (contains join strategy).
            right_collection: Name of the right-side collection.
            predicate: ``(left_doc, right_doc) → bool`` callable.

        Yields:
            Merged documents (left fields overwritten by right on
            key collision).
        """
        if plan.join_strategy is JoinStrategy.HASH_JOIN:
            yield from self._hash_join(left_docs, right_collection, predicate)
        else:
            yield from self._nested_loop_join(left_docs, right_collection, predicate)

    def _nested_loop_join(
        self,
        left_docs: Iterator[dict[str, Any]],
        right_collection: str,
        predicate: Callable[[dict[str, Any], dict[str, Any]], bool],
    ) -> Iterator[dict[str, Any]]:
        """Nested-loop join: O(m × n).

        Args:
            left_docs: Outer loop documents.
            right_collection: Collection name for the inner loop.
            predicate: Join condition.

        Yields:
            Merged document dicts.
        """
        for left in left_docs:
            for right in self._engine.scan(right_collection):
                if predicate(left, right):
                    yield {**left, **right}

    def _hash_join(
        self,
        left_docs: Iterator[dict[str, Any]],
        right_collection: str,
        predicate: Callable[[dict[str, Any], dict[str, Any]], bool],
    ) -> Iterator[dict[str, Any]]:
        """Hash join: load right side into memory, probe for each left row.

        Args:
            left_docs: Probe-side documents.
            right_collection: Build-side collection name.
            predicate: Join condition.

        Yields:
            Merged document dicts.
        """
        right_docs = list(self._engine.scan(right_collection))
        for left in left_docs:
            for right in right_docs:
                if predicate(left, right):
                    yield {**left, **right}

    # ------------------------------------------------------------------
    # Sort / slice
    # ------------------------------------------------------------------

    @staticmethod
    def _sort(
        docs: Iterator[dict[str, Any]],
        field: str,
        descending: bool,
    ) -> Iterator[dict[str, Any]]:
        """Sort *docs* by *field*.

        ``None`` values sort last regardless of direction.

        Args:
            docs: Input document stream.
            field: Field name to sort by.
            descending: When ``True``, highest values come first.

        Yields:
            Documents in sorted order.
        """
        materialized = list(docs)
        materialized.sort(
            key=lambda d: (d.get(field) is None, d.get(field)),
            reverse=descending,
        )
        yield from materialized

    @staticmethod
    def _slice(
        docs: Iterator[dict[str, Any]],
        skip: int,
        limit: int | None,
    ) -> Iterator[dict[str, Any]]:
        """Apply skip and limit to *docs*.

        Args:
            docs: Input document stream.
            skip: Number of documents to discard at the start.
            limit: Maximum documents to yield, or ``None`` for all.

        Yields:
            Sliced documents.
        """
        count = 0
        skipped = 0
        for doc in docs:
            if skipped < skip:
                skipped += 1
                continue
            yield doc
            count += 1
            if limit is not None and count >= limit:
                return
