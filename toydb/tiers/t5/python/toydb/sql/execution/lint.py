"""Statement lint for `LINT`: warnings about likely mistakes in a statement and
the statements it contains, without planning or executing them. Each warning
has a code and a message.

* `unfiltered_write`: UPDATE or DELETE without WHERE.
* `implicit_columns`: INSERT without a column list.
* `null_comparison`: a comparison with the NULL constant.
* `type_mismatch`: a column compared with a literal or parameter of a type it
  can't be compared with.
* `like_without_wildcard`: LIKE with a pattern without `%` or `_`.
* `division_by_zero`: division or remainder by the integer literal 0.
* `constant_condition`: a WHERE, HAVING or ON condition without columns,
  aggregates or parameters.
* `order_by_constant`: an ORDER BY expression without columns, aggregates or
  parameters.

Column references are resolved against the tables and views the statement
reads; references that don't resolve are not checked. Warnings are listed in
the order of the statement's canonical text.
"""

from dataclasses import dataclass

from toydb.error import InvalidInput, panic
from toydb.sql.engine import Catalog
from toydb.sql.execution.system import is_system_table
from toydb.sql.parser import ast
from toydb.sql.parser.format import format_expr, format_name
from toydb.sql.planner.describe import describe_table
from toydb.sql.planner.planner import Planner
from toydb.sql.types import DataType, Label, Row, Value

COLUMNS: list[Label] = [Label.Unqualified(name) for name in ("code", "message")]
"""The LINT result columns."""


@dataclass(frozen=True)
class LintWarning:
    """A lint warning: its code and message."""

    code: str
    message: str

    def row(self) -> Row:
        """Returns the LINT result row of the warning."""
        return [Value.String(self.code), Value.String(self.message)]


def lint(catalog: Catalog, statement: ast.Statement) -> list[LintWarning]:
    """Returns the warnings for a statement and the statements it contains, in
    canonical text order."""
    linter = Linter(catalog)
    linter.lint_statement(statement, None)
    return linter.warnings


@dataclass
class Source:
    """A table or view that a statement reads, with its column types."""

    name: str
    alias: str | None
    columns: dict[str, DataType | None]
    """The column types, None where unknown."""


@dataclass
class Scope:
    """The tables and views that a statement reads, and the parameter types of
    the PREPARE that contains it, if any."""

    sources: list[Source]
    types: list[DataType] | None

    def column_type(self, table: str | None, column: str) -> DataType | None:
        """Returns the type of a column reference, or None if it doesn't
        resolve or its type is unknown."""
        if table is not None:
            matches = [
                source
                for source in self.sources
                if (source.alias if source.alias is not None else source.name) == table
            ]
        else:
            matches = [source for source in self.sources if column in source.columns]
        if len(matches) != 1:
            return None
        return matches[0].columns.get(column)

    def parameter_type(self, number: int) -> DataType | None:
        """Returns the type of a parameter, or None if it is unknown."""
        if self.types is None or not 1 <= number <= len(self.types):
            return None
        return self.types[number - 1]


