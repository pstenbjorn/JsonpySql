"""Query-layer data models.

Defines the plain-data types used by every other query sub-module:
``ScanType``, ``JoinStrategy``, ``QueryPlan``, and the aggregate
specification family (``CountAgg``, ``SumAgg``, ``AvgAgg``,
``MinAgg``, ``MaxAgg``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ScanType(str, Enum):
    """Describes how the primary collection will be scanned."""

    INDEX_LOOKUP = "index_lookup"
    """Equality predicate on an indexed field — O(log n)."""

    RANGE_SCAN = "range_scan"
    """Range predicate on an indexed field — O(log n + k)."""

    FULL_SCAN = "full_scan"
    """No usable index — linear scan over every document."""


class JoinStrategy(str, Enum):
    """Algorithm chosen by the planner for a two-collection join."""

    NESTED_LOOP = "nested_loop"
    """For each row in the outer table, scan the inner table linearly."""

    HASH_JOIN = "hash_join"
    """Build an in-memory hash table from the smaller collection."""


# ---------------------------------------------------------------------------
# QueryPlan
# ---------------------------------------------------------------------------


@dataclass
class QueryPlan:
    """Immutable snapshot of how the executor will run a query.

    Returned by ``QueryBuilder.explain()`` for inspection and debugging.

    Args:
        collection: Primary collection name.
        scan_type: How the primary collection will be accessed.
        index_field: Field name used for index access, or ``None``.
        index_value: Equality value for ``INDEX_LOOKUP`` scans.
        index_low: Lower bound for ``RANGE_SCAN`` scans.
        index_high: Upper bound for ``RANGE_SCAN`` scans.
        predicate_count: Number of chained ``.where()`` predicates.
        join_strategy: Join algorithm, or ``None`` if no join.
        join_collection: Right-side collection name for a join.
        has_order_by: Whether an ``order_by`` clause is present.
        has_limit: Whether a ``limit`` clause is present.
        has_projection: Whether a ``select`` projection is present.
        has_aggregation: Whether ``group_by`` / ``aggregate`` is present.
    """

    collection: str
    scan_type: ScanType = ScanType.FULL_SCAN
    index_field: str | None = None
    index_value: Any = None
    index_low: Any = None
    index_high: Any = None
    predicate_count: int = 0
    join_strategy: JoinStrategy | None = None
    join_collection: str | None = None
    has_order_by: bool = False
    has_limit: bool = False
    has_projection: bool = False
    has_aggregation: bool = False

    def describe(self) -> str:
        """Return a human-readable one-line summary of this plan.

        Returns:
            Plain-text description suitable for logging or REPL output.
        """
        parts = [f"collection={self.collection!r}", f"scan={self.scan_type.value}"]
        if self.index_field is not None:
            parts.append(f"index_field={self.index_field!r}")
        if self.scan_type is ScanType.INDEX_LOOKUP and self.index_value is not None:
            parts.append(f"value={self.index_value!r}")
        if self.scan_type is ScanType.RANGE_SCAN:
            parts.append(f"low={self.index_low!r} high={self.index_high!r}")
        if self.predicate_count:
            parts.append(f"predicates={self.predicate_count}")
        if self.join_strategy is not None:
            parts.append(f"join={self.join_strategy.value}({self.join_collection!r})")
        flags = []
        if self.has_order_by:
            flags.append("order_by")
        if self.has_limit:
            flags.append("limit")
        if self.has_projection:
            flags.append("projection")
        if self.has_aggregation:
            flags.append("aggregation")
        if flags:
            parts.append("+".join(flags))
        return "QueryPlan(" + ", ".join(parts) + ")"


# ---------------------------------------------------------------------------
# Aggregate specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CountAgg:
    """Count of documents in each group.

    Equivalent to SQL ``COUNT(*)``.
    """

    def compute(self, values: list[Any]) -> int:
        """Return the count of *values*.

        Args:
            values: List of field values (may include ``None``).

        Returns:
            Number of elements.
        """
        return len(values)


@dataclass(frozen=True)
class SumAgg:
    """Sum of a numeric field within each group.

    Equivalent to SQL ``SUM(field)``.

    Args:
        field: Name of the field to sum.
    """

    field: str

    def compute(self, values: list[Any]) -> float | int:
        """Return the sum of numeric *values*, skipping ``None``.

        Args:
            values: List of field values.

        Returns:
            Numeric sum.
        """
        return sum(v for v in values if v is not None)


@dataclass(frozen=True)
class AvgAgg:
    """Arithmetic mean of a numeric field within each group.

    Equivalent to SQL ``AVG(field)``.

    Args:
        field: Name of the field to average.
    """

    field: str

    def compute(self, values: list[Any]) -> float | None:
        """Return the mean of numeric *values*, skipping ``None``.

        Args:
            values: List of field values.

        Returns:
            Mean as ``float``, or ``None`` if all values are ``None``.
        """
        non_null = [v for v in values if v is not None]
        if not non_null:
            return None
        return sum(non_null) / len(non_null)


@dataclass(frozen=True)
class MinAgg:
    """Minimum of a field within each group.

    Equivalent to SQL ``MIN(field)``.

    Args:
        field: Name of the field to find the minimum of.
    """

    field: str

    def compute(self, values: list[Any]) -> Any:
        """Return the minimum of *values*, skipping ``None``.

        Args:
            values: List of field values.

        Returns:
            Minimum value, or ``None`` if all values are ``None``.
        """
        non_null = [v for v in values if v is not None]
        return min(non_null) if non_null else None


@dataclass(frozen=True)
class MaxAgg:
    """Maximum of a field within each group.

    Equivalent to SQL ``MAX(field)``.

    Args:
        field: Name of the field to find the maximum of.
    """

    field: str

    def compute(self, values: list[Any]) -> Any:
        """Return the maximum of *values*, skipping ``None``.

        Args:
            values: List of field values.

        Returns:
            Maximum value, or ``None`` if all values are ``None``.
        """
        non_null = [v for v in values if v is not None]
        return max(non_null) if non_null else None


# Convenient type alias used by the query builder and executor.
AggSpec = CountAgg | SumAgg | AvgAgg | MinAgg | MaxAgg
