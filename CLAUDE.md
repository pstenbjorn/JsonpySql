# CLAUDE.md

Guidance for Claude Code when working in the JsonpySql repository.

## What this is

JsonpySql is a Python-native **embedded** database (no server process): one
database = one directory on the file system. Python callables are first-class
database objects (functions/procedures), and Python lambdas are the query
language. Python 3.11+.

The authoritative architecture reference is
**`.claude/skills/jsonpysql-architecture.md`** — read it before any design
decision, new module, or dependency choice. This file captures the practical
conventions and the non-obvious behaviors learned while building/maintaining
the code; it does not restate the skill.

## Architecture (strict build/layer order)

Storage Engine → Schema Engine → Query Engine → Public API (`database.py`).
A lower layer must never import from a higher one. Package layout:

- `jsonpysql/storage/` — `document_store` (JSONL), `index_manager` (SortedDict
  `.idx`/`.ridx`), `wal`, `lock_manager` (`fcntl`/`msvcrt`), `engine`, `models`
- `jsonpysql/schema/` — `fields`, `validator`, `constraints`, `functions`
  (dill), `collection`, `registry`
- `jsonpysql/query/` — `models`, `planner` (ast/inspect), `aggregates`,
  `executor`, `builder`
- `jsonpysql/` — `database.py` (public `Database`), `exceptions.py`, `__init__.py`
- `jsonpysql/utils/` — `platform`, `serialization`

## Commands

```bash
pip install -e ".[dev]"        # install (pytest may be absent in a fresh env)
python -m pytest tests/ -q     # full suite (currently 412 passed, 1 skipped)
python -m pytest --cov=jsonpysql --cov-report=term-missing
# Layer coverage gate (95% required for storage AND schema):
python -m pytest tests/unit/schema tests/integration/test_schema_collection_crud.py \
  && python -m coverage report --include="jsonpysql/schema/*" --fail-under=95
```

`pyproject.toml` has no global `--cov-fail-under`; the 95% gate is enforced
per-layer at the CLI (a global gate makes every partial run fail).

## Conventions (enforced)

- Type hints on every signature; Google-style docstrings on every public
  class/method. No bare `except`.
- **No raw built-in exceptions leak through the public API.** Raise only from
  the `jsonpysql.exceptions` hierarchy (base `JsonpySqlError`). E.g. a bad
  argument combo raises `SchemaError`, not `ValueError`.
- All internal paths are `pathlib.Path`; strings accepted only at the public
  boundary and converted immediately.
- Tests use `tmp_path` — no shared file state, no mocking of `StorageEngine`
  in schema/query integration tests. Canonical fixture: `Customer`/`Order`.
- Commit after each logical change; commit messages end with the
  `Co-Authored-By` / `Claude-Session` trailer. Do NOT put the model id in any
  committed artifact.

## Non-obvious behaviors / gotchas

- **JSON writes go through `utils.serialization.json_default`** at all three
  write sites (`document_store` append + compact, `wal` append). The
  `Validator` dumps with `model_dump(mode="json")` so schema docs are already
  JSON-native (datetime→ISO, Decimal/UUID→str). Keep new write sites consistent.
- **Primary key by convention:** `get_primary_key_field()` returns a
  `field(primary_key=True)` field, else falls back to a field literally named
  `id`, else `None` (→ UUID `_id` on insert). This keeps `insert()`/`update()`
  keyed consistently. (issue #4)
- **`SchemaCollection.update()` is replace-only** — raises `StorageError` if the
  id doesn't exist. The storage engine's `update` is an append-based upsert, so
  the schema layer guards against silently creating duplicate rows. (issue #4)
- **`register_collection(name, model=..., indexes=...)` raises `SchemaError`.**
  Schema collections derive indexes from `field(index=True)`/`unique=True`
  metadata; passing `indexes=` alongside a model was silently ignored.
- **`db.fn.<procedure>()` injects the `Database` as the first arg** (`db_ctx`).
  `Database.fn` returns a `_FunctionAccessor` that binds `self` via
  `functools.partial` for procedures; functions pass through unchanged. Call
  procedures as `db.fn.proc(args)` — do NOT pass the context manually.
- **Reopening a DB:** `CollectionManager.register_*` skips
  `engine.create_collection` when the engine already has the collection
  (preserves data); only drops when `drop_if_exists=True`.
- **Query planner** parses lambda source via `ast`/`inspect.getsource`. Only
  simple equality/range predicates on indexed fields use an index; anything else
  silently falls through to full scan (documented, expected).

## Git / CI

- Default branch is `main`. Recent bug fixes were committed directly to `main`
  at the user's explicit request; feature work normally goes on a branch + PR.
- The managed-env git proxy **allows pushes but blocks branch deletion (403)** —
  delete merged remote branches from the GitHub UI or a normal clone.
- CI (`.github/workflows/ci.yml`) is currently red due to an **account-level
  Actions provisioning failure** (0 billable ms, no logs), not the code —
  resolve under GitHub Settings → Billing. The suite is green locally.
