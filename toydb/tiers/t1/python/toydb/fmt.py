"""Rust-compatible `Display` and `Debug` formatting helpers.

The golden scripts contain output produced by Rust's `{}`, `{:?}` and `{:#?}`
format specifiers, which the Python port must reproduce exactly. This module
is the stand-in for `std::fmt`:

* `Display` (`{}`) maps to `__str__`. Use `display_f64` and `display_bool` for
  primitives whose Python `str()` differs from Rust.
* `Debug` (`{:?}` and `{:#?}`) maps to `debug(value)` and `debug(value,
  pretty=True)`. Dataclasses and enums get a `#[derive(Debug)]` equivalent for
  free; other types (e.g. tuple-like enum variants) implement the `Debug`
  protocol by defining `fmt_debug`, typically using `debug_tuple` and friends.
"""

import dataclasses
import enum
import math
import typing
import unicodedata
from collections.abc import Iterable, Mapping
from decimal import Decimal
from types import NoneType, UnionType
from typing import Protocol, runtime_checkable

# The indentation used by `{:#?}`.
_INDENT = "    "


@runtime_checkable
class Debug(Protocol):
    """Implemented by types with custom Rust `Debug` formatting."""

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the value like `{:?}`, or `{:#?}` if pretty is true."""
        ...


def display_bool(value: bool) -> str:
    """Formats a bool like Rust's `{}`."""
    return "true" if value else "false"


def _float_digits(value: float) -> tuple[str, int]:
    """Returns the shortest round-trip decimal digits of a finite float's
    absolute value, and the decimal exponent of the last digit."""
    _, digits, exponent = Decimal(repr(abs(value))).as_tuple()
    assert isinstance(exponent, int)
    string = "".join(map(str, digits)).lstrip("0")
    stripped = string.rstrip("0")
    exponent += len(string) - len(stripped)
    return (stripped, exponent) if stripped else ("0", 0)


def _float_sign(value: float) -> str:
    return "-" if math.copysign(1.0, value) < 0 else ""


def _float_decimal(value: float) -> str:
    """Formats the absolute value of a finite float without an exponent."""
    digits, exponent = _float_digits(value)
    if exponent >= 0:
        return digits + "0" * exponent
    point = len(digits) + exponent
    if point > 0:
        return f"{digits[:point]}.{digits[point:]}"
    return f"0.{'0' * -point}{digits}"


def display_f64(value: float) -> str:
    """Formats an f64 like Rust's `{}`: the shortest representation that
    round-trips, never using an exponent, and without a trailing `.0`."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return _float_sign(value) + "inf"
    return _float_sign(value) + _float_decimal(value)


def debug_f64(value: float) -> str:
    """Formats an f64 like Rust's `{:?}`: like `{}`, but with at least one
    decimal, and using an exponent for very large or small numbers."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return _float_sign(value) + "inf"
    magnitude = abs(value)
    if magnitude >= 1e16 or (magnitude != 0.0 and magnitude < 1e-4):
        digits, exponent = _float_digits(value)
        mantissa = digits[0] + (f".{digits[1:]}" if len(digits) > 1 else "")
        return f"{_float_sign(value)}{mantissa}e{exponent + len(digits) - 1}"
    decimal = _float_decimal(value)
    if "." not in decimal:
        decimal += ".0"
    return _float_sign(value) + decimal


# Characters with the Unicode Grapheme_Extend property that aren't in the Mn or
# Me general categories (which are always Grapheme_Extend).
_OTHER_GRAPHEME_EXTEND = frozenset(
    [0x09BE, 0x09D7, 0x0B3E, 0x0B57, 0x0BBE, 0x0BD7, 0x0CC0, 0x0CC2, 0x0CC7, 0x0CC8, 0x0CCA]
    + [0x0CCB, 0x0CD5, 0x0CD6, 0x0D3E, 0x0D57, 0x0DCF, 0x0DDF, 0x1715, 0x1734, 0x1B35, 0x1B3B]
    + [0x1B3D, 0x1B43, 0x1B44, 0x1BAA, 0x1BF2, 0x1BF3, 0x200C, 0x302E, 0x302F, 0xA953, 0xA9C0]
    + [0xFF9E, 0xFF9F, 0x111C0, 0x11235, 0x1134D, 0x1133E, 0x11357, 0x114B0, 0x114BD, 0x115AF]
    + [0x11930, 0x1193D, 0x1D165, 0x1D166, 0x1D16D, 0x1D16E, 0x1D16F, 0x1D170, 0x1D171, 0x1D172]
    + list(range(0xE0020, 0xE0080))
)


