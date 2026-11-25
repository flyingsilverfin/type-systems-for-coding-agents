"""PostgreSQL types of toydb values, and their text and binary formats.

| toydb data type | PostgreSQL type | OID | size |
|---|---|---|---|
| BOOLEAN | `bool` | 16 | 1 |
| INTEGER | `int8` | 20 | 8 |
| FLOAT | `float8` | 701 | 8 |
| STRING | `text` | 25 | -1 |
| every data type added later, and an unknown type | `text` | 25 | -1 |

A value is sent in its own type's format: BOOLEAN as `t`/`f` (binary: one
byte), INTEGER in decimal (binary: 8 bytes), FLOAT as displayed in query
results except `NaN`, `Infinity`, `-Infinity` (binary: 8 bytes IEEE 754),
STRING as itself (binary: UTF-8), a value of every data type added later as
displayed in query results (binary: the UTF-8 of that text).

Parameter OIDs map to data types: 16 BOOLEAN; 20, 21, 23 INTEGER; 700, 701
FLOAT; 25, 1043 STRING; other OIDs are not supported. Parameter values are
read by their declared OID: `t`, `true`, `f`, `false` in any case; integers
with an optional sign within the range of the type; floats as IMPORT reads
them, or `NaN`, `Infinity`, `-Infinity`, `inf`, `-inf` in any case; text as
itself. Binary parameters have the sizes of their types.
"""

import math
import re
import struct
from dataclasses import dataclass

from toydb.error import panic
from toydb.pgwire.message import PgError
from toydb.sql.types import DataType, Value
from toydb.sql.types.formats import from_text

_INTEGER = re.compile(r"[+-]?[0-9]+")
"""An integer with an optional sign, ASCII digits only."""


@dataclass(frozen=True)
class PgType:
    """A PostgreSQL type."""

    name: str
    oid: int
    size: int
    """The size in bytes, -1 for a variable size."""


BOOL = PgType("bool", 16, 1)
INT2 = PgType("int2", 21, 2)
INT4 = PgType("int4", 23, 4)
INT8 = PgType("int8", 20, 8)
FLOAT4 = PgType("float4", 700, 4)
FLOAT8 = PgType("float8", 701, 8)
TEXT = PgType("text", 25, -1)
VARCHAR = PgType("varchar", 1043, -1)

TYPES: dict[int, PgType] = {
    pgtype.oid: pgtype for pgtype in (BOOL, INT2, INT4, INT8, FLOAT4, FLOAT8, TEXT, VARCHAR)
}
"""The PostgreSQL types, by OID."""

PARAMETER_TYPES: dict[int, DataType] = {
    BOOL.oid: DataType.Boolean,
    INT2.oid: DataType.Integer,
    INT4.oid: DataType.Integer,
    INT8.oid: DataType.Integer,
    FLOAT4.oid: DataType.Float,
    FLOAT8.oid: DataType.Float,
    TEXT.oid: DataType.String,
    VARCHAR.oid: DataType.String,
}
"""The data types of the parameter OIDs."""

_FLOAT_WORDS: dict[str, float] = {
    "nan": math.nan,
    "infinity": math.inf,
    "-infinity": -math.inf,
    "inf": math.inf,
    "-inf": -math.inf,
}
"""The float words of parameter values, in lower case."""


def pg_type(datatype: DataType | None) -> PgType:
    """Returns the PostgreSQL type that a data type is sent as, text for an
    unknown type."""
    match datatype:
        case DataType.Boolean:
            return BOOL
        case DataType.Integer:
            return INT8
        case DataType.Float:
            return FLOAT8
        case _:
            return TEXT


def encode(value: Value, format: int) -> bytes | None:
    """Encodes a value in the given format, None for NULL."""
    match value:
        case Value.Null():
            return None
        case _ if format == 1:
            return encode_binary(value)
        case _:
            return encode_text(value).encode()


