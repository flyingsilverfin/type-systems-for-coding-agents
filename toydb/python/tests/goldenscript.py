"""The Goldenscript testing framework, a port of the Rust `goldenscript` crate
(version 0.8) that the toyDB tests are written against.

A goldenscript is a plain text file that contains a set of arbitrary commands
and their expected output, separated by `---`:

    command argument key=value
    ---
    output

    # Example testing a key/value store:
    get foo
    set foo=bar
    get foo
    ---
    get foo → None
    set foo=bar → new
    get foo → 1

Commands are executed by a provided `Runner`, and the generated output is
compared with the script itself. See `GOLDENSCRIPT_README.md` for the syntax.

The port mirrors the crate's modules as sections of this file: `command.rs`
(`Block`, `Command`, `Context`, `Argument`, `ArgumentConsumer`), `parser.rs`
(a hand-written equivalent of the `nom` parser, including its error
positions), `runner.rs` (`Runner`, `run`, `generate`), and the
`goldenscript-macros` crate (`#[derive(Command)]`, as `CommandParser`).

Differences from the Rust crate:

* A command fails by raising an exception. With a `!` prefix, the failure is
  expected and output as `Error: <message>`. Rust panics correspond to
  `AssertionError` (including `toydb.error.Panic`) and `NotImplementedError`,
  and are output as `Panic: <message>`.
* `run` never rewrites the script (there is no `UPDATE_GOLDENFILES`), since the
  scripts are the specification for the port. It raises `Mismatch` instead.
* `Runner::Command` and its `TryFrom<&Command>` conversion are the
  `Runner.parse_command` method, which can be implemented with `CommandParser`.
"""

import dataclasses
import difflib
import re
import typing
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import NoneType, UnionType
from typing import Any, Generic, NoReturn, TypeVar

from toydb.fmt import debug_str, debug_tuple

C = TypeVar("C")
T = TypeVar("T")


class Error(Exception):
    """A goldenscript failure: a parse error, an unexpected command failure
    (or success), or a failed hook. Corresponds to the `std::io::Error`
    returned by the Rust crate."""


class Mismatch(Error):
    """The generated output differs from the script."""

    def __init__(self, path: Path, expected: str, actual: str) -> None:
        super().__init__(f"goldenscript {path} differs from the generated output")
        self.path = path
        self.expected = expected
        self.actual = actual

    def diff(self) -> str:
        """Returns a unified diff from the expected to the actual output."""
        return "".join(
            line if line.endswith("\n") else line + "\n"
            for line in difflib.unified_diff(
                self.expected.splitlines(keepends=True),
                self.actual.splitlines(keepends=True),
                fromfile=f"{self.path} (expected)",
                tofile=f"{self.path} (actual)",
            )
        )


# ---------------------------------------------------------------------------
# command.rs
# ---------------------------------------------------------------------------


class Argument(ABC):
    """A command argument."""

    Positional: typing.ClassVar[type["Positional"]]
    KeyValue: typing.ClassVar[type["KeyValue"]]


@dataclass(frozen=True)
class Positional(Argument):
    """A positional argument value. Can be empty."""

    value: str

    def __str__(self) -> str:
        return maybe_quote(self.value)

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Positional", [self.value], pretty)


@dataclass(frozen=True)
class KeyValue(Argument):
    """A `key=value` style argument, with the key followed by the value. Keys
    are not guaranteed to be unique; the `Runner` can handle this as desired.
    Both the key and value can be empty."""

    key: str
    value: str

    def __str__(self) -> str:
        return f"{maybe_quote(self.key)}={maybe_quote(self.value)}"

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("KeyValue", [self.key, self.value], pretty)


Argument.Positional = Positional
Argument.KeyValue = KeyValue


@dataclass(frozen=True)
class Context:
    """A command context."""

    prefix: str | None
    """The command prefix, if given."""
    tags: frozenset[str]
    """Any command tags, if given."""
    silent: bool
    """Silences the output of this command. This is handled automatically, the
    `Runner` does not have to take this into account."""
    fail: bool
    """If true, the command is expected to fail with an error or panic. If the
    command succeeds, the test fails."""
    line_number: int
    """The command's line number position in the script."""


@dataclass(frozen=True)
class Command:
    """A command."""

    name: str
    """The name of the command. Can be empty."""
    args: list[Argument]
    """The command's arguments, in the given order."""
    context: Context = field(repr=False)
    """The command's context. This is passed to the runner as a separate
    parameter."""

    def consume_args(self) -> "ArgumentConsumer":
        """Returns an argument consumer, for more convenient argument
        processing. Does not affect `Command.args`."""
        return ArgumentConsumer(self.args)

    def __str__(self) -> str:
        return " ".join([maybe_quote(self.name)] + [str(arg) for arg in self.args])


@dataclass(frozen=True)
class Block:
    """A block, consisting of multiple commands."""

    commands: list[Command]
    """The commands in the block, and their context."""
    literal: str
    """The literal string of the input commands. Used to generate the output."""
    line_number: int
    """The block's line number position in the script."""


