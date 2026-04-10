"""Tests for query/planner.py (step 16)."""

from __future__ import annotations

import pytest

from jsonpysql.query.models import JoinStrategy, QueryPlan, ScanType
from jsonpysql.query.planner import (
    QueryPlanner,
    _extract_literal,
    _extract_lambda_source,
    _parse_predicate,
    _try_equality,
    _try_range,
)


# ---------------------------------------------------------------------------
# Helper: build a planner with specific indexed fields
# ---------------------------------------------------------------------------


def planner(
    indexed: set[str] | None = None,
    size: int = 100,
) -> QueryPlanner:
    return QueryPlanner(indexed_fields=indexed or set(), collection_size=size)


# ---------------------------------------------------------------------------
# _extract_lambda_source
# ---------------------------------------------------------------------------


class TestExtractLambdaSource:
    def test_simple_lambda(self) -> None:
        fn = lambda x: x["id"] == "c1"  # noqa: E731
        src = _extract_lambda_source(fn)
        assert src is not None
        assert "lambda" in src

    def test_non_lambda_returns_none(self) -> None:
        def regular(x: int) -> int:
            return x + 1

        src = _extract_lambda_source(regular)
        # Regular functions have no "lambda" keyword
        assert src is None


# ---------------------------------------------------------------------------
# _parse_predicate — INDEX_LOOKUP
# ---------------------------------------------------------------------------


class TestParsePredicateIndexLookup:
    def test_equality_on_indexed_field(self) -> None:
        fn = lambda x: x["email"] == "a@b.com"  # noqa: E731
        scan, field, val, low, high = _parse_predicate(fn, {"email"})
        assert scan is ScanType.INDEX_LOOKUP
        assert field == "email"
        assert val == "a@b.com"

    def test_equality_reversed_operand_order(self) -> None:
        fn = lambda x: "a@b.com" == x["email"]  # noqa: E731
        scan, field, val, low, high = _parse_predicate(fn, {"email"})
        assert scan is ScanType.INDEX_LOOKUP
        assert field == "email"
        assert val == "a@b.com"

    def test_equality_on_non_indexed_field_falls_back(self) -> None:
        fn = lambda x: x["name"] == "Alice"  # noqa: E731
        scan, *_ = _parse_predicate(fn, {"email"})
        assert scan is ScanType.FULL_SCAN

    def test_equality_with_integer_value(self) -> None:
        fn = lambda x: x["age"] == 30  # noqa: E731
        scan, field, val, *_ = _parse_predicate(fn, {"age"})
        assert scan is ScanType.INDEX_LOOKUP
        assert field == "age"
        assert val == 30

    def test_and_predicate_uses_first_clause(self) -> None:
        fn = lambda x: x["email"] == "a@b.com" and x["name"] == "Alice"  # noqa: E731
        scan, field, val, *_ = _parse_predicate(fn, {"email"})
        assert scan is ScanType.INDEX_LOOKUP
        assert field == "email"

    def test_non_subscript_falls_back(self) -> None:
        fn = lambda x: x == "something"  # noqa: E731
        scan, *_ = _parse_predicate(fn, {"email"})
        assert scan is ScanType.FULL_SCAN


# ---------------------------------------------------------------------------
# _parse_predicate — RANGE_SCAN
# ---------------------------------------------------------------------------


class TestParsePredicateRangeScan:
    def test_chained_lte_range(self) -> None:
        fn = lambda x: 10 <= x["age"] <= 30  # noqa: E731
        scan, field, _, low, high = _parse_predicate(fn, {"age"})
        assert scan is ScanType.RANGE_SCAN
        assert field == "age"
        assert low == 10
        assert high == 30

    def test_gte_lower_bound_only(self) -> None:
        fn = lambda x: x["age"] >= 18  # noqa: E731
        scan, field, _, low, high = _parse_predicate(fn, {"age"})
        assert scan is ScanType.RANGE_SCAN
        assert field == "age"
        assert low == 18
        assert high is None

    def test_lte_upper_bound_only(self) -> None:
        fn = lambda x: x["age"] <= 65  # noqa: E731
        scan, field, _, low, high = _parse_predicate(fn, {"age"})
        assert scan is ScanType.RANGE_SCAN
        assert field == "age"
        assert low is None
        assert high == 65

    def test_gt_lower_bound(self) -> None:
        fn = lambda x: x["score"] > 50  # noqa: E731
        scan, field, _, low, high = _parse_predicate(fn, {"score"})
        assert scan is ScanType.RANGE_SCAN
        assert low == 50

    def test_lt_upper_bound(self) -> None:
        fn = lambda x: x["score"] < 100  # noqa: E731
        scan, field, _, low, high = _parse_predicate(fn, {"score"})
        assert scan is ScanType.RANGE_SCAN
        assert high == 100

    def test_range_on_non_indexed_field_falls_back(self) -> None:
        fn = lambda x: 10 <= x["weight"] <= 100  # noqa: E731
        scan, *_ = _parse_predicate(fn, {"age"})
        assert scan is ScanType.FULL_SCAN


# ---------------------------------------------------------------------------
# _parse_predicate — FULL_SCAN fallbacks
# ---------------------------------------------------------------------------


