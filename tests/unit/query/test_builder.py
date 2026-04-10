"""Tests for query/builder.py (step 19)."""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonpysql.query.builder import QueryBuilder
from jsonpysql.query.models import CountAgg, JoinStrategy, ScanType, SumAgg
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import IndexSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> StorageEngine:
    eng = StorageEngine(tmp_path)
    eng.create_collection(
        "customers",
        [IndexSpec(["email"]), IndexSpec(["age"])],
    )
    eng.create_collection("orders", [])
    for cid, name, email, age in [
        ("c1", "Alice", "alice@ex.com", 30),
        ("c2", "Bob", "bob@ex.com", 25),
        ("c3", "Charlie", "charlie@ex.com", 30),
    ]:
        eng.insert(
            "customers",
            cid,
            {"id": cid, "name": name, "email": email, "age": age},
        )
    eng.insert("orders", "o1", {"id": "o1", "customer_id": "c1", "total": 100.0})
    eng.insert("orders", "o2", {"id": "o2", "customer_id": "c1", "total": 50.0})
    eng.insert("orders", "o3", {"id": "o3", "customer_id": "c2", "total": 200.0})
    return eng


def builder(engine: StorageEngine, collection: str = "customers") -> QueryBuilder:
    indexed = {"email", "age"} if collection == "customers" else set()
    return QueryBuilder(collection, engine, indexed_fields=indexed, collection_size=3)


# ---------------------------------------------------------------------------
# Immutability — each clause returns a new builder
# ---------------------------------------------------------------------------


class TestImmutability:
    def test_where_returns_new_instance(self, engine: StorageEngine) -> None:
        b = builder(engine)
        b2 = b.where(lambda d: True)
        assert b is not b2

    def test_chaining_does_not_mutate_original(self, engine: StorageEngine) -> None:
        b = builder(engine)
        b.where(lambda d: d["name"] == "Alice")
        assert len(b._predicates) == 0


# ---------------------------------------------------------------------------
# where / filter
# ---------------------------------------------------------------------------


class TestWhere:
    def test_single_where(self, engine: StorageEngine) -> None:
        results = builder(engine).where(lambda d: d["name"] == "Alice").to_list()
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    def test_chained_where_and_semantics(self, engine: StorageEngine) -> None:
        results = (
            builder(engine)
            .where(lambda d: d["age"] == 30)
            .where(lambda d: d["name"] == "Alice")
            .to_list()
        )
        assert len(results) == 1

    def test_where_no_match(self, engine: StorageEngine) -> None:
        results = builder(engine).where(lambda d: d["name"] == "Ghost").to_list()
        assert results == []


# ---------------------------------------------------------------------------
# order_by
# ---------------------------------------------------------------------------


class TestOrderBy:
    def test_ascending(self, engine: StorageEngine) -> None:
        results = builder(engine).order_by("name").to_list()
        names = [r["name"] for r in results]
        assert names == sorted(names)

    def test_descending(self, engine: StorageEngine) -> None:
        results = builder(engine).order_by("name", descending=True).to_list()
        names = [r["name"] for r in results]
        assert names == sorted(names, reverse=True)


# ---------------------------------------------------------------------------
# limit / skip
# ---------------------------------------------------------------------------


class TestLimitSkip:
    def test_limit(self, engine: StorageEngine) -> None:
        results = builder(engine).limit(1).to_list()
        assert len(results) == 1

    def test_skip(self, engine: StorageEngine) -> None:
        all_names = sorted(r["name"] for r in builder(engine).order_by("name").to_list())
        skipped = [
            r["name"] for r in builder(engine).order_by("name").skip(1).to_list()
        ]
        assert skipped == all_names[1:]

    def test_skip_and_limit(self, engine: StorageEngine) -> None:
        results = builder(engine).order_by("name").skip(1).limit(1).to_list()
        assert len(results) == 1
        assert results[0]["name"] == "Bob"


# ---------------------------------------------------------------------------
# select / projection
# ---------------------------------------------------------------------------


class TestSelect:
    def test_projection_keeps_only_selected_fields(self, engine: StorageEngine) -> None:
        results = builder(engine).select(lambda d: {"name": d["name"]}).to_list()
        assert all(set(r.keys()) == {"name"} for r in results)


