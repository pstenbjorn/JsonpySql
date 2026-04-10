"""Aggregation engine for the query layer.

``apply_aggregates`` groups a stream of documents by one or more key
fields and applies ``AggSpec`` aggregators to produce a result row per
group.  The output is a list of ``dict`` records where each record
contains the group-by key fields plus one key per named aggregate.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterator

from jsonpysql.query.models import AggSpec, AvgAgg, CountAgg, MaxAgg, MinAgg, SumAgg


def _group_key(document: dict[str, Any], group_fields: tuple[str, ...]) -> tuple[Any, ...]:
    """Return the group key for *document*.

    Args:
        document: A single document dict.
        group_fields: Ordered tuple of field names to group by.

    Returns:
        Tuple of field values in *group_fields* order.
    """
    return tuple(document.get(f) for f in group_fields)


def _collect_field_values(
    groups: dict[tuple[Any, ...], list[dict[str, Any]]],
    field: str,
) -> dict[tuple[Any, ...], list[Any]]:
    """Build a mapping from group key → list of *field* values.

    Args:
        groups: Mapping from group key → list of documents.
        field: Field name to extract from each document.

    Returns:
        Dict mapping each group key to its list of field values.
    """
    return {key: [doc.get(field) for doc in docs] for key, docs in groups.items()}


def apply_aggregates(
    documents: Iterator[dict[str, Any]],
    group_fields: tuple[str, ...],
    aggregates: dict[str, AggSpec],
) -> list[dict[str, Any]]:
    """Group *documents* and apply named aggregates.

    Args:
        documents: Iterable of document dicts (each includes ``_id``).
        group_fields: Ordered tuple of field names to group by.  When
            empty, all documents form a single group.
        aggregates: Mapping from output key name → ``AggSpec`` instance.
            Supported specs: ``CountAgg``, ``SumAgg``, ``AvgAgg``,
            ``MinAgg``, ``MaxAgg``.

    Returns:
        List of result dicts, one per group.  Each dict contains the
        group-by field values and one entry per named aggregate.

    Example::

        results = apply_aggregates(
            docs,
            group_fields=("country",),
            aggregates={
                "n": CountAgg(),
                "total_spend": SumAgg("amount"),
                "avg_spend": AvgAgg("amount"),
            },
        )
    """
    # Accumulate documents per group
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for doc in documents:
        key = _group_key(doc, group_fields)
        groups[key].append(doc)

    results: list[dict[str, Any]] = []

    for key, docs in groups.items():
        row: dict[str, Any] = {}
        # Re-populate group-by fields
        for field_name, value in zip(group_fields, key):
            row[field_name] = value

        # Apply each aggregate spec
        for agg_name, spec in aggregates.items():
            if isinstance(spec, CountAgg):
                # CountAgg operates on all documents in the group
                row[agg_name] = spec.compute(docs)  # type: ignore[arg-type]
            elif isinstance(spec, (SumAgg, AvgAgg, MinAgg, MaxAgg)):
                values = [doc.get(spec.field) for doc in docs]
                row[agg_name] = spec.compute(values)
            else:
                # Unknown spec — skip silently
                pass

        results.append(row)

    return results
