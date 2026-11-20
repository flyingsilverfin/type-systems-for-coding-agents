"""Statement digests: per-session statement statistics, grouped by the
statement's digest.

A digest is the statement's canonical SQL text with every literal replaced by
`?`. It is written from the parsed statement, so statements that differ only
in literal values, letter case, whitespace, redundant parentheses, optional
words or synonymous spellings (e.g. `<>` and `!=`, `INT` and `INTEGER`) have
the same digest. Otherwise, every construct is written as it was written:
`a >= b` is not `a > b OR a = b`, and `a IS NOT NULL` is not `NOT a IS NULL`.
"""

from dataclasses import dataclass

from toydb.error import panic
from toydb.sql.parser import ast
from toydb.sql.parser.lexer import format_ident
from toydb.sql.types import Label, Row, Value


def digest(statement: ast.Statement) -> str:
    """Returns the digest of a statement."""
    match statement:
        case ast.Statement.Begin(read_only, as_of):
            output = "BEGIN"
            if read_only:
                output += " READ ONLY"
            if as_of is not None:
                output += " AS OF SYSTEM TIME ?"
            return output
        case ast.Statement.Commit():
            return "COMMIT"
        case ast.Statement.Rollback():
            return "ROLLBACK"
        case ast.Statement.Explain(inner):
            return f"EXPLAIN {digest(inner)}"

        case ast.Statement.CreateTable(name, columns):
            return f"CREATE TABLE {format_ident(name)} ({', '.join(map(format_column, columns))})"
        case ast.Statement.DropTable(name, if_exists):
            return f"DROP TABLE {'IF EXISTS ' if if_exists else ''}{format_ident(name)}"

        case ast.Statement.Delete(table, where):
            return f"DELETE FROM {format_ident(table)}{format_where(where)}"
        case ast.Statement.Insert(table, columns, values):
            output = f"INSERT INTO {format_ident(table)}"
            if columns is not None:
                output += f" ({', '.join(map(format_ident, columns))})"
            rows = ", ".join(f"({format_exprs(row)})" for row in values)
            return f"{output} VALUES {rows}"
        case ast.Statement.Update(table, set, where):
            # The parser orders the assignments by column name.
            assignments = ", ".join(
                f"{format_ident(column)} = {format_expr(expr) if expr is not None else 'DEFAULT'}"
                for column, expr in set.items()
            )
            return f"UPDATE {format_ident(table)} SET {assignments}{format_where(where)}"
        case ast.Statement.Select():
            return format_select(statement)

        case ast.Statement.ShowStatements():
            return "SHOW STATEMENTS"
        case ast.Statement.Analyze(table):
            return "ANALYZE" if table is None else f"ANALYZE {format_ident(table)}"
        case ast.Statement.ShowStatistics(table):
            return f"SHOW STATISTICS {format_ident(table)}"
        case ast.Statement.Export(table, format):
            target = f" {format_ident(table)}" if table is not None else ""
            return f"EXPORT{target} AS {format.name.upper()}"
        case ast.Statement.Import(table, format, lines):
            literals = ", ".join("?" for _ in lines)
            return f"IMPORT INTO {format_ident(table)} FROM {format.name.upper()} {literals}"

        case _:
            panic(f"unknown statement {statement!r}")


def format_column(column: ast.Column) -> str:
    """Formats a CREATE TABLE column definition, with its constraints in a
    fixed order."""
    output = f"{format_ident(column.name)} {column.datatype}"
    if column.primary_key:
        output += " PRIMARY KEY"
    match column.nullable:
        case True:
            output += " NULL"
        case False:
            output += " NOT NULL"
        case None:
            pass
    if column.default is not None:
        output += f" DEFAULT {format_expr(column.default)}"
    if column.unique:
        output += " UNIQUE"
    if column.index:
        output += " INDEX"
    if column.references is not None:
        output += f" REFERENCES {format_ident(column.references)}"
    return output


