"""Canonical SQL text of statements and expressions, written from the AST. It
is the output of `FORMAT`, the text that views store for their query, the
statement text of `SHOW PREPARED`, and the text of expressions in lint
messages and of views in the change log.

* Keywords are upper case. Names are written so that they read back as the
  same name: unquoted only if they are a lower-case identifier that isn't a
  keyword, otherwise between double quotes with `"` doubled. (Statement
  digests and the schema display quote names differently.)
* Literals are written as SQL text that reads back as the same literal:
  floats as displayed in query results, `NAN` and `INFINITY`, strings between
  single quotes with `'` doubled, literals of other kinds as the SQL text of
  their value. A minus sign is an operator, not part of a literal. Parameters
  are written `$n`.
* Synonyms are written in one form, optional words are left out, aliases use
  `AS`, and operands are parenthesized exactly where the parser needs it, as
  in statement digests. Every construct is written as it was written.
* A statement is a list of lines: each clause on a line of its own (`FROM`,
  `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, `OFFSET`, `SET`), joins
  on lines indented by two spaces, the rows of `VALUES` and the columns of
  `CREATE TABLE` one per indented line, and a statement contained in another
  one on the following lines. The one-line form joins the lines with single
  spaces.
"""

from toydb.error import Error, panic
from toydb.sql.parser import ast
from toydb.sql.parser.lexer import Lexer, Token
from toydb.sql.types.formats import to_sql_literal
from toydb.sql.types.value import Value

INDENT = "  "
"""The indentation of indented lines."""


def format_statement(statement: ast.Statement) -> list[str]:
    """Returns the canonical text of a statement, as lines."""
    match statement:
        case ast.Statement.Begin(read_only, as_of):
            line = "BEGIN"
            if read_only:
                line += " READ ONLY"
            if as_of is not None:
                line += f" AS OF SYSTEM TIME {as_of}"
            return [line]
        case ast.Statement.Commit():
            return ["COMMIT"]
        case ast.Statement.Rollback():
            return ["ROLLBACK"]
        case ast.Statement.Explain(inner):
            return ["EXPLAIN", *format_statement(inner)]

        case ast.Statement.CreateTable(name, columns):
            lines = [f"CREATE TABLE {format_name(name)} ("]
            lines += format_list([format_column(column) for column in columns])
            return [*lines, ")"]
        case ast.Statement.DropTable(name, if_exists):
            return [f"DROP TABLE {'IF EXISTS ' if if_exists else ''}{format_name(name)}"]

        case ast.Statement.Delete(table, where):
            return [f"DELETE FROM {format_name(table)}", *format_where(where)]
        case ast.Statement.Insert(table, columns, values):
            line = f"INSERT INTO {format_name(table)}"
            if columns is not None:
                line += f" ({', '.join(map(format_name, columns))})"
            rows = [f"({format_exprs(row)})" for row in values]
            return [f"{line} VALUES", *format_list(rows)]
        case ast.Statement.Update(table, set, where):
            assignments = ", ".join(
                f"{format_name(column)} = {format_expr(expr) if expr is not None else 'DEFAULT'}"
                for column, expr in set.items()
            )
            return [f"UPDATE {format_name(table)}", f"SET {assignments}", *format_where(where)]
        case ast.Statement.Select():
            return format_select(statement)

        case ast.Statement.ShowStatements():
            return ["SHOW STATEMENTS"]
        case ast.Statement.Analyze(table):
            return ["ANALYZE" if table is None else f"ANALYZE {format_name(table)}"]
        case ast.Statement.ShowStatistics(table):
            return [f"SHOW STATISTICS {format_name(table)}"]
        case ast.Statement.Export(table, format):
            target = f" {format_name(table)}" if table is not None else ""
            return [f"EXPORT{target} AS {format.name.upper()}"]
        case ast.Statement.Import(table, format, lines):
            literals = ", ".join(format_literal(ast.Literal.String(line)) for line in lines)
            return [f"IMPORT INTO {format_name(table)} FROM {format.name.upper()} {literals}"]

        case ast.Statement.Format(inner):
            return ["FORMAT", *format_statement(inner)]
        case ast.Statement.CreateView(name, columns, query):
            line = f"CREATE VIEW {format_name(name)}"
            if columns is not None:
                line += f" ({', '.join(map(format_name, columns))})"
            return [f"{line} AS", *format_select(query)]
        case ast.Statement.DropView(name, if_exists):
            return [f"DROP VIEW {'IF EXISTS ' if if_exists else ''}{format_name(name)}"]
        case ast.Statement.ShowViews():
            return ["SHOW VIEWS"]
        case ast.Statement.Describe(str() as name):
            return [f"DESCRIBE {format_name(name)}"]
        case ast.Statement.Describe(ast.Statement() as inner):
            return ["DESCRIBE", *format_statement(inner)]
        case ast.Statement.Estimate(inner):
            return ["ESTIMATE", *format_statement(inner)]
        case ast.Statement.Prepare(name, types, inner):
            line = f"PREPARE {format_name(name)}"
            if types:
                line += f" ({', '.join(map(str, types))})"
            return [f"{line} AS", *format_statement(inner)]
        case ast.Statement.Execute(name, args):
            line = f"EXECUTE {format_name(name)}"
            if args:
                line += f" ({format_exprs(args)})"
            return [line]
        case ast.Statement.Deallocate(name):
            return [f"DEALLOCATE {format_name(name)}"]
        case ast.Statement.ShowPrepared():
            return ["SHOW PREPARED"]
        case ast.Statement.Lint(inner):
            return ["LINT", *format_statement(inner)]
        case ast.Statement.ShowChanges(since):
            return ["SHOW CHANGES" if since is None else f"SHOW CHANGES SINCE {since}"]

        case _:
            panic(f"unknown statement {statement!r}")


