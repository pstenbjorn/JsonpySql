---
name: jsonpysql-architecture
description: Authoritative architectural reference for the JsonpySql project. Use this skill whenever working on any part of the JsonpySql codebase — including storage engine, schema engine, query engine, tests, or public API. Must be consulted before making any design decision, choosing a dependency, adding a new module, or resolving an ambiguity about behavior. Also use when a Claude Code session resumes mid-project to re-establish full architectural context before continuing.
---

# JsonpySql Architecture Skill

## Purpose

This skill is the single source of truth for all architectural decisions in the JsonpySql project. Before writing any code, consult the relevant section here. If a situation arises that is not covered here, raise it explicitly rather than making an assumption.

---

## Project Identity

| Item | Value |
|---|---|
| Project name | JsonpySql |
| Package name | `jsonpysql` |
| Python target | 3.11+ |
| Dependencies | `pydantic>=2.0`, `sortedcontainers`, `dill`, `msgpack` (optional/deferred), `pytest`, `pytest-cov` |
| Database model | Embedded, no server process. One database = one directory on the file system. |
| Primary differentiator | Python callables as native database functions/procedures; Python as the query language |

---

## Three-Layer Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Public API                         │
│         Database / Collection / Transaction          │
├─────────────────────────────────────────────────────┤
│                  Query Engine                        │
│   QueryBuilder → QueryPlanner → QueryExecutor        │
│              FunctionRegistry                        │
├─────────────────────────────────────────────────────┤
│                  Schema Engine                       │
│   SchemaRegistry / Validator / ConstraintChecker     │
│         CollectionManager / FunctionRegistry         │
├─────────────────────────────────────────────────────┤
│                  Storage Engine                      │
│   DocumentStore (JSONL) / IndexManager (SortedDict)  │
│         WAL / LockManager / CompactionManager        │
└─────────────────────────────────────────────────────┘
              ↓ File System ↓
    database/
    ├── manifest.json
    ├── {collection}.jsonl
    ├── {collection}.{field}.idx
    ├── {collection}.{fk_field}.ridx
    ├── {collection}.wal               ← present only during active transaction
    └── functions.pkl
```

**Build order is strict.** Storage engine must be fully built and tested before schema engine begins. Schema engine must be fully built and tested before query engine begins.

---

## Project Structure

```
jsonpysql/
├── __init__.py
├── database.py
├── exceptions.py
├── storage/
│   ├── __init__.py
│   ├── engine.py
│   ├── document_store.py
│   ├── index_manager.py
│   ├── wal.py
│   ├── lock_manager.py
│   └── models.py
├── schema/
│   ├── __init__.py
│   ├── collection.py
│   ├── registry.py
│   ├── fields.py
│   ├── validator.py
│   ├── constraints.py
│   └── functions.py
├── query/
│   ├── __init__.py
│   ├── builder.py
│   ├── planner.py
│   ├── executor.py
│   ├── aggregates.py
│   └── models.py
└── utils/
    ├── __init__.py
    └── platform.py

tests/
├── conftest.py
├── fixtures/
│   └── sample_schemas.py
├── unit/
│   ├── storage/
│   ├── schema/
│   └── query/
└── integration/
```

---

## Layer 1: Storage Engine

### File Layout (per database directory)

| File | Purpose |
|---|---|
| `manifest.json` | Collection registry and metadata |
| `{collection}.jsonl` | JSONL document store, one per collection |
| `{collection}.{field}.idx` | Persisted SortedDict index (JSON) |
| `{collection}.{fk_field}.ridx` | Reverse index for FK cascade (JSON) |
| `{collection}.wal` | Write-ahead log — present only during active transaction |
| `functions.pkl` | dill-serialized registered functions and procedures |

### Document Store (JSONL)

- One JSON document per line, newline-delimited.
- Writes are **append-only**. Deletes write a tombstone: `{"_deleted": true, "_id": "<id>"}`.
- **Never rewrite the file during normal operations.** Rewrite only during compaction.
- Compaction triggers: deleted ratio > 20%, or explicit `db.compact(collection)` call.
- Compaction acquires exclusive write lock for duration.

### Index Implementation

- In-memory: `sortedcontainers.SortedDict`
- Single-field: `{ field_value: [doc_id, ...] }`
- Compound: `{ (field1_value, field2_value): [doc_id, ...] }`
- Reverse (`.ridx`): `{ parent_id: [child_doc_id, ...] }`
- Persisted to `.idx` / `.ridx` file as JSON on **every write**.
- Rebuilt from `.jsonl` full scan if index file missing or corrupt on open.
- Performance: O(log n) equality, O(log n + k) range.

### Write-Ahead Log (WAL)

- Activated only for multi-document transactions. Single-document writes bypass WAL.
- WAL entry format (one JSON line per operation):
  ```json
  {"op": "insert|update|delete", "collection": "name", "doc_id": "id", "document": {} }
  ```
- On commit: apply all entries → delete WAL file.
- On rollback: delete WAL file without applying.
- On database open: if any `.wal` exists, replay before accepting operations.
- Replay is idempotent (keyed on `doc_id`).

### Concurrency

- File-level locking: `fcntl` (Unix), `msvcrt` (Windows). Platform detection in `utils/platform.py`.
- Writers: exclusive lock on `.jsonl`. Readers: shared lock.
- Lock timeout: configurable, default 5 seconds. Raises `LockTimeoutError` on timeout.
- **Not suitable for multi-process high-write-throughput.** Designed for single-application embedded use.

### StorageEngine Interface

```python
class StorageEngine:
    def __init__(self, db_path: Path) -> None
    def create_collection(self, name: str, indexes: list[IndexSpec]) -> None
    def drop_collection(self, name: str) -> None
    def insert(self, collection: str, doc_id: str, document: dict) -> None
    def update(self, collection: str, doc_id: str, document: dict) -> None
    def delete(self, collection: str, doc_id: str) -> None
    def get(self, collection: str, doc_id: str) -> dict | None
    def scan(self, collection: str) -> Iterator[dict]
    def lookup(self, collection: str, field: str, value: Any) -> Iterator[str]
    def range_scan(self, collection: str, field: str, low: Any, high: Any) -> Iterator[str]
    def begin_transaction(self) -> Transaction
    def compact(self, collection: str) -> None
    def get_stats(self, collection: str) -> CollectionStats

