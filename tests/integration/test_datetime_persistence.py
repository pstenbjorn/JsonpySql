"""Regression tests: datetime / Decimal / UUID persistence.

Covers the bug where ``SchemaCollection.insert(model.model_dump())`` and
``DocumentCollection.insert`` raised ``TypeError: Object of type datetime
is not JSON serializable``.

Exercises all three write paths: single-doc append (DocumentStore),
compaction rewrite, and the WAL transaction path.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import BaseModel

from jsonpysql.database import Database
from jsonpysql.schema.fields import field


class Event(BaseModel):
    id: str = field(primary_key=True)
    name: str = field()
    occurred_at: datetime = field(index=True)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path)
    database.register_collection("events", Event)
    database.register_collection("raw")  # schema-free document collection
    return database


# ---------------------------------------------------------------------------
# SchemaCollection — the reported bug
# ---------------------------------------------------------------------------


class TestSchemaCollectionDatetime:
    def test_insert_model_dump_with_datetime(self, db: Database) -> None:
        event = Event(id="e1", name="launch", occurred_at=datetime(2026, 7, 13, 9, 0, 0))
        # This is exactly the call that used to raise TypeError.
        doc_id = db.events.insert(event.model_dump())
        assert doc_id == "e1"

    def test_datetime_round_trips_as_iso_string(self, db: Database) -> None:
        event = Event(id="e1", name="launch", occurred_at=datetime(2026, 7, 13, 9, 0, 0))
        db.events.insert(event.model_dump())
        stored = db.events.get("e1")
        assert stored is not None
        assert stored["occurred_at"] == "2026-07-13T09:00:00"

    def test_reload_reparses_into_model(self, db: Database) -> None:
        event = Event(id="e1", name="launch", occurred_at=datetime(2026, 7, 13, 9, 0, 0))
        db.events.insert(event.model_dump())
        stored = db.events.get("e1")
        # The stored ISO string re-coerces back into a datetime via the model.
        reparsed = Event.model_validate(stored)
        assert reparsed.occurred_at == datetime(2026, 7, 13, 9, 0, 0)

    def test_query_by_datetime_index(self, db: Database) -> None:
        db.events.insert(
            Event(id="e1", name="a", occurred_at=datetime(2026, 1, 1)).model_dump()
        )
        db.events.insert(
            Event(id="e2", name="b", occurred_at=datetime(2026, 6, 1)).model_dump()
        )
        results = db.events.where(
            lambda e: e["occurred_at"] == "2026-01-01T00:00:00"
        ).to_list()
        assert len(results) == 1
        assert results[0]["name"] == "a"


# ---------------------------------------------------------------------------
# DocumentCollection — defense-in-depth (raw dict bypasses the validator)
# ---------------------------------------------------------------------------


class TestDocumentCollectionDatetime:
    def test_insert_raw_datetime(self, db: Database) -> None:
        db.raw.insert("d1", {"when": datetime(2026, 7, 13, 9, 0, 0)})
        stored = db.raw.get("d1")
        assert stored == {"when": "2026-07-13T09:00:00"}

    def test_insert_raw_decimal_and_uuid(self, db: Database) -> None:
        uid = UUID("12345678-1234-5678-1234-567812345678")
        db.raw.insert("d2", {"price": Decimal("19.99"), "ref": uid})
        stored = db.raw.get("d2")
        assert stored == {
            "price": "19.99",
            "ref": "12345678-1234-5678-1234-567812345678",
        }


# ---------------------------------------------------------------------------
# WAL transaction path
# ---------------------------------------------------------------------------


class TestTransactionDatetime:
    def test_transaction_insert_with_datetime(self, db: Database) -> None:
        with db.transaction() as txn:
            txn.insert("raw", "d1", {"when": datetime(2026, 7, 13, 9, 0, 0)})
            txn.insert("raw", "d2", {"when": datetime(2026, 7, 14, 10, 0, 0)})
        assert db.raw.get("d1") == {"when": "2026-07-13T09:00:00"}
        assert db.raw.get("d2") == {"when": "2026-07-14T10:00:00"}


# ---------------------------------------------------------------------------
# Compaction rewrite path
# ---------------------------------------------------------------------------


class TestCompactionDatetime:
    def test_compact_preserves_datetime(self, db: Database) -> None:
        db.events.insert(
            Event(id="e1", name="a", occurred_at=datetime(2026, 1, 1)).model_dump()
        )
        db.events.insert(
            Event(id="e2", name="b", occurred_at=datetime(2026, 6, 1)).model_dump()
        )
        db.events.delete("e2")
        db.compact("events")  # rewrites the .jsonl file
        stored = db.events.get("e1")
        assert stored is not None
        assert stored["occurred_at"] == "2026-01-01T00:00:00"