class ArgumentConsumer(Iterator[Argument]):
    """Helper for argument processing. Returns and removes arguments as
    requested.

    Created by `Command.consume_args()`. Implements `Iterator`, but is also
    intended for out-of-order processing, unlike most iterators.
    """

    def __init__(self, args: Iterable[Argument]) -> None:
        self.args: deque[Argument] = deque(args)

    def __next__(self) -> Argument:
        if not self.args:
            raise StopIteration
        return self.args.popleft()

    def next(self) -> Argument | None:
        """Returns and removes the next argument, if any."""
        return self.args.popleft() if self.args else None

    def next_key(self) -> tuple[str, str] | None:
        """Returns and removes the next `KeyValue` argument, if any."""
        for arg in self.args:
            if isinstance(arg, KeyValue):
                self.args.remove(arg)
                return (arg.key, arg.value)
        return None

    def next_pos(self) -> str | None:
        """Returns and removes the next `Positional` argument, if any."""
        for arg in self.args:
            if isinstance(arg, Positional):
                self.args.remove(arg)
                return arg.value
        return None

    def take_key(self, key: str) -> str | None:
        """Looks up a `KeyValue` argument by key, removing it and returning its
        value. If multiple arguments have the same key, they are all removed
        and the last value is returned."""
        value = None
        for arg in self.args:
            if isinstance(arg, KeyValue) and arg.key == key:
                value = arg.value
        if value is None:
            return None
        self.args = deque(
            arg for arg in self.args if not (isinstance(arg, KeyValue) and arg.key == key)
        )
        return value

    def reject_next(self) -> None:
        """Rejects any remaining arguments with an error."""
        if self.args:
            raise ValueError(f"unexpected argument '{self.args[0]}'")


# ---------------------------------------------------------------------------
# parser.rs
# ---------------------------------------------------------------------------


class ParseError(Exception):
    """A parse error at an input position, with the kind of the `nom` parser
    that failed there (e.g. `Tag` or `CrLf`). Mirrors `nom::error::Error`."""

    def __init__(self, input: str, position: int, code: str) -> None:
        super().__init__(input, position, code)
        self.input = input
        self.position = position
        self.code = code

    def location_line(self) -> int:
        """The 1-based line number of the error position."""
        return location_line(self.input, self.position)

    def _line_start(self) -> int:
        return self.input.rfind("\n", 0, self.position) + 1

    def get_column(self) -> int:
        """The 1-based column of the error position, in UTF-8 bytes."""
        return len(self.input[self._line_start() : self.position].encode()) + 1

    def get_utf8_column(self) -> int:
        """The 1-based column of the error position, in characters."""
        return self.position - self._line_start() + 1

    def get_line_beginning(self) -> str:
        """The line containing the error position, without line ending."""
        end = self.input.find("\n", self.position)
        return self.input[self._line_start() : end if end >= 0 else len(self.input)]

    def __str__(self) -> str:
        return (
            f"parse error at line {self.location_line()} column {self.get_column()} "
            f"for {self.code}:\n{self.get_line_beginning()}\n{' ' * (self.get_utf8_column() - 1)}^"
        )


def location_line(input: str, position: int) -> int:
    """Returns the 1-based line number of a position in the input."""
    return input.count("\n", 0, position) + 1


def parse(input: str) -> list[Block]:
    """Parses the given goldenscript string into a list of command blocks."""
    return _Parser(input).blocks(0)[1]


def parse_command(input: str) -> Command:
    """Parses a command, for use in tests."""
    return _Parser(input).command(0)[1]


def _is_unquoted_string_start(c: str) -> bool:
    return (c.isascii() and c.isalnum()) or c == "_"


def _is_unquoted_string_char(c: str) -> bool:
    return _is_unquoted_string_start(c) or c in "-./@"


def maybe_quote(value: str) -> str:
    """Adds quotes and escapes to a string when required by Goldenscript syntax."""
    if (
        not value
        or not _is_unquoted_string_start(value[0])
        or not all(_is_unquoted_string_char(c) for c in value[1:])
    ):
        return debug_str(value)
    return value