class Transaction:
    def insert(self, collection: str, doc_id: str, document: dict) -> None
    def update(self, collection: str, doc_id: str, document: dict) -> None
    def delete(self, collection: str, doc_id: str) -> None
    def commit(self) -> None
    def rollback(self) -> None
    def __enter__(self) -> Transaction
    def __exit__(self, exc_type, exc_val, exc_tb) -> None
```

---

## Layer 2: Schema Engine

### Schema Definition

- Pydantic v2 `BaseModel` subclasses. No custom DSL.
- `jsonpysql.field()` — wraps `pydantic.Field()` with parameters:
  - `primary_key: bool`
  - `index: bool`
  - `unique: bool`
  - `nullable: bool`
- `jsonpysql.foreign_key(target_model, on_delete)` — FK declaration.
  - `on_delete`: `'restrict'` | `'cascade'` | `'set_null'`

### Collection Types

**`SchemaCollection[T: BaseModel]`:**
- Validates all inserts and updates via Pydantic.
- Enforces unique constraints, FK constraints.
- Raises `ValidationError`, `UniqueConstraintError`, `ReferentialIntegrityError`.

**`DocumentCollection`:**
- Accepts any JSON-serializable `dict`.
- Optional index declarations at registration.
- No schema validation, no FK enforcement.
- Identical query interface to `SchemaCollection`.

### Referential Integrity

- **On child insert/update:** Indexed lookup in parent collection. O(log n). Raises `ReferentialIntegrityError` if not found.
- **On parent delete:**
  - `restrict`: if reverse index has children → `ReferentialIntegrityError`, abort.
  - `cascade`: delete all children via WAL-protected transaction.
  - `set_null`: update all child FK fields to `null` via WAL-protected transaction.

### Function Registry

- `@db.function` — pure callable, no side effects, callable in `.select()` projections.
- `@db.procedure` — receives `db_context` as first arg, may span multiple collections, implicit transaction.
- Both serialized via `dill` to `functions.pkl` on registration.
- Deserialized and re-registered on `Database()` open.
- Accessed in queries via `db.fn.{name}(args)`.

**Why `dill` over `pickle`:** `dill` serializes closures, lambdas, and locally-defined functions. `pickle` cannot.

---

## Layer 3: Query Engine

### QueryBuilder (fluent chaining)

```python
db.{collection}
  .where(predicate: Callable)        # chainable, AND semantics
  .join(collection, on: Callable)    # two-collection join
  .order_by(field: str, descending: bool = False)
  .limit(n: int)
  .skip(n: int)
  .select(projection: Callable)      # db.fn.x() callable here

# Aggregation
db.{collection}
  .group_by(*fields: str)
  .aggregate(**named_aggregates)     # db.count(), db.sum(f), db.avg(f), db.min(f), db.max(f)
