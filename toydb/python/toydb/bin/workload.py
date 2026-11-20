"""Runs toyDB workload benchmarks. By default, it assumes a running 5-node
cluster as launched via cluster/run.sh, but this can be modified via -H.
For example, a read-only workload can be run as:

    python3 -m toydb.bin.workload read

See --help for a list of available workloads and arguments.

Mirrors `src/bin/workload.rs`. Python's `random.Random` replaces the Rust
`StdRng`, so a given seed yields different (but deterministic) data than in
Rust. Latencies are recorded exactly and percentiles computed from them,
instead of using an HDR histogram.
"""

import argparse
import itertools
import math
import queue
import random
import string
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar

from toydb import Client, StatementResult
from toydb.error import Error, assert_eq, panic

VERSION = "1.0.0"
"""The toyDB version (the Cargo package version)."""


def main() -> None:
    command = Command.parse()
    runner, subcommand = command.runner, command.subcommand
    try:
        match subcommand:
            case Read() as read:
                runner.run(read)
            case Write() as write:
                runner.run(write)
            case Bank() as bank:
                runner.run(bank)
    except Error as error:
        print(f"Error: {error}", file=sys.stderr)


@dataclass
class Command:
    """Handles command-line parsing."""

    runner: "Runner"
    subcommand: "Subcommand"

    @classmethod
    def parse(cls, args: list[str] | None = None) -> "Command":
        """Parses command-line arguments."""
        parser = argparse.ArgumentParser(
            prog="workload", description="Runs toyDB workload benchmarks."
        )
        parser.add_argument("-V", "--version", action="version", version=f"workload {VERSION}")
        Runner.add_arguments(parser)
        subparsers = parser.add_subparsers(dest="subcommand", required=True)
        for workload in (Read, Write, Bank):
            subparser = subparsers.add_parser(workload.NAME, help=workload.ABOUT)
            subparser.description = workload.ABOUT
            workload.add_arguments(subparser)
            subparser.set_defaults(workload=workload)
        parsed = parser.parse_args(args)
        return cls(runner=Runner.from_args(parsed), subcommand=parsed.workload.from_args(parsed))


type Subcommand = Read | Write | Bank
"""A workload subcommand."""


def _u64(value: str) -> int:
    """Parses a Rust u64 (or usize)."""
    number = int(value)
    if not 0 <= number < 2**64:
        raise ValueError(f"{value} is not a valid u64")
    return number


