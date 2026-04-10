# JsonpySql — Architectural Decision Record

**Project:** JsonpySql  
**Package:** `jsonpysql`  
**Python Target:** 3.11+  
**Document Status:** Authoritative. All implementation decisions trace back here.  
**Last Updated:** 2026-04-10

---

## Context

JsonpySql is a Python-native embedded database engine. It stores all data as JSON objects on disk, supports both schema-enforced relational collections and schema-free document collections, enforces referential integrity, and uses Python callables as native database functions and stored procedures. There is no external server process. A database is a directory on the file system.

**Design motivation:** The gap between TinyDB (too simple — no indexes, no schema, no referential integrity) and PostgreSQL/SurrealDB (too heavy for small container apps) is real. For applications needing 5–10 tables, custom functions, basic referential integrity, and zero infrastructure cost, no clean current solution exists. JsonpySql targets exactly that use case.

**Closest existing analogs reviewed:**
- **SurrealDB** — most architecturally similar; multi-model, embedded option, open source. JsonpySql's differentiation is Python-native query language and Python callables as first-class database objects.
- **TinyDB** — pure Python JSON store, widely used, but deliberately simple. JsonpySql targets the gap above it.
- **DuckDB** — embeddable, Python-native, excellent for OLAP. RDBMS model, SQL query language, not the target.
- **ArangoDB** — mature multi-model, but server-required and its own query language.

---

## ADR-001: Database File Layout

**Decision:** Directory-per-database with file-per-collection.

**Layout:**
```
{database_name}/
├── manifest.json                    ← collection registry and metadata
├── {collection}.jsonl               ← one JSONL document store per collection
├── {collection}.{field}.idx         ← persisted SortedDict index (JSON format)
├── {collection}.{fk_field}.ridx     ← reverse index for FK cascade (JSON format)
├── {collection}.wal                 ← WAL file (present only during active transaction)
└── functions.pkl                    ← dill-serialized registered functions
```

**Alternatives considered:**
- Single binary file (SQLite model): Best distribution story, hardest to implement, deferrable.
- Hybrid manifest + collection files: Chosen model.
- Single flat directory with all files: Same as chosen but less explicit manifest separation.

**Rationale:** Debuggable without tooling — any text editor can inspect a `.jsonl` or `.idx` file. Trivial backup (zip the directory). Enables per-collection locking. Refactoring to single-file format is a well-scoped future migration.

**Deferred:** Single binary file format deferred to v0.2+.

---

## ADR-002: Storage Format

**Decision:** JSONL (JSON Lines) as the primary document store format, with MessagePack as a pluggable option behind a serializer interface.

**JSONL characteristics:**
- One JSON document per line, newline-delimited.
- Writes are append-only. Deleted documents are tombstoned: `{"_deleted": true, "_id": "<id>"}`.
- File is never rewritten on individual operations; rewritten only during compaction.

**Compaction triggers:**
- Deleted document ratio exceeds 20% of total lines, OR
- Explicit `db.compact(collection)` call.

**Compaction behavior:** Rewrites the `.jsonl` file in place, omitting tombstoned documents. Updates in-memory indexes. Acquires exclusive write lock for duration.

**Alternatives considered:**
- Plain JSON (single array): Requires whole-file rewrite on every mutation. Rejected.
- MessagePack (binary): ~40% smaller, faster parse. Not human-readable. Deferred as an option.
- Custom paged binary: Maximum performance. Months of implementation work. Rejected for MVP.

**Rationale:** Append-only writes are OS-level atomic for small payloads. JSONL supports sequential scan natively. Still human-readable and debuggable. MessagePack swap-in is a single-line change behind the serializer interface when needed.

---

## ADR-003: Index Implementation

**Decision:** `sortedcontainers.SortedDict` as the in-memory index structure, persisted to `.idx` files as JSON.

**Index structure:**
```python
# Single-field index
{ field_value: [doc_id, doc_id, ...] }

# Compound index
{ (field1_value, field2_value): [doc_id, ...] }
```

**Performance characteristics:**
- Equality lookup: O(log n)
- Range scan: O(log n + k) where k is result set size
- Insert/delete: O(log n)

