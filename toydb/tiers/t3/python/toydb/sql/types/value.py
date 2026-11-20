"""SQL data types, values, rows and column labels.

Mirrors `src/sql/types/value.rs`.
"""

import enum
import math
import struct
from abc import ABC
from collections.abc import Iterator
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, ClassVar, Protocol, Self, runtime_checkable

from toydb import encoding
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import errdata, errinput, panic
from toydb.fmt import debug_f64, debug_tuple, escape_debug

if TYPE_CHECKING:
    from toydb.sql.parser import ast

I64_MIN = -(2**63)
I64_MAX = 2**63 - 1
U32_MAX = 2**32 - 1


class DataType(enum.Enum):
    """A primitive SQL data type. For simplicity, only a handful of scalar
    types are supported (no compound types)."""

    Boolean = 0
    """A boolean: true or false."""
    Integer = 1
    """A 64-bit signed integer."""
    Float = 2
    """A 64-bit floating point number."""
    String = 3
    """A UTF-8 encoded string."""

    def __str__(self) -> str:
        match self:
            case DataType.Boolean:
                return "BOOLEAN"
            case DataType.Integer:
                return "INTEGER"
            case DataType.Float:
                return "FLOAT"
            case DataType.String:
                return "STRING"

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_unit_variant(self.value)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "DataType":
        index = deserializer.deserialize_enum()
        if index >= len(cls):
            raise deserializer.invalid_variant(index, len(cls))
        return cls(index)