@dataclass
class Runner:
    """Runs a workload benchmark."""

    hosts: list[str]
    """Hosts to connect to (optionally with port number)."""
    concurrency: int
    """Number of concurrent workers to spawn."""
    count: int
    """Number of transactions to execute."""
    seed: int
    """Seed to use for random number generation."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "-H",
            "--hosts",
            type=lambda hosts: hosts.split(","),
            default="localhost:9601,localhost:9602,localhost:9603,localhost:9604,localhost:9605",
            help="Hosts to connect to (optionally with port number).",
        )
        parser.add_argument(
            "-c", "--concurrency", type=_u64, default=16, help="Number of concurrent workers to spawn."
        )
        parser.add_argument(
            "-n", "--count", type=_u64, default=100000, help="Number of transactions to execute."
        )
        parser.add_argument(
            "-s", "--seed", type=_u64, default=16791084677885396490, help="Seed to use for random number generation."
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Runner":
        return cls(
            hosts=args.hosts, concurrency=args.concurrency, count=args.count, seed=args.seed
        )

    def run(self, workload: "Workload") -> None:
        """Runs the specified workload."""
        rng = random.Random(self.seed)
        client = Client.connect(self.hosts[0])

        # Set up a histogram recording txn latencies as nanoseconds.
        hist = Histogram()

        # Prepare the dataset.
        print("Preparing initial dataset... ", end="", flush=True)
        start = time.monotonic()
        workload.prepare(client, rng)
        print(f"done ({time.monotonic() - start:.3f}s)")

        # Spawn workers, round robin across hosts.
        print(f"Spawning {self.concurrency} workers... ", end="", flush=True)
        start = time.monotonic()

        work_tx: queue.Queue[Any] = queue.Queue(max(self.concurrency, 1))
        stop = threading.Event()  # set when the work is exhausted or a worker failed
        errors: list[BaseException] = []

        def work(client: Client) -> None:
            try:
                while True:
                    try:
                        item = work_tx.get(timeout=0.1)
                    except queue.Empty:
                        if stop.is_set():
                            return
                        continue
                    if item is _DONE:
                        return
                    start = time.monotonic_ns()
                    client.with_retry(lambda client: workload.execute(client, item))
                    hist.record(time.monotonic_ns() - start)
            except BaseException as error:
                errors.append(error)
                stop.set()
            finally:
                client.close()

        workers: list[threading.Thread] = []
        for addr in itertools.islice(itertools.cycle(self.hosts), self.concurrency):
            worker_client = Client.connect(addr)
            workers.append(threading.Thread(target=work, args=(worker_client,), daemon=True))
        for worker in workers:
            worker.start()

        print(f"done ({time.monotonic() - start:.3f}s)")

        # Spawn work generator.
        print(f"Running workload {workload}...")
        generator = itertools.islice(workload.generate(rng), self.count)

        def generate() -> None:
            for item in generator:
                while not stop.is_set():
                    try:
                        work_tx.put(item, timeout=0.1)
                        break
                    except queue.Full:
                        continue
            for _ in workers:
                if stop.is_set():
                    break
                work_tx.put(_DONE)

        threading.Thread(target=generate, daemon=True).start()

        # Periodically print stats until all workers are done.
        start = time.monotonic()
        print()
        print("Time   Progress     Txns      Rate       p50       p90       p99      pMax")

        done = False
        while not done:
            deadline = time.monotonic() + 1.0
            for worker in workers:
                worker.join(max(deadline - time.monotonic(), 0))
            done = not any(worker.is_alive() for worker in workers)

            duration = time.monotonic() - start
            count = hist.len()
            progress = count / self.count * 100.0 if self.count else 100.0
            print(
                f"{f'{duration:.1f}s':<8} {progress:>5.1f}%  {count:>7}  "
                f"{count / duration:>6.0f}/s  "
                f"{hist.value_at_quantile(0.5) / 1e6:>6.1f}ms  "
                f"{hist.value_at_quantile(0.9) / 1e6:>6.1f}ms  "
                f"{hist.value_at_quantile(0.99) / 1e6:>6.1f}ms  "
                f"{hist.max() / 1e6:>6.1f}ms"
            )

        if errors:
            error = errors[0]
            raise error

        # Verify the final dataset.
        print()
        print("Verifying dataset... ", end="", flush=True)
        start = time.monotonic()
        workload.verify(client, self.count)
        print(f"done ({time.monotonic() - start:.3f}s)")
        client.close()


_DONE = object()
"""Signals a worker that there is no more work."""


class Histogram:
    """Records transaction latencies in nanoseconds, and computes quantiles.
    Stands in for an HDR histogram (exact, rather than approximate)."""

    def __init__(self) -> None:
        self.values: list[int] = []
        self.lock = threading.Lock()

    def record(self, value: int) -> None:
        with self.lock:
            self.values.append(value)

    def len(self) -> int:
        with self.lock:
            return len(self.values)

    def value_at_quantile(self, quantile: float) -> int:
        """Returns the smallest recorded value such that the given fraction of
        values are less than or equal to it, or 0 if nothing was recorded."""
        with self.lock:
            values = sorted(self.values)
        if not values:
            return 0
        rank = max(math.ceil(quantile * len(values)), 1)
        return values[min(rank, len(values)) - 1]

    def max(self) -> int:
        with self.lock:
            return max(self.values, default=0)


class Workload(ABC):
    """A workload."""

    NAME: ClassVar[str]
    """The subcommand name."""
    ABOUT: ClassVar[str]
    """The subcommand description."""

    @staticmethod
    @abstractmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        """Adds the workload's command-line arguments to a parser."""

    @classmethod
    @abstractmethod
    def from_args(cls, args: argparse.Namespace) -> "Workload":
        """Creates the workload from parsed command-line arguments."""

    @abstractmethod
    def prepare(self, client: Client, rng: random.Random) -> None:
        """Prepares the workload by creating initial tables and data."""

    @abstractmethod
    def generate(self, rng: random.Random) -> Iterator[Any]:
        """Generates work items as an iterator."""

    @abstractmethod
    def execute(self, client: Client, item: Any) -> None:
        """Executes a single work item. This will automatically be retried on
        certain errors, and must use a transaction where appropriate."""

    def verify(self, client: Client, txns: int) -> None:
        """Verifies the dataset after the workload has completed."""


def alphanumeric(rng: random.Random, size: int) -> str:
    """Generates a random alphanumeric string of the given size."""
    return "".join(rng.choices(string.ascii_letters + string.digits, k=size))