**Persistence model:**
- Index written to `.idx` file on every write operation.
- On database open: index loaded from `.idx` file.
- If `.idx` file is missing or fails validation: index rebuilt by full scan of `.jsonl` file.

**Reverse index (`.ridx`):**
- Maintained for every foreign key field.
- Structure: `{ parent_id: [child_doc_id, ...] }`
- Used for cascade delete and set_null operations without full collection scan.

**Alternatives considered:**
- B-tree from scratch: Weeks of implementation work. Rejected for MVP.
- Simple dict with sort-on-query: O(n) range scans. Rejected.
- SQLite as index store: Introduces the dependency being avoided. Rejected.

**Rationale:** `sortedcontainers` is production-tested (used in CPython itself), pure Python, and gives B-tree-equivalent performance for the target data sizes without implementation complexity.

---

## ADR-004: Write Model and Crash Safety

**Decision:** Append-only single-document writes with Write-Ahead Log (WAL) for multi-document transactions.

**Single-document write path:**
1. Append new/updated document to end of `.jsonl` file.
2. Update in-memory index.
3. Persist index to `.idx` file.

**Multi-document transaction write path (WAL):**
1. Write all intended operations to `{collection}.wal` as JSON lines.
   - Entry format: `{"op": "insert|update|delete", "collection": str, "doc_id": str, "document": dict|null}`
2. Apply each WAL entry sequentially to the document store and indexes.
3. Delete WAL file on commit.
4. On rollback: delete WAL file without applying entries.

**Crash recovery:**
- On database open: if any `.wal` file exists, replay all entries before accepting new operations.
- WAL replay is idempotent — at-least-once semantics are safe because each operation is identified by `doc_id`.

**Documented consistency guarantee:**
> Single-document writes are atomic at the OS level (append atomicity for payloads under 4KB on all supported platforms). Multi-document transactions are durable via WAL with at-least-once replay semantics. JsonpySql does not provide full ACID isolation; concurrent readers may observe partial transaction state.

**Rationale:** Full ACID is a multi-month engineering effort inappropriate for MVP scope. WAL provides crash safety for the most critical scenario (multi-document atomicity) with minimal implementation complexity.

---

## ADR-005: Concurrency Model

**Decision:** Single-writer, multiple-reader file locking via `fcntl` (Unix) and `msvcrt` (Windows).

**Lock semantics:**
- Writers acquire an exclusive lock on the collection `.jsonl` file.
- Readers acquire a shared lock on the collection `.jsonl` file.
- Lock acquisition timeout: configurable at `Database()` construction, default 5 seconds.
- Timeout raises `LockTimeoutError`.

**Platform detection:** `utils/platform.py` wraps both `fcntl` and `msvcrt` behind a common `FileLock` interface.

**Documented limitation:**
> JsonpySql is not suitable for multi-process high-write-throughput workloads. It is designed for single-application embedded use where write concurrency is low. This is the SQLite concurrency model applied to JSONL files.

**Rationale:** The GIL prevents true thread-level parallelism on CPU-bound work. File-level locking is sufficient for the target use case (small container apps, single application process). More sophisticated concurrency is deferred.

---

## ADR-006: Schema Definition Language

**Decision:** Pydantic v2 `BaseModel` subclasses as the native schema definition mechanism. No custom DSL.

**Custom metadata helpers:**
- `jsonpysql.field()`: wraps Pydantic `Field()` with additional parameters:
  - `primary_key: bool` — designates the primary key field
  - `index: bool` — registers an index for this field at collection creation
  - `unique: bool` — enforces uniqueness constraint on write
  - `nullable: bool` — allows null/None values (default False for schema collections)
- `jsonpysql.foreign_key(target_model, on_delete)`: declares a FK constraint
  - `on_delete` options: `'restrict'` | `'cascade'` | `'set_null'`

**Example:**
```python
from pydantic import BaseModel
from jsonpysql import field, foreign_key

class Customer(BaseModel):
    id: str = field(primary_key=True)
    name: str
    email: str = field(unique=True, index=True)
    region: str = field(index=True)

class Order(BaseModel):
    id: str = field(primary_key=True)
    customer_id: str = foreign_key(Customer, on_delete='restrict')
    amount: float
    status: str = field(index=True, default='pending')
```

