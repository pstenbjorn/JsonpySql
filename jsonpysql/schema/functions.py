"""Function and procedure registry for the JsonpySql schema engine.

Registered callables are serialised via ``dill`` to ``functions.pkl`` so they
survive database re-opens.  On load the file is deserialised and all
functions/procedures are re-registered in memory.

Terminology
-----------
``@db.function``
    A pure callable with no side effects.  Callable inside ``.select()``
    query projections.

``@db.procedure``
    A callable that receives a ``db_context`` as its first argument,
    enabling multi-collection operations within an implicit transaction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import dill  # type: ignore[import-untyped]

from jsonpysql.exceptions import FunctionError, StorageError

_PKL_FILENAME = "functions.pkl"


class FunctionRegistry:
    """In-memory registry of named database functions and procedures.

    Functions and procedures are stored separately so the caller can
    distinguish them.  Both are serialised together in a single
    ``functions.pkl`` file.

    Args:
        db_path: Database directory.  The registry file is placed here.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._functions: dict[str, Callable[..., Any]] = {}
        self._procedures: dict[str, Callable[..., Any]] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_function(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register *fn* as a named database function and persist it.

        Args:
            fn: A callable with a ``__name__`` attribute.

        Returns:
            The same callable (enables use as a decorator).

        Raises:
            FunctionError: If serialisation fails.
        """
        self._functions[fn.__name__] = fn
        self._save()
        return fn

    def register_procedure(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Register *fn* as a named database procedure and persist it.

        Args:
            fn: A callable whose first parameter will receive the database
                context when called.

        Returns:
            The same callable (enables use as a decorator).

        Raises:
            FunctionError: If serialisation fails.
        """
        self._procedures[fn.__name__] = fn
        self._save()
        return fn

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_function(self, name: str) -> Callable[..., Any]:
        """Return the registered function named *name*.

        Args:
            name: Function name as registered.

        Returns:
            The callable.

        Raises:
            FunctionError: If no function with *name* is registered.
        """
        try:
            return self._functions[name]
        except KeyError as exc:
            raise FunctionError(f"No function registered with name {name!r}") from exc

    def get_procedure(self, name: str) -> Callable[..., Any]:
        """Return the registered procedure named *name*.

        Args:
            name: Procedure name as registered.

        Returns:
            The callable.

        Raises:
            FunctionError: If no procedure with *name* is registered.
        """
        try:
            return self._procedures[name]
        except KeyError as exc:
            raise FunctionError(f"No procedure registered with name {name!r}") from exc

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """Allow ``registry.my_function(args)`` syntax.

        Looks up *name* in functions first, then procedures.

        Args:
            name: Function or procedure name.

        Returns:
            The callable.

        Raises:
            FunctionError: If *name* is not registered.
        """
        # Avoid infinite recursion for dunder / private attributes.
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._functions:
            return self._functions[name]
        if name in self._procedures:
            return self._procedures[name]
        raise FunctionError(f"No function or procedure registered with name {name!r}")

    def list_functions(self) -> list[str]:
        """Return the names of all registered functions.

        Returns:
            Sorted list of function names.
        """
        return sorted(self._functions)

    def list_procedures(self) -> list[str]:
        """Return the names of all registered procedures.

        Returns:
            Sorted list of procedure names.
        """
        return sorted(self._procedures)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Deserialise functions and procedures from ``functions.pkl``.

        Silently does nothing when the file does not exist.

        Raises:
            FunctionError: If the file exists but cannot be deserialised.
        """
        path = self._pkl_path()
        if not path.exists():
            return
        try:
            with path.open("rb") as fh:
                data: dict[str, Any] = dill.load(fh)
            self._functions = data.get("functions", {})
            self._procedures = data.get("procedures", {})
        except Exception as exc:
            raise FunctionError(
                f"Cannot load function registry from {path}: {exc}"
            ) from exc

    def _save(self) -> None:
        """Serialise the registry to ``functions.pkl``.

        Raises:
            FunctionError: If serialisation or file write fails.
        """
        path = self._pkl_path()
        tmp = path.with_suffix(".pkl.tmp")
        data = {"functions": self._functions, "procedures": self._procedures}
        try:
            with tmp.open("wb") as fh:
                dill.dump(data, fh)
            tmp.replace(path)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise FunctionError(
                f"Cannot save function registry to {path}: {exc}"
            ) from exc

    def _pkl_path(self) -> Path:
        """Return the path to ``functions.pkl``."""
        return self._db_path / _PKL_FILENAME
