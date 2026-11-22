"""The SQL Abstract Syntax Tree (AST).

Mirrors `src/sql/parser/ast.rs`.
"""

import enum
import struct
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import ClassVar

from toydb.encoding.serde import Deserializer, Serializer
from toydb.fmt import Raw, debug, debug_option, debug_struct, debug_tuple
from toydb.sql.types.value import DataType


class Statement(ABC):
    """SQL statements are represented as an Abstract Syntax Tree (AST). The
    statement is the root node of this tree, and describes the syntactic
    structure of a SQL statement. It is built from a raw SQL string by the
    parser, and passed on to the planner which validates it and builds an
    execution plan from it."""

    Begin: ClassVar[type["Begin"]]
    Commit: ClassVar[type["Commit"]]
    Rollback: ClassVar[type["Rollback"]]
    Explain: ClassVar[type["Explain"]]
    CreateTable: ClassVar[type["CreateTable"]]
    DropTable: ClassVar[type["DropTable"]]
    Delete: ClassVar[type["Delete"]]
    Insert: ClassVar[type["Insert"]]
    Update: ClassVar[type["Update"]]
    Select: ClassVar[type["Select"]]
    ShowStatements: ClassVar[type["ShowStatements"]]
    Analyze: ClassVar[type["Analyze"]]
    ShowStatistics: ClassVar[type["ShowStatistics"]]
    Export: ClassVar[type["Export"]]
    Import: ClassVar[type["Import"]]
    Format: ClassVar[type["Format"]]
    CreateView: ClassVar[type["CreateView"]]
    DropView: ClassVar[type["DropView"]]
    ShowViews: ClassVar[type["ShowViews"]]
    Describe: ClassVar[type["Describe"]]
    Estimate: ClassVar[type["Estimate"]]
    Prepare: ClassVar[type["Prepare"]]
    Execute: ClassVar[type["Execute"]]
    Deallocate: ClassVar[type["Deallocate"]]
    ShowPrepared: ClassVar[type["ShowPrepared"]]
    Lint: ClassVar[type["Lint"]]
    ShowChanges: ClassVar[type["ShowChanges"]]

    def __repr__(self) -> str:
        return debug(self)

    def expressions(self) -> list["Expression"]:
        """Returns the top-level expressions of the statement and the
        statements it contains."""
        match self:
            case (
                Explain(statement)
                | Format(statement)
                | Estimate(statement)
                | Lint(statement)
                | Describe(Statement() as statement)
                | Prepare(statement=statement)
            ):
                return statement.expressions()
            case CreateView(query=query):
                return query.expressions()
            case CreateTable(columns=columns):
                return [column.default for column in columns if column.default is not None]
            case Delete(where=where):
                return [where] if where is not None else []
            case Insert(values=values):
                return [expr for row in values for expr in row]
            case Update(set=set, where=where):
                exprs = [expr for expr in set.values() if expr is not None]
                return exprs + ([where] if where is not None else [])
            case Select() as select:
                exprs = [expr for expr, _ in select.select]
                for item in select.from_:
                    exprs += item.expressions()
                exprs += [select.where] if select.where is not None else []
                exprs += select.group_by
                exprs += [select.having] if select.having is not None else []
                exprs += [expr for expr, _ in select.order_by]
                exprs += [select.limit] if select.limit is not None else []
                exprs += [select.offset] if select.offset is not None else []
                return exprs
            case Execute(args=args):
                return list(args)
            case (
                Begin()
                | Commit()
                | Rollback()
                | DropTable()
                | ShowStatements()
                | Analyze()
                | ShowStatistics()
                | Export()
                | Import()
                | DropView()
                | ShowViews()
                | Describe()
                | Deallocate()
                | ShowPrepared()
                | ShowChanges()
            ):
                return []
            case _:
                raise AssertionError(f"unknown statement {self!r}")

    def parameters(self) -> list[int]:
        """Returns the numbers of the parameters in the statement and the
        statements it contains, in increasing order."""
        found: list[Expression] = []
        for expr in self.expressions():
            expr.collect(lambda expr: isinstance(expr, Parameter), found)
        return sorted({expr.number for expr in found if isinstance(expr, Parameter)})


