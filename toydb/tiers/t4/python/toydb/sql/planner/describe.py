"""Result description for `DESCRIBE`: the result columns of a statement, or
the columns of a table or view, with their data types and nullability.

A statement is described from its plan before optimization, so no expression
is evaluated.

* The type of an expression is the data type of the values it returns when
  its evaluation succeeds. It is unknown if that depends on the values of its
  operands, not only on their types, or if the expression can't return a
  non-NULL value. The NULL constant only returns NULL; an operand of unknown
  type may return values of any type.
* An expression is nullable if it can return NULL: a nullable column, a
  column of the inner side of an outer join, the NULL constant, an operator or
  function with a nullable operand (except `IS NULL`), and every aggregate
  function except `count`.
"""

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toydb.error import errinput, panic
from toydb.sql.planner.plan import Aggregate, Node, Plan, invert_remap
from toydb.sql.planner.planner import Planner
from toydb.sql.types import DataType, Expression, Label, Row, Value

if TYPE_CHECKING:
    from toydb.sql.engine import Catalog

COLUMNS: list[Label] = [Label.Unqualified(name) for name in ("column", "type", "nullable")]
"""The DESCRIBE result columns."""


@dataclass(frozen=True)
class ColumnDescription:
    """A described column: its name, data type and nullability."""

    name: str | None
    """The column name, or None if the column has none."""
    datatype: DataType | None
    """The data type, or None if it is unknown."""
    nullable: bool
    """True if the column can be NULL."""

    def row(self) -> Row:
        """Returns the DESCRIBE result row of the column."""
        return [
            Value.String(self.name) if self.name is not None else Value.Null(),
            Value.String(str(self.datatype)) if self.datatype is not None else Value.Null(),
            Value.Boolean(self.nullable),
        ]


def describe(plan: Plan) -> list[ColumnDescription]:
    """Describes the result columns of a plan: one entry per column of a
    query, none for other statements."""
    match plan:
        case Plan.Select(root):
            descriptions: list[ColumnDescription] = []
            for index in range(root.columns()):
                match root.column_label(index):
                    case Label.None_():
                        name = None
                    case label:
                        name = str(label)
                descriptions.append(ColumnDescription(name, *describe_column(root, index)))
            return descriptions
        case Plan.CreateTable() | Plan.DropTable() | Plan.Delete() | Plan.Insert() | Plan.Update():
            return []
        case _:
            panic(f"unknown plan {plan!r}")


def describe_table(catalog: "Catalog", name: str) -> list[ColumnDescription]:
    """Describes the columns of a table or view."""
    if (table := catalog.get_table(name)) is not None:
        return [
            ColumnDescription(column.name, column.datatype, column.nullable)
            for column in table.columns
        ]
    if (view := catalog.get_view(name)) is not None:
        node = Planner(catalog).build_view(view, None)
        return [
            ColumnDescription(column, *describe_column(node, index))
            for index, column in enumerate(view.columns)
        ]
    raise errinput(f"table {name} does not exist")


def describe_column(node: Node, index: int) -> tuple[DataType | None, bool]:
    """Returns the data type (None if unknown) and the nullability of a
    node's column."""
    match node:
        # Source nodes use the table's column schema.
        case Node.IndexLookup(table=table) | Node.KeyLookup(table=table) | Node.Scan(table=table):
            column = table.columns[index]
            return column.datatype, column.nullable

        # These nodes compute their columns from the source columns.
        case Node.Aggregate(source, group_by, aggregates):
            if index < len(group_by):
                expr = group_by[index]
                return expression_type(expr, source), is_nullable(expr, source)
            aggregate = aggregates[index - len(group_by)]
            return aggregate_type(aggregate, source), aggregate_nullable(aggregate)
        case Node.Projection(source, expressions):
            expr = expressions[index]
            return expression_type(expr, source), is_nullable(expr, source)
        case Node.Remap(source, targets):
            match invert_remap(targets)[index]:
                case None:
                    return None, True
                case source_index:
                    return describe_column(source, source_index)

        # Joins dispatch to the appropriate source. The right side of an outer
        # join is NULL for unmatched left rows.
        case Node.HashJoin(left=left, right=right, outer=outer) | Node.NestedLoopJoin(
            left=left, right=right, outer=outer
        ):
            if index < left.columns():
                return describe_column(left, index)
            datatype, nullable = describe_column(right, index - left.columns())
            return datatype, nullable or outer

        # Views and simple nodes pass through the source columns.
        case (
            Node.Filter(source=source)
            | Node.Limit(source=source)
            | Node.Offset(source=source)
            | Node.Order(source=source)
            | Node.View(source=source)
        ):
            return describe_column(source, index)

        # Nothing nodes don't retain the types of the replaced nodes.
        case Node.Nothing():
            return None, True

        # A Values column returns the value of one of its rows.
        case Node.Values(rows):
            exprs = [row[index] for row in rows]
            types = {expression_type(expr, node) for expr in exprs if not is_null(expr)}
            datatype = types.pop() if len(types) == 1 else None
            return datatype, any(is_nullable(expr, node) for expr in exprs)

        case _:
            panic(f"unknown node {node!r}")