def format_one_line(statement: ast.Statement) -> str:
    """Returns the canonical text of a statement on one line: its lines
    without indentation, joined by single spaces (without spaces inside the
    parentheses of a column list)."""
    output = ""
    for line in format_statement(statement):
        line = line.lstrip(" ")
        if output and not output.endswith("(") and line != ")":
            output += " "
        output += line
    return output


def format_list(items: list[str]) -> list[str]:
    """Formats the items of a list on indented lines."""
    return [f"{INDENT}{item}{',' if i < len(items) - 1 else ''}" for i, item in enumerate(items)]


def format_column(column: ast.Column) -> str:
    """Formats a CREATE TABLE column definition."""
    output = f"{format_name(column.name)} {column.datatype}"
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
        output += f" REFERENCES {format_name(column.references)}"
    return output


def format_select(select: ast.Select) -> list[str]:
    """Formats a SELECT statement, as lines."""
    items = ", ".join(
        format_expr(expr) + (f" AS {format_name(alias)}" if alias is not None else "")
        for expr, alias in select.select
    )
    lines = [f"SELECT {items}"]
    if select.from_:
        lines += format_from_clause(select.from_)
    lines += format_where(select.where)
    if select.group_by:
        lines.append(f"GROUP BY {format_exprs(select.group_by)}")
    if select.having is not None:
        lines.append(f"HAVING {format_expr(select.having)}")
    if select.order_by:
        order = ", ".join(
            format_expr(expr) + (" DESC" if direction == ast.Direction.Descending else "")
            for expr, direction in select.order_by
        )
        lines.append(f"ORDER BY {order}")
    if select.limit is not None:
        lines.append(f"LIMIT {format_expr(select.limit)}")
    if select.offset is not None:
        lines.append(f"OFFSET {format_expr(select.offset)}")
    return lines


def format_from_clause(items: list[ast.From]) -> list[str]:
    """Formats a FROM clause, as lines."""
    lines = ["FROM"]
    for i, item in enumerate(items):
        first, *rest = format_from(item)
        lines[-1] += (", " if i > 0 else " ") + first
        lines += rest
    return lines


def format_from(item: ast.From) -> list[str]:
    """Formats a FROM item, as lines."""
    match item:
        case ast.From.Table(name, alias):
            return [format_name(name) + (f" AS {format_name(alias)}" if alias is not None else "")]
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
            first, *rest = format_from(right)
            lines = [*format_from(left), f"{INDENT}{join} {first}", *rest]
            if predicate is not None:
                lines[-1] += f" ON {format_expr(predicate)}"
            return lines
        case _:
            panic(f"unknown from item {item!r}")


def format_where(where: ast.Expression | None) -> list[str]:
    """Formats an optional WHERE clause, as lines."""
    return [f"WHERE {format_expr(where)}"] if where is not None else []


def format_exprs(exprs: list[ast.Expression]) -> str:
    """Formats a comma-separated list of expressions."""
    return ", ".join(map(format_expr, exprs))


def format_expr(expr: ast.Expression) -> str:
    """Returns the canonical text of an expression."""
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
                return f"{format_name(table)}.{format_name(column)}"
            return format_name(column)
        case ast.Expression.Literal(literal):
            return format_literal(literal)
        case ast.Expression.Parameter(number):
            return f"${number}"
        case ast.Expression.Function(name, args):
            return f"{format_name(name)}({format_exprs(list(args))})"
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
            # ^ is right-associative.
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


def group(expr: ast.Expression, parenthesize: bool) -> str:
    """Formats an expression, in parentheses if requested."""
    formatted = format_expr(expr)
    return f"({formatted})" if parenthesize else formatted


def format_literal(literal: ast.Literal) -> str:
    """Formats a literal."""
    match literal:
        case ast.Literal.Null():
            value: Value = Value.Null()
        case ast.Literal.Boolean(b):
            value = Value.Boolean(b)
        case ast.Literal.Integer(i):
            value = Value.Integer(i)
        case ast.Literal.Float(f):
            value = Value.Float(f)
        case ast.Literal.String(s):
            value = Value.String(s)
        case _:
            panic(f"unknown literal {literal!r}")
    return to_sql_literal(value)


def format_name(name: str) -> str:
    """Writes a name so that the lexer reads it back as the same name, quoting
    it if necessary."""
    try:
        unquoted = list(Lexer(name)) == [Token.Ident(name)]
    except Error:
        unquoted = False
    if unquoted:
        return name
    return '"{}"'.format(name.replace('"', '""'))


def format_is(literal: ast.Literal) -> str:
    """Formats the NULL or NAN of IS [NOT] NULL/NAN, which is part of the
    operator, not a literal."""
    match literal:
        case ast.Literal.Null():
            return "NULL"
        case ast.Literal.Float():
            return "NAN"
        case _:
            panic(f"invalid IS value {literal!r}")  # enforced by parser


def precedence(expr: ast.Expression) -> int:
    """Returns the precedence of an expression, matching the parser: from 1
    (loosest) to 11 (atoms)."""
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
