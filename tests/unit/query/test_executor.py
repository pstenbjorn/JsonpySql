"""Tests for query/executor.py (step 18)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from jsonpysql.query.executor import QueryExecutor
from jsonpysql.query.models import (
    AvgAgg,
    CountAgg,
    JoinStrategy,
    MaxAgg,
    MinAgg,
    QueryPlan,
    ScanType,
    SumAgg,
)
from jsonpysql.schema.fields import field as schema_field
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import IndexSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class Customer(BaseModel):
    id: str = schema_field(primary_key=True)
    email: str = schema_field(unique=True, index=True)
    name: str = schema_field()
    age: int = schema_field(index=True)


@pytest.fixture
def engine(tmp_path: Path) -> StorageEngine:
    eng = StorageEngine(tmp_path)
    eng.create_collection(
        "customers",
        [IndexSpec(["email"]), IndexSpec(["age"])],
    )
    eng.create_collection("orders", [])
    for i, (name, email, age) in enumerate(
        [
            ("Alice", "alice@ex.com", 30),
            ("Bob", "bob@ex.com", 25),
            ("Charlie", "charlie@ex.com", 30),
        ],
        start=1,
    ):
        eng.insert("customers", f"c{i}", {"id": f"c{i}", "name": name, "email": email, "age": age})

    eng.insert("orders", "o1", {"id": "o1", "customer_id": "c1", "total": 100.0})
    eng.insert("orders", "o2", {"id": "o2", "customer_id": "c1", "total": 50.0})
    eng.insert("orders", "o3", {"id": "o3", "customer_id": "c2", "total": 200.0})
    return eng


@pytest.fixture
def executor(engine: StorageEngine) -> QueryExecutor:
    return QueryExecutor(engine)


def full_scan_plan(collection: str = "customers") -> QueryPlan:
    return QueryPlan(collection=collection, scan_type=ScanType.FULL_SCAN)


def index_lookup_plan(field: str, value: Any, collection: str = "customers") -> QueryPlan:
    return QueryPlan(
        collection=collection,
        scan_type=ScanType.INDEX_LOOKUP,
        index_field=field,
        index_value=value,
    )


def range_scan_plan(
    field: str, low: Any = None, high: Any = None, collection: str = "customers"
) -> QueryPlan:
    return QueryPlan(
        collection=collection,
        scan_type=ScanType.RANGE_SCAN,
        index_field=field,
        index_low=low,
        index_high=high,
    )


# ---------------------------------------------------------------------------
# Full scan
# ---------------------------------------------------------------------------


class TestFullScan:
    def test_returns_all_documents(self, executor: QueryExecutor) -> None:
        results = list(executor.execute(full_scan_plan(), []))
        assert len(results) == 3

    def test_results_contain_id(self, executor: QueryExecutor) -> None:
        results = list(executor.execute(full_scan_plan(), []))
        assert all("_id" in d for d in results)


# ---------------------------------------------------------------------------
# Index lookup
# ---------------------------------------------------------------------------


class TestIndexLookup:
    def test_equality_on_indexed_field(self, executor: QueryExecutor) -> None:
        plan = index_lookup_plan("email", "alice@ex.com")
        results = list(executor.execute(plan, []))
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    def test_no_match_returns_empty(self, executor: QueryExecutor) -> None:
        plan = index_lookup_plan("email", "ghost@ex.com")
        results = list(executor.execute(plan, []))
        assert results == []


# ---------------------------------------------------------------------------
# Range scan
# ---------------------------------------------------------------------------


class TestRangeScan:
    def test_range_on_numeric_field(self, executor: QueryExecutor) -> None:
        plan = range_scan_plan("age", low=30, high=30)
        results = list(executor.execute(plan, []))
        names = {r["name"] for r in results}
        assert names == {"Alice", "Charlie"}

    def test_open_low_bound(self, executor: QueryExecutor) -> None:
        # low=None → engine uses empty string; result depends on index type
        plan = range_scan_plan("age", low=None, high=25)
        results = list(executor.execute(plan, []))
        # Only Bob (age=25) should match
        assert any(r["name"] == "Bob" for r in results)


# ---------------------------------------------------------------------------
# Predicates (where filters)
# ---------------------------------------------------------------------------


class TestPredicates:
    def test_single_predicate(self, executor: QueryExecutor) -> None:
        results = list(
            executor.execute(full_scan_plan(), [lambda d: d["name"] == "Alice"])
        )
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    def test_multiple_predicates_and_semantics(self, executor: QueryExecutor) -> None:
        results = list(
            executor.execute(
                full_scan_plan(),
                [
                    lambda d: d["age"] == 30,
                    lambda d: d["name"] == "Alice",
                ],
            )
        )
        assert len(results) == 1

    def test_no_match(self, executor: QueryExecutor) -> None:
        results = list(
            executor.execute(full_scan_plan(), [lambda d: d["name"] == "Nobody"])
        )
        assert results == []


# ---------------------------------------------------------------------------
# Order by
# ---------------------------------------------------------------------------


class TestOrderBy:
    def test_order_ascending(self, executor: QueryExecutor) -> None:
        results = list(executor.execute(full_scan_plan(), [], order_by_field="name"))
        names = [r["name"] for r in results]
        assert names == sorted(names)

    def test_order_descending(self, executor: QueryExecutor) -> None:
        results = list(
            executor.execute(
                full_scan_plan(), [], order_by_field="name", order_descending=True
            )
        )
        names = [r["name"] for r in results]
        assert names == sorted(names, reverse=True)

    def test_none_values_sort_last(self, engine: StorageEngine) -> None:
        engine.insert("customers", "c99", {"id": "c99", "name": None, "email": "x@x.com", "age": 0})
        ex = QueryExecutor(engine)
        results = list(ex.execute(full_scan_plan(), [], order_by_field="name"))
        assert results[-1]["name"] is None


# ---------------------------------------------------------------------------
# Skip and limit
# ---------------------------------------------------------------------------


class TestSkipLimit:
    def test_limit_restricts_results(self, executor: QueryExecutor) -> None:
        results = list(executor.execute(full_scan_plan(), [], limit=2))
        assert len(results) == 2

    def test_skip_offsets_start(self, executor: QueryExecutor) -> None:
        all_results = list(executor.execute(full_scan_plan(), [], order_by_field="name"))
        skipped = list(
            executor.execute(full_scan_plan(), [], order_by_field="name", skip=1)
        )
        assert skipped == all_results[1:]

    def test_skip_and_limit_combined(self, executor: QueryExecutor) -> None:
        results = list(
            executor.execute(full_scan_plan(), [], order_by_field="name", skip=1, limit=1)
        )
        assert len(results) == 1

    def test_skip_beyond_results(self, executor: QueryExecutor) -> None:
        results = list(executor.execute(full_scan_plan(), [], skip=100))
        assert results == []


# ---------------------------------------------------------------------------
# Projection (select)
# ---------------------------------------------------------------------------


class TestProjection:
    def test_projection_applied(self, executor: QueryExecutor) -> None:
        results = list(
            executor.execute(
                full_scan_plan(), [], projection=lambda d: {"name": d["name"]}
            )
        )
        assert all(set(r.keys()) == {"name"} for r in results)


# ---------------------------------------------------------------------------
# Join — nested loop
# ---------------------------------------------------------------------------


class TestNestedLoopJoin:
    def test_nested_loop_join(self, executor: QueryExecutor) -> None:
        plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.FULL_SCAN,
            join_strategy=JoinStrategy.NESTED_LOOP,
            join_collection="orders",
        )
        results = list(
            executor.execute(
                plan,
                [],
                join_collection="orders",
                join_predicate=lambda c, o: c["_id"] == o["customer_id"],
            )
        )
        # Alice (c1) has 2 orders, Bob (c2) has 1 order, Charlie (c3) has none
        assert len(results) == 3

    def test_nested_loop_no_matches(self, executor: QueryExecutor) -> None:
        plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.FULL_SCAN,
            join_strategy=JoinStrategy.NESTED_LOOP,
            join_collection="orders",
        )
        results = list(
            executor.execute(
                plan,
                [],
                join_collection="orders",
                join_predicate=lambda c, o: False,
            )
        )
        assert results == []


# ---------------------------------------------------------------------------
# Join — hash join
# ---------------------------------------------------------------------------


class TestHashJoin:
    def test_hash_join_same_result_as_nested_loop(self, executor: QueryExecutor) -> None:
        predicate = lambda c, o: c["_id"] == o["customer_id"]  # noqa: E731

        nl_plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.FULL_SCAN,
            join_strategy=JoinStrategy.NESTED_LOOP,
            join_collection="orders",
        )
        hj_plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.FULL_SCAN,
            join_strategy=JoinStrategy.HASH_JOIN,
            join_collection="orders",
        )
        nl = sorted(
            executor.execute(nl_plan, [], join_collection="orders", join_predicate=predicate),
            key=lambda d: d.get("id", ""),
        )
        hj = sorted(
            executor.execute(hj_plan, [], join_collection="orders", join_predicate=predicate),
            key=lambda d: d.get("id", ""),
        )
        assert nl == hj


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_count_with_group_by(self, executor: QueryExecutor) -> None:
        plan = QueryPlan(
            collection="customers", scan_type=ScanType.FULL_SCAN, has_aggregation=True
        )
        results = list(
            executor.execute(
                plan,
                [],
                group_fields=("age",),
                aggregates={"n": CountAgg()},
            )
        )
        by_age = {r["age"]: r["n"] for r in results}
        assert by_age[30] == 2  # Alice + Charlie
        assert by_age[25] == 1  # Bob

    def test_sum_aggregate(self, executor: QueryExecutor) -> None:
        plan = QueryPlan(
            collection="orders", scan_type=ScanType.FULL_SCAN, has_aggregation=True
        )
        results = list(
            executor.execute(
                plan,
                [],
                group_fields=(),
                aggregates={"total": SumAgg("total")},
            )
        )
        assert results[0]["total"] == pytest.approx(350.0)

    def test_aggregation_with_order_by(self, executor: QueryExecutor) -> None:
        plan = QueryPlan(
            collection="customers", scan_type=ScanType.FULL_SCAN, has_aggregation=True
        )
        results = list(
            executor.execute(
                plan,
                [],
                group_fields=("age",),
                aggregates={"n": CountAgg()},
                order_by_field="age",
            )
        )
        ages = [r["age"] for r in results]
        assert ages == sorted(ages)

    def test_aggregation_with_limit(self, executor: QueryExecutor) -> None:
        plan = QueryPlan(
            collection="customers", scan_type=ScanType.FULL_SCAN, has_aggregation=True
        )
        results = list(
            executor.execute(
                plan,
                [],
                group_fields=("age",),
                aggregates={"n": CountAgg()},
                limit=1,
            )
        )
        assert len(results) == 1