class _Parser:
    """A recursive descent parser for goldenscripts.

    Each method parses a construct at the given position and returns the next
    position along with the parsed value, or raises a `ParseError`. The methods
    mirror the `nom` combinator functions in the Rust crate one-to-one, since
    the positions and kinds of parse errors are part of the crate's tested
    behavior: an alternation reports the error of the last alternative, and
    optional parsers swallow errors.
    """

    def __init__(self, input: str) -> None:
        self.input = input

    def error(self, pos: int, code: str) -> NoReturn:
        raise ParseError(self.input, pos, code)

    # Primitive parsers, corresponding to nom's.

    def tag(self, pos: int, tag: str) -> int:
        if not self.input.startswith(tag, pos):
            self.error(pos, "Tag")
        return pos + len(tag)

    def char(self, pos: int, char: str) -> int:
        if not self.input.startswith(char, pos):
            self.error(pos, "Char")
        return pos + 1

    def space0(self, pos: int) -> int:
        while pos < len(self.input) and self.input[pos] in " \t":
            pos += 1
        return pos

    def space1(self, pos: int) -> int:
        end = self.space0(pos)
        if end == pos:
            self.error(pos, "Space")
        return end

    def line_ending(self, pos: int) -> int:
        if self.input.startswith("\n", pos):
            return pos + 1
        if self.input.startswith("\r\n", pos):
            return pos + 2
        self.error(pos, "CrLf")

    def eof(self, pos: int) -> int:
        if pos < len(self.input):
            self.error(pos, "Eof")
        return pos

    def line_ending_or_eof(self, pos: int) -> int:
        try:
            return self.line_ending(pos)
        except ParseError:
            return self.eof(pos)

    def not_line_ending(self, pos: int) -> tuple[int, str]:
        end = pos
        while end < len(self.input) and self.input[end] not in "\r\n":
            end += 1
        if self.input.startswith("\r", end) and not self.input.startswith("\r\n", end):
            self.error(pos, "Tag")
        return end, self.input[pos:end]

    def opt(self, pos: int, parser: Callable[[int], T]) -> T | None:
        try:
            return parser(pos)
        except ParseError:
            return None

    # Goldenscript parsers.

    def blocks(self, pos: int) -> tuple[int, list[Block]]:
        """Parses a list of blocks until EOF."""
        blocks: list[Block] = []
        while pos < len(self.input):
            next, block = self.block(pos)
            if next == pos:
                self.error(pos, "Many0")
            blocks.append(block)
            pos = next
        return pos, blocks

    def block(self, pos: int) -> tuple[int, Block]:
        """Parses a single block, consisting of a set of commands, a ---
        separator, and the command output."""
        # Parse the command section, preserving the literal for output.
        line_number = location_line(self.input, pos)
        start = pos
        pos, commands = self.commands(pos)
        block = Block(commands=commands, literal=self.input[start:pos], line_number=line_number)

        # If there were no commands, and we're at the end of the input, preserve
        # the literal as an empty block for output.
        if pos == len(self.input) and not block.commands:
            return pos, block

        # Parse the separator. There must be one.
        pos = self.separator(pos)

        # Parse and skip the output section.
        pos = self.output(pos)

        return pos, block

    def commands(self, pos: int) -> tuple[int, list[Command]]:
        """Parses the command section of a block. This consists of lines that
        are either empty/blank, commands, or comments, up to the separator or
        EOF."""
        commands: list[Command] = []
        while True:
            # Skip empty/comment lines.
            if (next := self.opt(pos, self.empty_or_comment_line)) is not None:
                pos = next
                continue

            # Detect premature EOF. This case must be handled by the caller.
            if pos == len(self.input):
                return pos, commands

            # If we hit a separator and we've seen at least 1 command, we're done.
            # Otherwise, we want to error while attempting to parse the command.
            if self.opt(pos, self.separator) is not None and commands:
                return pos, commands

            # Parse a command.
            pos, command = self.command(pos)
            commands.append(command)

    def command(self, pos: int) -> tuple[int, Command]:
        """Parses a single command, consisting of a command name and optionally
        a set of arguments (with or without values), prefix, and silencing
        parentheses. Consumes the entire line, including any whitespace and
        comments at the end."""
        # Look for a silencing (.
        silent = False
        if (next := self.opt(pos, lambda p: self.space0(self.char(p, "(")))) is not None:
            pos, silent = next, True

        # The prefix, tags, and fail marker.
        tags: set[str] = set()
        prefix = None

        def parse_prefix(p: int) -> tuple[int, str]:
            p, string = self.string(p)
            return self.space0(self.tag(p, ":")), string

        if (result := self.opt(pos, parse_prefix)) is not None:
            pos, prefix = result

        def parse_tags(p: int) -> tuple[int, set[str]]:
            p, taglist = self.taglist(self.space0(p))
            return self.space0(p), taglist

        if (result := self.opt(pos, parse_tags)) is not None:
            pos, maybe_tags = result
            tags |= maybe_tags

        fail = False
        if (next := self.opt(pos, lambda p: self.space0(self.char(p, "!")))) is not None:
            pos, fail = next, True

        # A > takes the rest of the line as the literal command name. It allows
        # line continuation with \ to escape the newline.
        if (next := self.opt(pos, lambda p: self.space0(self.tag(p, ">")))) is not None:
            pos = next
            line_number = location_line(self.input, pos)
            pos, name = self.line_continuation(pos)
            context = Context(prefix, frozenset(tags), silent, fail, line_number)
            return pos, Command(name, [], context)

        # The command itself, and any trailing tags.
        line_number = location_line(self.input, pos)
        pos, name = self.string(pos)
        args: list[Argument] = []
        while (result := self.opt(pos, lambda p: self.argument(self.space1(p)))) is not None:
            pos, arg = result
            args.append(arg)
        if (result := self.opt(pos, lambda p: self.taglist(self.space1(p)))) is not None:
            pos, maybe_tags = result
            tags |= maybe_tags

        # If silenced, look for the closing brace.
        if silent:
            pos = self.char(self.space0(pos), ")")

        # Ignore trailing whitespace and comments on this line.
        pos = self.space0(pos)
        if (next := self.opt(pos, self.comment)) is not None:
            pos = next
        pos = self.line_ending(pos)

        context = Context(prefix, frozenset(tags), silent, fail, line_number)
        return pos, Command(name, args, context)

    def argument(self, pos: int) -> tuple[int, Argument]:
        """Parses a single command argument, consisting of an argument value
        and optionally a key separated by =."""

        def key_value(p: int) -> tuple[int, Argument]:
            p, key = self.string(p)
            p = self.tag(p, "=")
            if (result := self.opt(p, self.string)) is not None:
                p, value = result
                return p, KeyValue(key, value)
            return p, KeyValue(key, "")

        if (result := self.opt(pos, key_value)) is not None:
            return result
        pos, value = self.string(pos)
        return pos, Positional(value)

    def taglist(self, pos: int) -> tuple[int, set[str]]:
        """Parses a list of []-delimited command tags separated by comma or
        whitespace."""
        pos = self.tag(pos, "[")
        pos, tag = self.string(pos)
        tags = {tag}
        while pos < len(self.input) and self.input[pos] in ", ":
            if (result := self.opt(pos + 1, self.string)) is None:
                break
            pos, tag = result
            tags.add(tag)
        return self.tag(pos, "]"), tags

    def separator(self, pos: int) -> int:
        """Parses a command/output separator: --- followed by a line ending."""
        return self.line_ending_or_eof(self.tag(pos, "---"))

    def output(self, pos: int) -> int:
        """Parses the command output following a --- separator, up to the first
        blank line or EOF. This is typically two consecutive line endings,
        except the special case where there is no output, i.e. the first
        character is a line ending or EOF."""
        if (next := self.opt(pos, self.line_ending_or_eof)) is not None:
            return next
        while True:
            end = self.opt(pos, lambda p: self.line_ending_or_eof(self.line_ending_or_eof(p)))
            if end is not None:
                return end
            pos += 1

    def string(self, pos: int) -> tuple[int, str]:
        """Parses a string, both quoted (' or ") and unquoted."""
        for parser in (self.unquoted_string, lambda p: self.quoted_string(p, "'")):
            if (result := self.opt(pos, parser)) is not None:
                return result
        return self.quoted_string(pos, '"')

    def unquoted_string(self, pos: int) -> tuple[int, str]:
        """An unquoted string can't contain whitespace, and can only contain
        alphanumeric characters and some punctuation."""
        if pos >= len(self.input) or not _is_unquoted_string_start(self.input[pos]):
            self.error(pos, "Satisfy")
        end = pos + 1
        while end < len(self.input) and _is_unquoted_string_char(self.input[end]):
            end += 1
        return end, self.input[pos:end]

    def quoted_string(self, pos: int, quote: str) -> tuple[int, str]:
        """A quoted string can contain anything, and respects common escape
        sequences. It can be quoted using ' or "."""
        assert quote in ("'", '"'), f"invalid quote character {quote}"

        # Special-case the empty quoted string.
        if (next := self.opt(pos, lambda p: self.tag(p, quote + quote))) is not None:
            return next, ""

        pos = self.tag(pos, quote)
        pos, string = self.escaped_transform(pos, quote)
        return self.tag(pos, quote), string

    def escaped_transform(self, start: int, quote: str) -> tuple[int, str]:
        """Parses the contents of a quoted string, transforming escape
        sequences. Mirrors nom's escaped_transform() including its quirks."""
        end = len(self.input)
        index = start
        result: list[str] = []
        while index < end:
            # Consume normal characters.
            normal = index
            while normal < end and self.input[normal] not in ("\\", quote):
                normal += 1
            if normal > index:
                result.append(self.input[index:normal])
                if normal == end:
                    return end, "".join(result)
                index = normal
            elif self.input[index] == "\\":
                if index + 1 >= end:
                    self.error(index, "EscapedTransform")
                index, char = self.escape_sequence(index + 1)
                result.append(char)
                if index == end:
                    return end, "".join(result)
            else:
                if index == start:
                    self.error(index, "EscapedTransform")
                return index, "".join(result)
        return index, "".join(result)

    def escape_sequence(self, pos: int) -> tuple[int, str]:
        """Parses an escape sequence following a backslash."""
        simple = {"'": "'", '"': '"', "\\": "\\", "0": "\0", "n": "\n", "r": "\r", "t": "\t"}
        if pos < len(self.input) and self.input[pos] in simple:
            return pos + 1, simple[self.input[pos]]
        if (result := self.opt(pos, self.escape_hex)) is not None:
            return result
        return self.escape_unicode(pos)

    def escape_hex(self, pos: int) -> tuple[int, str]:
        """Parses a \\xNN escape sequence."""
        start = self.tag(pos, "x")
        if start + 2 > len(self.input):
            self.error(start, "Eof")
        # Rust's u8::from_str_radix() also accepts a leading +.
        if not re.fullmatch(r"\+?[0-9a-fA-F]+", self.input[start : start + 2]):
            self.error(pos, "MapRes")
        return start + 2, chr(int(self.input[start : start + 2], 16))

    def escape_unicode(self, pos: int) -> tuple[int, str]:
        """Parses a \\u{NNNNNN} escape sequence."""
        start = self.tag(pos, "u{")
        end = start
        while end < len(self.input) and end - start < 6 and self.input[end] in "0123456789abcdefABCDEF":
            end += 1
        if end == start:
            self.error(start, "TakeWhileMN")
        next = self.tag(end, "}")
        codepoint = int(self.input[start:end], 16)
        if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            self.error(pos, "MapRes")
        return next, chr(codepoint)

    def empty_or_comment_line(self, pos: int) -> int:
        """Parses a line that only contains whitespace and/or a comment."""
        end = self.space0(pos)
        if (next := self.opt(end, self.comment)) is not None:
            end = next
        end = self.line_ending_or_eof(end)
        if end == pos:
            self.error(pos, "Verify")
        return end

    def comment(self, pos: int) -> int:
        """Parses a # or // comment until the end of the line/file (not
        inclusive)."""
        if (next := self.opt(pos, lambda p: self.tag(p, "//"))) is None:
            next = self.tag(pos, "#")
        return self.not_line_ending(next)[0]

    def line_continuation(self, pos: int) -> tuple[int, str]:
        """Parses a raw line with optional \\ line continuation escapes. Naïve
        but sufficient implementation that e.g. doesn't support \\\\ escapes."""
        result = ""
        while True:
            pos, line = self.not_line_ending(pos)
            pos = self.line_ending(pos)
            result += line
            if line.endswith("\\"):
                # Remove \ and continue.
                result = result[:-1]
                continue
            return pos, result


