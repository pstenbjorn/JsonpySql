"""Exception hierarchy for JsonpySql.

All exceptions raised through the public API surface are subclasses of
``JsonpySqlError``. No built-in Python exceptions leak through public methods.
"""


class JsonpySqlError(Exception):
    """Base class for all JsonpySql exceptions."""


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


class StorageError(JsonpySqlError):
    """Raised when a storage-layer operation fails."""


class LockTimeoutError(StorageError):
    """Raised when a file lock cannot be acquired within the timeout period."""


class CompactionError(StorageError):
    """Raised when a collection compaction operation fails."""


class WALReplayError(StorageError):
    """Raised when write-ahead log replay fails on database open."""


# ---------------------------------------------------------------------------
# Schema layer
# ---------------------------------------------------------------------------


class SchemaError(JsonpySqlError):
    """Raised when a schema-level constraint or validation rule is violated."""


class ValidationError(SchemaError):
    """Raised when a document fails Pydantic model validation."""


class UniqueConstraintError(SchemaError):
    """Raised when an insert or update would violate a unique field constraint."""


class ReferentialIntegrityError(SchemaError):
    """Raised when a foreign-key constraint is violated.

    Examples:
        Child insert referencing a non-existent parent.
        Parent delete with ``on_delete='restrict'`` and existing children.
    """


class CollectionExistsError(SchemaError):
    """Raised by ``register_collection`` when the collection already exists.

    Pass ``drop_if_exists=True`` to silently replace it instead.
    """


# ---------------------------------------------------------------------------
# Query layer
# ---------------------------------------------------------------------------


class QueryError(JsonpySqlError):
    """Raised when a query cannot be executed."""


class JoinError(QueryError):
    """Raised when a join operation fails."""


# ---------------------------------------------------------------------------
# Function / procedure layer
# ---------------------------------------------------------------------------


class FunctionError(JsonpySqlError):
    """Raised when a registered function or procedure raises an error."""
