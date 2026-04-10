"""Tests for query/aggregates.py (step 17)."""

from __future__ import annotations

import pytest

from jsonpysql.query.aggregates import apply_aggregates
from jsonpysql.query.models import AvgAgg, CountAgg, MaxAgg, MinAgg, SumAgg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_docs(*rows: dict) -> list[dict]:
    """Wrap raw dicts as document dicts (with synthetic _id)."""
    return [{"_id": str(i), **row} for i, row in enumerate(rows)]


# ---------------------------------------------------------------------------
# CountAgg
# ---------------------------------------------------------------------------


class TestCountAgg:
    def test_count_all_documents(self) -> None:
        docs = make_docs({"country": "NO"}, {"country": "NO"}, {"country": "SE"})
        results = apply_aggregates(iter(docs), ("country",), {"n": CountAgg()})
        by_country = {r["country"]: r["n"] for r in results}
        assert by_country["NO"] == 2
        assert by_country["SE"] == 1

    def test_count_single_group_no_group_fields(self) -> None:
        docs = make_docs({"v": 1}, {"v": 2}, {"v": 3})
        results = apply_aggregates(iter(docs), (), {"n": CountAgg()})
        assert len(results) == 1
        assert results[0]["n"] == 3

    def test_count_empty_stream(self) -> None:
        results = apply_aggregates(iter([]), (), {"n": CountAgg()})
        assert results == []

    def test_count_agg_compute_directly(self) -> None:
        agg = CountAgg()
        assert agg.compute([{"_id": "1"}, {"_id": "2"}]) == 2


# ---------------------------------------------------------------------------
# SumAgg
# ---------------------------------------------------------------------------


class TestSumAgg:
    def test_sum_numeric_field(self) -> None:
        docs = make_docs({"cat": "A", "v": 10}, {"cat": "A", "v": 20}, {"cat": "B", "v": 5})
        results = apply_aggregates(iter(docs), ("cat",), {"total": SumAgg("v")})
        by_cat = {r["cat"]: r["total"] for r in results}
        assert by_cat["A"] == 30
        assert by_cat["B"] == 5

    def test_sum_skips_none(self) -> None:
        docs = make_docs({"v": 10}, {"v": None}, {"v": 20})
        results = apply_aggregates(iter(docs), (), {"total": SumAgg("v")})
        assert results[0]["total"] == 30

    def test_sum_all_none(self) -> None:
        docs = make_docs({"v": None}, {"v": None})
        results = apply_aggregates(iter(docs), (), {"total": SumAgg("v")})
        assert results[0]["total"] == 0

    def test_sum_agg_compute_directly(self) -> None:
        agg = SumAgg("v")
        assert agg.compute([1, 2, None, 4]) == 7


# ---------------------------------------------------------------------------
# AvgAgg
# ---------------------------------------------------------------------------


class TestAvgAgg:
    def test_avg_numeric_field(self) -> None:
        docs = make_docs({"v": 10}, {"v": 20}, {"v": 30})
        results = apply_aggregates(iter(docs), (), {"avg": AvgAgg("v")})
        assert results[0]["avg"] == pytest.approx(20.0)

    def test_avg_skips_none(self) -> None:
        docs = make_docs({"v": 10}, {"v": None}, {"v": 20})
        results = apply_aggregates(iter(docs), (), {"avg": AvgAgg("v")})
        assert results[0]["avg"] == pytest.approx(15.0)

    def test_avg_all_none_returns_none(self) -> None:
        docs = make_docs({"v": None})
        results = apply_aggregates(iter(docs), (), {"avg": AvgAgg("v")})
        assert results[0]["avg"] is None

    def test_avg_agg_compute_directly(self) -> None:
        agg = AvgAgg("v")
        assert agg.compute([10, 20, 30]) == pytest.approx(20.0)


# ---------------------------------------------------------------------------
# MinAgg
# ---------------------------------------------------------------------------


class TestMinAgg:
    def test_min_numeric_field(self) -> None:
        docs = make_docs({"v": 30}, {"v": 10}, {"v": 20})
        results = apply_aggregates(iter(docs), (), {"m": MinAgg("v")})
        assert results[0]["m"] == 10

    def test_min_string_field(self) -> None:
        docs = make_docs({"name": "Charlie"}, {"name": "Alice"}, {"name": "Bob"})
        results = apply_aggregates(iter(docs), (), {"m": MinAgg("name")})
        assert results[0]["m"] == "Alice"

    def test_min_all_none_returns_none(self) -> None:
        docs = make_docs({"v": None})
        results = apply_aggregates(iter(docs), (), {"m": MinAgg("v")})
        assert results[0]["m"] is None

    def test_min_agg_compute_directly(self) -> None:
        agg = MinAgg("v")
        assert agg.compute([5, 1, None, 3]) == 1


# ---------------------------------------------------------------------------
# MaxAgg
# ---------------------------------------------------------------------------


class TestMaxAgg:
    def test_max_numeric_field(self) -> None:
        docs = make_docs({"v": 10}, {"v": 30}, {"v": 20})
        results = apply_aggregates(iter(docs), (), {"m": MaxAgg("v")})
        assert results[0]["m"] == 30

    def test_max_string_field(self) -> None:
        docs = make_docs({"name": "Alice"}, {"name": "Charlie"}, {"name": "Bob"})
        results = apply_aggregates(iter(docs), (), {"m": MaxAgg("name")})
        assert results[0]["m"] == "Charlie"

    def test_max_all_none_returns_none(self) -> None:
        docs = make_docs({"v": None})
        results = apply_aggregates(iter(docs), (), {"m": MaxAgg("v")})
        assert results[0]["m"] is None

    def test_max_agg_compute_directly(self) -> None:
        agg = MaxAgg("v")
        assert agg.compute([None, 9, 3]) == 9


# ---------------------------------------------------------------------------
# Multi-aggregate
# ---------------------------------------------------------------------------


class TestMultiAggregate:
    def test_multiple_aggs_same_group(self) -> None:
        docs = make_docs(
            {"cat": "A", "v": 10},
            {"cat": "A", "v": 20},
            {"cat": "A", "v": 30},
        )
        results = apply_aggregates(
            iter(docs),
            ("cat",),
            {
                "n": CountAgg(),
                "total": SumAgg("v"),
                "avg": AvgAgg("v"),
                "lo": MinAgg("v"),
                "hi": MaxAgg("v"),
            },
        )
        assert len(results) == 1
        r = results[0]
        assert r["n"] == 3
        assert r["total"] == 60
        assert r["avg"] == pytest.approx(20.0)
        assert r["lo"] == 10
        assert r["hi"] == 30

    def test_multiple_group_fields(self) -> None:
        docs = make_docs(
            {"country": "NO", "city": "Oslo", "v": 1},
            {"country": "NO", "city": "Oslo", "v": 2},
            {"country": "NO", "city": "Bergen", "v": 3},
            {"country": "SE", "city": "Stockholm", "v": 4},
        )
        results = apply_aggregates(iter(docs), ("country", "city"), {"n": CountAgg()})
        by_city = {r["city"]: r["n"] for r in results}
        assert by_city["Oslo"] == 2
        assert by_city["Bergen"] == 1
        assert by_city["Stockholm"] == 1

    def test_group_key_preserved_in_output(self) -> None:
        docs = make_docs({"cat": "X", "v": 5})
        results = apply_aggregates(iter(docs), ("cat",), {"n": CountAgg()})
        assert results[0]["cat"] == "X"