# ---------------------------------------------------------------------------
# runner.rs
# ---------------------------------------------------------------------------


class Runner(ABC, Generic[C]):
    """Runs goldenscript commands, returning their output.

    The type parameter is the command type accepted by this runner. Parsed
    Goldenscript `Command`s are converted to this type by `parse_command`
    before being passed to the command hooks and `run`. To process arguments
    manually, use `Command` itself (the default).
    """

    def parse_command(self, command: Command) -> C:
        """Converts a parsed command to the runner's command type, or raises
        an error if it is invalid. Custom command types can usually implement
        this with a `CommandParser`. By default, the parsed command is
        returned as is."""
        return typing.cast(C, command)

    @abstractmethod
    def run(self, command: C, context: Context) -> str:
        """Runs a goldenscript command, returning its output, or raising an
        error if the command fails.

        Prefixes, tags, silencing, and expected failures are provided separately
        through `Context`.

        Error cases are typically tested by running the command with a `!`
        prefix (expecting a failure), but the runner can also handle these
        itself and return appropriate output.
        """

    def start_script(self) -> None:
        """Called at the start of a goldenscript. Used e.g. for initial setup.
        Can't return output, since it's not called in the context of a block."""

    def end_script(self) -> None:
        """Called at the end of a goldenscript. Used e.g. for state assertions.
        Can't return output, since it's not called in the context of a block."""

    def start_block(self, block: Block) -> str:
        """Called at the start of a block. Used e.g. to output initial state.
        Any output is prepended to the block's output."""
        return ""

    def end_block(self, block: Block) -> str:
        """Called at the end of a block. Used e.g. to output final state.
        Any output is appended to the block's output."""
        return ""

    def start_command(self, command: C, context: Context) -> str:
        """Called at the start of a command. Used e.g. for setup. Any output is
        prepended to the command's output, and is affected e.g. by the prefix
        and silencing of the command."""
        return ""

    def end_command(self, command: C, context: Context) -> str:
        """Called at the end of a command. Used e.g. for cleanup. Any output is
        appended to the command's output, and is affected e.g. by the prefix and
        silencing of the command."""
        return ""


# Exceptions corresponding to Rust panics. Other exceptions are errors.
PANICS = (AssertionError, NotImplementedError)


def panic_message(panic: BaseException) -> str:
    """Returns the message of an exception corresponding to a Rust panic."""
    message = str(panic)
    if isinstance(panic, NotImplementedError):
        return f"not implemented: {message}" if message else "not implemented"
    return message


def run(runner: Runner[Any], path: str | Path) -> None:
    """Runs a goldenscript at the given path.

    Raises `Mismatch` if the script output differs from the current input
    file. Errors on IO, parser, or runner failure.
    """
    path = Path(path)
    with open(path, encoding="utf-8", newline="") as file:
        input = file.read()
    output = generate(runner, input)
    if output != input:
        raise Mismatch(path, input, output)


def generate(runner: Runner[C], input: str) -> str:
    """Generates output for a goldenscript input, without comparing it."""
    output: list[str] = []

    # Detect end-of-line format.
    eol = "\r\n" if "\r\n" in input else "\n"

    # Parse the script.
    try:
        blocks = parse(input)
    except ParseError as error:
        raise Error(str(error)) from error

    # Call the start_script() hook.
    try:
        runner.start_script()
    except PANICS:
        raise
    except Exception as error:
        raise Error(f"start_script failed: {error}") from error

    for i, block in enumerate(blocks):
        # There may be a trailing block with no commands if the script has bare
        # comments at the end. If so, just retain its literal contents.
        if not block.commands:
            output.append(block.literal)
            continue

        # Process each block of commands and accumulate their output.
        block_output = ""

        # Call the start_block() hook.
        try:
            block_output += _ensure_eol(runner.start_block(block), eol)
        except PANICS:
            raise
        except Exception as error:
            raise Error(f"start_block failed at line {block.line_number}: {error}") from error

        for raw_command in block.commands:
            command_output = ""
            context = raw_command.context

            # Convert the command to the runner's command type.
            try:
                command = runner.parse_command(raw_command)
            except PANICS:
                raise
            except Exception as error:
                raise Error(
                    f"command '{raw_command.name}' failed at line {context.line_number}: {error}"
                ) from error

            # Call the start_command() hook.
            try:
                command_output += _ensure_eol(runner.start_command(command, context), eol)
            except PANICS:
                raise
            except Exception as error:
                raise Error(
                    f"start_command failed at line {context.line_number}: {error}"
                ) from error

            # Execute the command. Handle panics and errors if requested.
            try:
                result = runner.run(command, context)
            except PANICS as panic:
                # Unexpected panic, throw it.
                if not context.fail:
                    raise
                # Expected panic, output it.
                command_output += f"Panic: {panic_message(panic)}"
            except Exception as error:
                # Unexpected error, return it.
                if not context.fail:
                    raise Error(
                        f"command '{raw_command.name}' failed at line {context.line_number}: "
                        f"{error}"
                    ) from error
                # Expected error, output it.
                command_output += f"Error: {error}"
            else:
                # Unexpected success, error out.
                if context.fail:
                    raise Error(
                        f"expected command '{raw_command.name}' to fail at line "
                        f"{context.line_number}, succeeded with: {result}"
                    )
                # Expected success, output the result.
                command_output += result

            # Make sure the command output has a trailing newline, unless empty.
            command_output = _ensure_eol(command_output, eol)

            # Call the end_command() hook.
            try:
                command_output += _ensure_eol(runner.end_command(command, context), eol)
            except PANICS:
                raise
            except Exception as error:
                raise Error(f"end_command failed at line {context.line_number}: {error}") from error

            # Silence the output if requested.
            if context.silent:
                command_output = ""

            # Prefix output lines if requested.
            if context.prefix is not None and command_output:
                prefix = context.prefix
                command_output = "{}: {}{}".format(
                    prefix,
                    command_output.removesuffix(eol).replace("\n", f"\n{prefix}: "),
                    eol,
                )

            block_output += command_output

        # Call the end_block() hook.
        try:
            block_output += _ensure_eol(runner.end_block(block), eol)
        except PANICS:
            raise
        except Exception as error:
            raise Error(f"end_block failed at line {block.line_number}: {error}") from error

        # If the block doesn't have any output, default to "ok".
        if not block_output:
            block_output += "ok\n"

        # If the block output contains blank lines, use a > prefix for it.
        if (
            block_output.startswith("\n")
            or block_output.startswith("\r\n")
            or "\n\n" in block_output
            or "\n\r\n" in block_output
        ):
            block_output = "> " + block_output.replace("\n", "\n> ")
            # We guarantee above that block output ends with a newline, so we
            # remove the "> " at the end of the output.
            block_output = block_output[:-2]

        # Add the resulting block to the output. If this is not the last block,
        # also add a newline separator.
        output.append(f"{block.literal}---{eol}{block_output}")
        if i < len(blocks) - 1:
            output.append(eol)

    # Call the end_script() hook.
    try:
        runner.end_script()
    except PANICS:
        raise
    except Exception as error:
        raise Error(f"end_script failed: {error}") from error

    return "".join(output)