@dataclass
class Read(Workload):
    """A read-only workload. Creates an id,value table and populates it with
    the given row count and value size. Then runs batches of random primary
    key lookups (SELECT * FROM read WHERE id = 1 OR id = 2 ...)."""

    NAME: ClassVar[str] = "read"
    ABOUT: ClassVar[str] = "A read-only workload using primary key lookups"

    rows: int
    """Total number of rows in data set."""
    size: int
    """Row value size (excluding primary key)."""
    batch: int
    """Number of rows to fetch in a single select."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-r", "--rows", type=_u64, default=1000, help="Total number of rows in data set.")
        parser.add_argument("-s", "--size", type=_u64, default=64, help="Row value size (excluding primary key).")
        parser.add_argument("-b", "--batch", type=_u64, default=1, help="Number of rows to fetch in a single select.")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Read":
        return cls(rows=args.rows, size=args.size, batch=args.batch)

    def __str__(self) -> str:
        return f"read (rows={self.rows} size={self.size} batch={self.batch})"

    def prepare(self, client: Client, rng: random.Random) -> None:
        client.execute("BEGIN")
        client.execute('DROP TABLE IF EXISTS "read"')
        client.execute('CREATE TABLE "read" (id INT PRIMARY KEY, value STRING NOT NULL)')

        rows = ((id, alphanumeric(rng, self.size)) for id in range(1, self.rows + 1))
        for chunk in itertools.batched(rows, 100):
            values = "), (".join(f"{id}, '{value}'" for id, value in chunk)
            client.execute(f'INSERT INTO "read" (id, value) VALUES ({values})')
        client.execute("COMMIT")

    def generate(self, rng: random.Random) -> "ReadGenerator":
        return ReadGenerator(batch=self.batch, rng=rng, rows=self.rows)

    def execute(self, client: Client, item: set[int]) -> None:
        batch_size = len(item)
        predicate = " OR ".join(f"id = {id}" for id in item)
        rows = client.execute(f'SELECT * FROM "read" WHERE {predicate}').try_into_rows()
        assert_eq(sum(1 for _ in rows), batch_size, "Unexpected row count")

    def verify(self, client: Client, txns: int) -> None:
        count = client.execute('SELECT COUNT(*) FROM "read"').try_into_i64()
        assert_eq(count, self.rows, "Unexpected row count")


@dataclass
class ReadGenerator(Iterator[set[int]]):
    """A Read workload generator, yielding batches of random, unique primary
    keys."""

    batch: int
    rng: random.Random
    rows: int
    """The upper bound (inclusive) of the uniform key distribution, starting
    at 1 (the Rust `dist` field)."""

    def __next__(self) -> set[int]:
        ids: set[int] = set()
        while True:
            ids.add(self.rng.randint(1, self.rows))
            if len(ids) >= self.batch:
                break
        return ids


@dataclass
class Write(Workload):
    """A write-only workload. Creates an id,value table, and writes rows with
    sequential primary keys and the given value size, in the given batch size
    (INSERT INTO write (id, value) VALUES ...). The number of rows written is
    given by Runner.count * Write.batch."""

    NAME: ClassVar[str] = "write"
    ABOUT: ClassVar[str] = "A write-only workload writing sequential rows"

    size: int
    """Row value size (excluding primary key)."""
    batch: int
    """Number of rows to write in a single insert query."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-s", "--size", type=_u64, default=64, help="Row value size (excluding primary key).")
        parser.add_argument("-b", "--batch", type=_u64, default=1, help="Number of rows to write in a single insert query.")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Write":
        return cls(size=args.size, batch=args.batch)

    def __str__(self) -> str:
        return f"write (size={self.size} batch={self.batch})"

    def prepare(self, client: Client, rng: random.Random) -> None:
        client.execute("BEGIN")
        client.execute('DROP TABLE IF EXISTS "write"')
        client.execute('CREATE TABLE "write" (id INT PRIMARY KEY, value STRING NOT NULL)')
        client.execute("COMMIT")

    def generate(self, rng: random.Random) -> "WriteGenerator":
        return WriteGenerator(next_id=1, size=self.size, batch=self.batch, rng=rng)

    def execute(self, client: Client, item: list[tuple[int, str]]) -> None:
        batch_size = len(item)
        values = ", ".join(f"({id}, '{value}')" for id, value in item)
        match client.execute(f'INSERT INTO "write" (id, value) VALUES {values}'):
            case StatementResult.Insert(count):
                assert_eq(count, batch_size, "Unexpected row count")
            case _:
                panic("Unexpected result")

    def verify(self, client: Client, txns: int) -> None:
        count = client.execute('SELECT COUNT(*) FROM "write"').try_into_i64()
        assert_eq(count, txns * self.batch, "Unexpected row count")


