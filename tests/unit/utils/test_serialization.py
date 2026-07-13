"""Tests for utils/serialization.py — the JSON fallback encoder."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

import pytest

from jsonpysql.utils.serialization import json_default


class TestJsonDefault:
    def test_datetime_to_isoformat(self) -> None:
        dt = datetime(2026, 7, 13, 9, 30, 0)
        assert json_default(dt) == "2026-07-13T09:30:00"

    def test_date_to_isoformat(self) -> None:
        assert json_default(date(2026, 7, 13)) == "2026-07-13"

    def test_time_to_isoformat(self) -> None:
        assert json_default(time(9, 30, 15)) == "09:30:15"

    def test_decimal_to_string_preserves_precision(self) -> None:
        assert json_default(Decimal("19.99")) == "19.99"
        # A value that float() cannot represent exactly stays exact.
        assert json_default(Decimal("0.1")) == "0.1"

    def test_uuid_to_string(self) -> None:
        u = UUID("12345678-1234-5678-1234-567812345678")
        assert json_default(u) == "12345678-1234-5678-1234-567812345678"

    def test_unsupported_type_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            json_default(object())

    def test_usable_as_json_dumps_default(self) -> None:
        doc = {"when": datetime(2026, 1, 1, 12, 0, 0), "price": Decimal("5.50")}
        encoded = json.dumps(doc, default=json_default)
        assert json.loads(encoded) == {
            "when": "2026-01-01T12:00:00",
            "price": "5.50",
        }
