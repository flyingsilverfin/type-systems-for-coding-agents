"""SQL expressions, evaluated during query execution.

Mirrors `src/sql/types/expression.rs`.
"""

import math
import re
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, ClassVar, Protocol

from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import errinput, panic
from toydb.fmt import debug, debug_tuple
from toydb.sql.types.value import I64_MIN, Label, Row, Value

if TYPE_CHECKING:
    from toydb.sql.planner import Node


class Expression(ABC):
    """An expression, made up of nested operations and values. Values are
    either constants, or numeric column references which are looked up in
    rows. Evaluated to a final value during query execution.

    Expressions are immutable: the `into_*` and `transform` methods return new
    expressions.
    """

    Constant: ClassVar[type["Constant"]]
    Column: ClassVar[type["Column"]]
    And: ClassVar[type["And"]]
    Or: ClassVar[type["Or"]]
    Not: ClassVar[type["Not"]]
    Equal: ClassVar[type["Equal"]]
    GreaterThan: ClassVar[type["GreaterThan"]]
    LessThan: ClassVar[type["LessThan"]]
    Is: ClassVar[type["Is"]]
    Add: ClassVar[type["Add"]]
    Divide: ClassVar[type["Divide"]]
    Exponentiate: ClassVar[type["Exponentiate"]]
    Factorial: ClassVar[type["Factorial"]]
    Identity: ClassVar[type["Identity"]]
    Multiply: ClassVar[type["Multiply"]]
    Negate: ClassVar[type["Negate"]]
    Remainder: ClassVar[type["Remainder"]]
    SquareRoot: ClassVar[type["SquareRoot"]]
    Subtract: ClassVar[type["Subtract"]]
    Like: ClassVar[type["Like"]]

    def __str__(self) -> str:
        """NB: this can't look up column labels, and will print numeric column
        indexes instead. Use Expression.display() instead to print with labels
        resolved from a given plan node."""
        from toydb.sql.planner import Node

        return str(self.display(Node.Nothing(columns_=[])))

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the expression like the Rust `#[derive(Debug)]`."""
        values = [getattr(self, field.name) for field in fields(self)]  # type: ignore[arg-type]
        return debug_tuple(type(self).__name__, values, pretty)

    def serialize(self, serializer: Serializer) -> None:
        """Serializes the expression, like `#[derive(Serialize)]`."""
        index = _VARIANTS.index(type(self))
        match self:
            case Constant(value):
                serializer.serialize_newtype_variant(index)
                value.serialize(serializer)
            case Column(column):
                serializer.serialize_newtype_variant(index)
                serializer.serialize_u64(column)
            case Is(expr, value):
                serializer.serialize_tuple_variant(index)
                expr.serialize(serializer)
                value.serialize(serializer)
            case (
                Not(expr) | Factorial(expr) | Identity(expr) | Negate(expr) | SquareRoot(expr)
            ):
                serializer.serialize_newtype_variant(index)
                expr.serialize(serializer)
            case (
                And(lhs, rhs)
                | Or(lhs, rhs)
                | Equal(lhs, rhs)
                | GreaterThan(lhs, rhs)
                | LessThan(lhs, rhs)
                | Add(lhs, rhs)
                | Divide(lhs, rhs)
                | Exponentiate(lhs, rhs)
                | Multiply(lhs, rhs)
                | Remainder(lhs, rhs)
                | Subtract(lhs, rhs)
                | Like(lhs, rhs)
            ):
                serializer.serialize_tuple_variant(index)
                lhs.serialize(serializer)
                rhs.serialize(serializer)
            case _:
                panic(f"unknown expression {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Expression":
        """Deserializes an expression, like `#[derive(Deserialize)]`."""
        index = deserializer.deserialize_enum()
        if index >= len(_VARIANTS):
            raise deserializer.invalid_variant(index, len(_VARIANTS))
        variant = _VARIANTS[index]
        if variant is Constant:
            return Constant(Value.deserialize(deserializer))
        if variant is Column:
            return Column(deserializer.deserialize_u64())
        if variant is Is:
            return Is(Expression.deserialize(deserializer), Value.deserialize(deserializer))
        if variant in (Not, Factorial, Identity, Negate, SquareRoot):
            return variant(Expression.deserialize(deserializer))  # type: ignore[call-arg]
        lhs = Expression.deserialize(deserializer)
        rhs = Expression.deserialize(deserializer)
        return variant(lhs, rhs)  # type: ignore[call-arg]

    def display(self, node: "ColumnLabels") -> "ExpressionDisplay":
        """Displays the expression, using the given plan node to look up
        labels for column references."""
        return ExpressionDisplay(self, node, 0)

    def evaluate(self, row: Row | None) -> Value:
        """Evaluates an expression, returning a constant value. Column
        references are looked up in the given row (or panic if the row is
        None)."""
        Null, Boolean, Integer = Value.Null, Value.Boolean, Value.Integer
        Float, String = Value.Float, Value.String

        match self:
            # Constant values return themselves.
            case Constant(value):
                return value

            # Column references look up a row value. The planner ensures that
            # only constant expressions are evaluated without a row.
            case Column(index):
                if row is None or index >= len(row):
                    panic("invalid index")
                return row[index]

            # Logical AND. Inputs must be boolean or NULL. NULLs generally
            # yield NULL, except the special case NULL AND false == false.
            case And(lhs, rhs):
                match (lhs.evaluate(row), rhs.evaluate(row)):
                    case (Boolean(lhs), Boolean(rhs)):
                        return Boolean(lhs and rhs)
                    case (Boolean(b), Null()) | (Null(), Boolean(b)) if not b:
                        return Boolean(False)
                    case (Boolean(), Null()) | (Null(), Boolean()) | (Null(), Null()):
                        return Null()
                    case (lhs, rhs):
                        raise errinput(f"can't AND {lhs} and {rhs}")

            # Logical OR. Inputs must be boolean or NULL. NULLs generally
            # yield NULL, except the special case NULL OR true == true.
            case Or(lhs, rhs):
                match (lhs.evaluate(row), rhs.evaluate(row)):
                    case (Boolean(lhs), Boolean(rhs)):
                        return Boolean(lhs or rhs)
                    case (Boolean(b), Null()) | (Null(), Boolean(b)) if b:
                        return Boolean(True)
                    case (Boolean(), Null()) | (Null(), Boolean()) | (Null(), Null()):
                        return Null()
                    case (lhs, rhs):
                        raise errinput(f"can't OR {lhs} and {rhs}")

            # Logical NOT. Input must be boolean or NULL.
            case Not(expr):
                match expr.evaluate(row):
                    case Boolean(b):
                        return Boolean(not b)
                    case Null():
                        return Null()
                    case value:
                        raise errinput(f"can't NOT {value}")

            # Comparisons. Must be of same type, except floats and integers
            # which are interchangeable. NULLs yield NULL, NaNs yield NaN.
            #
            # Does not dispatch to Value.cmp() because comparison and sorting
            # is different for Nulls and NaNs in SQL and code.
            case Equal(lhs, rhs):
                match (lhs.evaluate(row), rhs.evaluate(row)):
                    case (Boolean(lhs), Boolean(rhs)):
                        return Boolean(lhs == rhs)
                    case (Integer(lhs), Integer(rhs)):
                        return Boolean(lhs == rhs)
                    case (Integer(lhs), Float(rhs)):
                        return Boolean(float(lhs) == rhs)
                    case (Float(lhs), Integer(rhs)):
                        return Boolean(lhs == float(rhs))
                    case (Float(lhs), Float(rhs)):
                        return Boolean(lhs == rhs)
                    case (String(lhs), String(rhs)):
                        return Boolean(lhs == rhs)
                    case (Null(), _) | (_, Null()):
                        return Null()
                    case (lhs, rhs):
                        raise errinput(f"can't compare {lhs} and {rhs}")

            case GreaterThan(lhs, rhs):
                match (lhs.evaluate(row), rhs.evaluate(row)):
                    case (Boolean(lhs), Boolean(rhs)):
                        return Boolean(lhs > rhs)
                    case (Integer(lhs), Integer(rhs)):
                        return Boolean(lhs > rhs)
                    case (Integer(lhs), Float(rhs)):
                        return Boolean(float(lhs) > rhs)
                    case (Float(lhs), Integer(rhs)):
                        return Boolean(lhs > float(rhs))
                    case (Float(lhs), Float(rhs)):
                        return Boolean(lhs > rhs)
                    case (String(lhs), String(rhs)):
                        return Boolean(lhs > rhs)
                    case (Null(), _) | (_, Null()):
                        return Null()
                    case (lhs, rhs):
                        raise errinput(f"can't compare {lhs} and {rhs}")

            case LessThan(lhs, rhs):
                match (lhs.evaluate(row), rhs.evaluate(row)):
                    case (Boolean(lhs), Boolean(rhs)):
                        return Boolean(lhs < rhs)
                    case (Integer(lhs), Integer(rhs)):
                        return Boolean(lhs < rhs)
                    case (Integer(lhs), Float(rhs)):
                        return Boolean(float(lhs) < rhs)
                    case (Float(lhs), Integer(rhs)):
                        return Boolean(lhs < float(rhs))
                    case (Float(lhs), Float(rhs)):
                        return Boolean(lhs < rhs)
                    case (String(lhs), String(rhs)):
                        return Boolean(lhs < rhs)
                    case (Null(), _) | (_, Null()):
                        return Null()
                    case (lhs, rhs):
                        raise errinput(f"can't compare {lhs} and {rhs}")

            case Is(expr, Null()):
                return Boolean(expr.evaluate(row) == Null())
            case Is(expr, Float(f)) if math.isnan(f):
                match expr.evaluate(row):
                    case Float(f):
                        return Boolean(math.isnan(f))
                    case Null():
                        return Null()
                    case v:
                        raise errinput(f"IS NAN can't be used with {v.datatype()}")
            case Is(_, v):
                panic(f"invalid IS value {v}")  # enforced by parser

            # Mathematical operations. Inputs must be numbers, but integers and
            # floats are interchangeable (float when mixed). NULLs yield NULL.
            # Errors on integer overflow, but floats yield infinity or NaN.
            case Add(lhs, rhs):
                return lhs.evaluate(row).checked_add(rhs.evaluate(row))
            case Divide(lhs, rhs):
                return lhs.evaluate(row).checked_div(rhs.evaluate(row))
            case Exponentiate(lhs, rhs):
                return lhs.evaluate(row).checked_pow(rhs.evaluate(row))
            case Factorial(expr):
                match expr.evaluate(row):
                    case Integer(i) if i >= 0:
                        product: Value = Integer(1)
                        for factor in range(1, i + 1):
                            product = product.checked_mul(Integer(factor))
                        return product
                    case Null():
                        return Null()
                    case value:
                        raise errinput(f"can't take factorial of {value}")
            case Identity(expr):
                match expr.evaluate(row):
                    case Integer() | Float() | Null() as value:
                        return value
                    case value:
                        raise errinput(f"can't take the identity of {value}")
            case Multiply(lhs, rhs):
                return lhs.evaluate(row).checked_mul(rhs.evaluate(row))
            case Negate(expr):
                match expr.evaluate(row):
                    case Integer(i):
                        if i == I64_MIN:
                            panic("attempt to negate with overflow")
                        return Integer(-i)
                    case Float(f):
                        return Float(-f)
                    case Null():
                        return Null()
                    case value:
                        raise errinput(f"can't negate {value}")
            case Remainder(lhs, rhs):
                return lhs.evaluate(row).checked_rem(rhs.evaluate(row))
            case SquareRoot(expr):
                match expr.evaluate(row):
                    case Integer(i) if i >= 0:
                        return Float(math.sqrt(float(i)))
                    case Float(f):
                        # The square root of a negative number is NaN (but
                        # the square root of -0.0 is -0.0).
                        return Float(math.sqrt(f) if f >= 0.0 else math.nan)
                    case Null():
                        return Null()
                    case value:
                        raise errinput(f"can't take square root of {value}")
            case Subtract(lhs, rhs):
                return lhs.evaluate(row).checked_sub(rhs.evaluate(row))

            # LIKE pattern matching, using _ and % as single- and
            # multi-character wildcards. Inputs must be strings. NULLs yield
            # NULL. There's no support for escaping an _ and %.
            case Like(lhs, rhs):
                match (lhs.evaluate(row), rhs.evaluate(row)):
                    case (String(lhs), String(rhs)):
                        # We could precompile the pattern if it's constant,
                        # instead of recompiling it for every row, but we keep
                        # it simple.
                        wildcards = {"%": ".*", "_": "."}
                        pattern = "".join(wildcards.get(c) or re.escape(c) for c in rhs)
                        return Boolean(re.fullmatch(pattern, lhs) is not None)
                    case (String(), Null()) | (Null(), String()) | (Null(), Null()):
                        return Null()
                    case (lhs, rhs):
                        raise errinput(f"can't LIKE {lhs} and {rhs}")

            case _:
                panic(f"unknown expression {self!r}")

    def walk(self, visitor: Callable[["Expression"], bool]) -> bool:
        """Recursively walks the expression tree depth-first, calling the
        given closure until it returns false. Returns true otherwise."""
        if not visitor(self):
            return False
        match self:
            case (
                Add(lhs, rhs)
                | And(lhs, rhs)
                | Divide(lhs, rhs)
                | Equal(lhs, rhs)
                | Exponentiate(lhs, rhs)
                | GreaterThan(lhs, rhs)
                | LessThan(lhs, rhs)
                | Like(lhs, rhs)
                | Multiply(lhs, rhs)
                | Or(lhs, rhs)
                | Remainder(lhs, rhs)
                | Subtract(lhs, rhs)
            ):
                return lhs.walk(visitor) and rhs.walk(visitor)

            case (
                Factorial(expr)
                | Identity(expr)
                | Is(expr, _)
                | Negate(expr)
                | Not(expr)
                | SquareRoot(expr)
            ):
                return expr.walk(visitor)

            case Constant() | Column():
                return True

            case _:
                panic(f"unknown expression {self!r}")

    def contains(self, visitor: Callable[["Expression"], bool]) -> bool:
        """Recursively walks the expression tree depth-first, calling the
        given closure until it returns true. Returns false otherwise. This is
        the inverse of walk()."""
        return not self.walk(lambda e: not visitor(e))

    def transform(
        self,
        before: Callable[["Expression"], "Expression"],
        after: Callable[["Expression"], "Expression"],
    ) -> "Expression":
        """Transforms the expression by recursively applying the given
        closures depth-first to each node before/after descending. The
        closures may raise errors."""

        def xform(expr: Expression) -> Expression:
            return expr.transform(before, after)

        expr = before(self)
        match expr:
            case Add(lhs, rhs):
                expr = Add(xform(lhs), xform(rhs))
            case And(lhs, rhs):
                expr = And(xform(lhs), xform(rhs))
            case Divide(lhs, rhs):
                expr = Divide(xform(lhs), xform(rhs))
            case Equal(lhs, rhs):
                expr = Equal(xform(lhs), xform(rhs))
            case Exponentiate(lhs, rhs):
                expr = Exponentiate(xform(lhs), xform(rhs))
            case GreaterThan(lhs, rhs):
                expr = GreaterThan(xform(lhs), xform(rhs))
            case LessThan(lhs, rhs):
                expr = LessThan(xform(lhs), xform(rhs))
            case Like(lhs, rhs):
                expr = Like(xform(lhs), xform(rhs))
            case Multiply(lhs, rhs):
                expr = Multiply(xform(lhs), xform(rhs))
            case Or(lhs, rhs):
                expr = Or(xform(lhs), xform(rhs))
            case Remainder(lhs, rhs):
                expr = Remainder(xform(lhs), xform(rhs))
            case SquareRoot(inner):
                expr = SquareRoot(xform(inner))
            case Subtract(lhs, rhs):
                expr = Subtract(xform(lhs), xform(rhs))

            case Factorial(inner):
                expr = Factorial(xform(inner))
            case Identity(inner):
                expr = Identity(xform(inner))
            case Is(inner, value):
                expr = Is(xform(inner), value)
            case Negate(inner):
                expr = Negate(xform(inner))
            case Not(inner):
                expr = Not(xform(inner))

            case Constant() | Column():
                pass

            case _:
                panic(f"unknown expression {expr!r}")
        return after(expr)

    def into_cnf(self) -> "Expression":
        """Converts the expression into conjunctive normal form, i.e. an AND
        of ORs, useful during plan optimization. This is done by converting to
        negation normal form and then applying De Morgan's distributive law."""

        def xform(expr: Expression) -> Expression:
            match expr:
                # (x AND y) OR z → (x OR z) AND (y OR z)
                case Or(And(l, r), rhs):
                    return And(Or(l, rhs), Or(r, rhs))
                # x OR (y AND z) → (x OR y) AND (x OR z)
                case Or(lhs, And(l, r)):
                    return And(Or(lhs, l), Or(lhs, r))
                # Otherwise, do nothing.
                case _:
                    return expr

        return self.into_nnf().transform(xform, lambda e: e)

    def into_cnf_vec(self) -> list["Expression"]:
        """Converts the expression into conjunctive normal form as a list of
        ANDed expressions (instead of nested ANDs)."""
        cnf: list[Expression] = []
        stack = [self.into_cnf()]
        while stack:
            match stack.pop():
                case And(lhs, rhs):
                    stack.extend([rhs, lhs])  # push lhs last to pop it first
                case expr:
                    cnf.append(expr)
        return cnf

    def into_nnf(self) -> "Expression":
        """Converts the expression into negation normal form. This pushes NOT
        operators into the tree using De Morgan's laws, such that they're
        always below other logical operators. It is a useful intermediate form
        for applying other logical normalizations."""

        def xform(expr: Expression) -> Expression:
            match expr:
                # NOT (x AND y) → (NOT x) OR (NOT y)
                case Not(And(lhs, rhs)):
                    return Or(Not(lhs), Not(rhs))
                # NOT (x OR y) → (NOT x) AND (NOT y)
                case Not(Or(lhs, rhs)):
                    return And(Not(lhs), Not(rhs))
                # NOT NOT x → x
                case Not(Not(inner)):
                    return inner
                # Otherwise, do nothing.
                case _:
                    return expr

        return self.transform(xform, lambda e: e)

    @staticmethod
    def and_vec(exprs: list["Expression"]) -> "Expression | None":
        """Creates an expression by ANDing together a list, or None if empty."""
        if not exprs:
            return None
        expr = exprs[0]
        for rhs in exprs[1:]:
            expr = And(expr, rhs)
        return expr

    def is_column_lookup(self) -> int | None:
        """Checks if an expression is a single column lookup (i.e. a
        disjunction of = or IS NULL/NAN for a single column), returning the
        column index."""
        match self:
            # Column/constant equality can use index lookups. NULL and NaN are
            # handled in into_column_values().
            case Equal(Column(c), Constant()) | Equal(Constant(), Column(c)):
                return c
            # IS NULL and IS NAN can use index lookups.
            case Is(Column(c), _):
                return c
            # All OR branches must be lookups on the same column:
            # id = 1 OR id = 2 OR id = 3.
            case Or(lhs, rhs):
                left, right = lhs.is_column_lookup(), rhs.is_column_lookup()
                if left is not None and left == right:
                    return left
                return None
            case _:
                return None

    def into_column_values(self, index: int) -> list[Value]:
        """Extracts column lookup values for the given column. Panics if the
        expression isn't a lookup of the given column, i.e. is_column_lookup()
        must return true for the expression."""
        match self:
            case Equal(Column(column), Constant(value)) | Equal(Constant(value), Column(column)):
                assert column == index, "unexpected column"
                # NULL and NAN index lookups are for IS NULL and IS NAN.
                # Equality shouldn't match anything, return empty list.
                return [] if value.is_undefined() else [value]
            # IS NULL and IS NAN can use index lookups.
            case Is(Column(column), value):
                assert column == index, "unexpected column"
                return [value]
            case Or(lhs, rhs):
                values = lhs.into_column_values(index)
                values.extend(rhs.into_column_values(index))
                return values
            case expr:
                panic(f"unexpected expression {debug(expr)}")

    def replace_column(self, from_: int, to: int) -> "Expression":
        """Replaces column references from → to."""

        def xform(expr: Expression) -> Expression:
            match expr:
                case Column(i) if i == from_:
                    return Column(to)
                case _:
                    return expr

        return self.transform(xform, lambda e: e)

    def shift_column(self, diff: int) -> "Expression":
        """Shifts column references by the given amount (can be negative)."""

        def xform(expr: Expression) -> Expression:
            match expr:
                case Column(i):
                    return Column(i + diff)
                case _:
                    return expr

        return self.transform(xform, lambda e: e)

    @staticmethod
    def from_value(value: Value) -> "Expression":
        """Builds a constant expression. Mirrors the Rust `From<Value> for
        Expression`."""
        return Constant(value)


