"""JSON text, as written and read by the SQL extensions (JSON output, and
JSON export and import).

JSON is always written compactly, without whitespace outside strings, with
object members in insertion order. Strings escape only what JSON requires (",
\\ and control characters); everything else, including / and non-ASCII
characters, is written as itself. Floats are written as SQL floats are
displayed in query results, which always gives a valid JSON number for finite
floats, e.g. 1.0, -0.0 or 1e100.

Reading is strict RFC 8259: NaN and Infinity are rejected, as is anything
after the top-level value, and so are strings with an unpaired surrogate
escape (e.g. "\\ud800"), which are not Unicode text. Objects are read with
their members in order (a repeated member keeps its first position and its
last value).

This module writes JSON from native Python values, with floats written by
`toydb.fmt.debug_f64`, and reads JSON with the standard `json` module.
"""

import json as stdjson
import math
from typing import Any

from toydb.fmt import debug_f64

type JSON = None | bool | int | float | str | list["JSON"] | dict[str, "JSON"]
"""A JSON value, as native Python values."""


def encode(value: JSON) -> str:
    """Encodes a value as compact JSON text. Floats must be finite."""
    match value:
        case None:
            return "null"
        case bool():
            return "true" if value else "false"
        case int():
            return str(value)
        case float():
            if not math.isfinite(value):
                raise ValueError(f"can't encode {value} as JSON")
            return debug_f64(value)
        case str():
            return stdjson.dumps(value, ensure_ascii=False)
        case list():
            return "[" + ",".join(encode(item) for item in value) + "]"
        case dict():
            members = (f"{encode(key)}:{encode(item)}" for key, item in value.items())
            return "{" + ",".join(members) + "}"
        case _:
            raise TypeError(f"can't encode {type(value).__name__} as JSON")


def decode(text: str) -> Any:
    """Decodes JSON text into native Python values. Raises ValueError if the
    text is not valid JSON."""

    def reject(constant: str) -> None:
        raise ValueError(f"invalid JSON constant {constant}")

    value = stdjson.loads(text, parse_constant=reject)
    _check_strings(value)
    return value


def _check_strings(value: Any) -> None:
    """Raises ValueError (UnicodeEncodeError) if a string in the decoded value
    contains an unpaired surrogate, which the json module accepts from an
    escape like \\ud800."""
    match value:
        case str():
            value.encode("utf-8")
        case list():
            for item in value:
                _check_strings(item)
        case dict():
            for key, item in value.items():
                _check_strings(key)
                _check_strings(item)
        case _:
            pass
