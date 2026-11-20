"""JSON output: a stable JSON encoding of statement results, query plans and
values, written as one line of compact JSON (see `toydb.encoding.json`).

Plans are encoded structurally, but describe exactly what the plan display
shows: nodes are objects whose first member is "node" (the display name in
snake case), followed by the node's parameters and then its inputs. Column
references are {"column": <index>, "label": <label or null>}, with labels
resolved as by the plan display. Operators and functions are {"op": <the
expression's display with each operand replaced by _>, "args": [...]}.
"""

from toydb.encoding import json
from toydb.encoding.json import JSON
from toydb.error import panic
from toydb.sql.execution.session import StatementResult
from toydb.sql.planner import Aggregate, Node, Plan
from toydb.sql.planner.plan import invert_remap
from toydb.sql.types import Expression, Label, Value
from toydb.sql.types.expression import ColumnLabels
from toydb.sql.types.formats import to_json


def encode_result(result: StatementResult) -> str:
    """Encodes a statement result as a line of JSON text."""
    return json.encode(result_to_json(result))


def result_to_json(result: StatementResult) -> JSON:
    """Converts a statement result to JSON."""
    match result:
        case StatementResult.Begin(state):
            return {"result": "begin", "version": state.version, "read_only": state.read_only}
        case StatementResult.Commit(version):
            return {"result": "commit", "version": version}
        case StatementResult.Rollback(version):
            return {"result": "rollback", "version": version}
        case StatementResult.Explain(plan):
            return {"result": "explain", "plan": plan_to_json(plan)}
        case StatementResult.CreateTable(name):
            return {"result": "create_table", "table": name}
        case StatementResult.DropTable(name, existed):
            return {"result": "drop_table", "table": name, "existed": existed}
        case StatementResult.Delete(count):
            return {"result": "delete", "count": count}
        case StatementResult.Insert(count):
            return {"result": "insert", "count": count}
        case StatementResult.Update(count):
            return {"result": "update", "count": count}
        case StatementResult.Select(columns, rows):
            return {
                "result": "select",
                "columns": [label_to_json(column) for column in columns],
                "rows": [[to_json(value) for value in row] for row in rows],
            }
        case StatementResult.Analyze(tables):
            return {"result": "analyze", "tables": list(tables)}
        case StatementResult.Export(format, lines):
            return {"result": "export", "format": str(format), "lines": list(lines)}
        case _:
            panic(f"unknown statement result {result!r}")


def label_to_json(label: Label) -> JSON:
    """Converts a column label to JSON: its display, or null if none."""
    match label:
        case Label.None_():
            return None
        case _:
            return str(label)


def plan_to_json(plan: Plan) -> JSON:
    """Converts a statement plan to JSON."""
    match plan:
        case Plan.Select(root):
            return {"statement": "select", "root": node_to_json(root)}
        case Plan.Insert(table=table, source=source):
            return {"statement": "insert", "table": table.name, "source": node_to_json(source)}
        case Plan.Update(table=table, source=source, expressions=expressions):
            set: list[JSON] = [
                {"column": table.columns[i].name, "expr": expression_to_json(expr, source)}
                for i, expr in expressions
            ]
            return {
                "statement": "update",
                "table": table.name,
                "set": set,
                "source": node_to_json(source),
            }
        case Plan.Delete(table=table, source=source):
            return {"statement": "delete", "table": table, "source": node_to_json(source)}
        case Plan.CreateTable(schema):
            return {"statement": "create_table", "table": schema.name}
        case Plan.DropTable(name=name):
            return {"statement": "drop_table", "table": name}
        case _:
            panic(f"unknown plan {plan!r}")