def aggregate_type(aggregate: Aggregate, source: Node) -> DataType | None:
    """Returns the data type of an aggregate function's result, or None if it
    is unknown."""
    match aggregate:
        case Aggregate.Count():
            return DataType.Integer
        case Aggregate.Average(expr) | Aggregate.Sum(expr):
            match expression_type(expr, source):
                case DataType.Integer | DataType.Float as datatype:
                    return datatype
                case _:
                    return None
        case Aggregate.Max(expr) | Aggregate.Min(expr):
            return expression_type(expr, source)
        case _:
            panic(f"unknown aggregate {aggregate!r}")


def aggregate_nullable(aggregate: Aggregate) -> bool:
    """Returns true if an aggregate function's result can be NULL."""
    match aggregate:
        case Aggregate.Count():
            return False
        case Aggregate.Average() | Aggregate.Max() | Aggregate.Min() | Aggregate.Sum():
            return True
        case _:
            panic(f"unknown aggregate {aggregate!r}")


def expression_type(expr: Expression, node: Node) -> DataType | None:
    """Returns the data type of an expression, or None if it is unknown."""
    Boolean, Integer, Float, String = (
        DataType.Boolean,
        DataType.Integer,
        DataType.Float,
        DataType.String,
    )

    def operand(expr: Expression) -> DataType | None:
        return expression_type(expr, node)

    match expr:
        case Expression.Constant(value):
            return value.datatype()
        case Expression.Column(index):
            return describe_column(node, index)[0]

        # a >= b and a <= b are planned as a > b OR a = b (resp. <), a != b as
        # NOT a = b, and a IS NOT x as NOT a IS x, so these have the type of the
        # comparison of a and b, or of a IS x.
        case Expression.Or(
            Expression.GreaterThan(lhs, rhs) | Expression.LessThan(lhs, rhs),
            Expression.Equal(eq_lhs, eq_rhs),
        ) if lhs == eq_lhs and rhs == eq_rhs:
            return comparison_type(lhs, rhs, node)
        case Expression.Not(Expression.Equal(lhs, rhs)):
            return comparison_type(lhs, rhs, node)
        case Expression.Not(Expression.Is() as inner):
            return operand(inner)

        # Logical operators.
        case Expression.And(lhs, rhs) | Expression.Or(lhs, rhs):
            return logical_type([lhs, rhs], node)
        case Expression.Not(inner):
            return logical_type([inner], node)

        # Comparisons.
        case Expression.Equal(lhs, rhs) | Expression.GreaterThan(lhs, rhs) | Expression.LessThan(
            lhs, rhs
        ):
            return comparison_type(lhs, rhs, node)
        case Expression.Is(_, Value.Null()):
            return Boolean
        case Expression.Is(inner, Value.Float(f)) if math.isnan(f):
            return Boolean if may_return(inner, [Float], node) else None
        case Expression.Is(_, value):
            panic(f"invalid IS value {value}")  # enforced by parser
        case Expression.Like(lhs, rhs):
            if may_return(lhs, [String], node) and may_return(rhs, [String], node):
                return Boolean
            return None

        # Mathematical operations.
        case (
            Expression.Add(lhs, rhs)
            | Expression.Divide(lhs, rhs)
            | Expression.Multiply(lhs, rhs)
            | Expression.Remainder(lhs, rhs)
            | Expression.Subtract(lhs, rhs)
        ):
            match (operand(lhs), operand(rhs)):
                case (DataType.Integer, DataType.Integer):
                    return Integer
                case _:
                    return float_type(lhs, rhs, node)
        case Expression.Exponentiate(lhs, rhs):
            return float_type(lhs, rhs, node)
        case Expression.Factorial(inner):
            return Integer if may_return(inner, [Integer], node) else None
        case Expression.Identity(inner) | Expression.Negate(inner):
            match operand(inner):
                case DataType.Integer | DataType.Float as datatype:
                    return datatype
                case _:
                    return None
        case Expression.SquareRoot(inner):
            return Float if may_return(inner, [Integer, Float], node) else None

        case _:
            panic(f"unknown expression {expr!r}")