def encode_text(value: Value) -> str:
    """Encodes a value in the text format."""
    match value:
        case Value.Boolean(b):
            return "t" if b else "f"
        case Value.Integer(i):
            return str(i)
        case Value.Float(f) if math.isnan(f):
            return "NaN"
        case Value.Float(f) if math.isinf(f):
            return "Infinity" if f > 0 else "-Infinity"
        case Value.String(s):
            return s
        case _:
            return str(value)


def encode_binary(value: Value) -> bytes:
    """Encodes a value in the binary format."""
    match value:
        case Value.Boolean(b):
            return bytes([b])
        case Value.Integer(i):
            return struct.pack(">q", i)
        case Value.Float(f):
            return struct.pack(">d", f)
        case Value.String(s):
            return s.encode()
        case _:
            return encode_text(value).encode()


def parameter_type(oid: int) -> DataType:
    """Returns the data type of a parameter OID, or an error if it isn't
    supported."""
    if (datatype := PARAMETER_TYPES.get(oid)) is None:
        raise PgError("0A000", f"unsupported parameter type {oid}")
    return datatype


def decode_parameter(oid: int, format: int, data: bytes | None, index: int) -> Value:
    """Decodes a parameter value of the given OID and format."""
    parameter_type(oid)
    pgtype = TYPES[oid]
    if data is None:
        return Value.Null()
    match format:
        case 0:
            try:
                text = data.decode()
            except UnicodeDecodeError:
                raise invalid_encoding() from None
            return decode_text(pgtype, text)
        case 1:
            return decode_binary(pgtype, data, index)
        case _:
            raise unsupported_format(format)


def decode_text(pgtype: PgType, text: str) -> Value:
    """Decodes a value of a parameter type from the text format."""
    match PARAMETER_TYPES[pgtype.oid]:
        case DataType.Boolean:
            match text.lower():
                case "t" | "true":
                    return Value.Boolean(True)
                case "f" | "false":
                    return Value.Boolean(False)
        case DataType.Integer:
            if _INTEGER.fullmatch(text):
                integer = int(text)
                bound = 1 << (8 * pgtype.size - 1)
                if not -bound <= integer < bound:
                    raise PgError(
                        "22003", f'value "{text}" is out of range for type {pgtype.name}'
                    )
                return Value.Integer(integer)
        case DataType.Float:
            if (word := _FLOAT_WORDS.get(text.lower())) is not None:
                return Value.Float(word)
            try:
                return from_text(text, DataType.Float)
            except ValueError:
                pass
        case DataType.String:
            return Value.String(text)
        case datatype:
            panic(f"unexpected parameter datatype {datatype}")
    raise PgError("22P02", f'invalid input syntax for type {pgtype.name}: "{text}"')


def decode_binary(pgtype: PgType, data: bytes, index: int) -> Value:
    """Decodes a value of a parameter type from the binary format."""
    if pgtype.size != -1 and len(data) != pgtype.size:
        raise invalid_binary(index)
    match PARAMETER_TYPES[pgtype.oid]:
        case DataType.Boolean:
            if data[0] > 1:
                raise invalid_binary(index)
            return Value.Boolean(data[0] == 1)
        case DataType.Integer:
            return Value.Integer(int.from_bytes(data, "big", signed=True))
        case DataType.Float:
            return Value.Float(struct.unpack(">f" if pgtype.size == 4 else ">d", data)[0])
        case DataType.String:
            try:
                return Value.String(data.decode())
            except UnicodeDecodeError:
                raise invalid_binary(index) from None
        case datatype:
            panic(f"unexpected parameter datatype {datatype}")


def invalid_binary(index: int) -> PgError:
    """Returns the error for invalid binary data of the parameter with the
    given number."""
    return PgError("22P03", f"incorrect binary data format in bind parameter {index}")


def check_format(code: int) -> None:
    """Checks that a format code is 0 (text) or 1 (binary)."""
    if code not in (0, 1):
        raise unsupported_format(code)


def unsupported_format(code: int) -> PgError:
    """Returns the error for an unsupported format code."""
    return PgError("22023", f"unsupported format code: {code}")


def invalid_encoding() -> PgError:
    """Returns the error for text that isn't UTF-8."""
    return PgError("22021", "invalid byte sequence for encoding UTF8")
