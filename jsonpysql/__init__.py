"""JsonpySql — Python-native embedded database.

Public API surface:

- ``Database`` — main entry point.
- ``field`` — Pydantic field with JsonpySql metadata (primary_key,
  index, unique, nullable).
- ``foreign_key`` — FK field declaration.
- Exception classes from ``jsonpysql.exceptions``.
- Aggregate helpers: ``count``, ``sum_``, ``avg``, ``min_``, ``max_``.
"""

from jsonpysql.database import Database
from jsonpysql.exceptions import (
    CollectionExistsError,
    CompactionError,
    FunctionError,
    JoinError,
    JsonpySqlError,
    LockTimeoutError,
    QueryError,
    ReferentialIntegrityError,
    SchemaError,
    StorageError,
    UniqueConstraintError,
    ValidationError,
    WALReplayError,
)
from jsonpysql.query.models import AvgAgg, CountAgg, MaxAgg, MinAgg, SumAgg
from jsonpysql.schema.fields import field, foreign_key

__all__ = [
    # Public API
    "Database",
    # Schema helpers
    "field",
    "foreign_key",
    # Exceptions
    "JsonpySqlError",
    "StorageError",
    "LockTimeoutError",
    "CompactionError",
    "WALReplayError",
    "SchemaError",
    "ValidationError",
    "UniqueConstraintError",
    "ReferentialIntegrityError",
    "CollectionExistsError",
    "QueryError",
    "JoinError",
    "FunctionError",
    # Aggregates
    "CountAgg",
    "SumAgg",
    "AvgAgg",
    "MinAgg",
    "MaxAgg",
]