class TestParsePredicateFullScan:
    def test_complex_predicate_falls_back(self) -> None:
        fn = lambda x: x["a"] == 1 or x["b"] == 2  # noqa: E731
        scan, *_ = _parse_predicate(fn, {"a", "b"})
        assert scan is ScanType.FULL_SCAN

    def test_empty_indexed_set_falls_back(self) -> None:
        fn = lambda x: x["email"] == "a@b.com"  # noqa: E731
        scan, *_ = _parse_predicate(fn, set())
        assert scan is ScanType.FULL_SCAN

    def test_negative_literal(self) -> None:
        fn = lambda x: x["offset"] == -5  # noqa: E731
        scan, field, val, *_ = _parse_predicate(fn, {"offset"})
        assert scan is ScanType.INDEX_LOOKUP
        assert val == -5


# ---------------------------------------------------------------------------
# QueryPlanner.plan — scan type
# ---------------------------------------------------------------------------


class TestQueryPlannerScanType:
    def test_no_predicates_yields_full_scan(self) -> None:
        p = planner({"email"})
        plan = p.plan("customers", [])
        assert plan.scan_type is ScanType.FULL_SCAN
        assert plan.collection == "customers"

    def test_indexed_equality_yields_index_lookup(self) -> None:
        p = planner({"email"})
        plan = p.plan("customers", [lambda x: x["email"] == "a@b.com"])
        assert plan.scan_type is ScanType.INDEX_LOOKUP
        assert plan.index_field == "email"
        assert plan.index_value == "a@b.com"

    def test_range_predicate_yields_range_scan(self) -> None:
        p = planner({"age"})
        plan = p.plan("customers", [lambda x: x["age"] >= 18])
        assert plan.scan_type is ScanType.RANGE_SCAN
        assert plan.index_low == 18

    def test_non_indexed_predicate_yields_full_scan(self) -> None:
        p = planner({"email"})
        plan = p.plan("customers", [lambda x: x["name"] == "Alice"])
        assert plan.scan_type is ScanType.FULL_SCAN

    def test_predicate_count_tracked(self) -> None:
        p = planner({"email"})
        plan = p.plan(
            "customers",
            [
                lambda x: x["email"] == "a@b.com",
                lambda x: x["name"] == "Alice",
            ],
        )
        assert plan.predicate_count == 2


# ---------------------------------------------------------------------------
# QueryPlanner.plan — join strategy
# ---------------------------------------------------------------------------


class TestQueryPlannerJoinStrategy:
    def test_no_join_yields_no_strategy(self) -> None:
        p = planner()
        plan = p.plan("customers", [])
        assert plan.join_strategy is None
        assert plan.join_collection is None

    def test_small_right_side_yields_hash_join(self) -> None:
        p = planner(size=100, indexed=set())
        plan = p.plan(
            "customers",
            [],
            join_collection="orders",
            join_predicate=None,
            join_collection_size=50,
        )
        assert plan.join_strategy is JoinStrategy.HASH_JOIN
        assert plan.join_collection == "orders"

    def test_large_collections_yield_nested_loop(self) -> None:
        p = QueryPlanner(indexed_fields=set(), collection_size=20_000, hash_join_threshold=10_000)
        plan = p.plan(
            "customers",
            [],
            join_collection="orders",
            join_collection_size=15_000,
        )
        assert plan.join_strategy is JoinStrategy.NESTED_LOOP

    def test_hash_join_threshold_boundary(self) -> None:
        p = QueryPlanner(indexed_fields=set(), collection_size=10_000, hash_join_threshold=10_000)
        plan = p.plan("a", [], join_collection="b", join_collection_size=10_000)
        assert plan.join_strategy is JoinStrategy.HASH_JOIN


# ---------------------------------------------------------------------------
# QueryPlanner.plan — metadata flags
# ---------------------------------------------------------------------------


class TestQueryPlannerFlags:
    def test_all_flags_false_by_default(self) -> None:
        p = planner()
        plan = p.plan("customers", [])
        assert plan.has_order_by is False
        assert plan.has_limit is False
        assert plan.has_projection is False
        assert plan.has_aggregation is False

    def test_flags_propagated(self) -> None:
        p = planner()
        plan = p.plan(
            "customers",
            [],
            has_order_by=True,
            has_limit=True,
            has_projection=True,
            has_aggregation=True,
        )
        assert plan.has_order_by is True
        assert plan.has_limit is True
        assert plan.has_projection is True
        assert plan.has_aggregation is True


# ---------------------------------------------------------------------------
# QueryPlan.describe
# ---------------------------------------------------------------------------


class TestQueryPlanDescribe:
    def test_describe_full_scan(self) -> None:
        plan = QueryPlan(collection="customers", scan_type=ScanType.FULL_SCAN)
        desc = plan.describe()
        assert "full_scan" in desc
        assert "customers" in desc

    def test_describe_index_lookup(self) -> None:
        plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.INDEX_LOOKUP,
            index_field="email",
            index_value="a@b.com",
        )
        desc = plan.describe()
        assert "index_lookup" in desc
        assert "email" in desc
        assert "a@b.com" in desc

    def test_describe_range_scan(self) -> None:
        plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.RANGE_SCAN,
            index_field="age",
            index_low=10,
            index_high=30,
        )
        desc = plan.describe()
        assert "range_scan" in desc
        assert "10" in desc
        assert "30" in desc

    def test_describe_with_join(self) -> None:
        plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.FULL_SCAN,
            join_strategy=JoinStrategy.HASH_JOIN,
            join_collection="orders",
        )
        desc = plan.describe()
        assert "hash_join" in desc
        assert "orders" in desc

    def test_describe_with_flags(self) -> None:
        plan = QueryPlan(
            collection="customers",
            scan_type=ScanType.FULL_SCAN,
            has_order_by=True,
            has_limit=True,
        )
        desc = plan.describe()
        assert "order_by" in desc
        assert "limit" in desc
