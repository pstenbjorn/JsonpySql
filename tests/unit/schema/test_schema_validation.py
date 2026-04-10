"""Tests for schema/validator.py."""

from __future__ import annotations

from pydantic import BaseModel

import pytest

from jsonpysql.exceptions import ValidationError
from jsonpysql.schema.fields import field
from jsonpysql.schema.validator import Validator


class Customer(BaseModel):
    id: str
    name: str
    age: int


class CustomerWithDefaults(BaseModel):
    id: str
    name: str = "Anonymous"
    age: int = 0


# ---------------------------------------------------------------------------
# validate()
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_document_passes(self) -> None:
        v = Validator(Customer)
        result = v.validate({"id": "c1", "name": "Alice", "age": 30})
        assert result == {"id": "c1", "name": "Alice", "age": 30}

    def test_type_coercion_applied(self) -> None:
        """Pydantic coerces "30" → 30 for an int field."""
        v = Validator(Customer)
        result = v.validate({"id": "c1", "name": "Alice", "age": "30"})
        assert result["age"] == 30

    def test_missing_required_field_raises_validation_error(self) -> None:
        v = Validator(Customer)
        with pytest.raises(ValidationError):
            v.validate({"id": "c1", "name": "Alice"})  # missing 'age'

    def test_wrong_type_raises_validation_error(self) -> None:
        v = Validator(Customer)
        with pytest.raises(ValidationError):
            v.validate({"id": "c1", "name": "Alice", "age": "not-a-number"})

    def test_extra_fields_ignored_by_default(self) -> None:
        v = Validator(Customer)
        result = v.validate({"id": "c1", "name": "Alice", "age": 30, "extra": "ignored"})
        assert "extra" not in result

    def test_model_property(self) -> None:
        v = Validator(Customer)
        assert v.model is Customer


class TestValidateWithDefaults:
    def test_defaults_are_applied(self) -> None:
        v = Validator(CustomerWithDefaults)
        result = v.validate({"id": "c1"})
        assert result["name"] == "Anonymous"
        assert result["age"] == 0