@dataclass(frozen=True, repr=False)
class Constant(Expression):
    """A constant value."""

    value: Value


@dataclass(frozen=True, repr=False)
class Column(Expression):
    """A column reference. Looks up the value in a row during evaluation."""

    index: int


@dataclass(frozen=True, repr=False)
class And(Expression):
    """a AND b: logical AND of two booleans."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Or(Expression):
    """a OR b: logical OR of two booleans."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Not(Expression):
    """NOT a: logical NOT of a boolean."""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Equal(Expression):
    """a = b: equality comparison of two values."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class GreaterThan(Expression):
    """a > b: greater than comparison of two values."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class LessThan(Expression):
    """a < b: less than comparison of two values."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Is(Expression):
    """a IS NULL or a IS NAN: checks for the given value."""

    expr: Expression
    value: Value


@dataclass(frozen=True, repr=False)
class Add(Expression):
    """a + b: adds two numbers."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Divide(Expression):
    """a / b: divides two numbers."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Exponentiate(Expression):
    """a ^ b: exponentiates two numbers."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Factorial(Expression):
    """a!: takes the factorial of a number (4! = 4*3*2*1)."""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Identity(Expression):
    """+a: the identity function, which simply returns the same number."""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Multiply(Expression):
    """a * b: multiplies two numbers."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Negate(Expression):
    """-a: negates the given number."""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Remainder(Expression):
    """a % b: the remainder after dividing two numbers."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class SquareRoot(Expression):
    """√a: takes the square root of a number."""

    expr: Expression


