"""Row estimates for `ESTIMATE`: the estimated number of rows of every node of
an optimized plan, computed from the column statistics of `ANALYZE` as the
transaction sees them.

* A table has the row count of its statistics, or 1000 without statistics.
* Conditions have a selectivity: from the statistics of the columns they
  compare (distinct values and NULLs for equality, interpolation between
  minimum and maximum for ranges of numbers), combined for AND, OR and NOT,
  and fixed values otherwise.
* Each node estimates its rows from its inputs; a column keeps the statistics
  of the table column it passes on unchanged.

Estimates are computed in floating point and rounded to the nearest integer
(halves up) only for display.
"""

import math
from typing import TYPE_CHECKING

from toydb.error import panic
from toydb.sql.planner.plan import Node, Plan, invert_remap
from toydb.sql.types import Expression, Label, Row, Value
from toydb.sql.types.statistics import ColumnStatistics, TableStatistics

if TYPE_CHECKING:
    from toydb.sql.engine import Catalog

COLUMNS: list[Label] = [Label.Unqualified(name) for name in ("node", "rows")]
"""The ESTIMATE result columns."""


def estimate_rows(catalog: "Catalog", plan: Plan) -> list[Row]:
    """Returns the ESTIMATE result rows of a plan."""
    estimates = Estimator(catalog).estimate(plan)
    return [
        [Value.String(line), Value.Integer(round_half_up(rows))]
        for line, rows in zip(str(plan).split("\n"), estimates)
    ]


def round_half_up(value: float) -> int:
    """Rounds a number to the nearest integer, halves up."""
    whole = math.floor(value)
    return whole + 1 if value - whole >= 0.5 else whole