# ---------------------------------------------------------------------------
# join
# ---------------------------------------------------------------------------


class TestJoin:
    def test_join_two_collections(self, engine: StorageEngine) -> None:
        results = (
            builder(engine)
            .join("orders", on=lambda c, o: c["_id"] == o["customer_id"])
            .to_list()
        )
        # Alice has 2 orders, Bob has 1 — Charlie has none
        assert len(results) == 3

    def test_join_with_filter(self, engine: StorageEngine) -> None:
        results = (
            builder(engine)
            .where(lambda d: d["name"] == "Alice")
            .join("orders", on=lambda c, o: c["_id"] == o["customer_id"])
            .to_list()
        )
        assert len(results) == 2
        assert all(r["customer_id"] == "c1" for r in results)


# ---------------------------------------------------------------------------
# group_by / aggregate
# ---------------------------------------------------------------------------


class TestGroupByAggregate:
    def test_count_by_age(self, engine: StorageEngine) -> None:
        results = (
            builder(engine)
            .group_by("age")
            .aggregate(n=CountAgg())
            .to_list()
        )
        by_age = {r["age"]: r["n"] for r in results}
        assert by_age[30] == 2
        assert by_age[25] == 1

    def test_sum_orders(self, engine: StorageEngine) -> None:
        results = (
            QueryBuilder("orders", engine, collection_size=3)
            .group_by("customer_id")
            .aggregate(total=SumAgg("total"))
            .to_list()
        )
        by_cust = {r["customer_id"]: r["total"] for r in results}
        assert by_cust["c1"] == pytest.approx(150.0)
        assert by_cust["c2"] == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Materialization methods
# ---------------------------------------------------------------------------


class TestMaterialization:
    def test_iter(self, engine: StorageEngine) -> None:
        results = list(builder(engine))
        assert len(results) == 3

    def test_to_list(self, engine: StorageEngine) -> None:
        results = builder(engine).to_list()
        assert isinstance(results, list)
        assert len(results) == 3

    def test_to_dict(self, engine: StorageEngine) -> None:
        d = builder(engine).to_dict("id")
        assert "c1" in d
        assert d["c1"]["name"] == "Alice"

    def test_first(self, engine: StorageEngine) -> None:
        result = builder(engine).order_by("name").first()
        assert result is not None
        assert result["name"] == "Alice"

    def test_first_empty(self, engine: StorageEngine) -> None:
        result = builder(engine).where(lambda d: d["name"] == "Ghost").first()
        assert result is None

    def test_count(self, engine: StorageEngine) -> None:
        assert builder(engine).count() == 3

    def test_count_with_filter(self, engine: StorageEngine) -> None:
        assert builder(engine).where(lambda d: d["age"] == 30).count() == 2


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class TestExplain:
    def test_explain_returns_query_plan(self, engine: StorageEngine) -> None:
        from jsonpysql.query.models import QueryPlan

        plan = builder(engine).where(lambda d: d["email"] == "alice@ex.com").explain()
        assert isinstance(plan, QueryPlan)

    def test_explain_reflects_scan_type(self, engine: StorageEngine) -> None:
        plan = builder(engine).where(lambda d: d["email"] == "alice@ex.com").explain()
        assert plan.scan_type is ScanType.INDEX_LOOKUP

    def test_explain_full_scan_for_unindexed(self, engine: StorageEngine) -> None:
        plan = builder(engine).where(lambda d: d["name"] == "Alice").explain()
        assert plan.scan_type is ScanType.FULL_SCAN

    def test_explain_with_join(self, engine: StorageEngine) -> None:
        plan = (
            builder(engine)
            .join("orders", on=lambda c, o: c["_id"] == o["customer_id"])
            .explain()
        )
        assert plan.join_collection == "orders"
        assert plan.join_strategy is not None

    def test_explain_flags(self, engine: StorageEngine) -> None:
        plan = (
            builder(engine)
            .order_by("name")
            .limit(5)
            .select(lambda d: d)
            .group_by("age")
            .aggregate(n=CountAgg())
            .explain()
        )
        assert plan.has_order_by is True
        assert plan.has_limit is True
        assert plan.has_projection is True
        assert plan.has_aggregation is True