def logical_type(operands: list[Expression], node: Node) -> DataType | None:
    """Returns the data type of AND, OR or NOT of the given operands."""
    if any(expression_type(expr, node) not in (DataType.Boolean, None) for expr in operands):
        return None
    if all(is_null(expr) for expr in operands):
        return None
    return DataType.Boolean


def comparison_type(lhs: Expression, rhs: Expression, node: Node) -> DataType | None:
    """Returns the data type of a comparison of two operands."""
    if is_null(lhs) or is_null(rhs):
        return None
    match (expression_type(lhs, node), expression_type(rhs, node)):
        case (None, _) | (_, None):
            return DataType.Boolean
        case (lhs_type, rhs_type) if lhs_type == rhs_type:
            return DataType.Boolean
        case (DataType.Integer | DataType.Float, DataType.Integer | DataType.Float):
            return DataType.Boolean
        case _:
            return None


def float_type(lhs: Expression, rhs: Expression, node: Node) -> DataType | None:
    """Returns the data type of arithmetic with a FLOAT operand."""
    numbers = [DataType.Integer, DataType.Float]
    match (expression_type(lhs, node), expression_type(rhs, node)):
        case (DataType.Float, _) if may_return(rhs, numbers, node):
            return DataType.Float
        case (_, DataType.Float) if may_return(lhs, numbers, node):
            return DataType.Float
        case _:
            return None


def may_return(expr: Expression, datatypes: list[DataType], node: Node) -> bool:
    """Returns true if an expression can return a value of one of the given
    data types."""
    match expression_type(expr, node):
        case None:
            return not is_null(expr)
        case datatype:
            return datatype in datatypes


def is_null(expr: Expression) -> bool:
    """Returns true if an expression is the NULL constant."""
    match expr:
        case Expression.Constant(Value.Null()):
            return True
        case _:
            return False


def is_nullable(expr: Expression, node: Node) -> bool:
    """Returns true if an expression can return NULL."""
    match expr:
        case Expression.Constant(value):
            return isinstance(value, Value.Null)
        case Expression.Column(index):
            return describe_column(node, index)[1]
        case Expression.Is(_, Value.Null()):
            return False

        case (
            Expression.Add(lhs, rhs)
            | Expression.And(lhs, rhs)
            | Expression.Divide(lhs, rhs)
            | Expression.Equal(lhs, rhs)
            | Expression.Exponentiate(lhs, rhs)
            | Expression.GreaterThan(lhs, rhs)
            | Expression.LessThan(lhs, rhs)
            | Expression.Like(lhs, rhs)
            | Expression.Multiply(lhs, rhs)
            | Expression.Or(lhs, rhs)
            | Expression.Remainder(lhs, rhs)
            | Expression.Subtract(lhs, rhs)
        ):
            return is_nullable(lhs, node) or is_nullable(rhs, node)

        case (
            Expression.Factorial(inner)
            | Expression.Identity(inner)
            | Expression.Is(inner, _)
            | Expression.Negate(inner)
            | Expression.Not(inner)
            | Expression.SquareRoot(inner)
        ):
            return is_nullable(inner, node)

        case _:
            panic(f"unknown expression {expr!r}")
