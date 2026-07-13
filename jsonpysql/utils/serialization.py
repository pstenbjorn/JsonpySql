"""JSON serialisation helpers for the storage layer.

The storage layer persists documents as JSONL.  ``json.dumps`` cannot
natively serialise several common Python types (``datetime``, ``date``,
``time``, ``Decimal``, ``UUID``).  ``json_default`` is the fallback
encoder passed as ``json.dumps(..., default=json_default)`` at every
write site.

``SchemaCollection`` documents are already JSON-native (``Validator``
dumps with ``mode="json"``), so this encoder primarily protects
``DocumentCollection`` inserts, which bypass Pydantic validation and may
contain raw ``datetime``/``Decimal``/``UUID`` values.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID


def json_default(obj: Any) -> Any:
    """Fallback encoder for ``json.dumps(default=...)``.

    Args:
        obj: A value that ``json.dumps`` cannot serialise natively.

    Returns:
        A JSON-native representation of *obj*.

    Raises:
        TypeError: If *obj* is of an unsupported type (preserving the
            standard ``json`` error contract).
    """
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        # Serialise as a string to preserve exactness; float() would lose
        # precision for values that are not exactly representable.
        return str(obj)
    if isinstance(obj, UUID):
        return str(obj)
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )
