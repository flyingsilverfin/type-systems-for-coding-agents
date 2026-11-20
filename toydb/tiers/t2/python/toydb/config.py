"""Layered configuration, standing in for the `config` crate as used by the
toyDB server (`src/bin/toydb.rs`): defaults, a YAML config file, and
environment variables with a prefix, deserialized into a dataclass.

Has no Rust module counterpart. Only the YAML subset that toyDB config files
use is supported: a top-level mapping of keys to scalars (plain, single- or
double-quoted), flow mappings and sequences (`{'2': localhost:9702}`, possibly
spanning several lines), and nested block mappings, with `#` comments.
"""

import dataclasses
import os
import re
import typing
from collections.abc import Mapping
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, Self

type ConfigValue = str | int | float | bool | None | dict[str, ConfigValue] | list[ConfigValue]
"""A configuration value, as parsed from a source."""


class ConfigError(ValueError):
    """A configuration error. A `ValueError`, so `Error.from_exception()`
    converts it to `Error.InvalidInput` like the Rust `From<ConfigError>`."""


class Source(typing.Protocol):
    """A configuration source."""

    def origin(self) -> str:
        """Describes the source in error messages, e.g. `in the environment`."""
        ...

    def collect(self) -> dict[str, ConfigValue]:
        """Collects the source's configuration values, keyed by name."""
        ...


class File:
    """A YAML configuration file source."""

    def __init__(self, name: str) -> None:
        self.name = name

    @classmethod
    def with_name(cls, name: str) -> "File":
        """A required configuration file at the given path."""
        return cls(name)

    def origin(self) -> str:
        return f"in {self.name}"

    def collect(self) -> dict[str, ConfigValue]:
        path = Path(self.name)
        if not path.is_file():
            raise ConfigError(f'configuration file "{self.name}" not found')
        try:
            text = path.read_text()
        except OSError as error:
            raise ConfigError(f"{error.strerror} (os error {error.errno})") from error
        try:
            return parse_yaml(text)
        except ConfigError as error:
            raise ConfigError(f"{error} in {path}") from error


class Environment:
    """An environment variable source: variables named `PREFIX_KEY` set the
    (lowercased) key `key`."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    @classmethod
    def with_prefix(cls, prefix: str) -> "Environment":
        return cls(prefix)

    def origin(self) -> str:
        return "in the environment"

    def collect(self) -> dict[str, ConfigValue]:
        prefix = self.prefix.lower() + "_"
        values: dict[str, ConfigValue] = {}
        for name, value in os.environ.items():
            if name.lower().startswith(prefix) and len(name) > len(prefix):
                values[name[len(prefix) :].lower()] = value
        return values


class Config:
    """A built configuration: merged values from all sources."""

    def __init__(self, values: dict[str, ConfigValue], origins: dict[str, str]) -> None:
        self.values = values
        """The configuration values, by key."""
        self.origins = origins
        """The origin of each value from a source (not a default), by key."""

    @staticmethod
    def builder() -> "ConfigBuilder":
        return ConfigBuilder()

    def try_deserialize[T](self, cls: type[T]) -> T:
        """Deserializes the configuration into a dataclass, converting values
        to the field types."""
        hints = typing.get_type_hints(cls)
        kwargs: dict[str, Any] = {}
        for field in dataclasses.fields(cls):  # type: ignore[arg-type]
            if field.name not in self.values:
                raise ConfigError(f'missing configuration field "{field.name}"')
            value, hint = self.values[field.name], hints[field.name]
            try:
                kwargs[field.name] = convert(value, hint, field.name)
            except ConfigError as error:
                if (origin := self.origins.get(field.name)) is not None:
                    raise ConfigError(f"{error} {origin}") from error
                raise
        return cls(**kwargs)


class ConfigBuilder:
    """Builds a configuration from defaults and sources. Later sources
    override earlier ones."""

    def __init__(self) -> None:
        self.defaults: dict[str, ConfigValue] = {}
        self.sources: list[Source] = []

    def set_default(self, key: str, value: ConfigValue) -> Self:
        self.defaults[key.lower()] = value
        return self

    def add_source(self, source: Source) -> Self:
        self.sources.append(source)
        return self

    def build(self) -> Config:
        values = dict(self.defaults)
        origins: dict[str, str] = {}
        for source in self.sources:
            collected = source.collect()
            values.update(collected)
            origins.update(dict.fromkeys(collected, source.origin()))
        return Config(values, origins)


def convert(value: ConfigValue, hint: Any, key: str) -> Any:
    """Converts a configuration value to the given type."""

    def invalid(expected: str) -> ConfigError:
        return ConfigError(f"invalid type: {describe(value)}, expected {expected} for key `{key}`")

    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin in (UnionType, typing.Union) and NoneType in args:
        if value is None:
            return None
        (hint,) = [arg for arg in args if arg is not NoneType]
        return convert(value, hint, key)
    if hint is bool:
        match value:
            case bool():
                return value
            case str() if value.lower() in ("true", "on", "yes", "1"):
                return True
            case str() if value.lower() in ("false", "off", "no", "0"):
                return False
        raise invalid("a boolean")
    if hint is int:
        match value:
            case bool():
                raise invalid("an integer")
            case int():
                return value
            case str() if re.fullmatch(r"[+-]?[0-9]+", value.strip()):
                return int(value)
        raise invalid("an integer")
    if hint is float:
        match value:
            case bool():
                raise invalid("a float")
            case int() | float():
                return float(value)
            case str():
                try:
                    return float(value)
                except ValueError:
                    pass
        raise invalid("a float")
    if hint is str:
        match value:
            case bool():
                return "true" if value else "false"
            case str() | int() | float():
                return str(value)
        raise invalid("a string")
    if origin is dict:
        key_hint, value_hint = args
        if not isinstance(value, Mapping):
            raise invalid("a map")
        return {
            convert(k, key_hint, f"{key}.{k}"): convert(v, value_hint, f"{key}.{k}")
            for k, v in value.items()
        }
    raise TypeError(f"unsupported config type {hint}")


def describe(value: ConfigValue) -> str:
    match value:
        case None:
            return "unit value"
        case bool():
            return f"boolean `{'true' if value else 'false'}`"
        case int():
            return f"integer `{value}`"
        case float():
            return f"floating point `{value}`"
        case str():
            return f'string "{value}"'
        case dict():
            return "map"
        case list():
            return "sequence"


def parse_yaml(text: str) -> dict[str, ConfigValue]:
    """Parses a YAML document whose root is a mapping."""
    parser = _YamlParser(text)
    return parser.document()


class _YamlParser:
    """A parser for the YAML subset used by config files (see module docs)."""

    def __init__(self, text: str) -> None:
        # Lines as (line number, indentation, content without comments).
        self.lines: list[tuple[int, int, str]] = []
        for number, line in enumerate(text.splitlines(), start=1):
            content = _strip_comment(line).rstrip()
            if content.strip() and content.strip() != "---":
                indent = len(content) - len(content.lstrip(" "))
                self.lines.append((number, indent, content.strip()))
        self.pos = 0

    def error(self, message: str) -> ConfigError:
        number = self.lines[min(self.pos, len(self.lines) - 1)][0] if self.lines else 1
        return ConfigError(f"{message} at line {number}")

    def document(self) -> dict[str, ConfigValue]:
        if not self.lines:
            return {}
        value = self.block_mapping(self.lines[0][1])
        if self.pos < len(self.lines):
            raise self.error("unexpected content")
        return value

    def block_mapping(self, indent: int) -> dict[str, ConfigValue]:
        mapping: dict[str, ConfigValue] = {}
        while self.pos < len(self.lines):
            _, line_indent, content = self.lines[self.pos]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise self.error("unexpected indentation")
            key, rest = self.split_key(content)
            self.pos += 1
            if rest:
                mapping[key] = self.inline_value(rest)
            elif self.pos < len(self.lines) and self.lines[self.pos][1] > indent:
                mapping[key] = self.block_mapping(self.lines[self.pos][1])
            else:
                mapping[key] = None
        return mapping

    def split_key(self, content: str) -> tuple[str, str]:
        key, end = _scalar_token(content, 0, ":")
        if end >= len(content) or content[end] != ":":
            raise self.error("expected a key: value pair")
        return str(key), content[end + 1 :].strip()

    def inline_value(self, rest: str) -> ConfigValue:
        if rest[0] not in "{[":
            value, end = _scalar_token(rest, 0, "")
            if rest[end:].strip():
                raise self.error("unexpected characters after value")
            return value
        # Flow collections may span several lines: gather until balanced.
        text = rest
        while _unbalanced(text):
            if self.pos >= len(self.lines):
                raise self.error("unterminated flow collection")
            text += " " + self.lines[self.pos][2]
            self.pos += 1
        value, end = _FlowParser(text, self.error).value(0)
        if text[end:].strip():
            raise self.error("unexpected characters after flow collection")
        return value


class _FlowParser:
    """Parses a YAML flow collection, e.g. `{'2': localhost:9702, }`."""

    def __init__(self, text: str, error: Any) -> None:
        self.text = text
        self.error = error

    def skip(self, pos: int) -> int:
        while pos < len(self.text) and self.text[pos].isspace():
            pos += 1
        return pos

    def value(self, pos: int) -> tuple[ConfigValue, int]:
        pos = self.skip(pos)
        if pos < len(self.text) and self.text[pos] == "{":
            mapping: dict[str, ConfigValue] = {}
            pos = self.skip(pos + 1)
            while pos < len(self.text) and self.text[pos] != "}":
                key, pos = _scalar_token(self.text, pos, ":,}")
                pos = self.skip(pos)
                value: ConfigValue = None
                if pos < len(self.text) and self.text[pos] == ":":
                    value, pos = self.value(pos + 1)
                mapping[str(key)] = value
                pos = self.separator(pos, "}")
            return mapping, pos + 1
        if pos < len(self.text) and self.text[pos] == "[":
            sequence: list[ConfigValue] = []
            pos = self.skip(pos + 1)
            while pos < len(self.text) and self.text[pos] != "]":
                item, pos = self.value(pos)
                sequence.append(item)
                pos = self.separator(pos, "]")
            return sequence, pos + 1
        return _scalar_token(self.text, pos, ",}]")

    def separator(self, pos: int, close: str) -> int:
        pos = self.skip(pos)
        if pos < len(self.text) and self.text[pos] == ",":
            return self.skip(pos + 1)
        if pos < len(self.text) and self.text[pos] == close:
            return pos
        raise self.error(f"expected ',' or '{close}'")


def _scalar_token(text: str, pos: int, stops: str) -> tuple[ConfigValue, int]:
    """Parses a scalar starting at pos, ending before a stop character (a ':'
    stop only counts when followed by whitespace or the end, as in YAML)."""
    while pos < len(text) and text[pos] == " ":
        pos += 1
    if pos < len(text) and text[pos] in "'\"":
        quote = text[pos]
        end = pos + 1
        chars: list[str] = []
        while end < len(text):
            char = text[end]
            if quote == "'" and char == "'":
                if text[end + 1 : end + 2] == "'":
                    chars.append("'")
                    end += 2
                    continue
                return "".join(chars), end + 1
            if quote == '"' and char == "\\" and end + 1 < len(text):
                escaped = text[end + 1]
                chars.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(escaped, escaped))
                end += 2
                continue
            if quote == '"' and char == '"':
                return "".join(chars), end + 1
            chars.append(char)
            end += 1
        raise ConfigError("unterminated quoted string")
    end = pos
    while end < len(text):
        char = text[end]
        if char == ":" and ":" in stops and (end + 1 == len(text) or text[end + 1] in " ,}]"):
            break
        if char != ":" and char in stops:
            break
        end += 1
    return _plain_scalar(text[pos:end].strip()), end


def _plain_scalar(token: str) -> ConfigValue:
    """Resolves a plain scalar's type, per the YAML core schema."""
    if token in ("", "~", "null", "Null", "NULL"):
        return None
    if token in ("true", "True", "TRUE"):
        return True
    if token in ("false", "False", "FALSE"):
        return False
    if re.fullmatch(r"[-+]?[0-9]+", token):
        return int(token)
    if re.fullmatch(r"[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?", token):
        return float(token)
    return token


def _strip_comment(line: str) -> str:
    """Removes a trailing # comment, outside of quotes."""
    quote: str | None = None
    for i, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def _unbalanced(text: str) -> bool:
    """Returns true if a flow collection's brackets aren't closed yet."""
    depth, quote = 0, None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
    return depth > 0