@dataclass(repr=False)
class Begin(Statement):
    """BEGIN: begins a new transaction."""

    read_only: bool
    """READ ONLY: if true, begin a read-only transaction."""
    as_of: int | None
    """AS OF: if given, the MVCC version to read at."""


@dataclass(repr=False)
class Commit(Statement):
    """COMMIT: commits a transaction."""


@dataclass(repr=False)
class Rollback(Statement):
    """ROLLBACK: rolls back a transaction."""


@dataclass(repr=False)
class Explain(Statement):
    """EXPLAIN: explains a SQL statement's execution plan."""

    statement: Statement

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Explain", [self.statement], pretty)


@dataclass(repr=False)
class CreateTable(Statement):
    """CREATE TABLE: creates a new table."""

    name: str
    """The table name."""
    columns: list["Column"]
    """Column specifications."""


@dataclass(repr=False)
class DropTable(Statement):
    """DROP TABLE: drops a table."""

    name: str
    """The table to drop."""
    if_exists: bool
    """IF EXISTS: if true, don't error if the table doesn't exist."""


@dataclass(repr=False)
class Delete(Statement):
    """DELETE: deletes rows from a table."""

    table: str
    """The table to delete from."""
    where: "Expression | None"
    """WHERE: optional condition to match rows to delete."""


@dataclass(repr=False)
class Insert(Statement):
    """INSERT INTO: inserts new rows into a table."""

    table: str
    """Table to insert into."""
    columns: list[str] | None
    """Columns to insert values into. If None, all columns are used."""
    values: list[list["Expression"]]
    """Row values to insert."""


@dataclass(repr=False)
class Update(Statement):
    """UPDATE: updates rows in a table."""

    table: str
    set: dict[str, "Expression | None"]
    """column → value, None for default value. Ordered by column name (a
    BTreeMap in Rust)."""
    where: "Expression | None"

    def fmt_debug(self, pretty: bool = False) -> str:
        set = {column: Raw(debug_option(expr, pretty)) for column, expr in self.set.items()}
        where = Raw(debug_option(self.where, pretty))
        return debug_struct(
            "Update", [("table", self.table), ("set", set), ("where", where)], pretty
        )


@dataclass(repr=False)
class Select(Statement):
    """SELECT: selects rows, possibly from a table."""

    select: list[tuple["Expression", str | None]]
    """Expressions to select, with an optional column alias."""
    from_: list["From"]
    """FROM: tables to select from. This is the Rust field `from`, which is a
    Python keyword."""
    where: "Expression | None"
    """WHERE: optional condition to filter rows."""
    group_by: list["Expression"]
    """GROUP BY: expressions to group and aggregate by."""
    having: "Expression | None"
    """HAVING: expression to filter groups by."""
    order_by: list[tuple["Expression", "Direction"]]
    """ORDER BY: expressions to sort by, with direction."""
    offset: "Expression | None"
    """OFFSET: row offset to start from."""
    limit: "Expression | None"
    """LIMIT: maximum number of rows to return."""

    def fmt_debug(self, pretty: bool = False) -> str:
        select = [(expr, Raw(debug_option(alias, pretty))) for expr, alias in self.select]
        return debug_struct(
            "Select",
            [
                ("select", select),
                ("from", self.from_),
                ("where", Raw(debug_option(self.where, pretty))),
                ("group_by", self.group_by),
                ("having", Raw(debug_option(self.having, pretty))),
                ("order_by", self.order_by),
                ("offset", Raw(debug_option(self.offset, pretty))),
                ("limit", Raw(debug_option(self.limit, pretty))),
            ],
            pretty,
        )


@dataclass(repr=False)
class ShowStatements(Statement):
    """SHOW STATEMENTS: shows the session's statement digest statistics."""