@dataclass(frozen=True, repr=False)
class Subtract(Expression):
    """a - b: subtracts two numbers."""

    lhs: Expression
    rhs: Expression


@dataclass(frozen=True, repr=False)
class Like(Expression):
    """a LIKE b: checks if a string matches a pattern."""

    lhs: Expression
    rhs: Expression


# The variants in Rust declaration order, which determines their encoding.
_VARIANTS: tuple[type[Expression], ...] = (
    Constant,
    Column,
    And,
    Or,
    Not,
    Equal,
    GreaterThan,
    LessThan,
    Is,
    Add,
    Divide,
    Exponentiate,
    Factorial,
    Identity,
    Multiply,
    Negate,
    Remainder,
    SquareRoot,
    Subtract,
    Like,
)

for _variant in _VARIANTS:
    setattr(Expression, _variant.__name__, _variant)


class ColumnLabels(Protocol):
    """Looks up column labels when displaying expressions. Implemented by
    `sql.planner.Node`, which is what the Rust code uses directly."""

    def column_label(self, index: int) -> Label:
        """Returns the label of the column with the given index."""
        ...


class ExpressionDisplay:
    """Helper to display expressions. Groups with () as needed by precedence
    rules, and looks up column labels in the given plan node."""

    def __init__(self, expr: Expression, node: "ColumnLabels | Node", parent_precedence: int) -> None:
        """Creates a new expression display."""
        self.expr = expr
        self.node = node
        self.parent_precedence = parent_precedence

    def __str__(self) -> str:
        # Group the expression if its precedence is lower than the parent.
        precedence = self.precedence(self.expr)

        # Helper to display a grouped expression.
        def group(expr: Expression) -> ExpressionDisplay:
            return ExpressionDisplay(expr, self.node, precedence)

        match self.expr:
            case Constant(value):
                output = str(value)
            case Column(index):
                match self.node.column_label(index):
                    case Label.None_():
                        output = f"#{index}"
                    case label:
                        output = str(label)

            case And(lhs, rhs):
                output = f"{group(lhs)} AND {group(rhs)}"
            case Or(lhs, rhs):
                output = f"{group(lhs)} OR {group(rhs)}"
            case Not(expr):
                output = f"NOT {group(expr)}"

            case Equal(lhs, rhs):
                output = f"{group(lhs)} = {group(rhs)}"
            case GreaterThan(lhs, rhs):
                output = f"{group(lhs)} > {group(rhs)}"
            case LessThan(lhs, rhs):
                output = f"{group(lhs)} < {group(rhs)}"
            case Is(expr, Value.Null()):
                output = f"{group(expr)} IS NULL"
            case Is(expr, Value.Float(n)) if math.isnan(n):
                output = f"{group(expr)} IS NAN"
            case Is(_, v):
                panic(f"unexpected IS value {v}")

            case Add(lhs, rhs):
                output = f"{group(lhs)} + {group(rhs)}"
            case Divide(lhs, rhs):
                output = f"{group(lhs)} / {group(rhs)}"
            case Exponentiate(lhs, rhs):
                output = f"{group(lhs)} ^ {group(rhs)}"
            case Factorial(expr):
                output = f"{group(expr)}!"
            case Identity(expr):
                output = f"{group(expr)}"
            case Multiply(lhs, rhs):
                output = f"{group(lhs)} * {group(rhs)}"
            case Negate(expr):
                output = f"-{group(expr)}"
            case Remainder(lhs, rhs):
                output = f"{group(lhs)} % {group(rhs)}"
            case SquareRoot(expr):
                output = f"sqrt({group(expr)})"
            case Subtract(lhs, rhs):
                output = f"{group(lhs)} - {group(rhs)}"

            case Like(lhs, rhs):
                output = f"{group(lhs)} LIKE {group(rhs)}"

            case expr:
                panic(f"unknown expression {expr!r}")

        if precedence < self.parent_precedence:
            return f"({output})"
        return output

    @staticmethod
    def precedence(expr: Expression) -> int:
        """Precedence levels for () grouping. Matches the parser."""
        match expr:
            case Column() | Constant() | SquareRoot():
                return 11
            case Identity() | Negate():
                return 10
            case Factorial():
                return 9
            case Exponentiate():
                return 8
            case Multiply() | Divide() | Remainder():
                return 7
            case Add() | Subtract():
                return 6
            case GreaterThan() | LessThan():
                return 5
            case Equal() | Like() | Is():
                return 4
            case Not():
                return 3
            case And():
                return 2
            case Or():
                return 1
            case _:
                panic(f"unknown expression {expr!r}")