@dataclass
class WriteGenerator(Iterator[list[tuple[int, str]]]):
    """A Write workload generator, yielding batches of sequential primary keys
    and random rows."""

    next_id: int
    size: int
    batch: int
    rng: random.Random

    def __next__(self) -> list[tuple[int, str]]:
        rows: list[tuple[int, str]] = []
        while len(rows) < self.batch:
            rows.append((self.next_id, alphanumeric(self.rng, self.size)))
            self.next_id += 1
        return rows


@dataclass
class Bank(Workload):
    """A bank workload. Creates a set of customers and accounts, and makes
    random transfers between them. Specifically, it picks two random customers
    A and B, and then finds A's highest-balance account and B's lowest-balance
    account, and transfers a random amount without overdrawing the account.
    This somewhat convoluted scheme is used to make the workload slightly less
    trivial, including joins, ordering, and secondary indexes."""

    NAME: ClassVar[str] = "bank"
    ABOUT: ClassVar[str] = "A bank workload, making transfers between customer accounts"

    customers: int
    """Number of customers."""
    accounts: int
    """Number of accounts per customer."""
    balance: int
    """Initial account balance."""
    max_transfer: int
    """Max amount to transfer."""

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("-c", "--customers", type=_u64, default=100, help="Number of customers.")
        parser.add_argument("-a", "--accounts", type=_u64, default=10, help="Number of accounts per customer.")
        parser.add_argument("-b", "--balance", type=_u64, default=100, help="Initial account balance.")
        parser.add_argument("-m", "--max-transfer", type=_u64, default=50, help="Max amount to transfer.")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "Bank":
        return cls(
            customers=args.customers,
            accounts=args.accounts,
            balance=args.balance,
            max_transfer=args.max_transfer,
        )

    def __str__(self) -> str:
        return f"bank (customers={self.customers} accounts={self.accounts})"

    def prepare(self, client: Client, rng: random.Random) -> None:
        client.execute("BEGIN")
        client.execute("DROP TABLE IF EXISTS account")
        client.execute("DROP TABLE IF EXISTS customer")
        client.execute(
            """CREATE TABLE customer (
                    id INTEGER PRIMARY KEY,
                    name STRING NOT NULL
                )"""
        )
        client.execute(
            """CREATE TABLE account (
                    id INTEGER PRIMARY KEY,
                    customer_id INTEGER NOT NULL INDEX REFERENCES customer,
                    balance INTEGER NOT NULL
                )"""
        )
        names = (
            " ".join(
                [
                    rng.choice(PETNAME_ADVERBS),
                    rng.choice(PETNAME_ADJECTIVES),
                    rng.choice(PETNAME_NOUNS),
                ]
            )
            for _ in range(self.customers)
        )
        customers = ", ".join(f"({id}, '{name}')" for id, name in enumerate(names, start=1))
        client.execute(f"INSERT INTO customer VALUES {customers}")
        accounts = ", ".join(
            f"({(c - 1) * self.accounts + a}, {c}, {self.balance})"
            for c in range(1, self.customers + 1)
            for a in range(1, self.accounts + 1)
        )
        client.execute(f"INSERT INTO account VALUES {accounts}")
        client.execute("COMMIT")

    def generate(self, rng: random.Random) -> Iterator[tuple[int, int, int]]:
        """Yields from,to,amount transfers. Generates random u64s, then picks
        random from,to,amount as the remainder of the max customer and
        amount."""
        while True:
            a, b, c = rng.getrandbits(64), rng.getrandbits(64), rng.getrandbits(64)
            transfer = (a % self.customers + 1, b % self.customers + 1, c % self.max_transfer + 1)
            if transfer[0] != transfer[1]:
                yield transfer

    def execute(self, client: Client, item: tuple[int, int, int]) -> None:
        from_, to, amount = item

        client.execute("BEGIN")

        row = client.execute(
            f"""SELECT a.id, a.balance
                        FROM account a JOIN customer c ON a.customer_id = c.id
                        WHERE c.id = {from_}
                        ORDER BY a.balance DESC
                        LIMIT 1"""
        ).try_into_row()
        values = iter(row)
        from_account = next(values).try_into_i64()
        from_balance = next(values).try_into_i64()
        amount = min(amount, from_balance)

        to_account = client.execute(
            f"""SELECT a.id, a.balance
                        FROM account a JOIN customer c ON a.customer_id = c.id
                        WHERE c.id = {to}
                        ORDER BY a.balance ASC
                        LIMIT 1"""
        ).try_into_i64()

        client.execute(f"UPDATE account SET balance = balance - {amount} WHERE id = {from_account}")
        client.execute(f"UPDATE account SET balance = balance + {amount} WHERE id = {to_account}")

        client.execute("COMMIT")

    def verify(self, client: Client, txns: int) -> None:
        balance = client.execute("SELECT SUM(balance) FROM account").try_into_i64()
        assert_eq(balance, self.customers * self.accounts * self.balance)
        negative = client.execute("SELECT COUNT(*) FROM account WHERE balance < 0").try_into_i64()
        assert_eq(negative, 0)