@dataclass(repr=False)
class Analyze(Statement):
    """ANALYZE: computes and stores column statistics."""

    table: str | None
    """The table to analyze, or None to analyze all tables."""


@dataclass(repr=False)
class ShowStatistics(Statement):
    """SHOW STATISTICS: shows a table's column statistics."""

    table: str


@dataclass(repr=False)
class Export(Statement):
    """EXPORT: exports a table's rows (or the whole database) as text lines."""

    table: str | None
    """The table to export, or None to export all tables (SQL only)."""
    format: "ExportFormat"


@dataclass(repr=False)
class Import(Statement):
    """IMPORT INTO: imports rows into a table from text lines."""

    table: str
    format: "ImportFormat"
    lines: list[str]
    """The lines to import, one per string literal."""


@dataclass(repr=False)
class Format(Statement):
    """FORMAT: returns the canonical SQL text of a statement."""

    statement: Statement

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Format", [self.statement], pretty)


@dataclass(repr=False)
class CreateView(Statement):
    """CREATE VIEW: creates a view."""

    name: str
    """The view name."""
    columns: list[str] | None
    """The column names, if given."""
    query: Select
    """The view's query, a SELECT statement."""


@dataclass(repr=False)
class DropView(Statement):
    """DROP VIEW: drops a view."""

    name: str
    """The view to drop."""
    if_exists: bool
    """IF EXISTS: if true, don't error if the view doesn't exist."""


@dataclass(repr=False)
class ShowViews(Statement):
    """SHOW VIEWS: lists the views."""


@dataclass(repr=False)
class Describe(Statement):
    """DESCRIBE: describes a table or view, or the result of a statement."""

    target: "str | Statement"
    """The table or view name, or the statement."""


@dataclass(repr=False)
class Estimate(Statement):
    """ESTIMATE: estimates the rows of a statement's plan."""

    statement: Statement

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Estimate", [self.statement], pretty)


@dataclass(repr=False)
class Prepare(Statement):
    """PREPARE: prepares a statement with typed parameters."""

    name: str
    """The prepared statement's name."""
    types: list[DataType]
    """The parameter types, for $1, $2, ..."""
    statement: Statement
    """The statement."""


@dataclass(repr=False)
class Execute(Statement):
    """EXECUTE: executes a prepared statement."""

    name: str
    """The prepared statement's name."""
    args: list["Expression"]
    """The arguments, one per parameter."""


@dataclass(repr=False)
class Deallocate(Statement):
    """DEALLOCATE: removes a prepared statement."""

    name: str


@dataclass(repr=False)
class ShowPrepared(Statement):
    """SHOW PREPARED: lists the session's prepared statements."""


@dataclass(repr=False)
class Lint(Statement):
    """LINT: returns warnings about a statement."""

    statement: Statement

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Lint", [self.statement], pretty)


@dataclass(repr=False)
class ShowChanges(Statement):
    """SHOW CHANGES: lists the committed changes."""

    since: int | None
    """SINCE: only changes of transactions above this version."""


Statement.Begin = Begin
Statement.Commit = Commit
Statement.Rollback = Rollback
Statement.Explain = Explain
Statement.CreateTable = CreateTable
Statement.DropTable = DropTable
Statement.Delete = Delete
Statement.Insert = Insert
Statement.Update = Update
Statement.Select = Select
Statement.ShowStatements = ShowStatements
Statement.Analyze = Analyze
Statement.ShowStatistics = ShowStatistics
Statement.Export = Export
Statement.Import = Import
Statement.Format = Format
Statement.CreateView = CreateView
Statement.DropView = DropView
Statement.ShowViews = ShowViews
Statement.Describe = Describe
Statement.Estimate = Estimate
Statement.Prepare = Prepare
Statement.Execute = Execute
Statement.Deallocate = Deallocate
Statement.ShowPrepared = ShowPrepared
Statement.Lint = Lint
Statement.ShowChanges = ShowChanges