def _ensure_eol(s: str, eol: str) -> str:
    """Appends a newline if the string is not empty and doesn't already have one."""
    if s and not s.endswith("\n"):
        s += eol
    return s


# ---------------------------------------------------------------------------
# goldenscript-macros: #[derive(Command)]
# ---------------------------------------------------------------------------

_COMMAND_ATTR = "__goldenscript_command__"
_ARG_METADATA = "goldenscript"


@dataclass(frozen=True)
class _VariantConfig:
    """Parsed config for an enum variant (i.e. command)."""

    name: str | None = None
    other: bool = False


@dataclass(frozen=True)
class _ArgConfig:
    """Explicit config for a variant field (i.e. argument), from `arg()`."""

    pos: bool = False
    key: bool | str = False
    optional: bool = False
    many: bool = False
    parse: Callable[[str], Any] | None = None


def command(*, name: str | None = None, other: bool = False) -> Callable[[type[T]], type[T]]:
    """Class decorator configuring a command variant, like `#[command(...)]`.

    * `name`: the command name, instead of the snake_case class name.
    * `other`: a catch-all variant for unknown commands. It must be the last
      variant, and have a single field which receives the parsed `Command`.
    """
    if name is not None and other:
        raise TypeError("name and other are exclusive")

    def decorator(cls: type[T]) -> type[T]:
        setattr(cls, _COMMAND_ATTR, _VariantConfig(name, other))
        return cls

    return decorator


