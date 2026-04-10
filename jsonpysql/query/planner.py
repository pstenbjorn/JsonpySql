"""Rule-based query planner.

Inspects ``where`` predicates by parsing the lambda source with the
standard library ``ast`` module.  Matches simple equality and range
patterns on indexed fields so the executor can use an index lookup or
range scan instead of a full collection scan.

**Precedence:**

1. Equality predicate on an indexed field → ``INDEX_LOOKUP``
2. Range predicate on an indexed field → ``RANGE_SCAN``
3. Any other predicate (complex, multi-field, closure) → ``FULL_SCAN``

Predicates that cannot be parsed fall through to ``FULL_SCAN`` silently.
"""

from __future__ import annotations

import ast
import inspect
import logging
from typing import Any, Callable

from jsonpysql.query.models import JoinStrategy, QueryPlan, ScanType

logger = logging.getLogger(__name__)

# Number of documents below which a collection qualifies for a hash join.
_HASH_JOIN_THRESHOLD = 10_000


def _extract_lambda_source(fn: Callable[..., Any]) -> str | None:
    """Return the source of *fn* if it is an inline lambda.

    Uses ``inspect.getsource`` and strips surrounding whitespace.
    When the lambda is embedded inside a function call (e.g.
    ``planner.plan([lambda x: x['f'] == v])``) the raw source line
    contains trailing ``])`` noise.  This function trims characters from
    the right until the source parses cleanly as a lambda expression.

    Returns ``None`` on any error (missing source, no lambda found, etc.).

    Args:
        fn: A callable, expected to be a single-expression lambda.

    Returns:
        Valid lambda source string, or ``None`` if extraction fails.
    """
    try:
        src = inspect.getsource(fn).strip()
        idx = src.find("lambda")
        if idx == -1:
            return None
        src = src[idx:]
        # Trim trailing noise one character at a time until the snippet
        # parses as a valid lambda expression.
        for end in range(len(src), 0, -1):
            candidate = src[:end].rstrip()
            try:
                tree = ast.parse(candidate, mode="eval")
                if isinstance(tree.body, ast.Lambda):
                    return candidate
            except SyntaxError:
                continue
        return None
    except (OSError, TypeError):
        return None


def _parse_predicate(
    fn: Callable[..., Any],
    indexed_fields: set[str],
) -> tuple[ScanType, str | None, Any, Any, Any]:
    """Attempt to parse a single-argument lambda predicate.

    Returns a tuple of:
    ``(scan_type, index_field, index_value, index_low, index_high)``

    On parse failure returns ``(FULL_SCAN, None, None, None, None)``.

    Recognized patterns (where *x* is the lambda argument):

    - ``lambda x: x['field'] == value``  → INDEX_LOOKUP
    - ``lambda x: x['field'] == value and ...`` (first clause only)
    - ``lambda x: low <= x['field'] <= high``  → RANGE_SCAN
    - ``lambda x: x['field'] >= low``  → RANGE_SCAN (open right bound)
    - ``lambda x: x['field'] <= high``  → RANGE_SCAN (open left bound)

    Args:
        fn: The predicate callable.
        indexed_fields: Set of field names that have an index.

    Returns:
        Five-element tuple as described above.
    """
    _fail = (ScanType.FULL_SCAN, None, None, None, None)

    src = _extract_lambda_source(fn)
    if src is None:
        return _fail

    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError:
        return _fail

    # Unwrap Expression → Lambda
    if not isinstance(tree, ast.Expression):
        return _fail
    if not isinstance(tree.body, ast.Lambda):
        return _fail

    lam = tree.body
    # Lambda must have exactly one argument
    if len(lam.args.args) != 1:
        return _fail

    arg_name = lam.args.args[0].arg
    body = lam.body

    # If the body is an 'and', inspect just the first clause for index use.
    if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And):
        body = body.values[0]

    result = _try_equality(body, arg_name, indexed_fields)
    if result is not None:
        field, value = result
        return (ScanType.INDEX_LOOKUP, field, value, None, None)

    result = _try_range(body, arg_name, indexed_fields)
    if result is not None:
        field, low, high = result
        return (ScanType.RANGE_SCAN, field, None, low, high)

    return _fail


# ---------------------------------------------------------------------------
# Pattern matchers
# ---------------------------------------------------------------------------


def _try_equality(
    node: ast.expr,
    arg: str,
    indexed: set[str],
) -> tuple[str, Any] | None:
    """Match ``x['field'] == literal`` or ``literal == x['field']``.

    Args:
        node: AST node to inspect.
        arg: Lambda argument name.
        indexed: Set of indexed field names.

    Returns:
        ``(field, value)`` on match, else ``None``.
    """
    if not isinstance(node, ast.Compare):
        return None
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return None
    if len(node.comparators) != 1:
        return None

    left, right = node.left, node.comparators[0]

    field = _extract_subscript_field(left, arg)
    if field and field in indexed:
        value = _extract_literal(right)
        if value is not None:
            return (field, value)

    field = _extract_subscript_field(right, arg)
    if field and field in indexed:
        value = _extract_literal(left)
        if value is not None:
            return (field, value)

    return None


