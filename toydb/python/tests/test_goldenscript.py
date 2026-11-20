"""Tests for the goldenscript port, mirroring the Rust crate's own tests:
the unit tests in `src/command.rs`, and the integration tests in `tests/`
(`debug.rs`, `hooks.rs`, `btreemap.rs` and `derive.rs`).

The integration tests run the crate's own test scripts, which are read from
the crate source in the local Cargo registry. They are skipped if it isn't
available.
"""

import unittest
from abc import ABC
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Self

from tests import goldenscript
from tests.goldenscript import Argument, ArgumentConsumer, Command, CommandParser, Context, arg
from tests.goldenscript import command as command_config
from toydb.fmt import debug, debug_option, debug_tuple
from toydb.ops import Bound


def crate_dir() -> Path | None:
    """Returns the goldenscript crate's tests directory, if available."""
    registry = Path.home() / ".cargo" / "registry" / "src"
    paths = sorted(registry.glob("*/goldenscript-0.8.0/tests"))
    return paths[0] if paths else None


def read(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as file:
        return file.read()


def cmd(input: str) -> Command:
    """Constructs a Command by parsing the given input string."""
    return goldenscript.parse_command(f"{input}\n")


class DebugRunner(goldenscript.Runner[Command]):
    """A goldenscript runner that debug-prints the parsed command. It
    understands the following special commands:

    _echo: prints back the arguments, space-separated
    _error: errors with the given string
    _panic: panics with the given string

    If a command is expected to fail via !, the parsed command string is
    returned as an error.
    """

    def run(self, command: Command, context: Context) -> str:
        match command.name:
            case "_echo":
                values: list[str] = []
                for argument in command.args:
                    match argument:
                        case Argument.Positional(value):
                            values.append(value)
                        case _:
                            raise ValueError(f"invalid echo arg: {argument}")
                return " ".join(values)
            case "_error":
                args = command.consume_args()
                message = args.next_pos() or "error"
                args.reject_next()
                raise ValueError(message)
            case "_panic":
                args = command.consume_args()
                message = args.next_pos() or "panic"
                args.reject_next()
                raise AssertionError(message)
            case _ if context.fail:
                raise ValueError(debug(command, pretty=True))
            case _:
                return debug(command, pretty=True)


def test_goldenscript(path: Path) -> None:
    """Runs a goldenscript in the crate's tests/scripts with a DebugRunner."""
    goldenscript.run(DebugRunner(), path)


class HookCommand(ABC):
    """Commands accepted by HookRunner."""


@dataclass
class Echo(HookCommand):
    _0: list[str]

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Echo", [self._0], pretty)


@dataclass
class Error(HookCommand):
    pass


@dataclass
class Panic(HookCommand):
    pass


class HookState(Enum):
    """Tracks hook state transitions, including start_script() and
    end_script(), which can't return output."""

    Start = 0
    Script = 1
    Block = 2
    Command = 3
    End = 4


class HookRunner(goldenscript.Runner[HookCommand]):
    """Exposes hook calls as goldenscript output."""

    PARSER = CommandParser.derive(HookCommand)

    def __init__(self) -> None:
        self.state = HookState.Start

    def transition(self, source: HookState, to: HookState) -> None:
        """Transitions to the next state, asserting that hooks are called in order."""
        assert self.state == source
        self.state = to

    def parse_command(self, command: Command) -> HookCommand:
        return self.PARSER.try_from(command)

    def run(self, command: HookCommand, context: Context) -> str:
        assert self.state == HookState.Command
        match command:
            case Echo(values):
                return " ".join(values)
            case Error():
                raise ValueError("error")
            case Panic():
                raise AssertionError("panic")
            case _:
                raise AssertionError("unreachable")

    def start_script(self) -> None:
        self.transition(HookState.Start, HookState.Script)

    def end_script(self) -> None:
        self.transition(HookState.Script, HookState.End)

    def start_block(self, block: goldenscript.Block) -> str:
        self.transition(HookState.Script, HookState.Block)
        return f"start_block: {', '.join(str(command) for command in block.commands)}"

    def end_block(self, block: goldenscript.Block) -> str:
        self.transition(HookState.Block, HookState.Script)
        return f"end_block: {', '.join(str(command) for command in block.commands)}"

    def start_command(self, command: HookCommand, context: Context) -> str:
        self.transition(HookState.Block, HookState.Command)
        return f"start_command: {debug(command)}"

    def end_command(self, command: HookCommand, context: Context) -> str:
        self.transition(HookState.Command, HookState.Block)
        return f"end_command: {debug(command)}"


def test_hooks(path: Path) -> None:
    """Runs the crate's tests/hooks script."""
    runner = HookRunner()
    assert runner.state == HookState.Start
    goldenscript.run(runner, path)
    assert runner.state == HookState.End


class BTreeMapCommand(ABC):
    """Commands for BTreeMapRunner."""


@dataclass
class Get(BTreeMapCommand):
    """Fetches the given keys."""

    _0: list[str]


@dataclass
class Insert(BTreeMapCommand):
    """Inserts the given key/value pairs."""

    _0: list[tuple[str, str]]


@dataclass
class Range(BTreeMapCommand):
    """Scans the given [from, to) range."""

    start: str | None = arg(key="from")
    to: str | None = arg(key=True)


class BTreeMapRunner(goldenscript.Runner[BTreeMapCommand]):
    """A runner for ordered map tests, standing in for Rust's BTreeMap."""

    PARSER = CommandParser.derive(BTreeMapCommand)

    def __init__(self) -> None:
        self.map: dict[str, str] = {}

    def parse_command(self, command: Command) -> BTreeMapCommand:
        return self.PARSER.try_from(command)

    def run(self, command: BTreeMapCommand, context: Context) -> str:
        output: list[str] = []
        match command:
            case Get(keys):
                for key in keys:
                    value = self.map.get(key)
                    output.append(f"get {debug(key)} → {debug_option(value)}\n")
            case Insert(entries):
                for key, value in entries:
                    old = self.map.get(key)
                    self.map[key] = value
                    output.append(
                        f"insert {debug(key)} = {debug(value)} (was {debug_option(old)})\n"
                    )
            case Range(start, to):
                lower: Bound[str] = Bound.Unbounded() if start is None else Bound.Included(start)
                upper: Bound[str] = Bound.Unbounded() if to is None else Bound.Excluded(to)
                output.append(f"range {debug(lower)} → {debug(upper)}\n")
                assert start is None or to is None or start <= to, (
                    "range start is greater than range end in BTreeMap"
                )
                for key in sorted(self.map):
                    if (start is None or key >= start) and (to is None or key < to):
                        output.append(f"{debug(key)} = {debug(self.map[key])}\n")
            case _:
                raise AssertionError("unreachable")
        return "".join(output)


def test_btreemap(path: Path) -> None:
    """Runs the crate's tests/btreemap script."""
    goldenscript.run(BTreeMapRunner(), path)


@unittest.skipIf(crate_dir() is None, "goldenscript crate source not found")
class TestScripts(unittest.TestCase):
    """The crate's integration tests in tests/debug.rs, hooks.rs and btreemap.rs."""

    maxDiff = None

    def setUp(self) -> None:
        directory = crate_dir()
        assert directory is not None
        self.dir = directory

    def test_scripts(self) -> None:
        """Run goldenscripts in tests/scripts that debug-print the commands."""
        paths = sorted(path for path in (self.dir / "scripts").iterdir() if path.is_file())
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path.name):
                self.assertEqual(goldenscript.generate(DebugRunner(), read(path)), read(path))
                test_goldenscript(path)

    def test_generate(self) -> None:
        """Run goldenscripts in tests/generate with output in a separate file."""
        paths = sorted((self.dir / "generate").glob("*.in"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path.name):
                output = goldenscript.generate(DebugRunner(), read(path))
                self.assertEqual(output, read(path.with_suffix(".out")))

    def test_errors(self) -> None:
        """The input scripts in tests/errors are expected to error or panic
        with the stored output."""
        paths = sorted((self.dir / "errors").glob("*.in"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path.name):
                with self.assertRaises((goldenscript.Error, AssertionError)) as context:
                    goldenscript.generate(DebugRunner(), read(path))
                self.assertEqual(str(context.exception), read(path.with_suffix(".error")))

    def test_hooks(self) -> None:
        test_hooks(self.dir / "hooks")

    def test_btreemap(self) -> None:
        test_btreemap(self.dir / "btreemap")

    def test_mismatch(self) -> None:
        """run() raises a Mismatch with a diff if the output differs."""
        with self.assertRaises(goldenscript.Mismatch) as context:
            goldenscript.run(DebugRunner(), self.dir / "hooks")
        self.assertIn("-start_block: echo one\n", context.exception.diff())
        self.assertIn("+Command {\n", context.exception.diff())


class TestCommand(unittest.TestCase):
    """The unit tests in src/command.rs."""

    def test_command_consume_args(self) -> None:
        """Tests Command.consume_args(). ArgumentConsumer is tested separately."""
        command = cmd("cmd foo key=value bar")
        self.assertEqual(list(command.consume_args()), command.args)

    def test_display(self) -> None:
        """Tests Command and Argument display formatting."""
        self.assertEqual(str(Argument.Positional("value")), "value")
        self.assertEqual(str(Argument.KeyValue("key", "value")), "key=value")
        self.assertEqual(
            str(Argument.KeyValue("key with spaces", "line\nbreak")),
            r'"key with spaces"="line\nbreak"',
        )
        self.assertEqual(str(Argument.Positional('value "quoted"')), r'"value \"quoted\""')

        self.assertEqual(str(cmd("command arg key=value")), "command arg key=value")
        self.assertEqual(
            str(cmd(r'"command with spaces" "arg with spaces" key="line\nbreak"')),
            r'"command with spaces" "arg with spaces" key="line\nbreak"',
        )

    def test_argument_consumer_take_key(self) -> None:
        """Tests ArgumentConsumer.take_key()."""
        command = cmd("cmd value key=1 foo=bar key=2")

        # take_key() returns None on unknown keys, including ones that match a
        # positional argument.
        args = command.consume_args()
        self.assertIsNone(args.take_key("unknown"))
        self.assertIsNone(args.take_key("value"))
        self.assertEqual(list(args), command.args)

        # take_key() removes duplicate keys, returning the last value.
        args = command.consume_args()
        self.assertEqual(args.take_key("key"), "2")
        self.assertEqual(list(args), [command.args[0], command.args[2]])

        # take_key() removes a single key and returns its value.
        args = command.consume_args()
        self.assertEqual(args.take_key("foo"), "bar")
        self.assertEqual(list(args), [command.args[0], command.args[1], command.args[3]])

    def test_argument_consumer_next(self) -> None:
        """Tests ArgumentConsumer.next()."""
        command = cmd("cmd foo key=1 key=2 bar")

        args = command.consume_args()
        self.assertEqual(args.next(), command.args[0])
        self.assertEqual(args.next(), command.args[1])
        self.assertEqual(args.next(), command.args[2])
        self.assertEqual(args.next(), command.args[3])
        self.assertIsNone(args.next())
        self.assertIsNone(args.next_key())
        self.assertIsNone(args.next_pos())

    def test_argument_consumer_next_key(self) -> None:
        """Tests ArgumentConsumer.next_key()."""
        command = cmd("cmd foo key=1 key=2 bar")
        args = command.consume_args()

        self.assertEqual(args.next_key(), ("key", "1"))
        self.assertEqual(args.next_key(), ("key", "2"))
        self.assertIsNone(args.next_key())
        self.assertEqual(args.next(), command.args[0])
        self.assertEqual(args.next(), command.args[3])
        self.assertIsNone(args.next())

    def test_argument_consumer_next_pos(self) -> None:
        """Tests ArgumentConsumer.next_pos()."""
        command = cmd("cmd foo key=1 key=2 bar")
        args = command.consume_args()

        self.assertEqual(args.next_pos(), "foo")
        self.assertEqual(args.next_pos(), "bar")
        self.assertIsNone(args.next_pos())
        self.assertEqual(args.next(), command.args[1])
        self.assertEqual(args.next(), command.args[2])
        self.assertIsNone(args.next())

    def test_argument_consumer_reject_next(self) -> None:
        """Tests ArgumentConsumer.reject_next()."""
        # Empty args return Ok.
        cmd("cmd").consume_args().reject_next()

        # Positional argument fails. It does not consume the arg.
        args = cmd("cmd value").consume_args()
        with self.assertRaisesRegex(ValueError, "^unexpected argument 'value'$"):
            args.reject_next()
        self.assertIsNotNone(args.next())

        # Key/value argument fails. It does not consume the arg.
        args = cmd("cmd key=value").consume_args()
        with self.assertRaisesRegex(ValueError, "^unexpected argument 'key=value'$"):
            args.reject_next()
        self.assertIsNotNone(args.next())

    def test_duplicate_arguments(self) -> None:
        """Equal arguments are consumed one at a time, in order."""
        args = ArgumentConsumer(cmd("cmd a k=1 a k=1").args)
        self.assertEqual(args.next_pos(), "a")
        self.assertEqual(args.next_key(), ("k", "1"))
        self.assertEqual(list(args), [Argument.Positional("a"), Argument.KeyValue("k", "1")])


@dataclass(frozen=True, order=True)
class Custom:
    """A custom type that implements from_str (FromStr)."""

    value: int

    @classmethod
    def from_str(cls, value: str) -> Self:
        return cls(goldenscript._parse_int(value))


class Variants(ABC):
    """An enum with each variant shape."""


@dataclass
class Unit(Variants):
    pass


@dataclass
class Tuple(Variants):
    _0: str
    _1: list[int]


@dataclass
class Struct(Variants):
    name: str
    values: list[int]


class Scalars(ABC):
    """An enum with various scalar values."""


@dataclass
class String(Scalars):
    _0: str


@dataclass
class Bool(Scalars):
    _0: bool


@dataclass
class I64(Scalars):
    _0: int


@dataclass
class F64(Scalars):
    _0: float


@command_config(name="custom")
@dataclass
class CustomScalar(Scalars):
    _0: Custom


class Containers(ABC):
    """An enum with various container values."""


@command_config(name="btree_map")
@dataclass
class BTreeMap(Containers):
    _0: dict[Custom, Custom]


@dataclass
class HashSet(Containers):
    _0: set[Custom]


@dataclass
class Vec(Containers):
    _0: list[Custom]


@dataclass
class VecDeque(Containers):
    _0: deque[Custom]


@dataclass
class VecKeyValue(Containers):
    _0: list[tuple[Custom, Custom]]


@dataclass
class Option(Containers):
    _0: Custom | None


class ArgumentKinds(ABC):
    pass


@dataclass
class Pos(ArgumentKinds):
    inferred: str
    required: str = arg(pos=True)
    optional: int | None = arg(pos=True)
    default: int = arg(pos=True, optional=True)
    many: list[int] = arg(pos=True, optional=True)


@dataclass
class Key(ArgumentKinds):
    required: str = arg(key=True)
    alias: str = arg(key="name")
    optional: int | None = arg(key=True)
    default: int = arg(key=True, optional=True)
    many: dict[str, int] = arg(key=True, optional=True)


class CommandsWithOther(ABC):
    """An enum with a fallback for unrecognized commands."""


@dataclass
class Known(CommandsWithOther):
    pass


@command_config(other=True)
@dataclass
class Other(CommandsWithOther):
    _0: Command


class MixedKinds(ABC):
    pass


@dataclass
class KeysFirst(MixedKinds):
    keys: dict[str, int]
    value: int
    values: list[int]


@dataclass
class ValuesFirst(MixedKinds):
    values: list[int]
    key: str = arg(key=True)
    keys: dict[str, int] = arg(key=True, many=True)


class EmptyNames(ABC):
    pass


@command_config(name="")
@dataclass
class Values(EmptyNames):
    value: str = arg(key="")


class TestDerive(unittest.TestCase):
    """Tests CommandParser, mirroring tests/derive.rs."""

    def parse[C](self, enum: type[C], input: str) -> C:
        return CommandParser.derive(enum).try_from(cmd(input))

    def parse_error(self, enum: type, input: str) -> str:
        with self.assertRaises(ValueError) as context:
            self.parse(enum, input)
        return str(context.exception)

    def test_variant_shapes(self) -> None:
        self.assertEqual(self.parse(Variants, "unit"), Unit())
        self.assertEqual(self.parse(Variants, "tuple text 1 2 3"), Tuple("text", [1, 2, 3]))
        self.assertEqual(self.parse(Variants, "struct alice 4 5 6"), Struct("alice", [4, 5, 6]))

    def test_scalar_values(self) -> None:
        self.assertEqual(self.parse(Scalars, "string text"), String("text"))
        self.assertEqual(self.parse(Scalars, "bool true"), Bool(True))
        self.assertEqual(self.parse(Scalars, 'i64 "-12"'), I64(-12))
        self.assertEqual(self.parse(Scalars, "f64 1.5"), F64(1.5))
        self.assertEqual(self.parse(Scalars, "custom 99"), CustomScalar(Custom(99)))

    def test_scalar_conversion_errors(self) -> None:
        self.assertEqual(self.parse_error(Scalars, "string"), "argument '1' not given")
        self.assertEqual(
            self.parse_error(Scalars, "bool invalid"),
            "invalid value 'invalid' for argument '1': provided string was not `true` or `false`",
        )
        self.assertEqual(
            self.parse_error(Scalars, "i64 invalid"),
            "invalid value 'invalid' for argument '1': invalid digit found in string",
        )
        self.assertEqual(
            self.parse_error(Scalars, "f64 invalid"),
            "invalid value 'invalid' for argument '1': invalid float literal",
        )
        self.assertEqual(
            self.parse_error(Scalars, "custom invalid"),
            "invalid value 'invalid' for argument '1': invalid digit found in string",
        )
        self.assertEqual(self.parse_error(Scalars, "unknown"), "unknown command 'unknown'")
        self.assertEqual(self.parse_error(Scalars, "bool true 1"), "unexpected argument '1'")

    def test_container_values(self) -> None:
        self.assertEqual(
            self.parse(Containers, "btree_map 1=3 2=2 1=1"),
            BTreeMap({Custom(1): Custom(1), Custom(2): Custom(2)}),
        )
        self.assertEqual(
            self.parse(Containers, "hash_set 3 1 2 1"), HashSet({Custom(1), Custom(2), Custom(3)})
        )
        self.assertEqual(
            self.parse(Containers, "vec 3 1 2"), Vec([Custom(3), Custom(1), Custom(2)])
        )
        self.assertEqual(
            self.parse(Containers, "vec_deque 3 1 2"),
            VecDeque(deque([Custom(3), Custom(1), Custom(2)])),
        )
        self.assertEqual(
            self.parse(Containers, "vec_key_value 3=1 1=2 3=1"),
            VecKeyValue([(Custom(3), Custom(1)), (Custom(1), Custom(2)), (Custom(3), Custom(1))]),
        )
        self.assertEqual(self.parse(Containers, "option 3"), Option(Custom(3)))
        self.assertEqual(self.parse(Containers, "option"), Option(None))

    def test_argument_kinds(self) -> None:
        self.assertEqual(
            self.parse(ArgumentKinds, "pos first second 1 2 3 4"),
            Pos(inferred="first", required="second", optional=1, default=2, many=[3, 4]),
        )
        self.assertEqual(
            self.parse(ArgumentKinds, "pos first second"),
            Pos(inferred="first", required="second", optional=None, default=0, many=[]),
        )
        self.assertEqual(
            self.parse(
                ArgumentKinds, "key a=3 required=first b=4 name=second optional=1 default=2 a=5"
            ),
            Key(required="first", alias="second", optional=1, default=2, many={"a": 5, "b": 4}),
        )
        self.assertEqual(
            self.parse(ArgumentKinds, "key required=first name=second"),
            Key(required="first", alias="second", optional=None, default=0, many={}),
        )
        self.assertEqual(self.parse_error(ArgumentKinds, "pos"), "argument 'inferred' not given")
        self.assertEqual(
            self.parse_error(ArgumentKinds, "pos first second invalid"),
            "invalid value 'invalid' for argument 'optional': invalid digit found in string",
        )
        self.assertEqual(
            self.parse_error(ArgumentKinds, "key name=second"), "argument 'required' not given"
        )
        self.assertEqual(
            self.parse_error(ArgumentKinds, "key required=first name=second optional=invalid"),
            "invalid value 'invalid' for argument 'optional': invalid digit found in string",
        )

    def test_keyed_collection_conversion_errors(self) -> None:
        self.assertEqual(
            self.parse_error(Containers, "btree_map invalid=1"),
            "invalid value 'invalid' for argument 'dict': invalid digit found in string",
        )
        self.assertEqual(
            self.parse_error(Containers, "btree_map 1=invalid"),
            "invalid value 'invalid' for argument 'dict': invalid digit found in string",
        )

    def test_other_command(self) -> None:
        self.assertEqual(self.parse(CommandsWithOther, "known"), Known())
        raw = cmd("unknown value key=7")
        self.assertEqual(CommandParser.derive(CommandsWithOther)(raw), Other(raw))

    def test_many_field_can_be_followed_by_the_other_argument_kind(self) -> None:
        self.assertEqual(
            self.parse(MixedKinds, "keys_first a=1 7 b=2 8"),
            KeysFirst(keys={"a": 1, "b": 2}, value=7, values=[8]),
        )
        self.assertEqual(
            self.parse(MixedKinds, "values_first key=value 7 a=1 b=2 8"),
            ValuesFirst(values=[7, 8], key="value", keys={"a": 1, "b": 2}),
        )

    def test_empty_names(self) -> None:
        self.assertEqual(self.parse(EmptyNames, '"" ""=""'), Values(value=""))

    def test_runner_metadata_is_ignored(self) -> None:
        self.assertEqual(self.parse(Variants, "(unit)"), Unit())
        self.assertEqual(self.parse(Variants, "!unit"), Unit())
        self.assertEqual(self.parse(Variants, "prefix:[tag] unit"), Unit())

    def test_invalid_enums(self) -> None:
        """Invalid command enums are rejected when deriving the parser."""

        class Duplicates(ABC):
            pass

        @dataclass
        class First(Duplicates):
            pass

        @command_config(name="first")
        @dataclass
        class Second(Duplicates):
            pass

        with self.assertRaisesRegex(TypeError, "duplicate command name 'first'"):
            CommandParser.derive(Duplicates)
        with self.assertRaisesRegex(TypeError, "must be the last enum variant"):
            CommandParser(Other, Known)
        with self.assertRaisesRegex(TypeError, "at least one command variant"):
            CommandParser()


if __name__ == "__main__":
    unittest.main()
