"""SQL tests are implemented as goldenscripts under tests/sql/testscripts.

Mirrors the tests in `src/sql/mod.rs`.
"""

import queue
import tempfile
import unittest
from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from tests import goldenscript
from tests.goldenscript import CommandParser, Context, arg
from tests.storage.test_engine import Emit, Mirror, Operation
from toydb.encoding import format
from toydb.fmt import debug
from toydb.ops import Bound
from toydb.sql.engine import Local
from toydb.sql.engine.local import Transaction
from toydb.sql.execution import Session, StatementResult, json
from toydb.sql.parser import Parser
from toydb.sql.planner import OPTIMIZERS, Plan, Planner, Scope
from toydb.storage.bitcask import BitCask
from toydb.storage.memory import Memory
from toydb.storage.mvcc import ScanIterator

# Don't fsync in tests, and use a small scan buffer to exercise the buffering
# code (like `cfg!(test)` in Rust).
BitCask.FSYNC = False
ScanIterator.BUFFER_SIZE = 2

TESTSCRIPTS = Path(__file__).resolve().parent / "testscripts"


def test_goldenscript(path: Path) -> None:
    """Runs SQL goldenscripts."""
    # Use both BitCask and Memory engines and mirror operations across them.
    # Emit engine operations to op_rx.
    op_rx: queue.Queue[Operation] = queue.Queue()
    with tempfile.TemporaryDirectory(prefix="toydb") as tempdir:
        bitcask = BitCask.new(Path(tempdir) / "bitcask")
        memory = Memory()
        engine: TestEngine = Local(Emit(Mirror(bitcask, memory), op_rx))
        runner = SQLRunner(engine, op_rx)
        try:
            goldenscript.run(runner, path)
        finally:
            runner.close()
            bitcask.close()


def test_goldenscript_expr(path: Path) -> None:
    """Runs expression goldenscripts."""
    goldenscript.run(ExpressionRunner(), path)


type TestStorage = Emit[Mirror[BitCask, Memory]]
type TestEngine = Local[TestStorage]


class SQLCommand(ABC):
    """Commands accepted by the SQL Goldenscript runner."""


@dataclass
class Dump(SQLCommand):
    """Dumps the raw SQL storage engine contents."""


@dataclass
class Schema(SQLCommand):
    """Displays table schemas."""

    _0: list[str] = arg(optional=True)
    """The table names, or empty to display all tables."""


@goldenscript.command(other=True)
@dataclass
class Statement(SQLCommand):
    """Any other command is executed as a raw SQL statement."""

    _0: goldenscript.Command