```

### Materialization

```python
query.to_list()           # list[dict]
query.to_dict(key='id')   # dict[str, dict]
query.first()             # dict | None
query.count()             # int
for doc in query: ...     # lazy streaming (default)
```

### Query Planner (rule-based)

Uses `ast` + `inspect.getsource` to inspect lambda predicates.

**Priority order:**
1. Equality predicate on indexed field → index lookup, O(log n)
2. Range predicate on indexed field → range scan, O(log n + k)
3. No usable index → full collection scan

Complex predicates that cannot be parsed fall through to full scan **without error**. This is documented, expected behavior.

`.explain()` returns a `QueryPlan` object for debugging.

### Join Strategies

- Default: nested loop join.
- Hash join: when smaller collection fits in memory (threshold: configurable, default 10,000 docs).

---

## Public API (`database.py`)

```python
class Database:
    def __init__(self, path: str | Path, lock_timeout: float = 5.0) -> None
    def register_collection(self, name: str, model: type[BaseModel] | None = None,
                            indexes: list[str] | None = None) -> None
    def drop_collection(self, name: str) -> None
    def __getattr__(self, name: str) -> SchemaCollection | DocumentCollection
    def transaction(self) -> Transaction         # context manager
    def compact(self, collection: str | None = None) -> None  # None = all collections
    def function(self, fn: Callable) -> Callable
    def procedure(self, fn: Callable) -> Callable
    @property
    def fn(self) -> FunctionRegistry
    def stats(self) -> DatabaseStats
    def close(self) -> None
```

---

## Exception Hierarchy (`jsonpysql.exceptions`)

```
JsonpySqlError (base)
├── StorageError
│   ├── LockTimeoutError
│   ├── CompactionError
│   └── WALReplayError
├── SchemaError
│   ├── ValidationError
│   ├── UniqueConstraintError
│   └── ReferentialIntegrityError
├── QueryError
│   └── JoinError
└── FunctionError
```

**Rule:** No raw Python built-in exceptions leak through the public API surface. All public methods raise only from this hierarchy.

---

## Build Sequence

Enforce this order strictly. Do not begin a step until all tests for the previous step pass at the required coverage level.

| Step | Module | Gate |
|---|---|---|
| 1 | `exceptions.py` | — |
| 2 | `storage/models.py` | — |
| 3 | `storage/lock_manager.py` | tests pass |
| 4 | `storage/document_store.py` | tests pass |
| 5 | `storage/index_manager.py` | tests pass |
| 6 | `storage/wal.py` | tests pass |
| 7 | `storage/engine.py` | integration tests, 95% coverage |
| 8 | Compaction | tests pass |
| 9 | `schema/fields.py` | — |
| 10 | `schema/validator.py` | tests pass |
| 11 | `schema/constraints.py` | tests pass |
| 12 | `schema/functions.py` | tests pass |
| 13 | `schema/collection.py` | tests pass |
| 14 | `schema/registry.py` | 95% coverage |
| 15 | `query/models.py` | — |
| 16 | `query/planner.py` | tests pass |
| 17 | `query/aggregates.py` | tests pass |
| 18 | `query/executor.py` | tests pass |
| 19 | `query/builder.py` | tests pass |
| 20 | `database.py` | full integration tests |
| 21 | `__init__.py` | clean exports, final coverage check |

---

## Coding Conventions

- **Type hints on every function and method signature.** No exceptions.
- **Docstrings on every public class and method.** Google style.
- **No bare `except` clauses.** Catch specific exception types only.
- **All internal paths are `pathlib.Path`.** Strings accepted at public API boundary and converted immediately.
- **`drop_if_exists=False` semantics:** `create_collection` raises `CollectionExistsError` unless `drop_if_exists=True` is passed.
- **All test files use `pytest tmp_path` fixture.** No shared file state between tests.
- **`pytest-cov` enforced.** Storage and schema layers must maintain 95% coverage minimum.
- **`pyproject.toml` for package definition.** No `setup.py`.
- **CI:** `.github/workflows/ci.yml` runs `pytest` on push and PR.

---

## Test Conventions

- Storage engine tests built and passing before any schema engine work begins.
- Crash recovery tests: truncate WAL mid-write, reopen database, assert correct state.
- Parametrize index tests across: single-field indexed, compound-indexed, no-index (full scan fallback).
- Integration tests use real file system — no mocking of the storage engine.
- Canonical test fixture: `Customer` / `Order` schema relationship used throughout all layers.
- No mocking of `StorageEngine` in schema or query integration tests.

---

## Scope Boundary: What Is NOT in v0.1

Do not implement, scaffold, or partially build any of the following. If a question arises about them, defer explicitly.

- Single binary file format
- MessagePack serialization (interface exists; implementation deferred)
- Cost-based query optimizer
- Graph traversal / multi-hop joins
- Multi-process / networked server mode
- Replication or sharding
- Full-text search
- `ALTER COLLECTION` / schema migration tooling
- CLI tooling / REPL
- `asyncio` support

---

## Resuming a Session Mid-Project

When resuming in a new Claude Code session:

1. Read this skill file completely.
2. Read `DECISIONS.md` in the repo root for detailed rationale on any decision.
3. Identify the current build sequence step by checking which test files exist and pass.
4. Do not proceed to the next step until the current step's tests pass at the required coverage level.
5. State the current step explicitly before writing any code.