def format_select(select: ast.Select) -> str:
    """Formats a SELECT statement, with its clauses in canonical order."""
    items = ", ".join(
        format_expr(expr) + (f" AS {format_ident(alias)}" if alias is not None else "")
        for expr, alias in select.select
    )
    output = f"SELECT {items}"
    if select.from_:
        output += f" FROM {', '.join(map(format_from, select.from_))}"
    output += format_where(select.where)
    if select.group_by:
        output += f" GROUP BY {format_exprs(select.group_by)}"
    if select.having is not None:
        output += f" HAVING {format_expr(select.having)}"
    if select.order_by:
        order = ", ".join(
            format_expr(expr) + (" DESC" if direction == ast.Direction.Descending else "")
            for expr, direction in select.order_by
        )
        output += f" ORDER BY {order}"
    if select.limit is not None:
        output += f" LIMIT {format_expr(select.limit)}"
    if select.offset is not None:
        output += f" OFFSET {format_expr(select.offset)}"
    return output


def format_from(item: ast.From) -> str:
    """Formats a FROM item. Joins are written left to right, as nested by the
    parser."""
    match item:
        case ast.From.Table(name, alias):
            return format_ident(name) + (f" AS {format_ident(alias)}" if alias is not None else "")
        case ast.From.Join(left, right, type, predicate):
            match type:
                case ast.JoinType.Cross:
                    join = "CROSS JOIN"
                case ast.JoinType.Inner:
                    join = "JOIN"
                case ast.JoinType.Left:
                    join = "LEFT JOIN"
                case ast.JoinType.Right:
                    join = "RIGHT JOIN"
            output = f"{format_from(left)} {join} {format_from(right)}"
            if predicate is not None:
                output += f" ON {format_expr(predicate)}"
            return output
        case _:
            panic(f"unknown from item {item!r}")


def format_where(where: ast.Expression | None) -> str:
    """Formats an optional WHERE clause, with a leading space."""
    return f" WHERE {format_expr(where)}" if where is not None else ""


def format_exprs(exprs: list[ast.Expression]) -> str:
    """Formats a comma-separated list of expressions."""
    return ", ".join(map(format_expr, exprs))


def format_expr(expr: ast.Expression) -> str:
    """Formats an expression, with literals as ? and parentheses only where
    the parser needs them."""
    level = precedence(expr)

    def left(operand: ast.Expression) -> str:
        """The left operand of a left-associative binary operator."""
        return group(operand, precedence(operand) < level)

    def right(operand: ast.Expression) -> str:
        """The right operand of a left-associative binary operator."""
        return group(operand, precedence(operand) <= level)

    match expr:
        case ast.Expression.All():
            return "*"
        case ast.Expression.Column(table, column):
            if table is not None:
                return f"{format_ident(table)}.{format_ident(column)}"
            return format_ident(column)
        case ast.Expression.Literal():
            return "?"
        case ast.Expression.Function(name, args):
            return f"{format_ident(name)}({format_exprs(list(args))})"
        case ast.Expression.Operator(op):
            pass
        case _:
            panic(f"unknown expression {expr!r}")

    match op:
        case ast.Operator.And(lhs, rhs):
            return f"{left(lhs)} AND {right(rhs)}"
        case ast.Operator.Or(lhs, rhs):
            return f"{left(lhs)} OR {right(rhs)}"
        case ast.Operator.Not(inner):
            return f"NOT {group(inner, precedence(inner) < level)}"

        case ast.Operator.Equal(lhs, rhs):
            return f"{left(lhs)} = {right(rhs)}"
        case ast.Operator.NotEqual(lhs, rhs):
            return f"{left(lhs)} != {right(rhs)}"
        case ast.Operator.GreaterThan(lhs, rhs):
            return f"{left(lhs)} > {right(rhs)}"
        case ast.Operator.GreaterThanOrEqual(lhs, rhs):
            return f"{left(lhs)} >= {right(rhs)}"
        case ast.Operator.LessThan(lhs, rhs):
            return f"{left(lhs)} < {right(rhs)}"
        case ast.Operator.LessThanOrEqual(lhs, rhs):
            return f"{left(lhs)} <= {right(rhs)}"
        case ast.Operator.Like(lhs, rhs):
            return f"{left(lhs)} LIKE {right(rhs)}"
        case ast.Operator.Is(inner, literal):
            return f"{group(inner, precedence(inner) < level)} IS {format_is(literal)}"
        case ast.Operator.IsNot(inner, literal):
            return f"{group(inner, precedence(inner) < level)} IS NOT {format_is(literal)}"

        case ast.Operator.Add(lhs, rhs):
            return f"{left(lhs)} + {right(rhs)}"
        case ast.Operator.Subtract(lhs, rhs):
            return f"{left(lhs)} - {right(rhs)}"
        case ast.Operator.Multiply(lhs, rhs):
            return f"{left(lhs)} * {right(rhs)}"
        case ast.Operator.Divide(lhs, rhs):
            return f"{left(lhs)} / {right(rhs)}"
        case ast.Operator.Remainder(lhs, rhs):
            return f"{left(lhs)} % {right(rhs)}"
        case ast.Operator.Exponentiate(lhs, rhs):
            # Right-associative.
            lhs_group = group(lhs, precedence(lhs) <= level)
            rhs_group = group(rhs, precedence(rhs) < level)
            return f"{lhs_group} ^ {rhs_group}"
        case ast.Operator.Factorial(inner):
            return f"{group(inner, precedence(inner) < level)}!"
        case ast.Operator.Identity(inner):
            return f"+{group(inner, precedence(inner) < level)}"
        case ast.Operator.Negate(inner):
            return f"-{group(inner, precedence(inner) < level)}"

        case _:
            panic(f"unknown operator {op!r}")