class Value(encoding.Value):
    """A primitive SQL value, represented as a native Python type.

    Values are immutable, hashable, and totally ordered: see `__eq__`,
    `__hash__` and `cmp`.
    """

    Null: ClassVar[type["Null"]]
    Boolean: ClassVar[type["Boolean"]]
    Integer: ClassVar[type["Integer"]]
    Float: ClassVar[type["Float"]]
    String: ClassVar[type["String"]]

    def __str__(self) -> str:
        match self:
            case Null():
                return "NULL"
            case Boolean(True):
                return "TRUE"
            case Boolean(False):
                return "FALSE"
            case Integer(integer):
                return str(integer)
            case Float(f):
                return debug_f64(f)
            case String(string):
                return f"'{escape_debug(string)}'"
            case _:
                panic(f"unknown value {self!r}")

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the value like the Rust `#[derive(Debug)]`."""
        values = [getattr(self, field.name) for field in fields(self)]  # type: ignore[arg-type]
        return debug_tuple(type(self).__name__, values, pretty)

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case Null():
                serializer.serialize_unit_variant(0)
            case Boolean(value):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_bool(value)
            case Integer(value):
                serializer.serialize_newtype_variant(2)
                serializer.serialize_i64(value)
            case Float(value):
                serializer.serialize_newtype_variant(3)
                serialize_f64(value, serializer)
            case String(value):
                serializer.serialize_newtype_variant(4)
                serializer.serialize_str(value)
            case _:
                panic(f"unknown value {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Value":
        match deserializer.deserialize_enum():
            case 0:
                return Null()
            case 1:
                return Boolean(deserializer.deserialize_bool())
            case 2:
                return Integer(deserializer.deserialize_i64())
            case 3:
                return Float(deserializer.deserialize_f64())
            case 4:
                return String(deserializer.deserialize_str())
            case index:
                raise deserializer.invalid_variant(index, 5)

    def __eq__(self, other: object) -> bool:
        """Consider Nulls and ±NaNs equal. Integers and floats are compared
        as floats, and -0.0 equals 0.0."""
        match (self, other):
            case (Boolean(a), Boolean(b)):
                return a == b
            case (Integer(a), Integer(b)):
                return a == b
            case (Integer(a), Float(b)):
                return float(a) == b
            case (Float(a), Integer(b)):
                return a == float(b)
            case (Float(a), Float(b)):
                return a == b or math.isnan(a) and math.isnan(b)
            case (String(a), String(b)):
                return a == b
            case (Null(), Null()):
                return True
            case (_, _):
                return False

    def __hash__(self) -> int:
        """Allow hashing Nulls and floats, and hash -0.0 and -NaN as
        positive. The type is hashed too, so an equal integer and float have
        different hashes (like in Rust)."""
        match self:
            case Null():
                return hash((Null,))
            case Boolean(v):
                return hash((Boolean, v))
            case Integer(v):
                return hash((Integer, v))
            case Float(v):
                # Hash -NaN and -0.0 as positive.
                if (math.isnan(v) or v == 0.0) and _is_sign_negative(v):
                    v = -v
                return hash((Float, _to_bits(v)))
            case String(v):
                return hash((String, v))
            case _:
                panic(f"unknown value {self!r}")

    def cmp(self, other: "Value") -> int:
        """Compares two values, returning -1, 0 or 1 (like Rust's `Ordering`).
        Consider Nulls and NaNs equal when ordering.

        We establish a total order across all types, even though mixed types
        will rarely/never come up: String > Integer/Float > Boolean > Null.
        """
        match (self, other):
            case (Null(), Null()):
                return 0
            case (Boolean(a), Boolean(b)):
                return (a > b) - (a < b)
            case (Integer(a), Integer(b)):
                return (a > b) - (a < b)
            case (Integer(a), Float(b)):
                return _total_cmp(float(a), b)
            case (Float(a), Integer(b)):
                return _total_cmp(a, float(b))
            case (Float(a), Float(b)):
                return _total_cmp(a, b)
            case (String(a), String(b)):
                return (a > b) - (a < b)

            case (Null(), _):
                return -1
            case (_, Null()):
                return 1
            case (Boolean(), _):
                return -1
            case (_, Boolean()):
                return 1
            case (Float(), _):
                return -1
            case (_, Float()):
                return 1
            case (Integer(), _):
                return -1
            case (_, Integer()):
                return 1
            # String is ordered last.
            case _:
                panic(f"can't compare {self!r} and {other!r}")

    def __lt__(self, other: "Value") -> bool:
        return self.cmp(other) < 0

    def __le__(self, other: "Value") -> bool:
        return self.cmp(other) <= 0

    def __gt__(self, other: "Value") -> bool:
        return self.cmp(other) > 0

    def __ge__(self, other: "Value") -> bool:
        return self.cmp(other) >= 0

    def datatype(self) -> DataType | None:
        """Returns the value's datatype, or None for null values."""
        match self:
            case Null():
                return None
            case Boolean():
                return DataType.Boolean
            case Integer():
                return DataType.Integer
            case Float():
                return DataType.Float
            case String():
                return DataType.String
            case _:
                panic(f"unknown value {self!r}")

    def is_undefined(self) -> bool:
        """Returns true if the value is undefined (NULL or NaN)."""
        match self:
            case Null():
                return True
            case Float(f) if math.isnan(f):
                return True
            case _:
                return False

    def checked_add(self, other: "Value") -> "Value":
        """Adds two values. Errors if invalid."""
        match (self, other):
            case (Integer(lhs), Integer(rhs)):
                if not I64_MIN <= lhs + rhs <= I64_MAX:
                    raise errinput("integer overflow")
                return Integer(lhs + rhs)
            case (Integer(lhs), Float(rhs)):
                return Float(float(lhs) + rhs)
            case (Float(lhs), Integer(rhs)):
                return Float(lhs + float(rhs))
            case (Float(lhs), Float(rhs)):
                return Float(lhs + rhs)
            case (Null(), Integer() | Float() | Null()):
                return Null()
            case (Integer() | Float(), Null()):
                return Null()
            case (lhs, rhs):
                raise errinput(f"can't add {lhs} and {rhs}")

    def checked_div(self, other: "Value") -> "Value":
        """Divides two values. Errors if invalid."""
        match (self, other):
            case (Integer(), Integer(0)):
                raise errinput("can't divide by zero")
            case (Integer(lhs), Integer(rhs)):
                if lhs == I64_MIN and rhs == -1:
                    panic("attempt to divide with overflow")
                # Integer division truncates towards zero.
                quotient = abs(lhs) // abs(rhs)
                return Integer(quotient if (lhs < 0) == (rhs < 0) else -quotient)
            case (Integer(lhs), Float(rhs)):
                return Float(_div(float(lhs), rhs))
            case (Float(lhs), Integer(rhs)):
                return Float(_div(lhs, float(rhs)))
            case (Float(lhs), Float(rhs)):
                return Float(_div(lhs, rhs))
            case (Null(), Integer() | Float() | Null()):
                return Null()
            case (Integer() | Float(), Null()):
                return Null()
            case (lhs, rhs):
                raise errinput(f"can't divide {lhs} and {rhs}")

    def checked_mul(self, other: "Value") -> "Value":
        """Multiplies two values. Errors if invalid."""
        match (self, other):
            case (Integer(lhs), Integer(rhs)):
                if not I64_MIN <= lhs * rhs <= I64_MAX:
                    raise errinput("integer overflow")
                return Integer(lhs * rhs)
            case (Integer(lhs), Float(rhs)):
                return Float(float(lhs) * rhs)
            case (Float(lhs), Integer(rhs)):
                return Float(lhs * float(rhs))
            case (Float(lhs), Float(rhs)):
                return Float(lhs * rhs)
            case (Null(), Integer() | Float() | Null()):
                return Null()
            case (Integer() | Float(), Null()):
                return Null()
            case (lhs, rhs):
                raise errinput(f"can't multiply {lhs} and {rhs}")

    def checked_pow(self, other: "Value") -> "Value":
        """Exponentiates two values. Errors if invalid."""
        match (self, other):
            case (Integer(lhs), Integer(rhs)) if rhs >= 0:
                if rhs > U32_MAX:
                    raise errinput("integer overflow")
                i = _checked_pow(lhs, rhs)
                if i is None:
                    raise errinput("integer overflow")
                return Integer(i)
            case (Integer(lhs), Integer(rhs)):
                return Float(_powf(float(lhs), float(rhs)))
            case (Integer(lhs), Float(rhs)):
                return Float(_powf(float(lhs), rhs))
            case (Float(lhs), Integer(rhs)):
                return Float(_powi(lhs, _wrap_i32(rhs)))
            case (Float(lhs), Float(rhs)):
                return Float(_powf(lhs, rhs))
            case (Integer() | Float(), Null()):
                return Null()
            case (Null(), Integer() | Float() | Null()):
                return Null()
            case (lhs, rhs):
                raise errinput(f"can't exponentiate {lhs} and {rhs}")

    def checked_rem(self, other: "Value") -> "Value":
        """Finds the remainder of two values. Errors if invalid.

        NB: uses the remainder, not modulo, like Postgres. This means that for
        negative values, the result has the sign of the dividend, rather than
        always returning a positive value.
        """
        match (self, other):
            case (Integer(), Integer(0)):
                raise errinput("can't divide by zero")
            case (Integer(lhs), Integer(rhs)):
                if lhs == I64_MIN and rhs == -1:
                    panic("attempt to calculate the remainder with overflow")
                remainder = abs(lhs) % abs(rhs)
                return Integer(-remainder if lhs < 0 else remainder)
            case (Integer(lhs), Float(rhs)):
                return Float(_rem(float(lhs), rhs))
            case (Float(lhs), Integer(rhs)):
                return Float(_rem(lhs, float(rhs)))
            case (Float(lhs), Float(rhs)):
                return Float(_rem(lhs, rhs))
            case (Integer() | Float() | Null(), Null()):
                return Null()
            case (Null(), Integer() | Float()):
                return Null()
            case (lhs, rhs):
                raise errinput(f"can't take remainder of {lhs} and {rhs}")

    def checked_sub(self, other: "Value") -> "Value":
        """Subtracts two values. Errors if invalid."""
        match (self, other):
            case (Integer(lhs), Integer(rhs)):
                if not I64_MIN <= lhs - rhs <= I64_MAX:
                    raise errinput("integer overflow")
                return Integer(lhs - rhs)
            case (Integer(lhs), Float(rhs)):
                return Float(float(lhs) - rhs)
            case (Float(lhs), Integer(rhs)):
                return Float(lhs - float(rhs))
            case (Float(lhs), Float(rhs)):
                return Float(lhs - rhs)
            case (Null(), Integer() | Float() | Null()):
                return Null()
            case (Integer() | Float(), Null()):
                return Null()
            case (lhs, rhs):
                raise errinput(f"can't subtract {lhs} and {rhs}")

    @staticmethod
    def from_native(v: bool | int | float | str) -> "Value":
        """Converts a native Python value into a Value, like the Rust
        `From<bool>`, `From<i64>`, `From<f64>` and `From<String>`."""
        match v:
            case bool():
                return Boolean(v)
            case int():
                return Integer(v)
            case float():
                return Float(v)
            case str():
                return String(v)

    def try_into_bool(self) -> bool:
        """Returns the boolean of a Value.Boolean, or errors. Mirrors the Rust
        `TryFrom<Value> for bool`."""
        match self:
            case Boolean(b):
                return b
            case _:
                raise errdata(f"not a boolean: {self}")

    def try_into_f64(self) -> float:
        """Returns the float of a Value.Float, or errors. Mirrors the Rust
        `TryFrom<Value> for f64`."""
        match self:
            case Float(f):
                return f
            case _:
                raise errdata(f"not a float: {self}")

    def try_into_i64(self) -> int:
        """Returns the integer of a Value.Integer, or errors. Mirrors the Rust
        `TryFrom<Value> for i64`."""
        match self:
            case Integer(i):
                return i
            case _:
                raise errdata(f"not an integer: {self}")

    def try_into_string(self) -> str:
        """Returns the string of a Value.String, or errors. Mirrors the Rust
        `TryFrom<Value> for String`."""
        match self:
            case String(s):
                return s
            case _:
                raise errdata(f"not a string: {self}")


@dataclass(frozen=True, eq=False, repr=False)
class Null(Value):
    """An unknown value of unknown type (i.e. SQL NULL).

    In code, Null is considered equal to Null, so that we can detect, index,
    and order these values. The SQL NULL semantics are implemented during
    Expression evaluation.
    """


@dataclass(frozen=True, eq=False, repr=False)
class Boolean(Value):
    """A boolean."""

    value: bool


@dataclass(frozen=True, eq=False, repr=False)
class Integer(Value):
    """A 64-bit signed integer."""

    value: int


@dataclass(frozen=True, eq=False, repr=False)
class Float(Value):
    """A 64-bit floating point number.

    In code, NaN is considered equal to NaN, so that we can detect, index,
    and order these values. The SQL NAN semantics are implemented during
    Expression evaluation.

    -0.0 and -NaN are considered equal to their positive counterpart, and
    normalized as positive when serialized (for key lookups).
    """

    value: float


@dataclass(frozen=True, eq=False, repr=False)
class String(Value):
    """A UTF-8 encoded string."""

    value: str


Value.Null = Null
Value.Boolean = Boolean
Value.Integer = Integer
Value.Float = Float
Value.String = String


def serialize_f64(value: float, serializer: Serializer) -> None:
    """Serialize f64 -0.0 and -NaN as positive, such that they're considered
    equal in the key/value store (e.g. for index lookups)."""
    if (math.isnan(value) or value == 0.0) and _is_sign_negative(value):
        value = -value
    serializer.serialize_f64(value)


def _is_sign_negative(value: float) -> bool:
    """Returns true if the float has a negative sign, including -0.0 and -NaN."""
    return math.copysign(1.0, value) < 0.0


def _to_bits(value: float) -> int:
    """Returns the IEEE 754 bits of a float as a u64, like `f64::to_bits`."""
    return struct.unpack("<Q", struct.pack("<d", value))[0]


def _total_cmp(a: float, b: float) -> int:
    """Compares two floats using the IEEE 754 total order, like
    `f64::total_cmp`: -NaN < -inf < ... < -0.0 < 0.0 < ... < inf < NaN."""

    def key(value: float) -> int:
        bits: int = struct.unpack("<q", struct.pack("<d", value))[0]
        # Flip all bits except the sign for negative numbers, such that the
        # bits can be compared as signed integers.
        return bits ^ I64_MAX if bits < 0 else bits

    left, right = key(a), key(b)
    return (left > right) - (left < right)


def _div(lhs: float, rhs: float) -> float:
    """Divides two floats like Rust, where division by zero yields infinity
    or NaN rather than an error."""
    if rhs != 0.0:
        return lhs / rhs
    if lhs == 0.0 or math.isnan(lhs):
        return math.nan
    return math.copysign(math.inf, lhs) * math.copysign(1.0, rhs)


def _rem(lhs: float, rhs: float) -> float:
    """Takes the remainder of two floats like Rust (i.e. C `fmod`), where
    invalid operands yield NaN rather than an error."""
    if math.isnan(lhs) or math.isnan(rhs) or math.isinf(lhs) or rhs == 0.0:
        return math.nan
    return math.fmod(lhs, rhs)


def _is_odd_integer(value: float) -> bool:
    return math.isfinite(value) and math.fmod(abs(value), 2.0) == 1.0


def _powf(lhs: float, rhs: float) -> float:
    """Raises a float to a float power like `f64::powf` (i.e. C `pow`), where
    overflows and invalid operands yield infinity or NaN rather than an error."""
    try:
        return math.pow(lhs, rhs)
    except OverflowError:
        return -math.inf if lhs < 0.0 and _is_odd_integer(rhs) else math.inf
    except ValueError:
        if lhs == 0.0:  # zero raised to a negative power
            negative = _is_sign_negative(lhs) and _is_odd_integer(rhs)
            return -math.inf if negative else math.inf
        return math.nan  # negative number raised to a fractional power


def _powi(lhs: float, rhs: int) -> float:
    """Raises a float to an i32 power like `f64::powi`, using repeated
    multiplication (which may round differently than `powf`)."""
    exponent = abs(rhs)
    result = 1.0
    while True:
        if exponent & 1:
            result *= lhs
        exponent >>= 1
        if exponent == 0:
            break
        lhs *= lhs
    return _div(1.0, result) if rhs < 0 else result


def _wrap_i32(value: int) -> int:
    """Truncates an integer to an i32, like the Rust cast `as i32`."""
    return (value + 2**31) % 2**32 - 2**31


def _checked_pow(base: int, exponent: int) -> int | None:
    """Raises an i64 to a u32 power like `i64::checked_pow`, returning None on
    overflow. Uses exponentiation by squaring, bailing out on overflow."""
    if exponent == 0:
        return 1
    result = 1
    while True:
        if exponent & 1:
            result *= base
            if not I64_MIN <= result <= I64_MAX:
                return None
            if exponent == 1:
                return result
        exponent //= 2
        base *= base
        if not I64_MIN <= base <= I64_MAX:
            return None


type Row = list[Value]
"""A row of values."""


@runtime_checkable
class RowIterator(Protocol):
    """A row iterator, which must also be clonable. Cloning allows resetting
    an iterator back to an initial state, e.g. for nested loop joins.

    Yields rows, or raises an `Error` (the Rust iterator yields `Result<Row>`).
    """

    def __iter__(self) -> Iterator[Row]: ...

    def __next__(self) -> Row: ...

    def clone(self) -> Self:
        """Clones the iterator in its current state."""
        ...


type Rows = RowIterator
"""A row iterator."""


class Label(ABC):
    """A column label, used in query results and plans."""

    None_: ClassVar[type["LabelNone"]]
    Unqualified: ClassVar[type["Unqualified"]]
    Qualified: ClassVar[type["Qualified"]]

    def __str__(self) -> str:
        match self:
            case LabelNone():
                return ""
            case Unqualified(name):
                return name
            case Qualified(table, column):
                return f"{table}.{column}"
            case _:
                panic(f"unknown label {self!r}")

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the label like the Rust `#[derive(Debug)]`."""
        values = [getattr(self, field.name) for field in fields(self)]  # type: ignore[arg-type]
        name = "None" if isinstance(self, LabelNone) else type(self).__name__
        return debug_tuple(name, values, pretty)

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case LabelNone():
                serializer.serialize_unit_variant(0)
            case Unqualified(name):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_str(name)
            case Qualified(table, column):
                serializer.serialize_tuple_variant(2)
                serializer.serialize_str(table)
                serializer.serialize_str(column)
            case _:
                panic(f"unknown label {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Label":
        match deserializer.deserialize_enum():
            case 0:
                return LabelNone()
            case 1:
                return Unqualified(deserializer.deserialize_str())
            case 2:
                return Qualified(deserializer.deserialize_str(), deserializer.deserialize_str())
            case index:
                raise deserializer.invalid_variant(index, 3)

    def as_header(self) -> str:
        """Formats the label as a short column header."""
        match self:
            case Qualified(_, column) | Unqualified(column):
                return column
            case _:
                return "?"

    def into_ast_expression(self) -> "ast.Expression":
        """Builds an ast.Expression.Column for a label. Can't be None. Mirrors
        the Rust `From<Label> for ast::Expression`."""
        from toydb.sql.parser import ast

        match self:
            case Qualified(table, column):
                return ast.Expression.Column(table, column)
            case Unqualified(column):
                return ast.Expression.Column(None, column)
            case _:
                panic("can't convert None label to AST expression")

    @staticmethod
    def from_name(name: str | None) -> "Label":
        """Builds an unqualified label from a name, or None if there is no
        name. Mirrors the Rust `From<Option<String>> for Label`."""
        return Unqualified(name) if name is not None else LabelNone()


@dataclass(frozen=True, repr=False)
class LabelNone(Label):
    """No label. This is the Rust `Label::None`, available as `Label.None_`
    since `None` is a Python keyword."""


@dataclass(frozen=True, repr=False)
class Unqualified(Label):
    """An unqualified column name."""

    name: str


@dataclass(frozen=True, repr=False)
class Qualified(Label):
    """A fully qualified table/column name."""

    table: str
    column: str


Label.None_ = LabelNone
Label.Unqualified = Unqualified
Label.Qualified = Qualified