class Estimator:
    """Estimates the rows of plans from the column statistics of a catalog."""

    def __init__(self, catalog: "Catalog") -> None:
        self.catalog = catalog
        """The catalog to read the statistics from."""
        self.statistics: dict[str, TableStatistics | None] = {}
        """The statistics read so far, by table name."""

    def estimate(self, plan: Plan) -> list[float]:
        """Returns the estimated rows of every line of the plan display, in
        display order."""
        lines: list[float] = []
        match plan:
            case Plan.Select(root):
                self.estimate_node(root, lines)
            case Plan.Delete(source=source) | Plan.Insert(source=source) | Plan.Update(
                source=source
            ):
                lines.append(0.0)
                lines[0] = self.estimate_node(source, lines)
            case Plan.CreateTable() | Plan.DropTable():
                lines.append(0.0)
            case _:
                panic(f"unknown plan {plan!r}")
        return lines

    def estimate_node(self, node: Node, lines: list[float]) -> float:
        """Returns the estimated number of rows of a node."""
        # Appends the estimates of the node's display lines to lines: the
        # node's own line, then those of its inputs.
        line = len(lines)
        lines.append(0.0)

        rows: float
        match node:
            # Source nodes read the table's rows.
            case Node.Scan(table=table, filter=filter):
                rows = self.table_rows(table.name)
                if filter is not None:
                    rows *= self.selectivity(filter, node)
            case Node.KeyLookup(table=table, keys=keys):
                rows = min(float(len(keys)), self.table_rows(table.name))
            case Node.IndexLookup(table=table, column=column, values=values):
                table_rows = self.table_rows(table.name)
                rows = 0.0
                for value in values:
                    condition: Expression
                    if isinstance(value, Value.Null):
                        condition = Expression.Is(Expression.Column(column), value)
                    else:
                        condition = Expression.Equal(
                            Expression.Column(column), Expression.Constant(value)
                        )
                    rows += table_rows * self.selectivity(condition, node)

            # Filters keep the rows for which the predicate is true.
            case Node.Filter(source, predicate):
                rows = self.estimate_node(source, lines) * self.selectivity(predicate, source)

            # These nodes emit a row for every source row.
            case (
                Node.Order(source=source)
                | Node.Projection(source=source)
                | Node.Remap(source=source)
                | Node.View(source=source)
            ):
                rows = self.estimate_node(source, lines)

            # Limit and Offset count rows.
            case Node.Limit(source, limit):
                rows = min(self.estimate_node(source, lines), float(limit))
            case Node.Offset(source, offset):
                rows = max(self.estimate_node(source, lines) - offset, 0.0)

            # Aggregates emit one row per group.
            case Node.Aggregate(source, group_by):
                source_rows = self.estimate_node(source, lines)
                if not group_by:
                    rows = 1.0
                else:
                    groups = 1.0
                    for expr in group_by:
                        groups *= self.group_count(expr, source)
                    rows = min(source_rows, groups)

            # Joins combine the rows of both sides.
            case Node.HashJoin(left, left_column, right, right_column, outer):
                left_rows = self.estimate_node(left, lines)
                right_rows = self.estimate_node(right, lines)
                condition = Expression.Equal(
                    Expression.Column(left_column),
                    Expression.Column(left.columns() + right_column),
                )
                rows = left_rows * right_rows * self.selectivity(condition, node)
                if outer:
                    rows = max(rows, left_rows)
            case Node.NestedLoopJoin(left, right, predicate, outer):
                left_rows = self.estimate_node(left, lines)
                right_rows = self.estimate_node(right, lines)
                rows = left_rows * right_rows
                if predicate is not None:
                    rows *= self.selectivity(predicate, node)
                if outer:
                    rows = max(rows, left_rows)

            # Constant nodes have a known number of rows.
            case Node.Nothing():
                rows = 0.0
            case Node.Values(rows=values):
                rows = float(len(values))

            # System tables have no statistics.
            case Node.SystemTable():
                rows = 0.0

            # Materialized views have the number of rows they store.
            case Node.MaterializedView(name=name):
                rows = float(len(self.catalog.get_matview_rows(name)))

            case _:
                panic(f"unknown node {node!r}")

        lines[line] = rows
        return rows

    def selectivity(self, expr: Expression, node: Node) -> float:
        """Returns the estimated fraction of rows for which a condition is
        true."""
        match expr:
            case Expression.Constant(Value.Boolean(True)):
                return 1.0
            case Expression.Constant(Value.Boolean(False) | Value.Null()):
                return 0.0

            # Logical operators combine the selectivities of their operands.
            case Expression.And(lhs, rhs):
                return self.selectivity(lhs, node) * self.selectivity(rhs, node)
            case Expression.Or(lhs, rhs):
                lhs_selectivity = self.selectivity(lhs, node)
                rhs_selectivity = self.selectivity(rhs, node)
                return lhs_selectivity + rhs_selectivity - lhs_selectivity * rhs_selectivity
            case Expression.Not(inner):
                return 1.0 - self.selectivity(inner, node)

            # Comparisons with the NULL constant.
            case (
                Expression.Equal(lhs, rhs)
                | Expression.GreaterThan(lhs, rhs)
                | Expression.LessThan(lhs, rhs)
            ) if is_null(lhs) or is_null(rhs):
                return 0.0

            # Comparisons of columns with constants or other columns.
            case Expression.Equal(Expression.Column(lhs), Expression.Column(rhs)):
                return self.columns_equal_selectivity(node, lhs, rhs)
            case (
                Expression.Equal(Expression.Column(index), Expression.Constant())
                | Expression.Equal(Expression.Constant(), Expression.Column(index))
            ):
                return self.equal_selectivity(node, index)
            case Expression.Is(Expression.Column(index), Value.Null()):
                return self.null_selectivity(node, index)
            case (
                Expression.LessThan(Expression.Column(index), Expression.Constant(value))
                | Expression.GreaterThan(Expression.Constant(value), Expression.Column(index))
            ):
                return self.range_selectivity(node, index, value, True)
            case (
                Expression.GreaterThan(Expression.Column(index), Expression.Constant(value))
                | Expression.LessThan(Expression.Constant(value), Expression.Column(index))
            ):
                return self.range_selectivity(node, index, value, False)

            case _:
                return 0.25

    def columns_equal_selectivity(self, node: Node, lhs: int, rhs: int) -> float:
        """Returns the selectivity of an equality of two columns."""
        lhs_statistics = self.column_statistics(node, lhs)
        rhs_statistics = self.column_statistics(node, rhs)
        if lhs_statistics is None or rhs_statistics is None:
            return 0.1
        distinct = max(lhs_statistics[1].distinct, rhs_statistics[1].distinct)
        return 1.0 / distinct if distinct > 0 else 0.0

    def equal_selectivity(self, node: Node, index: int) -> float:
        """Returns the selectivity of an equality of a column and a constant."""
        if (statistics := self.column_statistics(node, index)) is None:
            return 0.1
        rows, column = statistics
        if rows == 0 or column.distinct == 0:
            return 0.0
        return (1.0 - column.nulls / rows) / column.distinct

    def null_selectivity(self, node: Node, index: int) -> float:
        """Returns the selectivity of IS NULL of a column."""
        if (statistics := self.column_statistics(node, index)) is None:
            return 0.1
        rows, column = statistics
        return column.nulls / rows if rows > 0 else 0.0

    def range_selectivity(self, node: Node, index: int, value: Value, below: bool) -> float:
        """Returns the selectivity of a column being below (or above) a
        constant."""
        statistics = self.column_statistics(node, index)
        bound = number(value)
        if statistics is None or bound is None:
            return 1 / 3
        rows, column = statistics
        low, high = number(column.min), number(column.max)
        if low is None or high is None or not math.isfinite(high - low):
            return 1 / 3
        if below:
            if high == low:
                fraction = 1.0 if bound > low else 0.0
            else:
                fraction = (bound - low) / (high - low)
        elif high == low:
            fraction = 1.0 if bound < high else 0.0
        else:
            fraction = (high - bound) / (high - low)
        return min(max(fraction, 0.0), 1.0) * (1.0 - column.nulls / rows)

    def group_count(self, expr: Expression, source: Node) -> float:
        """Returns the estimated number of groups of a GROUP BY expression."""
        match expr:
            case Expression.Column(index):
                if (statistics := self.column_statistics(source, index)) is not None:
                    column = statistics[1]
                    return float(column.distinct + 1 if column.nulls > 0 else column.distinct)
                return 10.0
            case _:
                return 10.0

    def column_statistics(self, node: Node, index: int) -> tuple[int, ColumnStatistics] | None:
        """Returns the statistics of the table column that a column reference
        passes on unchanged, if any."""
        match node:
            # Source nodes use the table's statistics.
            case Node.IndexLookup(table=table) | Node.KeyLookup(table=table) | Node.Scan(
                table=table
            ):
                if (statistics := self.table_statistics(table.name)) is None:
                    return None
                column = statistics.columns.get(table.columns[index].name)
                return (statistics.rows, column) if column is not None else None

            # These nodes pass on plain column references.
            case Node.Aggregate(source, group_by):
                if index < len(group_by):
                    match group_by[index]:
                        case Expression.Column(column):
                            return self.column_statistics(source, column)
                return None
            case Node.Projection(source, expressions):
                match expressions[index]:
                    case Expression.Column(column):
                        return self.column_statistics(source, column)
                    case _:
                        return None
            case Node.Remap(source, targets):
                match invert_remap(targets)[index]:
                    case None:
                        return None
                    case source_index:
                        return self.column_statistics(source, source_index)

            # Joins dispatch to the appropriate source.
            case Node.HashJoin(left=left, right=right) | Node.NestedLoopJoin(
                left=left, right=right
            ):
                if index < left.columns():
                    return self.column_statistics(left, index)
                return self.column_statistics(right, index - left.columns())

            # Views and simple nodes pass through the source columns.
            case (
                Node.Filter(source=source)
                | Node.Limit(source=source)
                | Node.Offset(source=source)
                | Node.Order(source=source)
                | Node.View(source=source)
            ):
                return self.column_statistics(source, index)

            # Constant nodes, system tables and materialized views have no
            # statistics.
            case Node.Nothing() | Node.SystemTable() | Node.Values() | Node.MaterializedView():
                return None

            case _:
                panic(f"unknown node {node!r}")

    def table_rows(self, table: str) -> float:
        """Returns the row count of a table."""
        if (statistics := self.table_statistics(table)) is None:
            return 1000.0
        return float(statistics.rows)

    def table_statistics(self, table: str) -> TableStatistics | None:
        """Returns the statistics of a table, if any."""
        if table not in self.statistics:
            self.statistics[table] = self.catalog.get_statistics(table)
        return self.statistics[table]


def number(value: Value | None) -> float | None:
    """Returns the value of a finite number, or None."""
    match value:
        case Value.Integer(integer):
            return float(integer)
        case Value.Float(f) if math.isfinite(f):
            return f
        case _:
            return None


def is_null(expr: Expression) -> bool:
    """Returns true if an expression is the NULL constant."""
    match expr:
        case Expression.Constant(Value.Null()):
            return True
        case _:
            return False