def _is_grapheme_extended(char: str) -> bool:
    return unicodedata.category(char) in ("Mn", "Me") or ord(char) in _OTHER_GRAPHEME_EXTEND


def _escape_debug_char(
    char: str,
    *,
    escape_grapheme_extended: bool,
    escape_single_quote: bool,
    escape_double_quote: bool,
) -> str:
    """Mirrors Rust's `char::escape_debug_ext`."""
    match char:
        case "\0":
            return "\\0"
        case "\t":
            return "\\t"
        case "\r":
            return "\\r"
        case "\n":
            return "\\n"
        case "\\":
            return "\\\\"
        case '"' if escape_double_quote:
            return '\\"'
        case "'" if escape_single_quote:
            return "\\'"
        case _ if escape_grapheme_extended and _is_grapheme_extended(char):
            return f"\\u{{{ord(char):x}}}"
        case _ if char.isprintable():
            return char
        case _:
            return f"\\u{{{ord(char):x}}}"


def escape_debug(string: str) -> str:
    """Escapes a string like Rust's `str::escape_debug`, without quoting it."""
    return "".join(
        _escape_debug_char(
            char,
            escape_grapheme_extended=True,
            escape_single_quote=True,
            escape_double_quote=True,
        )
        for char in string
    )


def debug_str(string: str) -> str:
    """Formats a string like Rust's `{:?}`: double-quoted and escaped."""
    escaped = "".join(
        _escape_debug_char(
            char,
            escape_grapheme_extended=True,
            escape_single_quote=False,
            escape_double_quote=True,
        )
        for char in string
    )
    return f'"{escaped}"'


def debug_char(char: str) -> str:
    """Formats a single character like Rust's `{:?}` for `char`."""
    escaped = _escape_debug_char(
        char, escape_grapheme_extended=True, escape_single_quote=True, escape_double_quote=False
    )
    return f"'{escaped}'"


def escape_default(data: bytes) -> str:
    """Escapes bytes like Rust's `std::ascii::escape_default`, yielding
    printable ASCII."""
    output: list[str] = []
    for byte in data:
        match byte:
            case 0x09:
                output.append("\\t")
            case 0x0D:
                output.append("\\r")
            case 0x0A:
                output.append("\\n")
            case 0x27:
                output.append("\\'")
            case 0x22:
                output.append('\\"')
            case 0x5C:
                output.append("\\\\")
            case _ if 0x20 <= byte <= 0x7E:
                output.append(chr(byte))
            case _:
                output.append(f"\\x{byte:02x}")
    return "".join(output)


def utf8_error(error: UnicodeDecodeError) -> tuple[int, int | None]:
    """Converts a Python UTF-8 decode error into the fields of a Rust
    `Utf8Error`: `valid_up_to`, and `error_len` (None if the input ended
    unexpectedly)."""
    if error.end >= len(error.object) and "unexpected end" in error.reason:
        return error.start, None
    return error.start, max(error.end - error.start, 1)


def display_utf8_error(error: UnicodeDecodeError) -> str:
    """Formats a Python UTF-8 decode error like the `Display` of Rust's
    `Utf8Error` and `FromUtf8Error`."""
    valid_up_to, error_len = utf8_error(error)
    if error_len is None:
        return f"incomplete utf-8 byte sequence from index {valid_up_to}"
    return f"invalid utf-8 sequence of {error_len} bytes from index {valid_up_to}"


def debug_bytes(data: bytes) -> str:
    """Formats bytes like Rust's `{:?}` for `Vec<u8>` or `&[u8]`."""
    return f"[{', '.join(str(byte) for byte in data)}]"


def debug_bytes_hex(data: bytes) -> str:
    """Formats bytes like Rust's `{:x?}` for `Vec<u8>` or `&[u8]`."""
    return f"[{', '.join(f'{byte:x}' for byte in data)}]"


def _pad(string: str) -> str:
    """Indents every line, like Rust's `PadAdapter` does for `{:#?}`."""
    return "".join(_INDENT + line for line in string.splitlines(keepends=True))


def _debug_items(open: str, items: list[str], close: str, pretty: bool) -> str:
    if not items:
        return open + close
    if pretty:
        return f"{open}\n{''.join(_pad(item + ',\n') for item in items)}{close}"
    return f"{open}{', '.join(items)}{close}"


def debug_struct(name: str, fields: Iterable[tuple[str, object]], pretty: bool = False) -> str:
    """Formats a struct (or struct-like enum variant) with named fields, like
    Rust's `Formatter::debug_struct`. Field values that are already formatted
    can be passed as `Raw`."""
    items = [f"{field}: {debug(value, pretty)}" for field, value in fields]
    if not items:
        return name
    if pretty:
        return _debug_items(f"{name} {{", items, "}", pretty)
    return f"{name} {{ {', '.join(items)} }}"