def node_to_json(node: Node) -> JSON:
    """Converts a plan node to JSON. Expressions resolve column labels like
    the plan display does."""
    match node:
        case Node.Aggregate(source, group_by, aggregates):
            return {
                "node": "aggregate",
                "group_by": [expression_to_json(expr, source) for expr in group_by],
                "aggregates": [aggregate_to_json(agg, source) for agg in aggregates],
                "source": node_to_json(source),
            }
        case Node.Filter(source, predicate):
            return {
                "node": "filter",
                "predicate": expression_to_json(predicate, source),
                "source": node_to_json(source),
            }
        case Node.HashJoin(left, left_column, right, right_column, outer):
            return {
                "node": "hash_join",
                "join": "outer" if outer else "inner",
                "left_column": column_to_json(left_column, left),
                "right_column": column_to_json(right_column, right),
                "left": node_to_json(left),
                "right": node_to_json(right),
            }
        case Node.IndexLookup(table, column, values, alias):
            return {
                "node": "index_lookup",
                "table": table.name,
                "alias": alias,
                "column": table.columns[column].name,
                "values": [to_json(value) for value in values],
            }
        case Node.KeyLookup(table, keys, alias):
            return {
                "node": "key_lookup",
                "table": table.name,
                "alias": alias,
                "keys": [to_json(key) for key in keys],
            }
        case Node.Limit(source, limit):
            return {"node": "limit", "limit": limit, "source": node_to_json(source)}
        case Node.NestedLoopJoin(left, right, predicate, outer):
            return {
                "node": "nested_loop_join",
                "join": "outer" if outer else "inner",
                "predicate": expression_to_json(predicate, node) if predicate is not None else None,
                "left": node_to_json(left),
                "right": node_to_json(right),
            }
        case Node.Nothing():
            return {"node": "nothing"}
        case Node.Offset(source, offset):
            return {"node": "offset", "offset": offset, "source": node_to_json(source)}
        case Node.Order(source, key):
            return {
                "node": "order",
                "key": [
                    {"expr": expression_to_json(expr, source), "direction": str(direction)}
                    for expr, direction in key
                ],
                "source": node_to_json(source),
            }
        case Node.Projection(source, expressions, aliases):
            projected: list[JSON] = []
            for i, expr in enumerate(expressions):
                alias = aliases[i] if i < len(aliases) else Label.None_()
                projected.append(
                    {"expr": expression_to_json(expr, source), "alias": label_to_json(alias)}
                )
            return {"node": "projection", "expressions": projected, "source": node_to_json(source)}
        case Node.Remap(source, targets):
            return {
                "node": "remap",
                "columns": [
                    column_to_json(index, source) if index is not None else None
                    for index in invert_remap(targets)
                ],
                "dropped": [
                    column_to_json(index, source)
                    for index, target in enumerate(targets)
                    if target is None
                ],
                "source": node_to_json(source),
            }
        case Node.Scan(table, filter, alias):
            return {
                "node": "scan",
                "table": table.name,
                "alias": alias,
                "filter": expression_to_json(filter, node) if filter is not None else None,
            }
        case Node.Values(rows):
            return {
                "node": "values",
                "rows": [[expression_to_json(expr, node) for expr in row] for row in rows],
            }
        case _:
            panic(f"unknown node {node!r}")


def column_to_json(index: int, node: ColumnLabels) -> JSON:
    """Converts a column reference to JSON, with its label in the given node."""
    return {"column": index, "label": label_to_json(node.column_label(index))}


def aggregate_to_json(aggregate: Aggregate, node: Node) -> JSON:
    """Converts an aggregate function call to JSON, like a function call."""
    match aggregate:
        case Aggregate.Average():
            name = "avg"
        case Aggregate.Count():
            name = "count"
        case Aggregate.Max():
            name = "max"
        case Aggregate.Min():
            name = "min"
        case Aggregate.Sum():
            name = "sum"
        case _:
            panic(f"unknown aggregate {aggregate!r}")
    return {"op": f"{name}(_)", "args": [expression_to_json(aggregate.expr(), node)]}


def expression_to_json(expr: Expression, node: ColumnLabels) -> JSON:
    """Converts an expression to JSON, looking up column labels in the given
    node (the same node the plan display uses for the expression)."""

    def op(template: str, *args: Expression) -> JSON:
        return {"op": template, "args": [expression_to_json(arg, node) for arg in args]}

    match expr:
        case Expression.Constant(value):
            return {"constant": to_json(value)}
        case Expression.Column(index):
            return column_to_json(index, node)

        case Expression.And(lhs, rhs):
            return op("_ AND _", lhs, rhs)
        case Expression.Or(lhs, rhs):
            return op("_ OR _", lhs, rhs)
        case Expression.Not(inner):
            return op("NOT _", inner)

        case Expression.Equal(lhs, rhs):
            return op("_ = _", lhs, rhs)
        case Expression.GreaterThan(lhs, rhs):
            return op("_ > _", lhs, rhs)
        case Expression.LessThan(lhs, rhs):
            return op("_ < _", lhs, rhs)
        case Expression.Is(inner, Value.Null()):
            return op("_ IS NULL", inner)
        case Expression.Is(inner, Value.Float()):
            return op("_ IS NAN", inner)

        case Expression.Add(lhs, rhs):
            return op("_ + _", lhs, rhs)
        case Expression.Divide(lhs, rhs):
            return op("_ / _", lhs, rhs)
        case Expression.Exponentiate(lhs, rhs):
            return op("_ ^ _", lhs, rhs)
        case Expression.Factorial(inner):
            return op("_!", inner)
        case Expression.Identity(inner):
            # The identity is not displayed, only its operand.
            return expression_to_json(inner, node)
        case Expression.Multiply(lhs, rhs):
            return op("_ * _", lhs, rhs)
        case Expression.Negate(inner):
            return op("-_", inner)
        case Expression.Remainder(lhs, rhs):
            return op("_ % _", lhs, rhs)
        case Expression.SquareRoot(inner):
            return op("sqrt(_)", inner)
        case Expression.Subtract(lhs, rhs):
            return op("_ - _", lhs, rhs)

        case Expression.Like(lhs, rhs):
            return op("_ LIKE _", lhs, rhs)

        case _:
            panic(f"unknown expression {expr!r}")