def arg(
    *,
    pos: bool = False,
    key: bool | str = False,
    optional: bool = False,
    many: bool = False,
    parse: Callable[[str], Any] | None = None,
) -> Any:
    """Configures a command variant field, like `#[arg(...)]`. Returns a
    dataclass field.

    * `pos`: a positional argument (the default for scalar fields).
    * `key`: a `key=value` argument, keyed by the field name or the given key.
    * `optional`: the argument may be omitted, yielding the type's default.
    * `many`: a variadic argument, collecting all remaining arguments.
    * `parse`: parses the argument string, instead of inferring a parser from
      the field's type hint (the equivalent of `FromStr`).
    """
    if pos and key is not False:
        raise TypeError("pos and key are exclusive")
    return field(metadata={_ARG_METADATA: _ArgConfig(pos, key, optional, many, parse)})


def _to_snake_case(name: str) -> str:
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return name.lower()


def _parse_bool(value: str) -> bool:
    match value:
        case "true":
            return True
        case "false":
            return False
        case _:
            raise ValueError("provided string was not `true` or `false`")


def _parse_int(value: str) -> int:
    if not value:
        raise ValueError("cannot parse integer from empty string")
    if not re.fullmatch(r"[+-]?[0-9]+", value, re.ASCII):
        raise ValueError("invalid digit found in string")
    return int(value)


def _parse_float(value: str) -> float:
    if not re.fullmatch(r"[+-]?(inf|infinity|nan|[0-9.]+([eE][+-]?[0-9]+)?)", value, re.I | re.A):
        raise ValueError("invalid float literal")
    try:
        return float(value)
    except ValueError:
        raise ValueError("invalid float literal") from None


def _value_parser(hint: Any) -> Callable[[str], Any]:
    """Returns a function that parses a string as the given type, like
    `FromStr`."""
    if hint is str or hint is Any:
        return str
    if hint is bool:
        return _parse_bool
    if hint is int:
        return _parse_int
    if hint is float:
        return _parse_float
    if hasattr(hint, "from_str"):
        return hint.from_str
    if callable(hint):
        return hint
    raise TypeError(f"can't parse arguments of type {hint}")


@dataclass
class _Field:
    """A command variant field, and how to parse it from arguments."""

    name: str
    label: str
    keyed: bool
    key: str | None
    optional: bool
    many: bool
    default: Callable[[], Any] | None
    collect: Callable[[list[Any]], Any]
    parsers: tuple[Callable[[str], Any], ...]

    def parse_value(self, value: str, parser: Callable[[str], Any]) -> Any:
        try:
            return parser(value)
        except PANICS:
            raise
        except Exception as error:
            raise ValueError(
                f"invalid value '{value}' for argument '{self.label}': {error}"
            ) from error

    def parse(self, args: ArgumentConsumer) -> Any:
        """Consumes and parses the field's argument(s)."""
        if self.many and self.keyed:
            pairs: list[tuple[str, str]] = []
            while (pair := args.next_key()) is not None:
                pairs.append(pair)
            if not pairs and not self.optional:
                raise ValueError(f"argument '{self.label}' not given")
            key_parser, value_parser = self.parsers
            return self.collect(
                [
                    (self.parse_value(k, key_parser), self.parse_value(v, value_parser))
                    for k, v in pairs
                ]
            )
        if self.many:
            values: list[str] = []
            while (value := args.next_pos()) is not None:
                values.append(value)
            if not values and not self.optional:
                raise ValueError(f"argument '{self.label}' not given")
            return self.collect([self.parse_value(v, self.parsers[0]) for v in values])
        if self.keyed:
            assert self.key is not None
            value = args.take_key(self.key)
        else:
            value = args.next_pos()
        if value is None:
            if not self.optional:
                raise ValueError(f"argument '{self.label}' not given")
            return self.default() if self.default is not None else None
        return self.parse_value(value, self.parsers[0])


def _derive_field(dcfield: dataclasses.Field[Any], hint: Any, pos_count: int) -> _Field:
    """Derives a field parser from a dataclass field and its type hint."""
    config: _ArgConfig = dcfield.metadata.get(_ARG_METADATA, _ArgConfig())

    # Infer the base config from the field type.
    keyed, optional, many, is_option = False, False, False, False
    collect: Callable[[list[Any]], Any] = list
    if typing.get_origin(hint) in (UnionType, typing.Union):
        inner = [a for a in typing.get_args(hint) if a is not NoneType]
        if len(inner) == 1 and len(typing.get_args(hint)) == 2:
            hint, optional, is_option = inner[0], True, True
    origin, type_args = typing.get_origin(hint) or hint, typing.get_args(hint)
    types: tuple[Any, ...] = (hint,)
    if origin is dict:
        keyed, many, collect = True, True, dict
        types = type_args or (str, str)
    elif origin in (list, deque, set, frozenset, tuple):
        many, collect = True, origin
        element = type_args[0] if type_args else str
        if typing.get_origin(element) is tuple and len(typing.get_args(element)) == 2:
            keyed, types = True, typing.get_args(element)
        else:
            types = (element,)
    elif config.many:
        collect = origin  # a custom collection constructed from a list
        types = (type_args[0] if type_args else str,)

    # Apply the explicit config.
    if config.pos:
        keyed = False
    if config.key is not False:
        keyed = True
    optional = optional or config.optional
    many = many or config.many
    key = None
    if keyed and not many:
        key = config.key if isinstance(config.key, str) else dcfield.name
    elif isinstance(config.key, str):
        raise TypeError("key fields with many cannot give key name")
    if config.parse is not None:
        parsers: tuple[Callable[[str], Any], ...] = (config.parse,) * len(types)
    else:
        parsers = tuple(_value_parser(t) for t in types)

    # Unnamed (tuple variant) fields are labeled by position or type name.
    label = key if key is not None else dcfield.name
    if key is None and re.fullmatch(r"_?[0-9]+", dcfield.name):
        label = getattr(origin, "__name__", str(origin)) if keyed else str(pos_count)

    default = None if is_option or many else origin
    return _Field(dcfield.name, label, keyed, key, optional, many, default, collect, parsers)


