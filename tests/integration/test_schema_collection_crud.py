"""Integration tests for schema/collection.py (step 13).

Uses the canonical Customer/Order schema relationship and real file system.
No mocking of any storage sub-module.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from jsonpysql.exceptions import (
    ReferentialIntegrityError,
    UniqueConstraintError,
    ValidationError,
)
from jsonpysql.schema.collection import DocumentCollection, SchemaCollection
from jsonpysql.schema.constraints import ReferentialIntegrityChecker, UniqueConstraintChecker
from jsonpysql.schema.fields import field, foreign_key, get_indexed_fields, get_unique_fields
from jsonpysql.storage.engine import StorageEngine
from jsonpysql.storage.models import IndexSpec


# ---------------------------------------------------------------------------
# Canonical models
# ---------------------------------------------------------------------------


class Customer(BaseModel):
    id: str = field(primary_key=True)
    email: str = field(unique=True, index=True)
    name: str = field()


class Order(BaseModel):
    id: str = field(primary_key=True)
    customer_id: str = foreign_key(Customer, on_delete="restrict")
    total: float = field()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> StorageEngine:
    eng = StorageEngine(tmp_path)
    eng.create_collection("customers", [IndexSpec(["email"])])
    eng.create_collection("orders", [])
    return eng


@pytest.fixture
def customers(engine: StorageEngine) -> SchemaCollection:
    return SchemaCollection("customers", Customer, engine)


@pytest.fixture
def orders(engine: StorageEngine, customers: SchemaCollection) -> SchemaCollection:
    fk_checker = ReferentialIntegrityChecker(
        engine=engine,
        parent_collection="customers",
        parent_pk_field="id",
        child_collection="orders",
        fk_field="customer_id",
        on_delete="restrict",
    )
    return SchemaCollection("orders", Order, engine, fk_checkers={"customer_id": fk_checker})


# ---------------------------------------------------------------------------
# SchemaCollection — insert / get
# ---------------------------------------------------------------------------


class TestSchemaCollectionInsert:
    def test_insert_valid_document(self, customers: SchemaCollection) -> None:
        doc_id = customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        assert doc_id == "c1"
        doc = customers.get("c1")
        assert doc is not None
        assert doc["name"] == "Alice"

    def test_insert_invalid_document_raises_validation_error(
        self, customers: SchemaCollection
    ) -> None:
        with pytest.raises(ValidationError):
            customers.insert({"id": "c1", "email": "a@b.com"})  # missing 'name'

    def test_insert_duplicate_unique_raises(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        with pytest.raises(UniqueConstraintError):
            customers.insert({"id": "c2", "email": "a@b.com", "name": "Bob"})

    def test_insert_uses_pk_as_doc_id(self, customers: SchemaCollection) -> None:
        doc_id = customers.insert({"id": "custom-id", "email": "x@y.com", "name": "X"})
        assert doc_id == "custom-id"

    def test_insert_generates_uuid_without_pk(self, engine: StorageEngine) -> None:
        class Simple(BaseModel):
            name: str

        engine.create_collection("simple", [])
        col = SchemaCollection("simple", Simple, engine)
        doc_id = col.insert({"name": "thing"})
        assert len(doc_id) == 36  # UUID format


# ---------------------------------------------------------------------------
# SchemaCollection — update
# ---------------------------------------------------------------------------


class TestSchemaCollectionUpdate:
    def test_update_replaces_document(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        customers.update("c1", {"id": "c1", "email": "a@b.com", "name": "Alicia"})
        assert customers.get("c1")["name"] == "Alicia"

    def test_update_unique_conflict_raises(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        customers.insert({"id": "c2", "email": "b@b.com", "name": "Bob"})
        with pytest.raises(UniqueConstraintError):
            customers.update("c2", {"id": "c2", "email": "a@b.com", "name": "Bob"})

    def test_update_same_email_allowed(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        customers.update("c1", {"id": "c1", "email": "a@b.com", "name": "Alicia"})


# ---------------------------------------------------------------------------
# SchemaCollection — delete
# ---------------------------------------------------------------------------


class TestSchemaCollectionDelete:
    def test_delete_removes_document(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        customers.delete("c1")
        assert customers.get("c1") is None


# ---------------------------------------------------------------------------
# Foreign-key constraints
# ---------------------------------------------------------------------------


class TestForeignKeyConstraints:
    def test_insert_order_with_valid_customer(
        self, customers: SchemaCollection, orders: SchemaCollection
    ) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        orders.insert({"id": "o1", "customer_id": "c1", "total": 99.0})
        assert orders.get("o1")["total"] == 99.0

    def test_insert_order_with_missing_customer_raises(
        self, orders: SchemaCollection
    ) -> None:
        with pytest.raises(ReferentialIntegrityError):
            orders.insert({"id": "o1", "customer_id": "ghost", "total": 10.0})


# ---------------------------------------------------------------------------
# SchemaCollection — scan, lookup, range_scan
# ---------------------------------------------------------------------------


class TestSchemaCollectionQuery:
    def test_scan_returns_all_documents(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        customers.insert({"id": "c2", "email": "b@b.com", "name": "Bob"})
        ids = {d["_id"] for d in customers.scan()}
        assert ids == {"c1", "c2"}

    def test_lookup_by_indexed_field(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        result = list(customers.lookup("email", "a@b.com"))
        assert result == ["c1"]

    def test_range_scan(self, customers: SchemaCollection) -> None:
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        customers.insert({"id": "c2", "email": "b@b.com", "name": "Bob"})
        results = sorted(customers.range_scan("email", "a@b.com", "a@b.com"))
        assert results == ["c1"]

    def test_properties(self, customers: SchemaCollection) -> None:
        assert customers.name == "customers"
        assert customers.model is Customer


# ---------------------------------------------------------------------------
# DocumentCollection
# ---------------------------------------------------------------------------


class TestDocumentCollection:
    def test_insert_and_get(self, engine: StorageEngine) -> None:
        col = DocumentCollection("customers", engine)
        col.insert("d1", {"arbitrary": "data", "num": 42})
        doc = col.get("d1")
        assert doc == {"arbitrary": "data", "num": 42}

    def test_update_replaces(self, engine: StorageEngine) -> None:
        col = DocumentCollection("customers", engine)
        col.insert("d1", {"v": 1})
        col.update("d1", {"v": 2})
        assert col.get("d1") == {"v": 2}

    def test_delete_removes(self, engine: StorageEngine) -> None:
        col = DocumentCollection("customers", engine)
        col.insert("d1", {"v": 1})
        col.delete("d1")
        assert col.get("d1") is None

    def test_scan(self, engine: StorageEngine) -> None:
        col = DocumentCollection("customers", engine)
        col.insert("d1", {"v": 1})
        col.insert("d2", {"v": 2})
        ids = {d["_id"] for d in col.scan()}
        assert ids == {"d1", "d2"}

    def test_lookup(self, engine: StorageEngine) -> None:
        col = DocumentCollection("customers", engine)
        col.insert("d1", {"email": "x@y.com"})
        # No index → falls back to full scan
        result = list(col.lookup("email", "x@y.com"))
        assert result == ["d1"]

    def test_range_scan(self, engine: StorageEngine) -> None:
        col = DocumentCollection("customers", engine)
        col.insert("d1", {"v": 1})
        col.insert("d2", {"v": 5})
        col.insert("d3", {"v": 10})
        result = sorted(col.range_scan("v", 1, 5))
        assert result == ["d1", "d2"]

    def test_name_property(self, engine: StorageEngine) -> None:
        col = DocumentCollection("customers", engine)
        assert col.name == "customers"
