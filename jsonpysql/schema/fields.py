"""Schema field metadata helpers.

Provides ``field()`` and ``foreign_key()`` — thin wrappers that attach
JsonpySql-specific metadata to Pydantic ``FieldInfo`` objects so that
the schema engine can inspect them at collection registration time.

Usage example::

    from pydantic import BaseModel
    from jsonpysql import field, foreign_key

    class Customer(BaseModel):
        id: str = field(primary_key=True)
        email: str = field(unique=True, index=True)
        name: str = field()

    class Order(BaseModel):
        id: str = field(primary_key=True)
        customer_id: str = foreign_key(Customer, on_delete="cascade")
        total: float = field()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import fields as _pydantic_fields
from pydantic.fields import FieldInfo

OnDelete = Literal["restrict", "cascade", "set_null"]

# Metadata key stored inside Pydantic FieldInfo.metadata
_JSONPYSQL_META_KEY = "__jsonpysql__"


@dataclass(frozen=True)
class FieldMeta:
    """JsonpySql-specific metadata attached to a Pydantic field.

    Attributes:
        primary_key: Marks this field as the collection's primary key.
        index: Create a storage-level index on this field.
        unique: Enforce a unique constraint on this field (implies index).
        nullable: Allow ``None`` as a value for this field.
    """

    primary_key: bool = False
    index: bool = False
    unique: bool = False
    nullable: bool = False


@dataclass(frozen=True)
class ForeignKeyMeta:
    """Foreign-key metadata attached to a Pydantic field.

    Attributes:
        target_model: The Pydantic ``BaseModel`` class this field references.
        on_delete: Action taken when the referenced parent document is
            deleted.  One of ``"restrict"``, ``"cascade"``, or
            ``"set_null"``.
    """

    target_model: type
    on_delete: OnDelete


def field(
    *,
    primary_key: bool = False,
    index: bool = False,
    unique: bool = False,
    nullable: bool = False,
    **kwargs: Any,
) -> Any:
    """Return a Pydantic ``FieldInfo`` annotated with JsonpySql metadata.

    Drop-in replacement for ``pydantic.Field()`` that accepts extra
    JsonpySql-specific keyword arguments.  Any extra ``**kwargs`` are
    forwarded to ``pydantic.Field()``.

    Args:
        primary_key: Mark this field as the primary key.
        index: Create a storage-level index.
        unique: Enforce uniqueness (implies an index is created).
        nullable: Allow ``None`` values.
        **kwargs: Forwarded to ``pydantic.Field()``.

    Returns:
        A ``FieldInfo`` instance with ``FieldMeta`` embedded in its
        ``metadata`` list.
    """
    meta = FieldMeta(
        primary_key=primary_key,
        index=index,
        unique=unique,
        nullable=nullable,
    )
    info: FieldInfo = _pydantic_fields.Field(**kwargs)
    info.metadata.append(meta)
    return info


def foreign_key(
    target_model: type,
    on_delete: OnDelete = "restrict",
    **kwargs: Any,
) -> Any:
    """Return a Pydantic ``FieldInfo`` annotated with foreign-key metadata.

    Args:
        target_model: The Pydantic ``BaseModel`` subclass this field
            references.
        on_delete: Behaviour when the referenced parent document is deleted.
            ``"restrict"`` (default) — raise ``ReferentialIntegrityError``.
            ``"cascade"`` — delete all child documents.
            ``"set_null"`` — set this field to ``None`` on all children.
        **kwargs: Forwarded to ``pydantic.Field()``.

    Returns:
        A ``FieldInfo`` instance with ``ForeignKeyMeta`` embedded in its
        ``metadata`` list.
    """
    fk_meta = ForeignKeyMeta(target_model=target_model, on_delete=on_delete)
    info: FieldInfo = _pydantic_fields.Field(**kwargs)
    info.metadata.append(fk_meta)
    return info


# ---------------------------------------------------------------------------
# Introspection helpers used by the schema engine
# ---------------------------------------------------------------------------


def get_field_meta(info: FieldInfo) -> FieldMeta | None:
    """Extract ``FieldMeta`` from a Pydantic ``FieldInfo``, or ``None``.

    Args:
        info: The ``FieldInfo`` to inspect.

    Returns:
        The ``FieldMeta`` instance, or ``None`` if the field was not
        declared with ``jsonpysql.field()``.
    """
    for item in info.metadata:
        if isinstance(item, FieldMeta):
            return item
    return None


def get_fk_meta(info: FieldInfo) -> ForeignKeyMeta | None:
    """Extract ``ForeignKeyMeta`` from a Pydantic ``FieldInfo``, or ``None``.

    Args:
        info: The ``FieldInfo`` to inspect.

    Returns:
        The ``ForeignKeyMeta`` instance, or ``None`` if the field was not
        declared with ``jsonpysql.foreign_key()``.
    """
    for item in info.metadata:
        if isinstance(item, ForeignKeyMeta):
            return item
    return None


def get_primary_key_field(model: type) -> str | None:
    """Return the name of the primary-key field of *model*, or ``None``.

    Resolution order:

    1. A field explicitly marked ``field(primary_key=True)``.
    2. Convention fallback: a field literally named ``"id"``.

    The convention fallback keeps ``insert()`` (which keys the stored
    document by its primary key) and ``update(doc_id, ...)`` (which keys
    by the caller-supplied id) in agreement.  Without it, a model that has
    an ``id`` field but no declared primary key would have inserts keyed by
    a random UUID while updates key by ``id`` — silently creating duplicate
    rows on update.

    Args:
        model: A Pydantic ``BaseModel`` subclass.

    Returns:
        The primary-key field name, or ``None`` if the model has neither a
        ``primary_key=True`` field nor a field named ``"id"``.
    """
    for name, info in model.model_fields.items():
        meta = get_field_meta(info)
        if meta and meta.primary_key:
            return name
    if "id" in model.model_fields:
        return "id"
    return None


def get_indexed_fields(model: type) -> list[str]:
    """Return field names that should have storage-level indexes.

    Includes fields declared with ``index=True`` or ``unique=True``.

    Args:
        model: A Pydantic ``BaseModel`` subclass.

    Returns:
        List of field names that require an index.
    """
    result: list[str] = []
    for name, info in model.model_fields.items():
        meta = get_field_meta(info)
        if meta and (meta.index or meta.unique):
            result.append(name)
    return result


def get_unique_fields(model: type) -> list[str]:
    """Return field names with a unique constraint.

    Args:
        model: A Pydantic ``BaseModel`` subclass.

    Returns:
        List of field names declared with ``unique=True``.
    """
    result: list[str] = []
    for name, info in model.model_fields.items():
        meta = get_field_meta(info)
        if meta and meta.unique:
            result.append(name)
    return result


def get_foreign_keys(model: type) -> dict[str, ForeignKeyMeta]:
    """Return a mapping of field name → ``ForeignKeyMeta`` for all FK fields.

    Args:
        model: A Pydantic ``BaseModel`` subclass.

    Returns:
        Dict mapping field name to its ``ForeignKeyMeta``.
    """
    result: dict[str, ForeignKeyMeta] = {}
    for name, info in model.model_fields.items():
        fk = get_fk_meta(info)
        if fk is not None:
            result[name] = fk
    return result
