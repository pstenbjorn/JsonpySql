# JsonpySql

A Python-native embedded database with JSON storage and Python callables as first-class database objects. No server process, no configuration files — one directory on the file system is one database.

```python
from jsonpysql import Database, field
from pydantic import BaseModel

class Customer(BaseModel):
    id: str = field(primary_key=True)
    email: str = field(unique=True, index=True)
    name: str = field()

db = Database("my_db")
db.register_collection("customers", Customer)

db.customers.insert({"id": "c1", "email": "alice@example.com", "name": "Alice"})

results = db.customers.where(lambda c: c["email"] == "alice@example.com").to_list()
```

---

## Table of Contents

- [Installation](#installation)
- [Core Concepts](#core-concepts)
- [Defining a Schema](#defining-a-schema)
- [Opening a Database](#opening-a-database)
- [Collections](#collections)
  - [Schema Collections](#schema-collections)
  - [Document Collections](#document-collections)
- [CRUD Operations](#crud-operations)
- [Querying](#querying)
  - [Filtering](#filtering)
  - [Ordering](#ordering)
  - [Limit and Skip](#limit-and-skip)
  - [Projection](#projection)
  - [Joins](#joins)
  - [Aggregation](#aggregation)
  - [Materializing Results](#materializing-results)
  - [Query Plans](#query-plans)
- [Transactions](#transactions)
- [Functions and Procedures](#functions-and-procedures)
- [Compaction](#compaction)
- [Statistics](#statistics)
- [Exception Hierarchy](#exception-hierarchy)
- [Architecture Overview](#architecture-overview)

---

## Installation

```bash
pip install jsonpysql
```

For development:

```bash
git clone https://github.com/pstenbjorn/JsonpySql
cd JsonpySql
pip install -e ".[dev]"
pytest
```

**Runtime dependencies:** `pydantic>=2.0`, `sortedcontainers`, `dill`

---

## Core Concepts

| Concept | Description |
|---|---|
| **Database** | A directory on the file system. One `Database()` instance per directory. |
| **Collection** | A named set of JSON documents, equivalent to a table. |
| **Schema collection** | Documents validated against a Pydantic model; supports unique and FK constraints. |
| **Document collection** | Schema-free; accepts any JSON-serializable `dict`. |
| **Index** | SortedDict-backed, persisted to disk; used automatically by the query planner. |
| **Transaction** | WAL-based multi-document atomicity with automatic rollback on exception. |
| **Function/Procedure** | Python callables serialized with `dill` and stored alongside the data. |

---

## Defining a Schema

Use Pydantic `BaseModel` with JsonpySql field helpers:

```python
from pydantic import BaseModel
from jsonpysql import field, foreign_key

class Customer(BaseModel):
    id: str = field(primary_key=True)   # used as doc_id; auto-indexed
    email: str = field(unique=True, index=True)
    name: str = field()
    country: str = field(index=True)    # index enables fast lookup

class Order(BaseModel):
    id: str = field(primary_key=True)
    customer_id: str = foreign_key(Customer, on_delete="restrict")
    total: float = field()
    status: str = field(index=True)
```

### `field()` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `primary_key` | `bool` | `False` | Value is used as the document ID. |
| `index` | `bool` | `False` | Creates a single-field index for fast lookup and range scan. |
| `unique` | `bool` | `False` | Enforces uniqueness across the collection. Also creates an index. |
| `nullable` | `bool` | `False` | Allows `None` as a valid value. |

All other kwargs are forwarded to `pydantic.Field()`.

**Primary key by convention:** if no field is marked `primary_key=True` but the model has a field literally named `id`, that `id` field is used as the primary key. This keeps `insert()` and `update()` keyed consistently. A model with neither a declared primary key nor an `id` field gets an auto-generated UUID as its document id on insert.

### `foreign_key()` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `target_model` | `type[BaseModel]` | — | The parent model class. |
| `on_delete` | `str` | `"restrict"` | `"restrict"` · `"cascade"` · `"set_null"` |

The parent collection name is derived by convention: `ModelName.lower() + "s"` (e.g. `Customer` → `"customers"`).

---

## Opening a Database

```python
from jsonpysql import Database

# Directory is created automatically if it does not exist.
db = Database("path/to/my_db")

# Custom lock timeout (seconds); default is 5.0
db = Database("path/to/my_db", lock_timeout=10.0)
```

The database performs crash recovery on open — any incomplete WAL transactions from a previous run are replayed or discarded automatically.

---

## Collections

### Schema Collections

```python
db.register_collection("customers", Customer)
db.register_collection("orders", Order)

# Replace an existing collection (drops all data)
db.register_collection("customers", Customer, drop_if_exists=True)
```

Reopening an existing database and calling `register_collection` again reconnects to the existing data without dropping it.

### Document Collections

```python
# No model — accepts any dict
db.register_collection("logs")

# With explicit indexes
db.register_collection("events", indexes=["type", "user_id"])
```

### Managing Collections

```python
db.has_collection("customers")   # True / False
db.list_collections()            # ["customers", "events", "logs", "orders"]
db.drop_collection("logs")       # removes collection and all its data
```

---

## CRUD Operations

Access a collection via attribute syntax on the database object:

```python
col = db.customers
```

### Insert

```python
# Schema collection — returns the doc_id (from primary_key field or auto UUID)
doc_id = db.customers.insert({
    "id": "c1",
    "email": "alice@example.com",
    "name": "Alice",
    "country": "NO",
})
# doc_id == "c1"

# Document collection — explicit id required
db.logs.insert("log-001", {"level": "INFO", "message": "started"})
```

### Get

```python
doc = db.customers.get("c1")
# {"id": "c1", "email": "alice@example.com", "name": "Alice", "country": "NO"}

doc = db.customers.get("missing")
# None
```

### Update

```python
# Full replacement (complete document required)
db.customers.update("c1", {
    "id": "c1",
    "email": "alice@example.com",
    "name": "Alice Liddell",   # changed
    "country": "NO",
})
```

`update()` on a `SchemaCollection` is **replace-only**: if no document with the given id exists it raises `StorageError` rather than creating a new row. Use `insert()` to add new documents.

### Delete

```python
db.customers.delete("c1")
```

---

## Querying

JsonpySql uses a **fluent, chainable** query API. Each method returns a new `QueryBuilder` — the original is unchanged. The query is not executed until a **materialization** method is called.

```python
query = (
    db.customers
    .where(lambda c: c["country"] == "NO")
    .order_by("name")
    .limit(10)
)

results = query.to_list()   # executes here
```

### Filtering

```python
# Single predicate
db.customers.where(lambda c: c["name"] == "Alice")

# Chained predicates — AND semantics
db.customers \
    .where(lambda c: c["country"] == "NO") \
    .where(lambda c: c["age"] >= 18)

# Complex predicates (arbitrary Python — always a full scan)
db.customers.where(lambda c: c["name"].startswith("A") and c["age"] > 25)
```

**Index usage:** The query planner inspects simple lambda predicates for equality and range patterns on indexed fields:

```python
# Equality on indexed field → index lookup, O(log n)
db.customers.where(lambda c: c["email"] == "alice@example.com")

# Range on indexed field → range scan, O(log n + k)
db.customers.where(lambda c: 18 <= c["age"] <= 65)
db.customers.where(lambda c: c["age"] >= 18)
```

Use `.explain()` to see which strategy was chosen (see [Query Plans](#query-plans)).

### Ordering

```python
db.customers.order_by("name")                    # ascending
db.customers.order_by("age", descending=True)    # descending
```

`None` values always sort last.

### Limit and Skip

```python
db.customers.limit(10)           # first 10 results
db.customers.skip(20).limit(10)  # pagination: page 3 of 10
```

### Projection

Transform each result document with `.select()`:

```python
db.customers.select(lambda c: {"name": c["name"], "email": c["email"]})

# Combine with registered functions
@db.function
def full_name(c):
    return c["name"].upper()

db.customers.select(lambda c: {"display": db.fn.full_name(c)})
```

### Joins

```python
results = (
    db.customers
    .join("orders", on=lambda c, o: c["_id"] == o["customer_id"])
    .where(lambda r: r["total"] > 100)
    .to_list()
)
```

The join predicate receives `(left_doc, right_doc)`. Fields from the right document are merged into the result; overlapping keys are overwritten by the right side.

The query planner automatically chooses **hash join** when the smaller collection fits within the threshold (default: 10,000 documents), otherwise **nested-loop join**.

### Aggregation

```python
from jsonpysql import CountAgg, SumAgg, AvgAgg, MinAgg, MaxAgg

# Count customers per country
db.customers \
    .group_by("country") \
    .aggregate(n=CountAgg()) \
    .to_list()
# [{"country": "NO", "n": 42}, {"country": "SE", "n": 17}, ...]

# Revenue stats per customer
db.orders \
    .group_by("customer_id") \
    .aggregate(
        order_count=CountAgg(),
        total_spend=SumAgg("total"),
        avg_order=AvgAgg("total"),
        min_order=MinAgg("total"),
        max_order=MaxAgg("total"),
    ) \
    .order_by("total_spend", descending=True) \
    .to_list()

# Grand total across all orders (no group_by → single group)
db.orders \
    .group_by() \
    .aggregate(grand_total=SumAgg("total")) \
    .first()
# {"grand_total": 12345.67}
```

| Aggregate | SQL equivalent | Description |
|---|---|---|
| `CountAgg()` | `COUNT(*)` | Number of documents in the group |
| `SumAgg("field")` | `SUM(field)` | Sum of numeric values (skips `None`) |
| `AvgAgg("field")` | `AVG(field)` | Arithmetic mean (skips `None`) |
| `MinAgg("field")` | `MIN(field)` | Minimum value (skips `None`) |
| `MaxAgg("field")` | `MAX(field)` | Maximum value (skips `None`) |

### Materializing Results

| Method | Returns | Description |
|---|---|---|
| `__iter__` | `Iterator[dict]` | Lazy streaming — default when used in `for` loop |
| `.to_list()` | `list[dict]` | Eagerly collect all results |
| `.to_dict(key)` | `dict[str, dict]` | Index results by a field value |
| `.first()` | `dict \| None` | First result, or `None` if empty |
| `.count()` | `int` | Total number of matching documents |
| `.all()` | `list[dict]` | Shorthand for `.to_list()` with no clauses |

```python
# Lazy streaming
for customer in db.customers.where(lambda c: c["country"] == "NO"):
    process(customer)

# Keyed dict
by_id = db.customers.to_dict("id")
alice = by_id["c1"]

# Count without fetching documents
n = db.customers.where(lambda c: c["age"] >= 18).count()
```

### Query Plans

Use `.explain()` to inspect the execution plan without running the query:

```python
plan = db.customers.where(lambda c: c["email"] == "alice@example.com").explain()

print(plan.scan_type)    # ScanType.INDEX_LOOKUP
print(plan.index_field)  # "email"
print(plan.describe())   # "QueryPlan(collection='customers', scan=index_lookup, index_field='email', value='alice@example.com')"
```

---

## Transactions

Use `db.transaction()` as a context manager for atomic multi-document writes. On exception, all writes in the block are automatically rolled back.

```python
with db.transaction() as txn:
    txn.insert("customers", "c1", {
        "id": "c1", "email": "alice@example.com", "name": "Alice", "country": "NO"
    })
    txn.insert("orders", "o1", {
        "id": "o1", "customer_id": "c1", "total": 99.0, "status": "pending"
    })
# Both inserted atomically

# Rollback example
try:
    with db.transaction() as txn:
        txn.insert("orders", "o2", {"id": "o2", "customer_id": "c1", "total": 50.0, "status": "pending"})
        raise ValueError("something went wrong")
except ValueError:
    pass
# o2 was NOT inserted
```

Transactions use a **write-ahead log (WAL)**. If the process crashes mid-transaction, the WAL is replayed or discarded automatically when the database is next opened.

---

## Functions and Procedures

Register Python callables alongside your data. They are serialized with `dill` and persist across database restarts.

### Functions

Pure callables with no side effects, usable in `.select()` projections:

```python
@db.function
def discount_price(price: float, pct: float) -> float:
    return round(price * (1 - pct / 100), 2)

# Use in a projection
db.products.select(lambda p: {
    "name": p["name"],
    "sale_price": db.fn.discount_price(p["price"], 20),
})

# Call directly
db.fn.discount_price(100.0, 20)  # 80.0
```

### Procedures

Callables that receive the database as their first argument, enabling multi-collection operations:

```python
@db.procedure
def place_order(db_ctx, customer_id: str, items: list) -> str:
    total = sum(i["price"] * i["qty"] for i in items)
    order_id = f"o-{customer_id}-{total}"
    with db_ctx.transaction() as txn:
        txn.insert("orders", order_id, {
            "id": order_id,
            "customer_id": customer_id,
            "total": total,
            "status": "pending",
        })
    return order_id

# Call via db.fn — the db_ctx argument is injected automatically,
# so you pass only the remaining arguments.
order_id = db.fn.place_order("c1", [{"price": 10.0, "qty": 3}])
```

### Listing registered callables

```python
db.fn.list_functions()    # ["discount_price"]
db.fn.list_procedures()   # ["place_order"]
```

---

## Compaction

Documents are stored append-only; deletions write a tombstone. When deleted documents accumulate, compact to reclaim disk space:

```python
# Compact a single collection
db.compact("customers")

# Compact all collections
db.compact()
```

Compaction rewrites the JSONL file without tombstoned records and acquires an exclusive lock for its duration.

---

## Statistics

```python
stats = db.stats()

print(stats.total_documents)   # sum across all collections

for col in stats.collections:
    print(col.name, col.document_count, col.deleted_count, col.file_size_bytes)
    print(f"  deleted ratio: {col.deleted_ratio:.1%}")
```

---

## Exception Hierarchy

All exceptions inherit from `JsonpySqlError` and are importable directly from `jsonpysql`:

```
JsonpySqlError
├── StorageError          — I/O and engine errors
│   ├── LockTimeoutError  — file lock not acquired within timeout
│   ├── CompactionError   — compaction failed
│   └── WALReplayError    — WAL replay failed on open
├── SchemaError           — schema and constraint violations
│   ├── ValidationError   — document does not match Pydantic model
│   ├── UniqueConstraintError  — unique field value already exists
│   ├── ReferentialIntegrityError  — FK parent not found / restrict violated
│   └── CollectionExistsError  — duplicate registration without drop_if_exists
├── QueryError            — query execution errors
│   └── JoinError
└── FunctionError         — function registry errors
```

```python
from jsonpysql import (
    ValidationError,
    UniqueConstraintError,
    ReferentialIntegrityError,
    StorageError,
)

try:
    db.customers.insert({"id": "c1", "email": "dupe@example.com", "name": "X", "country": "SE"})
except UniqueConstraintError as e:
    print(f"Duplicate email: {e}")
except ValidationError as e:
    print(f"Bad document: {e}")
```

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   Public API                         │
│              database.py / __init__.py               │
├─────────────────────────────────────────────────────┤
│                  Query Engine                        │
│   QueryBuilder → QueryPlanner → QueryExecutor        │
│              Aggregation Engine                      │
├─────────────────────────────────────────────────────┤
│                  Schema Engine                       │
│   CollectionManager / SchemaCollection               │
│   Validator / UniqueConstraintChecker                │
│   ReferentialIntegrityChecker / FunctionRegistry     │
├─────────────────────────────────────────────────────┤
│                  Storage Engine                      │
│   DocumentStore (JSONL) / IndexManager (SortedDict)  │
│         WAL / LockManager / CompactionManager        │
└─────────────────────────────────────────────────────┘
              ↓ File System ↓
    my_db/
    ├── manifest.json
    ├── customers.jsonl
    ├── customers.email.idx
    ├── customers.age.idx
    ├── orders.jsonl
    ├── orders.status.idx
    └── functions.pkl
```

### Key design decisions

- **JSONL storage** — append-only writes, tombstone deletes; compaction rewrites atomically via a `.tmp` file.
- **SortedDict indexes** — O(log n) equality and range lookups, persisted as JSON `.idx` files; rebuilt from JSONL on corruption.
- **WAL transactions** — multi-document atomicity; replay is idempotent (keyed on `doc_id`).
- **File-level locking** — `fcntl` (Unix) / `msvcrt` (Windows); configurable timeout; exclusive for writes, shared for reads.
- **Rule-based query planner** — parses lambda AST via `ast` + `inspect.getsource` to detect index-eligible predicates; complex predicates fall through to full scan silently.
- **dill serialization** — functions and procedures (including closures and lambdas) survive database restarts.
- **Pydantic v2** — schema validation at the boundary; FK and unique constraints checked before every write.

---

## Running Tests

```bash
pytest                          # all tests
pytest tests/unit/              # unit tests only
pytest tests/integration/       # integration tests only
pytest --cov=jsonpysql          # with coverage report
```

Coverage gates: storage layer ≥ 95%, schema layer ≥ 95%.