class ExportFormat(enum.Enum):
    """EXPORT formats."""

    Csv = 0
    Json = 1
    Sql = 2

    def __str__(self) -> str:
        """The format name in lower case, as used in JSON results."""
        return self.name.lower()

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_unit_variant(self.value)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "ExportFormat":
        index = deserializer.deserialize_enum()
        if index >= len(cls):
            raise deserializer.invalid_variant(index, len(cls))
        return cls(index)


class ImportFormat(enum.Enum):
    """IMPORT formats."""

    Csv = enum.auto()
    Json = enum.auto()


class From(ABC):
    """A FROM item."""

    Table: ClassVar[type["Table"]]
    Join: ClassVar[type["Join"]]

    def __repr__(self) -> str:
        return debug(self)

    def expressions(self) -> list["Expression"]:
        """Returns the join predicates of the FROM item."""
        match self:
            case Table():
                return []
            case Join(left, right, _, predicate):
                exprs = left.expressions() + right.expressions()
                return exprs + ([predicate] if predicate is not None else [])
            case _:
                raise AssertionError(f"unknown from item {self!r}")


@dataclass(repr=False)
class Table(From):
    """A table."""

    name: str
    """The table name."""
    alias: str | None
    """An optional alias for the table."""


@dataclass(repr=False)
class Join(From):
    """A join of two or more tables (may be nested)."""

    left: From
    """The left table to join."""
    right: From
    """The right table to join."""
    type: "JoinType"
    """The join type."""
    predicate: "Expression | None"
    """The join condition. None for a cross join."""


From.Table = Table
From.Join = Join


@dataclass(repr=False)
class Column:
    """A CREATE TABLE column definition."""

    name: str
    datatype: DataType
    primary_key: bool
    nullable: bool | None
    default: "Expression | None"
    unique: bool
    index: bool
    references: str | None

    def __repr__(self) -> str:
        return debug(self)


class JoinType(enum.Enum):
    """JOIN types."""

    Cross = enum.auto()
    Inner = enum.auto()
    Left = enum.auto()
    Right = enum.auto()

    def is_outer(self) -> bool:
        """If true, the join is an outer join, where rows with no join matches
        are emitted with a NULL match."""
        match self:
            case JoinType.Left | JoinType.Right:
                return True
            case JoinType.Cross | JoinType.Inner:
                return False


class Direction(enum.Enum):
    """ORDER BY direction. The default is ascending."""

    Ascending = enum.auto()
    Descending = enum.auto()