# A small petname word list, standing in for the `petname` crate's default
# word lists.
PETNAME_ADVERBS: list[str] = [
    "abnormally", "absolutely", "accurately", "actively", "actually", "adequately",
    "amazingly", "badly", "barely", "boldly", "briefly", "brightly", "busily",
    "calmly", "carefully", "certainly", "cheaply", "clearly", "closely", "correctly",
    "curiously", "daily", "deeply", "directly", "eagerly", "early", "easily",
    "evenly", "exactly", "fairly", "firmly", "freely", "gently", "gladly",
    "greatly", "happily", "hardly", "highly", "honestly", "humbly", "jointly",
    "kindly", "largely", "lively", "loudly", "mainly", "merely", "mildly",
    "mostly", "namely", "nearly", "neatly", "newly", "nicely", "openly",
    "partly", "politely", "promptly", "properly", "quickly", "quietly", "rapidly",
    "rarely", "really", "safely", "sharply", "simply", "slowly", "smoothly",
    "solely", "strongly", "surely", "swiftly", "truly", "vastly", "warmly",
    "weekly", "wholly", "widely", "wildly", "wisely",
]  # fmt: skip
PETNAME_ADJECTIVES: list[str] = [
    "able", "active", "adapted", "alert", "amazed", "amused", "apt", "awake",
    "aware", "balanced", "big", "blessed", "bold", "brave", "bright", "brief",
    "busy", "calm", "capable", "careful", "charmed", "cheerful", "civil", "clean",
    "clear", "clever", "close", "cool", "cosmic", "crack", "credible", "crisp",
    "cuddly", "curious", "darling", "decent", "deep", "delicate", "direct", "divine",
    "driven", "eager", "easy", "enabled", "endless", "enormous", "equal", "exact",
    "fair", "famous", "fancy", "fast", "feasible", "fine", "firm", "fit", "fluent",
    "flying", "fond", "frank", "free", "fresh", "full", "funny", "gentle", "giving",
    "glad", "golden", "good", "grand", "great", "growing", "handy", "happy",
    "hardy", "healthy", "helped", "heroic", "honest", "hopeful", "humane", "ideal",
    "immense", "innocent", "intense", "joint", "just", "keen", "kind", "large",
    "legal", "liberal", "lively", "living", "loved", "loving", "loyal", "lucky",
    "magical", "main", "major", "massive", "mature", "measured", "merry", "mighty",
    "modern", "moved", "musical", "mutual", "natural", "neat", "nice", "noble",
    "normal", "notable", "novel", "obliging", "open", "optimal", "patient",
    "perfect", "pleasant", "polite", "precious", "present", "pretty", "prime",
    "proper", "proud", "quick", "quiet", "rapid", "rare", "ready", "real", "regular",
    "relaxed", "relevant", "renewed", "resolved", "rich", "robust", "romantic",
    "safe", "secure", "sensible", "settled", "sharp", "shining", "simple", "smart",
    "smooth", "social", "solid", "sound", "special", "splendid", "stable", "steady",
    "still", "strong", "sunny", "super", "sure", "sweet", "swift", "tender",
    "tidy", "touched", "true", "trusty", "united", "up", "upward", "useful",
    "valid", "vast", "verified", "vital", "warm", "welcome", "wise", "worthy",
]  # fmt: skip
PETNAME_NOUNS: list[str] = [
    "aardvark", "airedale", "akita", "albacore", "alien", "alpaca", "anchovy",
    "anemone", "ant", "anteater", "antelope", "ape", "aphid", "arachnid", "baboon",
    "badger", "barnacle", "basilisk", "bass", "bat", "beagle", "bear", "beetle",
    "bengal", "bird", "bison", "blowfish", "bluegill", "boa", "bobcat", "bonefish",
    "boxer", "bream", "buck", "buffalo", "bug", "bull", "bulldog", "bunny",
    "buzzard", "calf", "camel", "canary", "caribou", "cat", "catfish", "cattle",
    "chamois", "cheetah", "chicken", "chimp", "chipmunk", "cicada", "clam",
    "cobra", "cod", "collie", "colt", "condor", "corgi", "cougar", "cow", "coyote",
    "crab", "crane", "crawdad", "cricket", "crow", "cub", "dane", "deer", "dingo",
    "dodo", "doe", "dog", "dolphin", "donkey", "dory", "dove", "dragon", "drake",
    "duck", "eagle", "eel", "egret", "elephant", "elk", "emu", "falcon", "fawn",
    "ferret", "finch", "fish", "flamingo", "flea", "fly", "foal", "fowl", "fox",
    "frog", "gannet", "gar", "gator", "gazelle", "gecko", "gelding", "gibbon",
    "giraffe", "gnat", "gnu", "goat", "goblin", "goldfish", "goose", "gopher",
    "gorilla", "grackle", "griffon", "grizzly", "grouper", "grouse", "grub",
    "guinea", "gull", "guppy", "haddock", "halibut", "hamster", "hare", "hawk",
    "hedgehog", "hen", "heron", "hippo", "hog", "hornet", "horse", "hound",
    "husky", "hyena", "ibex", "iguana", "impala", "insect", "jackal", "jaguar",
    "jay", "jennet", "joey", "kangaroo", "katydid", "kid", "killdeer", "kingfish",
    "kit", "kite", "kitten", "kiwi", "koala", "lab", "lamb", "lark", "leech",
    "lemming", "lemur", "leopard", "liger", "lion", "lizard", "llama", "lobster",
    "locust", "longhorn", "loon", "louse", "lynx", "macaque", "macaw", "magpie",
    "mako", "malamute", "mallard", "mammal", "mammoth", "manatee", "mantis",
    "marlin", "marmoset", "marmot", "marten", "martin", "mastiff", "mayfly",
    "meerkat", "midge", "mink", "minnow", "mole", "mollusk", "molly", "monarch",
    "mongoose", "monkey", "moose", "moth", "mouse", "mudfish", "mule", "mullet",
    "muskox", "mustang", "mutt", "narwhal", "newt", "octopus", "opossum", "orca",
    "oriole", "osprey", "ostrich", "otter", "owl", "ox", "oyster", "panda",
    "panther", "parakeet", "parrot", "peacock", "pegasus", "pelican", "penguin",
    "perch", "pheasant", "pig", "pigeon", "piglet", "pika", "pipefish", "piranha",
    "platypus", "polecat", "pony", "poodle", "porpoise", "possum", "prawn",
    "primate", "pug", "puma", "pup", "python", "quagga", "quail", "rabbit",
    "raccoon", "racer", "ram", "raptor", "rat", "raven", "redbird", "redfish",
    "reindeer", "reptile", "rhino", "robin", "rodent", "rooster", "sailfish",
    "salmon", "satyr", "sawfish", "scorpion", "seagull", "seahorse", "seal",
    "serval", "shad", "shark", "sheep", "shepherd", "shiner", "shrew", "shrimp",
    "silkworm", "skink", "skunk", "skylark", "sloth", "slug", "snail", "snake",
    "snapper", "snipe", "sole", "sparrow", "spider", "sponge", "squid", "squirrel",
    "stag", "stallion", "starfish", "starling", "stingray", "stinkbug", "stork",
    "sturgeon", "sunbeam", "sunbird", "sunfish", "swan", "swift", "swine",
    "tadpole", "tahr", "tapir", "tarpon", "teal", "termite", "terrier", "tetra",
    "thrush", "tick", "tiger", "titmouse", "toad", "tomcat", "tortoise", "toucan",
    "treefrog", "trout", "tuna", "turkey", "turtle", "unicorn", "urchin",
    "vervet", "viper", "vulture", "wahoo", "walleye", "walrus", "warthog", "wasp",
    "weasel", "weevil", "werewolf", "whale", "whippet", "wildcat", "wolf",
    "wombat", "woodcock", "worm", "wren", "yak", "yeti", "zebra",
]  # fmt: skip


if __name__ == "__main__":
    main()
