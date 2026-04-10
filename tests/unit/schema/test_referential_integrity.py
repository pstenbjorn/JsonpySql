"""Tests for schema/constraints.py.

Uses a real StorageEngine backed by tmp_path (no mocking of storage).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonpysql.exceptions import ReferentialIntegrityError, UniqueConstraintError
from jsonpysql.schema.constraints import ReferentialIntegrityChecker, UniqueConstraintChecker
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import IndexSpec


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> StorageEngine:
    eng = StorageEngine(tmp_path)
    eng.create_collection("customers", [IndexSpec(["email"])])
    eng.create_collection("orders", [])
    return eng


# ---------------------------------------------------------------------------
# UniqueConstraintChecker
# ---------------------------------------------------------------------------


class TestUniqueConstraintChecker:
    def test_insert_unique_value_passes(self, engine: StorageEngine) -> None:
        checker = UniqueConstraintChecker(engine, "customers", ["email"])
        checker.check_insert({"email": "alice@example.com"})  # must not raise

    def test_insert_duplicate_value_raises(self, engine: StorageEngine) -> None:
        engine.insert("customers", "c1", {"email": "alice@example.com", "name": "Alice"})
        checker = UniqueConstraintChecker(engine, "customers", ["email"])
        with pytest.raises(UniqueConstraintError):
            checker.check_insert({"email": "alice@example.com"})

    def test_insert_none_value_skipped(self, engine: StorageEngine) -> None:
        checker = UniqueConstraintChecker(engine, "customers", ["email"])
        checker.check_insert({"email": None})  # must not raise

    def test_update_same_doc_does_not_conflict(self, engine: StorageEngine) -> None:
        engine.insert("customers", "c1", {"email": "alice@example.com", "name": "Alice"})
        checker = UniqueConstraintChecker(engine, "customers", ["email"])
        checker.check_update("c1", {"email": "alice@example.com"})  # self-update OK

    def test_update_conflicts_with_other_doc(self, engine: StorageEngine) -> None:
        engine.insert("customers", "c1", {"email": "alice@example.com", "name": "Alice"})
        engine.insert("customers", "c2", {"email": "bob@example.com", "name": "Bob"})
        checker = UniqueConstraintChecker(engine, "customers", ["email"])
        with pytest.raises(UniqueConstraintError):
            checker.check_update("c2", {"email": "alice@example.com"})

    def test_update_none_value_skipped(self, engine: StorageEngine) -> None:
        checker = UniqueConstraintChecker(engine, "customers", ["email"])
        checker.check_update("c1", {"email": None})  # must not raise

    def test_no_unique_fields_always_passes(self, engine: StorageEngine) -> None:
        checker = UniqueConstraintChecker(engine, "customers", [])
        checker.check_insert({"email": "x@y.com"})  # must not raise


# ---------------------------------------------------------------------------
# ReferentialIntegrityChecker
# ---------------------------------------------------------------------------


class TestReferentialIntegrityChecker:
    def _make_checker(self, engine: StorageEngine, on_delete: str = "restrict") -> ReferentialIntegrityChecker:
        return ReferentialIntegrityChecker(
            engine=engine,
            parent_collection="customers",
            parent_pk_field="id",
            child_collection="orders",
            fk_field="customer_id",
            on_delete=on_delete,
        )

    def test_child_insert_with_valid_parent_passes(self, engine: StorageEngine) -> None:
        engine.insert("customers", "c1", {"email": "a@b.com", "name": "Alice"})
        checker = self._make_checker(engine)
        checker.check_child_insert({"customer_id": "c1", "total": 100})

    def test_child_insert_with_missing_parent_raises(self, engine: StorageEngine) -> None:
        checker = self._make_checker(engine)
        with pytest.raises(ReferentialIntegrityError):
            checker.check_child_insert({"customer_id": "ghost", "total": 50})

    def test_child_insert_none_fk_skipped(self, engine: StorageEngine) -> None:
        checker = self._make_checker(engine)
        checker.check_child_insert({"customer_id": None})  # nullable FK

    def test_parent_delete_restrict_with_children_raises(
        self, engine: StorageEngine
    ) -> None:
        engine.insert("customers", "c1", {"email": "a@b.com", "name": "Alice"})
        engine.insert("orders", "o1", {"customer_id": "c1", "total": 10})
        # Register the child in the reverse index manually.
        engine._indexes["orders"].add_reverse("c1", "o1")
        checker = self._make_checker(engine, on_delete="restrict")
        with pytest.raises(ReferentialIntegrityError):
            checker.check_parent_delete("c1")

    def test_parent_delete_cascade_does_not_raise(
        self, engine: StorageEngine
    ) -> None:
        engine.insert("customers", "c1", {"email": "a@b.com", "name": "Alice"})
        engine.insert("orders", "o1", {"customer_id": "c1", "total": 10})
        engine._indexes["orders"].add_reverse("c1", "o1")
        checker = self._make_checker(engine, on_delete="cascade")
        checker.check_parent_delete("c1")  # must not raise

    def test_parent_delete_no_children_passes(self, engine: StorageEngine) -> None:
        engine.insert("customers", "c1", {"email": "a@b.com", "name": "Alice"})
        checker = self._make_checker(engine, on_delete="restrict")
        checker.check_parent_delete("c1")  # no children → must not raise