class Expression(ABC):
    """SQL expressions, e.g. `a + 7 > b`. Can be nested. Expressions are
    immutable and hashable."""

    All: ClassVar[type["All"]]
    Column: ClassVar[type["ExpressionColumn"]]
    Literal: ClassVar[type["ExpressionLiteral"]]
    Function: ClassVar[type["Function"]]
    Operator: ClassVar[type["ExpressionOperator"]]
    Parameter: ClassVar[type["Parameter"]]

    def __repr__(self) -> str:
        return debug(self)

    def walk(self, visitor: Callable[["Expression"], bool]) -> bool:
        """Walks the expression tree depth-first, calling a closure for every
        node. Halts and returns false if the closure returns false."""
        if not visitor(self):
            return False

        match self:
            case ExpressionOperator(op):
                match op:
                    case (
                        Add(lhs, rhs)
                        | And(lhs, rhs)
                        | Divide(lhs, rhs)
                        | Equal(lhs, rhs)
                        | Exponentiate(lhs, rhs)
                        | GreaterThan(lhs, rhs)
                        | GreaterThanOrEqual(lhs, rhs)
                        | LessThan(lhs, rhs)
                        | LessThanOrEqual(lhs, rhs)
                        | Like(lhs, rhs)
                        | Multiply(lhs, rhs)
                        | NotEqual(lhs, rhs)
                        | Or(lhs, rhs)
                        | Remainder(lhs, rhs)
                        | Subtract(lhs, rhs)
                    ):
                        return lhs.walk(visitor) and rhs.walk(visitor)

                    case (
                        Factorial(expr)
                        | Identity(expr)
                        | Is(expr, _)
                        | IsNot(expr, _)
                        | Negate(expr)
                        | Not(expr)
                    ):
                        return expr.walk(visitor)

                    case _:
                        raise AssertionError(f"unknown operator {op!r}")

            case Function(_, exprs):
                return any(expr.walk(visitor) for expr in exprs)

            case All() | ExpressionColumn() | ExpressionLiteral() | Parameter():
                return True

            case _:
                raise AssertionError(f"unknown expression {self!r}")

    def transform(self, after: Callable[["Expression"], "Expression"]) -> "Expression":
        """Transforms the expression tree depth-first, rebuilding each node
        from its transformed children and then applying the closure to it."""

        def xform(expr: Expression) -> Expression:
            return expr.transform(after)

        match self:
            case ExpressionOperator(op):
                match op:
                    case (
                        Add(lhs, rhs)
                        | And(lhs, rhs)
                        | Divide(lhs, rhs)
                        | Equal(lhs, rhs)
                        | Exponentiate(lhs, rhs)
                        | GreaterThan(lhs, rhs)
                        | GreaterThanOrEqual(lhs, rhs)
                        | LessThan(lhs, rhs)
                        | LessThanOrEqual(lhs, rhs)
                        | Like(lhs, rhs)
                        | Multiply(lhs, rhs)
                        | NotEqual(lhs, rhs)
                        | Or(lhs, rhs)
                        | Remainder(lhs, rhs)
                        | Subtract(lhs, rhs)
                    ):
                        binary = type(op)(xform(lhs), xform(rhs))  # type: ignore[call-arg]
                        expr: Expression = ExpressionOperator(binary)
                    case Factorial(inner) | Identity(inner) | Negate(inner) | Not(inner):
                        unary = type(op)(xform(inner))  # type: ignore[call-arg]
                        expr = ExpressionOperator(unary)
                    case Is(inner, literal) | IsNot(inner, literal):
                        is_ = type(op)(xform(inner), literal)  # type: ignore[call-arg]
                        expr = ExpressionOperator(is_)
                    case _:
                        raise AssertionError(f"unknown operator {op!r}")

            case Function(name, args):
                expr = Function(name, tuple(xform(arg) for arg in args))

            case All() | ExpressionColumn() | ExpressionLiteral() | Parameter():
                expr = self

            case _:
                raise AssertionError(f"unknown expression {self!r}")
        return after(expr)

    def contains(self, visitor: Callable[["Expression"], bool]) -> bool:
        """Walks the expression tree depth-first while calling a closure until
        it returns true. This is the inverse of walk()."""
        return not self.walk(lambda expr: not visitor(expr))

    def collect(
        self, visitor: Callable[["Expression"], bool], exprs: list["Expression"]
    ) -> None:
        """Finds and collects expressions for which the given closure returns
        true, adding them to exprs. Does not recurse into matching
        expressions."""
        if visitor(self):
            exprs.append(self)
            return

        match self:
            case ExpressionOperator(op):
                match op:
                    case (
                        Add(lhs, rhs)
                        | And(lhs, rhs)
                        | Divide(lhs, rhs)
                        | Equal(lhs, rhs)
                        | Exponentiate(lhs, rhs)
                        | GreaterThan(lhs, rhs)
                        | GreaterThanOrEqual(lhs, rhs)
                        | LessThan(lhs, rhs)
                        | LessThanOrEqual(lhs, rhs)
                        | Like(lhs, rhs)
                        | Multiply(lhs, rhs)
                        | NotEqual(lhs, rhs)
                        | Or(lhs, rhs)
                        | Remainder(lhs, rhs)
                        | Subtract(lhs, rhs)
                    ):
                        lhs.collect(visitor, exprs)
                        rhs.collect(visitor, exprs)

                    case (
                        Factorial(expr)
                        | Identity(expr)
                        | Is(expr, _)
                        | IsNot(expr, _)
                        | Negate(expr)
                        | Not(expr)
                    ):
                        expr.collect(visitor, exprs)

                    case _:
                        raise AssertionError(f"unknown operator {op!r}")

            case Function(_, args):
                for arg in args:
                    arg.collect(visitor, exprs)

            case All() | ExpressionColumn() | ExpressionLiteral() | Parameter():
                pass

            case _:
                raise AssertionError(f"unknown expression {self!r}")

    @staticmethod
    def from_literal(literal: "Literal") -> "Expression":
        """Builds a literal expression. Mirrors the Rust `From<Literal> for
        Expression`."""
        return ExpressionLiteral(literal)

    @staticmethod
    def from_operator(op: "Operator") -> "Expression":
        """Builds an operator expression. Mirrors the Rust `From<Operator> for
        Expression`."""
        return ExpressionOperator(op)