class SQLRunner(goldenscript.Runner[SQLCommand]):
    """The SQL test runner."""

    PARSER: ClassVar[CommandParser[SQLCommand]] = CommandParser.derive(SQLCommand)

    def __init__(self, engine: TestEngine, op_rx: "queue.Queue[Operation]") -> None:
        self.engine = engine
        self.sessions: dict[str, Session[Transaction[TestStorage]]] = {}
        self.op_rx = op_rx

    def close(self) -> None:
        """Closes the sessions, rolling back any open transactions."""
        for session in self.sessions.values():
            session.close()

    def parse_command(self, command: goldenscript.Command) -> SQLCommand:
        return self.PARSER.try_from(command)

    def run(self, command: SQLCommand, context: Context) -> str:
        output = ""

        # Obtain a session based on the command prefix ("" if none).
        prefix = context.prefix or ""
        if (session := self.sessions.get(prefix)) is None:
            session = self.sessions[prefix] = self.engine.session()

        # Handle runner commands.
        match command:
            case Dump():
                with self.engine.mvcc.engine.lock() as engine:
                    for key, value in engine.scan((Bound.Unbounded(), Bound.Unbounded())):
                        fmtkv = format.MVCC(format.SQL()).key_value(key, value)
                        rawkv = format.Raw().key_value(key, value)
                        output += f"{fmtkv} [{rawkv}]\n"
                return output

            case Schema(tables):
                if not tables:
                    schemas = session.with_txn(True, lambda txn: txn.list_tables())
                else:
                    schemas = [
                        session.with_txn(True, lambda txn: txn.must_get_table(t)) for t in tables
                    ]
                return "\n".join(str(schema) for schema in schemas)

            case Statement(statement):
                pass

            case _:
                raise TypeError(f"unknown command {command!r}")

        # The entire command is the SQL statement. There are no args.
        if statement.args:
            raise ValueError("SQL statements should be given as a command with no args")
        input = statement.name
        tags = set(context.tags)

        # Output the plan if requested.
        if "plan" in tags:
            tags.remove("plan")
            ast = Parser.parse(input)
            plan = session.with_txn(True, lambda txn: Planner(txn).build(ast).optimize())
            output += f"{plan}\n"

        # Output plan optimizations if requested.
        if "opt" in tags:
            tags.remove("opt")
            if "plan" in tags:
                raise ValueError("using both plan and opt is redundant")
            ast = Parser.parse(input)
            plan = session.with_txn(True, lambda txn: Planner(txn).build(ast))
            if not isinstance(plan, Plan.Select):
                raise ValueError("can only use opt with SELECT plans")
            root = plan.root
            output += f"Initial:\n{root}".replace("\n", "\n   ") + "\n"
            for optimizer in OPTIMIZERS:
                prev = root
                root = optimizer.optimize(root)
                if root != prev:
                    output += f"{optimizer!r}:\n{root}".replace("\n", "\n   ") + "\n"

        # Execute the statement.
        result = session.execute(input)

        # Output engine ops if requested.
        if "ops" in tags:
            tags.remove("ops")
            while True:
                try:
                    op = self.op_rx.get_nowait()
                except queue.Empty:
                    break
                match op:
                    case Operation.Delete(key):
                        fmtkey = format.MVCC(format.SQL()).key(key)
                        rawkey = format.Raw().key(key)
                        output += f"delete {fmtkey} [{rawkey}]\n"
                    case Operation.Flush():
                        output += "flush\n"
                    case Operation.Set(key, value):
                        fmtkv = format.MVCC(format.SQL()).key_value(key, value)
                        rawkv = format.Raw().key_value(key, value)
                        output += f"set {fmtkv} [{rawkv}]\n"
                    case _:
                        raise TypeError(f"unknown operation {op!r}")

        # Output the result if requested. SELECT results and EXPORT lines are
        # always output. With [json], output the result's JSON form instead.
        if "json" in tags:
            tags.remove("json")
            tags.discard("header")
            tags.discard("result")
            output += f"{json.encode_result(result)}\n"
        else:
            match result:
                case StatementResult.Select(columns, rows):
                    if "header" in tags:
                        tags.remove("header")
                        output += ", ".join(str(column) for column in columns) + "\n"
                    for row in rows:
                        output += ", ".join(str(value) for value in row) + "\n"
                case StatementResult.Export(lines=lines):
                    for line in lines:
                        output += f"{line}\n"
                case _ if "result" in tags:
                    tags.remove("result")
                    output += f"{debug(result)}\n"
                case _:
                    pass

        # Reject unknown tags.
        if tags:
            raise ValueError(f"unknown tag {min(tags)}")

        return output

    def end_command(self, command: SQLCommand, context: Context) -> str:
        """Drain unprocessed operations after each command."""
        while True:
            try:
                self.op_rx.get_nowait()
            except queue.Empty:
                break
        return ""


class ExpressionRunner(goldenscript.Runner[goldenscript.Command]):
    """A test runner for expressions. Evaluates expressions to values, and
    optionally emits the expression tree."""

    def run(self, command: goldenscript.Command, context: goldenscript.Context) -> str:
        # The entire command is the expression to evaluate. There are no args.
        if command.consume_args().next() is not None:
            raise ValueError("expressions should be given as a command with no args")
        input = command.name
        tags = set(context.tags)

        # Parse and build the expression.
        ast = Parser.parse_expr(input)
        expr = Planner.build_expression(ast, Scope())

        # Evaluate the expression.
        value = expr.evaluate(None)
        output = f"{value}"

        # If requested, convert the expression to conjunctive normal form
        # and dump it. Assert that it produces the same result.
        if "cnf" in tags:
            tags.remove("cnf")
            cnf = expr.into_cnf()
            assert value == cnf.evaluate(None), "CNF result differs"
            output += f" ← {cnf}"

        # If requested, debug-dump the parsed expression.
        if "expr" in tags:
            tags.remove("expr")
            output += f" ← {debug(expr)}"
        output += "\n"

        # Reject unknown tags.
        if tags:
            raise ValueError(f"unknown tag {sorted(tags)[0]}")

        return output


def scripts(directory: Path) -> list[Path]:
    """Returns the goldenscripts in a directory, sorted by name."""
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.iterdir() if p.is_file() and not p.name.startswith("."))


class TestSQL(unittest.TestCase):
    """Runs the goldenscripts in tests/sql/testscripts."""

    def test_goldenscripts(self) -> None:
        for name in ("ext", "optimizers", "queries", "schema", "task", "transactions", "writes"):
            for path in scripts(TESTSCRIPTS / name):
                with self.subTest(path=path):
                    test_goldenscript(path)


class TestExpressions(unittest.TestCase):
    """Runs the goldenscripts in tests/sql/testscripts/expressions."""

    def test_goldenscripts(self) -> None:
        for path in scripts(TESTSCRIPTS / "expressions"):
            with self.subTest(path=path):
                test_goldenscript_expr(path)


if __name__ == "__main__":
    unittest.main()