class Linter:
    """Collects the warnings of a statement."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.warnings: list[LintWarning] = []

    def warn(self, code: str, message: str) -> None:
        """Adds a warning."""
        self.warnings.append(LintWarning(code, message))

    def lint_statement(self, statement: ast.Statement, types: list[DataType] | None) -> None:
        """Lints a statement and the statements it contains, given the
        parameter types of the PREPARE that contains it, if any."""
        match statement:
            case (
                ast.Statement.Explain(inner)
                | ast.Statement.Format(inner)
                | ast.Statement.Describe(ast.Statement() as inner)
                | ast.Statement.Estimate(inner)
                | ast.Statement.Lint(inner)
            ):
                self.lint_statement(inner, types)
            case ast.Statement.Prepare(_, prepared_types, inner):
                self.lint_statement(inner, prepared_types)
            case ast.Statement.CreateView(query=query) | ast.Statement.CreateMaterializedView(
                query=query
            ):
                self.lint_statement(query, types)

            case ast.Statement.CreateTable():
                # Defaults and CHECK conditions, in canonical text order.
                scope = Scope([], types)
                for expr in statement.expressions():
                    self.lint_expression(expr, scope)
            case ast.Statement.CreatePolicy():
                # The USING and WITH CHECK conditions.
                scope = Scope([], types)
                for expr in statement.expressions():
                    self.lint_expression(expr, scope)
            case ast.Statement.Delete(table, where):
                if where is None:
                    self.warn(
                        "unfiltered_write",
                        f"DELETE without WHERE deletes every row of {format_name(table)}",
                    )
                else:
                    self.lint_condition(where, Scope(self.sources(table, None), types))
            case ast.Statement.Insert(table, columns, values):
                if columns is None:
                    self.warn(
                        "implicit_columns",
                        "INSERT without a column list depends on the column order of "
                        f"{format_name(table)}",
                    )
                scope = Scope([], types)
                for row in values:
                    for expr in row:
                        self.lint_expression(expr, scope)
            case ast.Statement.Update(table, set, where):
                if where is None:
                    self.warn(
                        "unfiltered_write",
                        f"UPDATE without WHERE changes every row of {format_name(table)}",
                    )
                scope = Scope(self.sources(table, None), types)
                for expr in set.values():
                    if expr is not None:
                        self.lint_expression(expr, scope)
                if where is not None:
                    self.lint_condition(where, scope)
            case ast.Statement.Select() as select:
                self.lint_select(select, types)
            case ast.Statement.Execute(args=args):
                scope = Scope([], types)
                for arg in args:
                    self.lint_expression(arg, scope)

            case (
                ast.Statement.Begin()
                | ast.Statement.Commit()
                | ast.Statement.Rollback()
                | ast.Statement.DropTable()
                | ast.Statement.ShowStatements()
                | ast.Statement.Analyze()
                | ast.Statement.ShowStatistics()
                | ast.Statement.Export()
                | ast.Statement.Import()
                | ast.Statement.DropView()
                | ast.Statement.ShowViews()
                | ast.Statement.Describe()
                | ast.Statement.Deallocate()
                | ast.Statement.ShowPrepared()
                | ast.Statement.ShowChanges()
                | ast.Statement.CreateUser()
                | ast.Statement.DropUser()
                | ast.Statement.Grant()
                | ast.Statement.Revoke()
                | ast.Statement.SetRole()
                | ast.Statement.ResetRole()
                | ast.Statement.ShowUsers()
                | ast.Statement.ShowGrants()
                | ast.Statement.ShowAudit()
                | ast.Statement.ShowSessions()
                | ast.Statement.DropPolicy()
                | ast.Statement.RefreshMaterializedView()
                | ast.Statement.DropMaterializedView()
            ):
                pass

            case _:
                panic(f"unknown statement {statement!r}")

    def lint_select(self, select: ast.Select, types: list[DataType] | None) -> None:
        """Lints a SELECT statement, clause by clause."""
        sources: list[Source] = []
        for item in select.from_:
            sources += self.from_sources(item)
        scope = Scope(sources, types)

        for expr, _ in select.select:
            self.lint_expression(expr, scope)
        for item in select.from_:
            self.lint_from(item, scope)
        if select.where is not None:
            self.lint_condition(select.where, scope)
        for expr in select.group_by:
            self.lint_expression(expr, scope)
        if select.having is not None:
            self.lint_condition(select.having, scope)
        for expr, _ in select.order_by:
            self.lint_order(expr, scope)
        if select.limit is not None:
            self.lint_expression(select.limit, scope)
        if select.offset is not None:
            self.lint_expression(select.offset, scope)

    def lint_from(self, item: ast.From, scope: Scope) -> None:
        """Lints the join conditions of a FROM item."""
        match item:
            case ast.From.Table():
                pass
            case ast.From.Join(left, right, _, predicate):
                self.lint_from(left, scope)
                self.lint_from(right, scope)
                if predicate is not None:
                    self.lint_condition(predicate, scope)
            case _:
                panic(f"unknown from item {item!r}")

    def from_sources(self, item: ast.From) -> list[Source]:
        """Returns the tables and views of a FROM item that exist."""
        match item:
            case ast.From.Table(name, alias):
                return self.sources(name, alias)
            case ast.From.Join(left, right):
                return self.from_sources(left) + self.from_sources(right)
            case _:
                panic(f"unknown from item {item!r}")

    def sources(self, name: str, alias: str | None) -> list[Source]:
        """Returns the table or view of the given name, if it resolves, with
        the column types that DESCRIBE gives it."""
        if is_system_table(self.catalog, name):
            return []
        try:
            described = describe_table(self.catalog, name)
        except InvalidInput:
            # Tables and views that don't resolve are not checked.
            return []
        columns = {
            column.name: column.datatype for column in described if column.name is not None
        }
        return [Source(name, alias, columns)]

    def lint_condition(self, expr: ast.Expression, scope: Scope) -> None:
        """Lints a WHERE, HAVING or ON condition."""
        self.check(expr, scope)
        if is_constant(expr):
            self.warn("constant_condition", f"condition is constant: {format_expr(expr)}")
        self.lint_operands(expr, scope)

    def lint_order(self, expr: ast.Expression, scope: Scope) -> None:
        """Lints an ORDER BY expression."""
        self.check(expr, scope)
        if is_constant(expr):
            self.warn(
                "order_by_constant", f"ORDER BY a constant has no effect: {format_expr(expr)}"
            )
        self.lint_operands(expr, scope)

    def lint_expression(self, expr: ast.Expression, scope: Scope) -> None:
        """Lints an expression and the expressions it contains."""
        self.check(expr, scope)
        self.lint_operands(expr, scope)

    def lint_operands(self, expr: ast.Expression, scope: Scope) -> None:
        """Lints the operands of an expression."""
        for operand in operands(expr):
            self.lint_expression(operand, scope)

    def check(self, expr: ast.Expression, scope: Scope) -> None:
        """Checks an expression node, without its operands."""
        if (comparison := as_comparison(expr)) is not None:
            lhs, rhs = comparison
            if is_null(lhs) or is_null(rhs):
                self.warn(
                    "null_comparison", f"comparison with NULL is never true: {format_expr(expr)}"
                )
            for column, value in ((lhs, rhs), (rhs, lhs)):
                if (message := type_mismatch(column, value, scope)) is not None:
                    self.warn("type_mismatch", message)

        match expr:
            case ast.Expression.Operator(
                ast.Operator.Like(_, ast.Expression.Literal(ast.Literal.String(pattern)))
            ) if "%" not in pattern and "_" not in pattern:
                self.warn(
                    "like_without_wildcard",
                    f"LIKE without wildcards matches like =: {format_expr(expr)}",
                )
            case ast.Expression.Operator(
                ast.Operator.Divide(_, ast.Expression.Literal(ast.Literal.Integer(0)))
                | ast.Operator.Remainder(_, ast.Expression.Literal(ast.Literal.Integer(0)))
            ):
                self.warn("division_by_zero", f"division by zero: {format_expr(expr)}")
            case _:
                pass


def operands(expr: ast.Expression) -> list[ast.Expression]:
    """Returns the operands of an expression, left to right."""
    match expr:
        case ast.Expression.Operator(op):
            match op:
                case (
                    ast.Operator.Add(lhs, rhs)
                    | ast.Operator.And(lhs, rhs)
                    | ast.Operator.Divide(lhs, rhs)
                    | ast.Operator.Equal(lhs, rhs)
                    | ast.Operator.Exponentiate(lhs, rhs)
                    | ast.Operator.GreaterThan(lhs, rhs)
                    | ast.Operator.GreaterThanOrEqual(lhs, rhs)
                    | ast.Operator.LessThan(lhs, rhs)
                    | ast.Operator.LessThanOrEqual(lhs, rhs)
                    | ast.Operator.Like(lhs, rhs)
                    | ast.Operator.Multiply(lhs, rhs)
                    | ast.Operator.NotEqual(lhs, rhs)
                    | ast.Operator.Or(lhs, rhs)
                    | ast.Operator.Remainder(lhs, rhs)
                    | ast.Operator.Subtract(lhs, rhs)
                ):
                    return [lhs, rhs]
                case (
                    ast.Operator.Factorial(inner)
                    | ast.Operator.Identity(inner)
                    | ast.Operator.Is(inner, _)
                    | ast.Operator.IsNot(inner, _)
                    | ast.Operator.Negate(inner)
                    | ast.Operator.Not(inner)
                ):
                    return [inner]
                case _:
                    panic(f"unknown operator {op!r}")
        case ast.Expression.Function(_, args):
            return list(args)
        case (
            ast.Expression.All()
            | ast.Expression.Column()
            | ast.Expression.Literal()
            | ast.Expression.Parameter()
        ):
            return []
        case _:
            panic(f"unknown expression {expr!r}")


def as_comparison(expr: ast.Expression) -> tuple[ast.Expression, ast.Expression] | None:
    """Returns the operands of a comparison, or None if the expression isn't
    one."""
    match expr:
        case ast.Expression.Operator(
            ast.Operator.Equal(lhs, rhs)
            | ast.Operator.NotEqual(lhs, rhs)
            | ast.Operator.GreaterThan(lhs, rhs)
            | ast.Operator.GreaterThanOrEqual(lhs, rhs)
            | ast.Operator.LessThan(lhs, rhs)
            | ast.Operator.LessThanOrEqual(lhs, rhs)
        ):
            return lhs, rhs
        case _:
            return None


def type_mismatch(column: ast.Expression, value: ast.Expression, scope: Scope) -> str | None:
    """Returns the type_mismatch message for a column compared with a value,
    or None if there is no mismatch."""
    match column:
        case ast.Expression.Column(table, name):
            column_type = scope.column_type(table, name)
        case _:
            return None
    match value:
        case (
            ast.Expression.Literal(literal)
            | ast.Expression.Operator(ast.Operator.Negate(ast.Expression.Literal(literal)))
            | ast.Expression.Operator(ast.Operator.Identity(ast.Expression.Literal(literal)))
        ):
            value_type = literal_type(literal)
            kind = "value"
        case ast.Expression.Parameter(number):
            value_type = scope.parameter_type(number)
            kind = "parameter"
        case _:
            return None
    if column_type is None or value_type is None or comparable(column_type, value_type):
        return None
    return (
        f"{column_type} column {format_expr(column)} compared with "
        f"{value_type} {kind} {format_expr(value)}"
    )


def literal_type(literal: ast.Literal) -> DataType | None:
    """Returns the data type of a literal, or None for NULL."""
    return Planner.build_constant_value(ast.Expression.from_literal(literal)).datatype()


def comparable(a: DataType, b: DataType) -> bool:
    """Returns true if values of the two data types can be compared."""
    numbers = (DataType.Integer, DataType.Float)
    return a == b or (a in numbers and b in numbers)


def is_null(expr: ast.Expression) -> bool:
    """Returns true if an expression is the NULL constant."""
    match expr:
        case ast.Expression.Literal(ast.Literal.Null()):
            return True
        case _:
            return False


def is_constant(expr: ast.Expression) -> bool:
    """Returns true if an expression contains no column reference, aggregate
    function or parameter."""
    match expr:
        case ast.Expression.Column() | ast.Expression.Parameter():
            return False
        case _ if Planner.is_aggregate_function(expr):
            return False
        case _:
            return all(is_constant(operand) for operand in operands(expr))