**Rationale:** Pydantic v2 provides type validation, JSON serialization, and field metadata for free. It is familiar to the entire Python ecosystem. Avoids designing and maintaining a custom DSL.

---

## ADR-007: Referential Integrity Implementation

**Decision:** Indexed lookup for existence checks on write; reverse index for cascade and set_null operations.

**On insert/update of a child document:**
1. Extract FK field value.
2. Perform indexed lookup in parent collection for that value.
3. If not found: raise `ReferentialIntegrityError`.

**On delete of a parent document:**
1. Look up reverse index (`.ridx`) to find all dependent child document IDs.
2. Apply `on_delete` behavior:
   - `restrict`: if any children exist, raise `ReferentialIntegrityError`. Abort delete.
   - `cascade`: delete all child documents within the same transaction (WAL-protected).
   - `set_null`: update all child FK fields to `null` within the same transaction.

**Performance:**
- Existence check: O(log n) — one index lookup.
- Cascade/set_null: O(k) where k is number of children — bounded by reverse index lookup.

**Rationale:** Loading the full parent document on every child write is unnecessary; index lookup gives existence confirmation at O(log n). Maintaining a reverse index makes cascade and set_null operations efficient without full collection scans.

---

## ADR-008: Collection Types

**Decision:** Two distinct collection types sharing the same storage engine.

**`SchemaCollection[T: BaseModel]`:**
- Enforces schema via Pydantic validation on all inserts and updates.
- Enforces unique constraints.
- Enforces foreign key constraints.
- Raises typed exceptions: `ValidationError`, `UniqueConstraintError`, `ReferentialIntegrityError`.

**`DocumentCollection`:**
- Accepts any JSON-serializable `dict`.
- Optional index declarations passed at registration time.
- No schema validation; no FK enforcement.
- Identical query interface to `SchemaCollection`.

**Cross-collection queries:** Fully supported. The query engine operates on Python dicts and does not distinguish collection type.

---

## ADR-009: Function and Procedure Registry

**Decision:** Python callables registered via decorators, serialized to disk via `dill`, callable from within queries.

**`@db.function`:**
- Registers a pure Python callable as a named database function.
- No side effects; receives arguments, returns a value.
- Callable inside `.select()` projections via `db.fn.{function_name}(args)`.

**`@db.procedure`:**
- Registers a Python callable that receives a `db_context` as its first argument.
- May perform multi-collection operations within an implicit transaction.
- Not callable from within a query projection; invoked directly as `db.fn.{procedure_name}(args)`.

**Serialization:**
- All registered functions and procedures serialized via `dill` to `functions.pkl` on registration.
- Deserialized and re-registered on `Database()` open.

**Rationale:** Python-as-the-query-language is JsonpySql's primary differentiator. Registered functions eliminate the impedance mismatch that custom SQL UDFs introduce. `dill` is used over `pickle` because `dill` can serialize closures, lambdas, and locally-defined functions.

---

## ADR-010: Query API Design

**Decision:** Fluent method-chaining with lambda predicates and a rule-based query planner that inspects lambda AST for predicate pushdown.

**Query interface:**
```python
# Filtered query with index pushdown
results = db.orders \
    .where(lambda o: o.status == 'pending') \
    .where(lambda o: o.amount > 100) \
    .order_by('amount', descending=True) \
    .limit(50) \
    .select(lambda o: {'id': o.id, 'amount': o.amount})

# Two-collection join
results = db.query() \
    .from_(db.orders) \
    .join(db.customers, on=lambda o, c: o.customer_id == c.id) \
    .where(lambda o, c: c.region == 'East') \
    .select(lambda o, c: {'order_id': o.id, 'customer': c.name})

# Aggregation
summary = db.orders \
    .group_by('status') \
    .aggregate(
        total=db.sum('amount'),
        count=db.count(),
        avg=db.avg('amount')
    )

# Registered function in projection
results = db.orders \
    .where(lambda o: o.status == 'pending') \
    .select(lambda o: {
        'id': o.id,
        'tax': db.fn.calculate_tax(o.amount, o.region)
    })
```