def format_is(literal: ast.Literal) -> str:
    """Formats the NULL or NAN of IS [NOT] NULL/NAN, which belongs to the
    operator rather than being a literal."""
    match literal:
        case ast.Literal.Null():
            return "NULL"
        case ast.Literal.Float():
            return "NAN"
        case _:
            panic(f"invalid IS value {literal!r}")  # enforced by parser


def group(expr: ast.Expression, parenthesize: bool) -> str:
    """Formats an expression, in parentheses if requested."""
    formatted = format_expr(expr)
    return f"({formatted})" if parenthesize else formatted


def precedence(expr: ast.Expression) -> int:
    """The precedence level of an expression, from 1 (loosest) to 11 (atoms).
    Matches the parser."""
    match expr:
        case ast.Expression.Operator(op):
            match op:
                case ast.Operator.Or():
                    return 1
                case ast.Operator.And():
                    return 2
                case ast.Operator.Not():
                    return 3
                case (
                    ast.Operator.Equal()
                    | ast.Operator.NotEqual()
                    | ast.Operator.Like()
                    | ast.Operator.Is()
                    | ast.Operator.IsNot()
                ):
                    return 4
                case (
                    ast.Operator.GreaterThan()
                    | ast.Operator.GreaterThanOrEqual()
                    | ast.Operator.LessThan()
                    | ast.Operator.LessThanOrEqual()
                ):
                    return 5
                case ast.Operator.Add() | ast.Operator.Subtract():
                    return 6
                case ast.Operator.Multiply() | ast.Operator.Divide() | ast.Operator.Remainder():
                    return 7
                case ast.Operator.Exponentiate():
                    return 8
                case ast.Operator.Factorial():
                    return 9
                case ast.Operator.Identity() | ast.Operator.Negate():
                    return 10
                case _:
                    panic(f"unknown operator {op!r}")
        case _:
            return 11


@dataclass
class StatementStatistics:
    """Statistics of the statements with one digest."""

    calls: int = 0
    """The number of statements executed, successfully or not."""
    errors: int = 0
    """The number of statements that failed."""
    rows: int = 0
    """The number of rows returned or written by successful statements."""


class StatementDigests:
    """A session's statement statistics, by digest, in the order in which the
    session first executed a statement with each digest."""

    COLUMNS: list[Label] = [
        Label.Unqualified("digest"),
        Label.Unqualified("calls"),
        Label.Unqualified("errors"),
        Label.Unqualified("rows"),
    ]
    """The SHOW STATEMENTS result columns."""

    def __init__(self) -> None:
        self.statements: dict[str, StatementStatistics] = {}
        """Statement statistics by digest, in insertion order."""

    def record(self, digest: str, rows: int) -> None:
        """Records a successful statement, which produced the given number of
        rows."""
        statistics = self.statements.setdefault(digest, StatementStatistics())
        statistics.calls += 1
        statistics.rows += rows

    def record_error(self, digest: str) -> None:
        """Records a failed statement."""
        statistics = self.statements.setdefault(digest, StatementStatistics())
        statistics.calls += 1
        statistics.errors += 1

    def rows(self) -> list[Row]:
        """Returns the SHOW STATEMENTS result rows."""
        return [
            [
                Value.String(digest),
                Value.Integer(statistics.calls),
                Value.Integer(statistics.errors),
                Value.Integer(statistics.rows),
            ]
            for digest, statistics in self.statements.items()
        ]
