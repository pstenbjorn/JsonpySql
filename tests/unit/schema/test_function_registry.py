"""Tests for schema/functions.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from jsonpysql.exceptions import FunctionError
from jsonpysql.schema.functions import FunctionRegistry


@pytest.fixture
def registry(tmp_path: Path) -> FunctionRegistry:
    return FunctionRegistry(tmp_path)


# ---------------------------------------------------------------------------
# Function registration
# ---------------------------------------------------------------------------


class TestFunctionRegistration:
    def test_register_and_call_function(self, registry: FunctionRegistry) -> None:
        @registry.register_function
        def double(x: int) -> int:
            return x * 2

        fn = registry.get_function("double")
        assert fn(5) == 10

    def test_register_returns_same_callable(self, registry: FunctionRegistry) -> None:
        def add(a: int, b: int) -> int:
            return a + b

        result = registry.register_function(add)
        assert result is add

    def test_get_unknown_function_raises(self, registry: FunctionRegistry) -> None:
        with pytest.raises(FunctionError):
            registry.get_function("nonexistent")

    def test_list_functions(self, registry: FunctionRegistry) -> None:
        registry.register_function(lambda: None)  # lambdas have __name__ == '<lambda>'
        assert "<lambda>" in registry.list_functions()

    def test_register_closure(self, registry: FunctionRegistry) -> None:
        factor = 3

        def triple(x: int) -> int:
            return x * factor

        registry.register_function(triple)
        assert registry.get_function("triple")(4) == 12


# ---------------------------------------------------------------------------
# Procedure registration
# ---------------------------------------------------------------------------


class TestProcedureRegistration:
    def test_register_and_call_procedure(self, registry: FunctionRegistry) -> None:
        @registry.register_procedure
        def my_proc(db_ctx: object, x: int) -> int:
            return x + 1

        proc = registry.get_procedure("my_proc")
        assert proc(None, 5) == 6

    def test_get_unknown_procedure_raises(self, registry: FunctionRegistry) -> None:
        with pytest.raises(FunctionError):
            registry.get_procedure("nonexistent")

    def test_list_procedures(self, registry: FunctionRegistry) -> None:
        def do_thing(ctx: object) -> None:
            pass

        registry.register_procedure(do_thing)
        assert "do_thing" in registry.list_procedures()


# ---------------------------------------------------------------------------
# __getattr__ shorthand
# ---------------------------------------------------------------------------


class TestGetAttr:
    def test_getattr_finds_function(self, registry: FunctionRegistry) -> None:
        def sq(x: int) -> int:
            return x ** 2

        registry.register_function(sq)
        assert registry.sq(3) == 9  # type: ignore[attr-defined]

    def test_getattr_finds_procedure(self, registry: FunctionRegistry) -> None:
        def proc(ctx: object) -> str:
            return "ok"

        registry.register_procedure(proc)
        assert registry.proc(None) == "ok"  # type: ignore[attr-defined]

    def test_getattr_unknown_raises_function_error(self, registry: FunctionRegistry) -> None:
        with pytest.raises(FunctionError):
            _ = registry.unknown_fn  # type: ignore[attr-defined]

    def test_getattr_private_raises_attribute_error(
        self, registry: FunctionRegistry
    ) -> None:
        with pytest.raises(AttributeError):
            _ = registry._nonexistent  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Persistence (save / load)
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_function_survives_reload(self, tmp_path: Path) -> None:
        r1 = FunctionRegistry(tmp_path)

        def greet(name: str) -> str:
            return f"Hello, {name}"

        r1.register_function(greet)

        r2 = FunctionRegistry(tmp_path)
        r2.load()
        assert r2.get_function("greet")("World") == "Hello, World"

    def test_procedure_survives_reload(self, tmp_path: Path) -> None:
        r1 = FunctionRegistry(tmp_path)

        def my_proc(ctx: object) -> int:
            return 42

        r1.register_procedure(my_proc)

        r2 = FunctionRegistry(tmp_path)
        r2.load()
        assert r2.get_procedure("my_proc")(None) == 42

    def test_load_missing_file_is_noop(self, registry: FunctionRegistry) -> None:
        registry.load()  # must not raise
        assert registry.list_functions() == []

    def test_load_corrupt_file_raises_function_error(self, tmp_path: Path) -> None:
        pkl = tmp_path / "functions.pkl"
        pkl.write_bytes(b"not-valid-dill-data")
        r = FunctionRegistry(tmp_path)
        with pytest.raises(FunctionError):
            r.load()

    def test_pkl_file_created_after_register(
        self, tmp_path: Path, registry: FunctionRegistry
    ) -> None:
        def noop() -> None:
            pass

        registry.register_function(noop)
        assert (tmp_path / "functions.pkl").exists()