**Rationale:** Pure generator/comprehension is most Pythonic but opaque to the query planner — an unexamined lambda is always a full scan. Fluent chaining exposes the query structure so the planner can inspect individual `.where()` predicates independently and push eligible ones to indexes.

---

## ADR-011: Query Planner

**Decision:** Rule-based planner using `ast` + `inspect.getsource` for lambda AST inspection. No cost-based optimization in MVP.

**Predicate evaluation rules (in priority order):**
1. Equality predicate on indexed field → index lookup, O(log n).
2. Range predicate on indexed field → range scan, O(log n + k).
3. No usable index detected → full collection scan (logged in query stats).

**Complex predicates:** Fall through to full scan without error or warning. This is documented behavior.

**Query plan inspection:** `.explain()` returns a `QueryPlan` object describing the chosen strategy for each predicate. Intended for debugging.

**Join strategies:**
- Default: nested loop join.
- Hash join: used when the smaller collection fits in memory. Threshold: configurable, default 10,000 documents.

**Rationale:** A full cost-based optimizer is a major engineering investment inappropriate for MVP. Rule-based pushdown captures the majority of real-world performance wins (equality and range filters on indexed fields). The planner degrades gracefully — falling through to full scan rather than erroring on complex predicates.

---

## ADR-012: Result Materialization Model

**Decision:** Lazy iterators by default; explicit materialization methods.

```python
# All of these are lazy — nothing executes until iteration
query = db.orders.where(lambda o: o.status == 'pending')

# Explicit materialization
results  = query.to_list()              # list[dict]
by_id    = query.to_dict(key='id')     # dict[str, dict]
first    = query.first()               # dict | None
n        = query.count()               # int

# Lazy iteration
for order in query:
    process(order)
```

**Rationale:** Lazy evaluation avoids loading full collection results into memory for large datasets. Materialization is always an explicit, named operation — there is no implicit full-load behavior.

---

## ADR-013: Error Hierarchy

**Decision:** Single base exception `JsonpySqlError` with typed subclasses per layer.

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

All exceptions defined in `jsonpysql.exceptions`. All public API methods raise only from this hierarchy — no raw Python built-in exceptions leak through the public surface.

---

## ADR-014: Build and Test Sequence

**Decision:** Strict bottom-up build order. No layer is started until the layer below it has 95%+ test coverage and all tests pass.

**Sequence:**
1. `exceptions.py`
2. `storage/models.py`
3. `storage/lock_manager.py` + tests
4. `storage/document_store.py` + tests
5. `storage/index_manager.py` + tests
6. `storage/wal.py` + tests
7. `storage/engine.py` + integration tests (full storage layer)
8. Compaction + tests
9. `schema/fields.py`
10. `schema/validator.py` + tests
11. `schema/constraints.py` + tests
12. `schema/functions.py` + tests
13. `schema/collection.py` + tests
14. `schema/registry.py` + tests
15. `query/models.py`
16. `query/planner.py` + tests
17. `query/aggregates.py` + tests
18. `query/executor.py` + tests
19. `query/builder.py` + tests
20. `database.py` + full integration tests
21. `__init__.py` — clean public exports last

**Test conventions:**
- `pytest` with `tmp_path` fixture for all file-based tests. No shared file state between tests.
- Storage and schema layer coverage gates: 95% minimum enforced by `pytest-cov`.
- Crash recovery tests simulate mid-write failure by truncating WAL files and reopening the database.
- Parametrize index tests across indexed, compound-indexed, and no-index paths.
- Integration tests use real file system — no storage engine mocking.
- Canonical test fixture: `Customer` / `Order` schema relationship used throughout.

---

## Deferred to v0.2+

The following are explicitly out of scope for v0.1 and should not be partially implemented:

- Single binary file format
- MessagePack serialization (interface exists, implementation deferred)
- Cost-based query optimizer
- Graph traversal / multi-hop joins
- Multi-process / networked server mode
- Replication or sharding
- Full-text search
- `ALTER COLLECTION` schema migration tooling (manual for MVP; document this explicitly)
- CLI tooling / REPL
- `asyncio` support