class CommandParser(Generic[C]):
    """Parses goldenscript commands into a command enum, like the Rust
    `#[derive(goldenscript::Command)]`.

    A command enum is a base class with a dataclass subclass per variant. Each
    variant is a command, named by the snake_case class name (or `@command(name
    = ...)`), and each field is an argument, parsed according to its type hint:

    * Scalars (`str`, `int`, `float`, `bool`, or any type with a `from_str`
      classmethod or a constructor taking a string) are required positional
      arguments, or `key=value` arguments with `arg(key=True)`.
    * `T | None` is an optional argument.
    * `list[T]`, `set[T]`, etc. collect all positional arguments.
    * `dict[K, V]` and `list[tuple[K, V]]` collect all `key=value` arguments.

    Fields are processed in order, and any unconsumed arguments are rejected.
    Use `arg()` to override the inferred behavior.

        class Command: ...

        @dataclass
        class Get(Command):
            keys: list[str]

        @dataclass
        class Range(Command):
            start: str | None = goldenscript.arg(key="from")
            reverse: bool = goldenscript.arg(key=True, optional=True)

        parser = goldenscript.CommandParser.derive(Command)
    """

    def __init__(self, *variants: type[C]) -> None:
        if not variants:
            raise TypeError("Command requires at least one command variant")
        self.commands: dict[str, tuple[type[C], list[_Field]]] = {}
        self.other: tuple[type[C], str] | None = None
        for variant in variants:
            if self.other is not None:
                raise TypeError("command with 'other' must be the last enum variant")
            if not dataclasses.is_dataclass(variant):
                raise TypeError(f"command variant {variant.__name__} must be a dataclass")
            config: _VariantConfig = variant.__dict__.get(_COMMAND_ATTR, _VariantConfig())
            fields = dataclasses.fields(variant)
            if config.other:
                if len(fields) != 1:
                    raise TypeError("other command must have exactly one unnamed field")
                self.other = (variant, fields[0].name)
                continue
            name = config.name if config.name is not None else _to_snake_case(variant.__name__)
            if name in self.commands:
                raise TypeError(f"duplicate command name '{name}'")
            self.commands[name] = (variant, self._derive_fields(variant, fields))

    @classmethod
    def derive(cls, enum: type[C]) -> "CommandParser[C]":
        """Derives a parser for a command enum, using its direct subclasses as
        variants in definition order."""
        return cls(*enum.__subclasses__())

    @staticmethod
    def _derive_fields(variant: type, fields: tuple[dataclasses.Field[Any], ...]) -> list[_Field]:
        hints = typing.get_type_hints(variant)
        derived: list[_Field] = []
        pos_count, pos_optional_seen = 0, False
        many_seen = {False: False, True: False}
        keys: set[str] = set()
        for dcfield in fields:
            parsed = _derive_field(dcfield, hints[dcfield.name], pos_count + 1)
            # `many` fields must be the last for their kind.
            if many_seen[parsed.keyed]:
                raise TypeError("a field can't follow a many field of the same kind")
            many_seen[parsed.keyed] |= parsed.many
            # Optional positional fields can't be followed by required fields.
            if not parsed.keyed:
                pos_count += 1
                if not parsed.optional and pos_optional_seen:
                    raise TypeError("a required positional argument can't follow an optional one")
                pos_optional_seen |= parsed.optional
            # Argument keys must be unique.
            if parsed.key is not None:
                if parsed.key in keys:
                    raise TypeError(f"duplicate argument key '{parsed.key}'")
                keys.add(parsed.key)
            derived.append(parsed)
        return derived

    def try_from(self, command: Command) -> C:
        """Converts a parsed command into a command enum variant, or raises
        an error if the command or its arguments are invalid."""
        if command.name not in self.commands:
            if self.other is not None:
                variant, name = self.other
                return variant(**{name: command})
            raise ValueError(f"unknown command '{command.name}'")
        variant, fields = self.commands[command.name]
        args = command.consume_args()
        values = {f.name: f.parse(args) for f in fields}
        args.reject_next()
        return variant(**values)

    __call__ = try_from
