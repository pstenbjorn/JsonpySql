"""Pydantic-backed document validation for the schema engine.

``Validator`` wraps a Pydantic ``BaseModel`` subclass and provides a single
``validate()`` method that converts a raw ``dict`` into a validated,
serialisable ``dict`` (by round-tripping through the model).  Any Pydantic
validation failure is translated into a ``jsonpysql.ValidationError``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError as _PydanticValidationError

from jsonpysql.exceptions import ValidationError


class Validator:
    """Validates documents against a Pydantic model.

    Args:
        model: A ``BaseModel`` subclass that defines the document schema.
    """

    def __init__(self, model: type[BaseModel]) -> None:
        self._model = model

    @property
    def model(self) -> type[BaseModel]:
        """The underlying Pydantic model class."""
        return self._model

    def validate(self, document: dict[str, Any]) -> dict[str, Any]:
        """Validate and coerce *document* against the model.

        The document is parsed by the model (which performs type coercion
        and constraint checking) and then serialised back to a plain dict
        via ``model_dump(mode="json")``.  JSON mode ensures non-native
        types (``datetime`` → ISO string, ``Enum`` → value, ``UUID`` →
        str, ``Decimal`` → str) become JSON-serialisable, matching the
        storage layer's "accepts any JSON-serializable dict" contract.

        Args:
            document: Raw key-value mapping to validate.

        Returns:
            A validated, JSON-native dict.

        Raises:
            ValidationError: If the document does not conform to the model.
        """
        try:
            instance = self._model.model_validate(document)
            return instance.model_dump(mode="json")
        except _PydanticValidationError as exc:
            raise ValidationError(
                f"Document failed validation against {self._model.__name__}: {exc}"
            ) from exc