@dataclass(frozen=True, repr=False)
class All(Expression):
    """All columns, i.e. *."""


@dataclass(frozen=True, repr=False)
class ExpressionColumn(Expression):
    """A column reference, optionally qualified with a table name. This is
    the Rust `Expression::Column`, available as `Expression.Column`."""

    table: str | None
    column: str

    def fmt_debug(self, pretty: bool = False) -> str:
        table = Raw(debug_option(self.table, pretty))
        return debug_tuple("Column", [table, self.column], pretty)


@dataclass(frozen=True, repr=False)
class ExpressionLiteral(Expression):
    """A literal value. This is the Rust `Expression::Literal`, available as
    `Expression.Literal`."""

    literal: "Literal"

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Literal", [self.literal], pretty)


@dataclass(frozen=True, repr=False)
class Function(Expression):
    """A function call (name and parameters). The parameters are a tuple
    rather than a list, to keep expressions immutable and hashable."""

    name: str
    args: tuple[Expression, ...]

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Function", [self.name, list(self.args)], pretty)


@dataclass(frozen=True, repr=False)
class ExpressionOperator(Expression):
    """An operator. This is the Rust `Expression::Operator`, available as
    `Expression.Operator`."""

    operator: "Operator"

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Operator", [self.operator], pretty)


@dataclass(frozen=True, repr=False)
class Parameter(Expression):
    """A parameter $n of a prepared statement."""

    number: int

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Parameter", [self.number], pretty)


Expression.All = All
Expression.Column = ExpressionColumn
Expression.Literal = ExpressionLiteral
Expression.Function = Function
Expression.Operator = ExpressionOperator
Expression.Parameter = Parameter


