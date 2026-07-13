"""Full integration tests for database.py (step 20).

All tests use a real file system via ``tmp_path`` — no mocking.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from jsonpysql.database import Database, DatabaseStats
from jsonpysql.exceptions import (
    CollectionExistsError,
    ReferentialIntegrityError,
    SchemaError,
    StorageError,
    UniqueConstraintError,
    ValidationError,
)
from jsonpysql.schema.fields import field, foreign_key
from jsonpysql.query.builder import QueryBuilder


# ---------------------------------------------------------------------------
# Canonical models
# ---------------------------------------------------------------------------


class Customer(BaseModel):
    id: str = field(primary_key=True)
    email: str = field(unique=True, index=True)
    name: str = field()
    age: int = field(index=True)


class Order(BaseModel):
    id: str = field(primary_key=True)
    customer_id: str = foreign_key(Customer, on_delete="restrict")
    total: float = field()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path)
    database.register_collection("customers", Customer)
    database.register_collection("orders", Order)
    return database


@pytest.fixture
def populated_db(db: Database) -> Database:
    db.customers.insert({"id": "c1", "email": "alice@ex.com", "name": "Alice", "age": 30})
    db.customers.insert({"id": "c2", "email": "bob@ex.com", "name": "Bob", "age": 25})
    db.customers.insert({"id": "c3", "email": "charlie@ex.com", "name": "Charlie", "age": 30})
    db.orders.insert({"id": "o1", "customer_id": "c1", "total": 100.0})
    db.orders.insert({"id": "o2", "customer_id": "c1", "total": 50.0})
    db.orders.insert({"id": "o3", "customer_id": "c2", "total": 200.0})
    return db


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------


class TestDatabaseLifecycle:
    def test_creates_directory(self, tmp_path: Path) -> None:
        db_path = tmp_path / "newdb"
        db = Database(db_path)
        assert db_path.is_dir()

    def test_nested_path_created(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "a" / "b" / "c")
        assert (tmp_path / "a" / "b" / "c").is_dir()

    def test_close_is_noop(self, db: Database) -> None:
        db.close()  # should not raise

    def test_str_path_accepted(self, tmp_path: Path) -> None:
        db = Database(str(tmp_path / "strpath"))
        assert (tmp_path / "strpath").is_dir()


# ---------------------------------------------------------------------------
# Collection registration
# ---------------------------------------------------------------------------


class TestCollectionRegistration:
    def test_register_schema_collection(self, tmp_path: Path) -> None:
        db = Database(tmp_path)
        db.register_collection("customers", Customer)
        assert db.has_collection("customers")

    def test_register_document_collection(self, tmp_path: Path) -> None:
        db = Database(tmp_path)
        db.register_collection("logs")
        assert db.has_collection("logs")

    def test_register_doc_collection_with_indexes(self, tmp_path: Path) -> None:
        db = Database(tmp_path)
        db.register_collection("events", indexes=["type"])
        db.events.insert("e1", {"type": "click", "x": 10})
        result = list(db.events.where(lambda d: d["type"] == "click"))
        assert len(result) == 1

    def test_model_with_indexes_raises_schema_error(self, tmp_path: Path) -> None:
        db = Database(tmp_path)
        with pytest.raises(SchemaError):
            db.register_collection("customers", Customer, indexes=["email"])

    def test_model_without_indexes_still_indexes_from_metadata(
        self, tmp_path: Path
    ) -> None:
        db = Database(tmp_path)
        db.register_collection("customers", Customer)
        db.customers.insert(
            {"id": "c1", "email": "a@b.com", "name": "Alice", "age": 30}
        )
        # 'email' is field(index=True) on the model → index lookup works.
        result = db.customers.where(lambda c: c["email"] == "a@b.com").to_list()
        assert len(result) == 1

    def test_duplicate_collection_raises(self, tmp_path: Path) -> None:
        db = Database(tmp_path)
        db.register_collection("customers", Customer)
        with pytest.raises(CollectionExistsError):
            db.register_collection("customers", Customer)

    def test_drop_if_exists_replaces(self, tmp_path: Path) -> None:
        db = Database(tmp_path)
        db.register_collection("customers", Customer)
        db.register_collection("customers", Customer, drop_if_exists=True)
        assert db.has_collection("customers")

    def test_drop_collection(self, db: Database) -> None:
        db.drop_collection("customers")
        assert not db.has_collection("customers")

    def test_drop_nonexistent_raises(self, db: Database) -> None:
        with pytest.raises(StorageError):
            db.drop_collection("ghost")

    def test_list_collections(self, db: Database) -> None:
        names = db.list_collections()
        assert "customers" in names
        assert "orders" in names


# ---------------------------------------------------------------------------
# Attribute access
# ---------------------------------------------------------------------------


class TestAttributeAccess:
    def test_getattr_returns_queryable(self, db: Database) -> None:
        from jsonpysql.database import _QueryableCollection

        assert isinstance(db.customers, _QueryableCollection)

    def test_getattr_unknown_raises_storage_error(self, db: Database) -> None:
        with pytest.raises(StorageError):
            _ = db.unknown_collection

    def test_getattr_private_raises_attribute_error(self, db: Database) -> None:
        with pytest.raises(AttributeError):
            _ = db._nonexistent


# ---------------------------------------------------------------------------
# CRUD via db.collection
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_insert_and_get(self, db: Database) -> None:
        db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})
        doc = db.customers.get("c1")
        assert doc is not None
        assert doc["name"] == "Alice"

    def test_insert_invalid_raises_validation_error(self, db: Database) -> None:
        with pytest.raises(ValidationError):
            db.customers.insert({"id": "c1", "email": "a@b.com"})  # missing name + age

    def test_insert_duplicate_unique_raises(self, db: Database) -> None:
        db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})
        with pytest.raises(UniqueConstraintError):
            db.customers.insert({"id": "c2", "email": "a@b.com", "name": "Bob", "age": 22})

    def test_update_document(self, db: Database) -> None:
        db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})
        db.customers.update("c1", {"id": "c1", "email": "a@b.com", "name": "Alicia", "age": 28})
        assert db.customers.get("c1")["name"] == "Alicia"

    def test_delete_document(self, db: Database) -> None:
        db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})
        db.customers.delete("c1")
        assert db.customers.get("c1") is None

    def test_fk_constraint_on_insert(self, db: Database) -> None:
        with pytest.raises(ReferentialIntegrityError):
            db.orders.insert({"id": "o1", "customer_id": "ghost", "total": 10.0})

    def test_fk_insert_succeeds_with_parent(self, db: Database) -> None:
        db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})
        db.orders.insert({"id": "o1", "customer_id": "c1", "total": 99.0})
        assert db.orders.get("o1") is not None


# ---------------------------------------------------------------------------
# Fluent query API
# ---------------------------------------------------------------------------


class TestFluentQuery:
    def test_all(self, populated_db: Database) -> None:
        results = populated_db.customers.all()
        assert len(results) == 3

    def test_where_filter(self, populated_db: Database) -> None:
        results = populated_db.customers.where(lambda c: c["name"] == "Alice").to_list()
        assert len(results) == 1

    def test_where_chained(self, populated_db: Database) -> None:
        results = (
            populated_db.customers.where(lambda c: c["age"] == 30)
            .where(lambda c: c["name"] == "Alice")
            .to_list()
        )
        assert len(results) == 1

    def test_order_by(self, populated_db: Database) -> None:
        results = populated_db.customers.order_by("name").to_list()
        names = [r["name"] for r in results]
        assert names == sorted(names)

    def test_limit(self, populated_db: Database) -> None:
        results = populated_db.customers.limit(1).to_list()
        assert len(results) == 1

    def test_index_lookup_via_where(self, populated_db: Database) -> None:
        results = (
            populated_db.customers
            .where(lambda c: c["email"] == "alice@ex.com")
            .to_list()
        )
        assert len(results) == 1
        assert results[0]["name"] == "Alice"

    def test_join(self, populated_db: Database) -> None:
        results = (
            populated_db.customers
            .join("orders", on=lambda c, o: c["_id"] == o["customer_id"])
            .to_list()
        )
        assert len(results) == 3

    def test_group_by_count(self, populated_db: Database) -> None:
        from jsonpysql.query.models import CountAgg

        results = (
            populated_db.customers
            .group_by("age")
            .aggregate(n=CountAgg())
            .to_list()
        )
        by_age = {r["age"]: r["n"] for r in results}
        assert by_age[30] == 2
        assert by_age[25] == 1

    def test_first(self, populated_db: Database) -> None:
        result = populated_db.customers.order_by("name").first()
        assert result is not None
        assert result["name"] == "Alice"

    def test_count(self, populated_db: Database) -> None:
        assert populated_db.customers.where(lambda c: c["age"] == 30).count() == 2

    def test_to_dict(self, populated_db: Database) -> None:
        d = populated_db.customers.order_by("name").to_dict("id")
        assert "c1" in d
        assert d["c1"]["name"] == "Alice"

    def test_explain(self, populated_db: Database) -> None:
        from jsonpysql.query.models import QueryPlan

        plan = populated_db.customers.where(
            lambda c: c["email"] == "alice@ex.com"
        ).explain()
        assert isinstance(plan, QueryPlan)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TestTransactions:
    def test_transaction_commit(self, db: Database) -> None:
        db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})
        with db.transaction() as txn:
            txn.insert("orders", "o1", {"id": "o1", "customer_id": "c1", "total": 50.0})
            txn.insert("orders", "o2", {"id": "o2", "customer_id": "c1", "total": 75.0})
        assert db.orders.get("o1") is not None
        assert db.orders.get("o2") is not None

    def test_transaction_rollback_on_exception(self, db: Database) -> None:
        db.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})
        try:
            with db.transaction() as txn:
                txn.insert("orders", "o1", {"id": "o1", "customer_id": "c1", "total": 50.0})
                raise RuntimeError("intentional")
        except RuntimeError:
            pass
        assert db.orders.get("o1") is None


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


class TestCompaction:
    def test_compact_single_collection(self, populated_db: Database) -> None:
        populated_db.customers.delete("c1")
        populated_db.compact("customers")
        stats = populated_db.stats()
        cust = next(s for s in stats.collections if s.name == "customers")
        assert cust.deleted_count == 0

    def test_compact_all(self, populated_db: Database) -> None:
        populated_db.customers.delete("c2")
        populated_db.compact()  # all collections
        stats = populated_db.stats()
        for s in stats.collections:
            assert s.deleted_count == 0


# ---------------------------------------------------------------------------
# Function registry integration
# ---------------------------------------------------------------------------


class TestFunctionRegistry:
    def test_register_and_call_function(self, db: Database) -> None:
        @db.function
        def double(x: int) -> int:
            return x * 2

        assert db.fn.double(5) == 10

    def test_function_survives_reopen(self, tmp_path: Path) -> None:
        db1 = Database(tmp_path)

        @db1.function
        def greet(name: str) -> str:
            return f"Hello, {name}"

        db2 = Database(tmp_path)
        assert db2.fn.greet("World") == "Hello, World"

    def test_register_procedure(self, db: Database) -> None:
        @db.procedure
        def noop(ctx: object) -> str:
            return "ok"

        # The db context is injected automatically as the first argument.
        assert db.fn.noop() == "ok"

    def test_procedure_receives_db_context(self, db: Database) -> None:
        """db.fn.<procedure>() injects the owning Database as db_ctx."""

        @db.procedure
        def whoami(db_ctx: object) -> bool:
            return db_ctx is db

        assert db.fn.whoami() is True

    def test_procedure_can_use_db_context(self, db: Database) -> None:
        """A procedure operates across collections via the injected db."""
        db.customers.insert(
            {"id": "c1", "email": "a@b.com", "name": "Alice", "age": 30}
        )

        @db.procedure
        def place_order(db_ctx: object, order_id: str, customer_id: str, total: float) -> str:
            db_ctx.orders.insert(
                {"id": order_id, "customer_id": customer_id, "total": total}
            )
            return order_id

        result = db.fn.place_order("o1", "c1", 42.0)
        assert result == "o1"
        assert db.orders.get("o1")["total"] == 42.0

    def test_procedure_with_extra_args(self, db: Database) -> None:
        """Positional args after db_ctx are forwarded correctly."""

        @db.procedure
        def add(db_ctx: object, a: int, b: int) -> int:
            return a + b

        assert db.fn.add(2, 3) == 5

    def test_function_via_accessor_unchanged(self, db: Database) -> None:
        """Functions are NOT passed a db context — only procedures are."""

        @db.function
        def triple(x: int) -> int:
            return x * 3

        assert db.fn.triple(4) == 12

    def test_fn_accessor_delegates_registry_methods(self, db: Database) -> None:
        """Registry helpers (e.g. list_procedures) remain reachable."""

        @db.procedure
        def proc(db_ctx: object) -> None:
            return None

        assert "proc" in db.fn.list_procedures()

    def test_procedure_survives_reopen_with_context(self, tmp_path: Path) -> None:
        db1 = Database(tmp_path)

        @db1.procedure
        def ping(db_ctx: object) -> str:
            return "pong"

        db2 = Database(tmp_path)
        assert db2.fn.ping() == "pong"


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_returns_database_stats(self, populated_db: Database) -> None:
        s = populated_db.stats()
        assert isinstance(s, DatabaseStats)

    def test_stats_total_documents(self, populated_db: Database) -> None:
        s = populated_db.stats()
        assert s.total_documents == 6  # 3 customers + 3 orders

    def test_stats_repr(self, populated_db: Database) -> None:
        s = populated_db.stats()
        assert "DatabaseStats" in repr(s)

    def test_stats_collections_list(self, populated_db: Database) -> None:
        s = populated_db.stats()
        names = {cs.name for cs in s.collections}
        assert "customers" in names
        assert "orders" in names


# ---------------------------------------------------------------------------
# Persistence (reopen)
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_data_survives_reopen(self, tmp_path: Path) -> None:
        db1 = Database(tmp_path)
        db1.register_collection("customers", Customer)
        db1.customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice", "age": 28})

        db2 = Database(tmp_path)
        db2.register_collection("customers", Customer)
        assert db2.customers.get("c1")["name"] == "Alice"

    def test_schema_collection_queryable_after_reopen(self, tmp_path: Path) -> None:
        db1 = Database(tmp_path)
        db1.register_collection("customers", Customer)
        db1.customers.insert({"id": "c1", "email": "alice@ex.com", "name": "Alice", "age": 28})

        db2 = Database(tmp_path)
        db2.register_collection("customers", Customer)
        results = (
            db2.customers.where(lambda c: c["email"] == "alice@ex.com").to_list()
        )
        assert len(results) == 1
        assert results[0]["name"] == "Alice"