def _try_range(
    node: ast.expr,
    arg: str,
    indexed: set[str],
) -> tuple[str, Any, Any] | None:
    """Match range patterns on an indexed field.

    Recognized forms:

    - ``low <= x['f'] <= high`` (chained comparison)
    - ``x['f'] >= low``
    - ``x['f'] <= high``
    - ``x['f'] > low``
    - ``x['f'] < high``

    Args:
        node: AST node to inspect.
        arg: Lambda argument name.
        indexed: Set of indexed field names.

    Returns:
        ``(field, low, high)`` on match, else ``None``.
    """
    if not isinstance(node, ast.Compare):
        return None

    # Chained: low OP x['f'] OP high
    if len(node.ops) == 2 and len(node.comparators) == 2:
        left_val = _extract_literal(node.left)
        field = _extract_subscript_field(node.comparators[0], arg)
        right_val = _extract_literal(node.comparators[1])
        if left_val is not None and field and field in indexed and right_val is not None:
            op0, op1 = node.ops
            if isinstance(op0, (ast.LtE, ast.Lt)) and isinstance(op1, (ast.LtE, ast.Lt)):
                return (field, left_val, right_val)

    # Single comparison: x['f'] >= low  /  x['f'] <= high
    if len(node.ops) == 1 and len(node.comparators) == 1:
        op = node.ops[0]
        left, right = node.left, node.comparators[0]
        field = _extract_subscript_field(left, arg)
        if field and field in indexed:
            val = _extract_literal(right)
            if val is not None:
                if isinstance(op, (ast.GtE, ast.Gt)):
                    return (field, val, None)
                if isinstance(op, (ast.LtE, ast.Lt)):
                    return (field, None, val)

    return None


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _extract_subscript_field(node: ast.expr, arg: str) -> str | None:
    """Return field name if *node* looks like ``arg['field']``.

    Args:
        node: AST node.
        arg: Lambda argument name.

    Returns:
        Field name string or ``None``.
    """
    if not isinstance(node, ast.Subscript):
        return None
    if not (isinstance(node.value, ast.Name) and node.value.id == arg):
        return None
    slice_node = node.slice
    if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
        return slice_node.value
    return None


def _extract_literal(node: ast.expr) -> Any:
    """Return the Python value if *node* is a simple literal, else ``None``.

    Supports ``ast.Constant`` and unary negation ``-Constant``.

    Args:
        node: AST node.

    Returns:
        Python literal value, or ``None`` if not a recognized literal.
    """
    if isinstance(node, ast.Constant):
        return node.value
    # Handle negated literals: -5, -3.14
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
    ):
        return -node.operand.value
    return None


# ---------------------------------------------------------------------------
# Public planner class
# ---------------------------------------------------------------------------


class QueryPlanner:
    """Produces a ``QueryPlan`` from a query specification.

    Consults the engine's index metadata to decide whether an index
    lookup, range scan, or full scan should be used.

    Args:
        indexed_fields: Set of field names that have a single-field index
            in the target collection.
        collection_size: Estimated number of documents in the collection,
            used to choose the join strategy.
        hash_join_threshold: Collections with fewer documents than this
            qualify for hash join (default: 10,000).
    """

    def __init__(
        self,
        indexed_fields: set[str],
        collection_size: int = 0,
        hash_join_threshold: int = _HASH_JOIN_THRESHOLD,
    ) -> None:
        self._indexed = indexed_fields
        self._collection_size = collection_size
        self._threshold = hash_join_threshold

    def plan(
        self,
        collection: str,
        predicates: list[Callable[..., Any]],
        join_collection: str | None = None,
        join_predicate: Callable[..., Any] | None = None,
        join_collection_size: int = 0,
        has_order_by: bool = False,
        has_limit: bool = False,
        has_projection: bool = False,
        has_aggregation: bool = False,
    ) -> QueryPlan:
        """Build and return a ``QueryPlan``.

        Analyses the first predicate in *predicates* for index
        opportunities.  All subsequent predicates are applied as
        post-filters (full scan over the indexed result set).

        Args:
            collection: Primary collection name.
            predicates: List of callable predicates (from ``.where()``).
            join_collection: Right-side collection name, or ``None``.
            join_predicate: Join predicate callable, or ``None``.
            join_collection_size: Doc count in the right collection.
            has_order_by: Whether the query has an order_by clause.
            has_limit: Whether the query has a limit clause.
            has_projection: Whether the query has a select projection.
            has_aggregation: Whether the query has group_by/aggregate.

        Returns:
            A fully populated ``QueryPlan``.
        """
        scan_type = ScanType.FULL_SCAN
        index_field: str | None = None
        index_value: Any = None
        index_low: Any = None
        index_high: Any = None

        if predicates:
            scan_type, index_field, index_value, index_low, index_high = (
                _parse_predicate(predicates[0], self._indexed)
            )

        # Join strategy
        join_strategy: JoinStrategy | None = None
        if join_collection is not None:
            smaller = min(self._collection_size, join_collection_size)
            join_strategy = (
                JoinStrategy.HASH_JOIN
                if smaller <= self._threshold
                else JoinStrategy.NESTED_LOOP
            )

        return QueryPlan(
            collection=collection,
            scan_type=scan_type,
            index_field=index_field,
            index_value=index_value,
            index_low=index_low,
            index_high=index_high,
            predicate_count=len(predicates),
            join_strategy=join_strategy,
            join_collection=join_collection,
            has_order_by=has_order_by,
            has_limit=has_limit,
            has_projection=has_projection,
            has_aggregation=has_aggregation,
        )
