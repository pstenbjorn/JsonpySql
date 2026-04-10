"""Tests for schema/registry.py (step 14 — CollectionManager)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from jsonpysql.exceptions import CollectionExistsError, StorageError
from jsonpysql.schema.collection import DocumentCollection, SchemaCollection
from jsonpysql.schema.fields import field, foreign_key
from jsonpysql.schema.registry import CollectionManager
from jsonpysql.storage.engine import StorageEngine


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


class Product(BaseModel):
    id: str = field(primary_key=True)
    sku: str = field(unique=True, index=True)
    price: float = field()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> StorageEngine:
    return StorageEngine(tmp_path)


@pytest.fixture
def manager(engine: StorageEngine) -> CollectionManager:
    return CollectionManager(engine)


# ---------------------------------------------------------------------------
# register_schema_collection
# ---------------------------------------------------------------------------


class TestRegisterSchemaCollection:
    def test_returns_schema_collection(self, manager: CollectionManager) -> None:
        col = manager.register_schema_collection("customers", Customer)
        assert isinstance(col, SchemaCollection)

    def test_collection_name_matches(self, manager: CollectionManager) -> None:
        col = manager.register_schema_collection("customers", Customer)
        assert col.name == "customers"

    def test_model_matches(self, manager: CollectionManager) -> None:
        col = manager.register_schema_collection("customers", Customer)
        assert col.model is Customer

    def test_duplicate_raises_collection_exists_error(
        self, manager: CollectionManager
    ) -> None:
        manager.register_schema_collection("customers", Customer)
        with pytest.raises(CollectionExistsError):
            manager.register_schema_collection("customers", Customer)

    def test_drop_if_exists_replaces_collection(
        self, manager: CollectionManager
    ) -> None:
        col1 = manager.register_schema_collection("customers", Customer)
        col2 = manager.register_schema_collection(
            "customers", Customer, drop_if_exists=True
        )
        assert isinstance(col2, SchemaCollection)
        assert col2 is not col1

    def test_indexes_derived_from_model_fields(
        self, manager: CollectionManager
    ) -> None:
        col = manager.register_schema_collection("customers", Customer)
        # email is indexed; should be findable via lookup
        col.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        result = list(col.lookup("email", "a@b.com"))
        assert result == ["c1"]

    def test_unique_constraint_enforced(self, manager: CollectionManager) -> None:
        from jsonpysql.exceptions import UniqueConstraintError

        col = manager.register_schema_collection("customers", Customer)
        col.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        with pytest.raises(UniqueConstraintError):
            col.insert({"id": "c2", "email": "a@b.com", "name": "Bob"})

    def test_fk_checker_wired_for_child_model(
        self, manager: CollectionManager
    ) -> None:
        from jsonpysql.exceptions import ReferentialIntegrityError

        manager.register_schema_collection("customers", Customer)
        orders = manager.register_schema_collection("orders", Order)
        with pytest.raises(ReferentialIntegrityError):
            orders.insert({"id": "o1", "customer_id": "ghost", "total": 9.99})

    def test_fk_insert_succeeds_when_parent_exists(
        self, manager: CollectionManager
    ) -> None:
        customers = manager.register_schema_collection("customers", Customer)
        orders = manager.register_schema_collection("orders", Order)
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        doc_id = orders.insert({"id": "o1", "customer_id": "c1", "total": 42.0})
        assert doc_id == "o1"

    def test_duplicate_raises_when_doc_col_exists(
        self, manager: CollectionManager
    ) -> None:
        """Schema registration blocked by existing document collection."""
        manager.register_document_collection("customers")
        with pytest.raises(CollectionExistsError):
            manager.register_schema_collection("customers", Customer)


# ---------------------------------------------------------------------------
# register_document_collection
# ---------------------------------------------------------------------------


class TestRegisterDocumentCollection:
    def test_returns_document_collection(self, manager: CollectionManager) -> None:
        col = manager.register_document_collection("docs")
        assert isinstance(col, DocumentCollection)

    def test_collection_name_matches(self, manager: CollectionManager) -> None:
        col = manager.register_document_collection("docs")
        assert col.name == "docs"

    def test_insert_and_get(self, manager: CollectionManager) -> None:
        col = manager.register_document_collection("docs")
        col.insert("d1", {"foo": "bar"})
        assert col.get("d1") == {"foo": "bar"}

    def test_duplicate_raises_collection_exists_error(
        self, manager: CollectionManager
    ) -> None:
        manager.register_document_collection("docs")
        with pytest.raises(CollectionExistsError):
            manager.register_document_collection("docs")

    def test_drop_if_exists_replaces(self, manager: CollectionManager) -> None:
        col1 = manager.register_document_collection("docs")
        col2 = manager.register_document_collection("docs", drop_if_exists=True)
        assert isinstance(col2, DocumentCollection)
        assert col2 is not col1

    def test_no_indexes_option(self, manager: CollectionManager) -> None:
        col = manager.register_document_collection("docs")
        col.insert("d1", {"v": 1})
        result = list(col.lookup("v", 1))
        assert result == ["d1"]

    def test_with_explicit_indexes(self, manager: CollectionManager) -> None:
        col = manager.register_document_collection("docs", indexes=["email"])
        col.insert("d1", {"email": "x@y.com"})
        result = list(col.lookup("email", "x@y.com"))
        assert result == ["d1"]

    def test_duplicate_raises_when_schema_col_exists(
        self, manager: CollectionManager
    ) -> None:
        """Document registration blocked by existing schema collection."""
        manager.register_schema_collection("customers", Customer)
        with pytest.raises(CollectionExistsError):
            manager.register_document_collection("customers")


# ---------------------------------------------------------------------------
# drop_collection
# ---------------------------------------------------------------------------


class TestDropCollection:
    def test_drop_schema_collection(self, manager: CollectionManager) -> None:
        manager.register_schema_collection("customers", Customer)
        manager.drop_collection("customers")
        assert not manager.has_collection("customers")

    def test_drop_document_collection(self, manager: CollectionManager) -> None:
        manager.register_document_collection("docs")
        manager.drop_collection("docs")
        assert not manager.has_collection("docs")

    def test_drop_nonexistent_raises_storage_error(
        self, manager: CollectionManager
    ) -> None:
        with pytest.raises(StorageError):
            manager.drop_collection("ghost")

    def test_dropped_collection_can_be_re_registered(
        self, manager: CollectionManager
    ) -> None:
        manager.register_schema_collection("customers", Customer)
        manager.drop_collection("customers")
        col = manager.register_schema_collection("customers", Customer)
        assert isinstance(col, SchemaCollection)


# ---------------------------------------------------------------------------
# get_collection / has_collection / list_collections
# ---------------------------------------------------------------------------


class TestCollectionRetrieval:
    def test_get_schema_collection(self, manager: CollectionManager) -> None:
        manager.register_schema_collection("customers", Customer)
        col = manager.get_collection("customers")
        assert isinstance(col, SchemaCollection)

    def test_get_document_collection(self, manager: CollectionManager) -> None:
        manager.register_document_collection("docs")
        col = manager.get_collection("docs")
        assert isinstance(col, DocumentCollection)

    def test_get_unknown_raises_storage_error(
        self, manager: CollectionManager
    ) -> None:
        with pytest.raises(StorageError):
            manager.get_collection("missing")

    def test_has_collection_true_for_schema(
        self, manager: CollectionManager
    ) -> None:
        manager.register_schema_collection("customers", Customer)
        assert manager.has_collection("customers") is True

    def test_has_collection_true_for_document(
        self, manager: CollectionManager
    ) -> None:
        manager.register_document_collection("docs")
        assert manager.has_collection("docs") is True

    def test_has_collection_false_for_unknown(
        self, manager: CollectionManager
    ) -> None:
        assert manager.has_collection("ghost") is False

    def test_list_collections_empty(self, manager: CollectionManager) -> None:
        assert manager.list_collections() == []

    def test_list_collections_sorted(self, manager: CollectionManager) -> None:
        manager.register_schema_collection("zebras", Customer)
        manager.register_document_collection("apples")
        assert manager.list_collections() == ["apples", "zebras"]

    def test_list_collections_includes_both_types(
        self, manager: CollectionManager
    ) -> None:
        manager.register_schema_collection("customers", Customer)
        manager.register_document_collection("docs")
        names = manager.list_collections()
        assert "customers" in names
        assert "docs" in names


# ---------------------------------------------------------------------------
# _build_fk_checkers (indirect via registration)
# ---------------------------------------------------------------------------


class TestBuildFkCheckers:
    def test_no_fk_model_yields_no_checkers(
        self, manager: CollectionManager
    ) -> None:
        """Customer has no FK fields; register Order's checker indirectly."""
        col = manager.register_schema_collection("customers", Customer)
        # No FK on Customer — insert should succeed without any checker
        doc_id = col.insert({"id": "c1", "email": "x@y.com", "name": "X"})
        assert doc_id == "c1"

    def test_fk_uses_plural_convention(self, manager: CollectionManager) -> None:
        """FK parent collection name = ModelName.lower() + 's'."""
        customers = manager.register_schema_collection("customers", Customer)
        orders = manager.register_schema_collection("orders", Order)
        customers.insert({"id": "c1", "email": "a@b.com", "name": "Alice"})
        # FK checker resolves 'Customer' → 'customers'
        orders.insert({"id": "o1", "customer_id": "c1", "total": 5.0})
        assert orders.get("o1") is not None