class Literal(ABC):
    """Expression literal values.

    To allow using expressions and literals in e.g. dicts, literals implement
    simple equality by value for all types, including Null and NaN. This only
    checks that the values are the same, and ignores SQL semantics for e.g.
    NULL and NaN (which is handled by SQL expression evaluation).
    """

    Null: ClassVar[type["Null"]]
    Boolean: ClassVar[type["Boolean"]]
    Integer: ClassVar[type["Integer"]]
    Float: ClassVar[type["Float"]]
    String: ClassVar[type["String"]]

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the literal like the Rust `#[derive(Debug)]`."""
        values = [getattr(self, field.name) for field in fields(self)]  # type: ignore[arg-type]
        return debug_tuple(type(self).__name__, values, pretty)


@dataclass(frozen=True, repr=False)
class Null(Literal):
    pass


@dataclass(frozen=True, repr=False)
class Boolean(Literal):
    value: bool


@dataclass(frozen=True, repr=False)
class Integer(Literal):
    value: int


@dataclass(frozen=True, eq=False, repr=False)
class Float(Literal):
    """A float literal. Floats are equal if they have the same bits, so NaN
    equals NaN but 0.0 does not equal -0.0."""

    value: float

    def to_bits(self) -> int:
        """Returns the IEEE 754 bits of the float, like `f64::to_bits`."""
        return struct.unpack("<Q", struct.pack("<d", self.value))[0]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Float) and self.to_bits() == other.to_bits()

    def __hash__(self) -> int:
        return hash((Float, self.to_bits()))


@dataclass(frozen=True, repr=False)
class String(Literal):
    value: str


Literal.Null = Null
Literal.Boolean = Boolean
Literal.Integer = Integer
Literal.Float = Float
Literal.String = String


class Operator(ABC):
    """Expression operators."""

    And: ClassVar[type["And"]]
    Not: ClassVar[type["Not"]]
    Or: ClassVar[type["Or"]]
    Equal: ClassVar[type["Equal"]]
    GreaterThan: ClassVar[type["GreaterThan"]]
    GreaterThanOrEqual: ClassVar[type["GreaterThanOrEqual"]]
    Is: ClassVar[type["Is"]]
    IsNot: ClassVar[type["IsNot"]]
    LessThan: ClassVar[type["LessThan"]]
    LessThanOrEqual: ClassVar[type["LessThanOrEqual"]]
    NotEqual: ClassVar[type["NotEqual"]]
    Add: ClassVar[type["Add"]]
    Divide: ClassVar[type["Divide"]]
    Exponentiate: ClassVar[type["Exponentiate"]]
    Factorial: ClassVar[type["Factorial"]]
    Identity: ClassVar[type["Identity"]]
    Multiply: ClassVar[type["Multiply"]]
    Negate: ClassVar[type["Negate"]]
    Remainder: ClassVar[type["Remainder"]]
    Subtract: ClassVar[type["Subtract"]]
    Like: ClassVar[type["Like"]]

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the operator like the Rust `#[derive(Debug)]`."""
        values = [getattr(self, field.name) for field in fields(self)]  # type: ignore[arg-type]
        return debug_tuple(type(self).__name__, values, pretty)


@dataclass(frozen=True, repr=False)
class And(Operator):
    """a AND b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Not(Operator):
    """NOT a"""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Or(Operator):
    """a OR b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Equal(Operator):
    """a = b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class GreaterThan(Operator):
    """a > b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class GreaterThanOrEqual(Operator):
    """a >= b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Is(Operator):
    """IS NULL or IS NAN"""

    expr: Expression
    literal: Literal


@dataclass(frozen=True, repr=False)
class IsNot(Operator):
    """IS NOT NULL or IS NOT NAN. Kept distinct from NOT (a IS ...) so that
    the statement can be written back as it was (e.g. for statement digests);
    the planner lowers it to NOT (a IS ...)."""

    expr: Expression
    literal: Literal


@dataclass(frozen=True, repr=False)
class LessThan(Operator):
    """a < b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class LessThanOrEqual(Operator):
    """a <= b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class NotEqual(Operator):
    """a != b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Add(Operator):
    """a + b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Divide(Operator):
    """a / b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Exponentiate(Operator):
    """a ^ b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Factorial(Operator):
    """a!"""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Identity(Operator):
    """+a"""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Multiply(Operator):
    """a * b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Negate(Operator):
    """-a"""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Remainder(Operator):
    """a % b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Subtract(Operator):
    """a - b"""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Like(Operator):
    """a LIKE b"""

    lhs: Expression
    rhs: Expression


Operator.And = And
Operator.Not = Not
Operator.Or = Or
Operator.Equal = Equal
Operator.GreaterThan = GreaterThan
Operator.GreaterThanOrEqual = GreaterThanOrEqual
Operator.Is = Is
Operator.IsNot = IsNot
Operator.LessThan = LessThan
Operator.LessThanOrEqual = LessThanOrEqual
Operator.NotEqual = NotEqual
Operator.Add = Add
Operator.Divide = Divide
Operator.Exponentiate = Exponentiate
Operator.Factorial = Factorial
Operator.Identity = Identity
Operator.Multiply = Multiply
Operator.Negate = Negate
Operator.Remainder = Remainder
Operator.Subtract = Subtract
Operator.Like = Like