def debug_tuple(name: str, values: Iterable[object], pretty: bool = False) -> str:
    """Formats a tuple struct (or tuple-like enum variant) with positional
    fields, like Rust's `Formatter::debug_tuple`. With an empty name, formats a
    plain tuple."""
    items = [debug(value, pretty) for value in values]
    if not items:
        return name or "()"
    if not name and len(items) == 1 and not pretty:
        return f"({items[0]},)"
    return _debug_items(f"{name}(", items, ")", pretty)


def debug_list(values: Iterable[object], pretty: bool = False) -> str:
    """Formats a list, like Rust's `Formatter::debug_list`."""
    return _debug_items("[", [debug(value, pretty) for value in values], "]", pretty)


def debug_set(values: Iterable[object], pretty: bool = False) -> str:
    """Formats a set in iteration order, like Rust's `Formatter::debug_set`."""
    return _debug_items("{", [debug(value, pretty) for value in values], "}", pretty)


def debug_map(entries: Iterable[tuple[object, object]], pretty: bool = False) -> str:
    """Formats a map in iteration order, like Rust's `Formatter::debug_map`."""
    items = [f"{debug(key, pretty)}: {debug(value, pretty)}" for key, value in entries]
    return _debug_items("{", items, "}", pretty)


def debug_option(value: object | None, pretty: bool = False) -> str:
    """Formats an `Option<T>`: `None`, or the value wrapped in `Some(...)`."""
    if value is None:
        return "None"
    return debug_tuple("Some", [value], pretty)


@dataclasses.dataclass(frozen=True)
class Raw:
    """A preformatted string, emitted as is by `debug`. Used for fields whose
    `Debug` output can't be inferred from the Python value."""

    string: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return self.string


@dataclasses.dataclass(frozen=True)
class Some:
    """Marks a value as `Option::Some` for `debug`, e.g. as a struct field."""

    value: object

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Some", [self.value], pretty)


def _is_optional(hint: object) -> bool:
    """Returns true if a type hint is a `T | None` union, i.e. `Option<T>`."""
    if typing.get_origin(hint) in (UnionType, typing.Union):
        return NoneType in typing.get_args(hint)
    return False


def _sorted(values: Iterable[object]) -> list[object]:
    """Sorts set members like a `BTreeSet`, if they're comparable."""
    values = list(values)
    try:
        return sorted(values)  # type: ignore[type-var]
    except TypeError:
        return values


def debug(value: object, pretty: bool = False) -> str:
    """Formats a value like Rust's `{:?}`, or `{:#?}` if pretty is true.

    * Types implementing `Debug` use their `fmt_debug` method.
    * Dataclasses are formatted like `#[derive(Debug)]` structs (or struct
      variants) using their field names, with `T | None` fields as `Option<T>`.
      A dataclass without fields is formatted as a unit struct or variant.
    * `enum.Enum` members are formatted as their name.
    * `None` is `None`, `bool` is `true`/`false`, `str` is a quoted and escaped
      string, `bytes` is a list of integers (like `Vec<u8>`), and `list`,
      `tuple`, `set` (sorted, like `BTreeSet`) and `dict` (in iteration order)
      are formatted like the corresponding Rust collections.

    An `Option<T>` that is `Some` must be formatted via `debug_option` or
    `Some`, unless it is a dataclass field annotated as `T | None`.
    """
    if isinstance(value, Debug) and not isinstance(value, type):
        return value.fmt_debug(pretty)
    if isinstance(value, enum.Enum):
        return value.name
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        hints = typing.get_type_hints(type(value))
        fields: list[tuple[str, object]] = []
        for field in dataclasses.fields(value):
            item = getattr(value, field.name)
            if item is not None and _is_optional(hints.get(field.name)):
                item = Some(item)
            fields.append((field.name, item))
        return debug_struct(type(value).__name__, fields, pretty)
    match value:
        case None:
            return "None"
        case bool():
            return display_bool(value)
        case int():
            return str(value)
        case float():
            return debug_f64(value)
        case str():
            return debug_str(value)
        case bytes() | bytearray() | memoryview():
            return debug_list(list(bytes(value)), pretty)
        case list():
            return debug_list(value, pretty)
        case tuple():
            return debug_tuple("", value, pretty)
        case set() | frozenset():
            return debug_set(_sorted(value), pretty)
        case Mapping():
            return debug_map(value.items(), pretty)
        case _:
            raise TypeError(f"no Debug formatting for {type(value).__name__}")
