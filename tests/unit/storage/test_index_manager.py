"""Tests for storage/index_manager.py.

Parametrised across: single-field indexed, compound-indexed, and no-index
(full-scan fallback) scenarios as required by the test conventions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonpysql.exceptions import StorageError
from jsonpysql.storage.index_manager import IndexManager
from jsonpysql.storage.models import IndexSpec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DOC_A = {"_id": "a", "name": "Alice", "age": 30, "city": "Oslo"}
DOC_B = {"_id": "b", "name": "Bob", "age": 25, "city": "Oslo"}
DOC_C = {"_id": "c", "name": "Carol", "age": 30, "city": "Bergen"}


def make_im(tmp_path: Path, specs: list[IndexSpec]) -> IndexManager:
    im = IndexManager(tmp_path, "users", specs)
    return im


# ---------------------------------------------------------------------------
# Parametrised fixture: single-field, compound, no-index
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        pytest.param([IndexSpec(["name"]), IndexSpec(["age"])], id="single_field"),
        pytest.param([IndexSpec(["name", "city"])], id="compound"),
        pytest.param([], id="no_index"),
    ]
)
def im(tmp_path: Path, request: pytest.FixtureRequest) -> IndexManager:
    return make_im(tmp_path, request.param)


# ---------------------------------------------------------------------------
# Insert → lookup
# ---------------------------------------------------------------------------


class TestLookup:
    def test_lookup_returns_inserted_id(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        assert list(im.lookup("name", "Alice")) == ["a"]

    def test_lookup_unknown_value_returns_empty(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        assert list(im.lookup("name", "Zara")) == []

    def test_lookup_unindexed_field_returns_empty(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        assert list(im.lookup("age", 30)) == []

    def test_lookup_multiple_docs_same_value(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["age"])])
        im.on_insert("a", DOC_A)
        im.on_insert("c", DOC_C)
        result = sorted(im.lookup("age", 30))
        assert result == ["a", "c"]

    def test_lookup_no_index_returns_empty(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [])
        im.on_insert("a", DOC_A)
        assert list(im.lookup("name", "Alice")) == []


# ---------------------------------------------------------------------------
# Range scan
# ---------------------------------------------------------------------------


class TestRangeScan:
    def test_range_scan_finds_in_range(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        im.on_insert("b", DOC_B)
        im.on_insert("c", DOC_C)
        results = sorted(im.range_scan("name", "Alice", "Bob"))
        assert results == ["a", "b"]

    def test_range_scan_empty_result(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        assert list(im.range_scan("name", "Z", "ZZZ")) == []

    def test_range_scan_unindexed_field_empty(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        assert list(im.range_scan("age", 1, 100)) == []


# ---------------------------------------------------------------------------
# Update / delete removes old entry
# ---------------------------------------------------------------------------


class TestUpdateAndDelete:
    def test_update_removes_old_key(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        updated = {**DOC_A, "name": "Alicia"}
        im.on_update("a", DOC_A, updated)
        assert list(im.lookup("name", "Alice")) == []
        assert list(im.lookup("name", "Alicia")) == ["a"]

    def test_delete_removes_doc_from_index(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        im.on_delete("a", DOC_A)
        assert list(im.lookup("name", "Alice")) == []


# ---------------------------------------------------------------------------
# Persist and reload
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_load_restores_index(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)

        im2 = make_im(tmp_path, [IndexSpec(["name"])])
        im2.load()
        assert list(im2.lookup("name", "Alice")) == ["a"]

    def test_rebuild_from_documents(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.rebuild([DOC_A, DOC_B])
        assert sorted(im.lookup("name", "Alice")) == ["a"]
        assert sorted(im.lookup("name", "Bob")) == ["b"]

    def test_corrupt_index_loads_as_empty(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)

        # Corrupt the index file.
        idx_file = tmp_path / "users.name.idx"
        idx_file.write_text("not-json", encoding="utf-8")

        im2 = make_im(tmp_path, [IndexSpec(["name"])])
        im2.load()
        # After corrupt load the index is empty; caller must rebuild.
        assert list(im2.lookup("name", "Alice")) == []

    def test_destroy_removes_index_files(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        im.on_insert("a", DOC_A)
        im.destroy()
        assert not (tmp_path / "users.name.idx").exists()


# ---------------------------------------------------------------------------
# Reverse index (FK cascade)
# ---------------------------------------------------------------------------


class TestReverseIndex:
    def test_add_and_get_children(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [])
        im.add_reverse("parent1", "child1")
        im.add_reverse("parent1", "child2")
        assert sorted(im.get_children("parent1")) == ["child1", "child2"]

    def test_get_children_unknown_parent(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [])
        assert im.get_children("nobody") == []

    def test_remove_reverse(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [])
        im.add_reverse("p", "c")
        im.remove_reverse("p", "c")
        assert im.get_children("p") == []

    def test_remove_reverse_unknown_is_noop(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [])
        im.remove_reverse("p", "ghost")  # must not raise

    def test_reverse_index_persisted(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [])
        im.add_reverse("p", "c")

        im2 = make_im(tmp_path, [])
        im2.load()
        assert "c" in im2.get_children("p")


# ---------------------------------------------------------------------------
# has_index_for
# ---------------------------------------------------------------------------


class TestHasIndexFor:
    def test_returns_true_for_indexed_field(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        assert im.has_index_for("name") is True

    def test_returns_false_for_unindexed_field(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [IndexSpec(["name"])])
        assert im.has_index_for("age") is False

    def test_returns_false_with_no_specs(self, tmp_path: Path) -> None:
        im = make_im(tmp_path, [])
        assert im.has_index_for("name") is False
