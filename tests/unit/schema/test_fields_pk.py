"""Unit tests for primary-key resolution in schema/fields.py (issue #4)."""

from __future__ import annotations

from pydantic import BaseModel

from jsonpysql.schema.fields import field, get_primary_key_field


class TestGetPrimaryKeyField:
    def test_explicit_primary_key(self) -> None:
        class M(BaseModel):
            uid: str = field(primary_key=True)
            n: int

        assert get_primary_key_field(M) == "uid"

    def test_id_field_is_pk_by_convention(self) -> None:
        class M(BaseModel):
            id: str
            n: int

        assert get_primary_key_field(M) == "id"

    def test_explicit_pk_wins_over_id_convention(self) -> None:
        class M(BaseModel):
            id: str
            uid: str = field(primary_key=True)

        assert get_primary_key_field(M) == "uid"

    def test_no_pk_and_no_id_returns_none(self) -> None:
        class M(BaseModel):
            name: str
            n: int

        assert get_primary_key_field(M) is None
