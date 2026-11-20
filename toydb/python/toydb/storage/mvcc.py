"""This module implements MVCC (Multi-Version Concurrency Control), a widely
used method for ACID transactions and concurrency control. It allows
multiple concurrent transactions to access and modify the same dataset,
isolates them from each other, detects and handles conflicts, and commits
their writes atomically as a single unit. It uses an underlying storage
engine to store raw keys and values.

VERSIONS
========

MVCC handles concurrency control by managing multiple historical versions of
keys, identified by a timestamp. Every write adds a new version at a higher
timestamp, with deletes having a special tombstone value. For example, the
keys a,b,c,d may have the following values at various logical timestamps (x
is tombstone):

Time
5
4  a4
3      b3      x
2
1  a1      c1  d1
   a   b   c   d   Keys

A transaction t2 that started at T=2 will see the values a=a1, c=c1, d=d1. A
different transaction t5 running at T=5 will see a=a4, b=b3, c=c1.

toyDB uses logical timestamps with a sequence number stored in
Key::NextVersion. Each new read-write transaction takes its timestamp from
the current value of Key::NextVersion and then increments the value for the
next transaction.

ISOLATION
=========

MVCC provides an isolation level called snapshot isolation. Briefly,
transactions see a consistent snapshot of the database state as of their
start time. Writes made by concurrent or subsequent transactions are never
visible to it. If two concurrent transactions write to the same key they
will conflict and one of them must retry. A transaction's writes become
atomically visible to subsequent transactions only when they commit, and are
rolled back on failure. Read-only transactions never conflict with other
transactions.

Transactions write new versions at their timestamp, storing them as
Key::Version(key, version) => value. If a transaction writes to a key and
finds a newer version, it returns an error and the client must retry.

Active (uncommitted) read-write transactions record their version in the
active set, stored as Key::Active(version). When new transactions begin, they
take a snapshot of this active set, and any key versions that belong to a
transaction in the active set are considered invisible (to anyone except that
transaction itself). Writes to keys that already have a past version in the
active set will also return an error.

To commit, a transaction simply deletes its record in the active set. This
will immediately (and, crucially, atomically) make all of its writes visible
to subsequent transactions, but not ongoing ones. If the transaction is
cancelled and rolled back, it maintains a record of all keys it wrote as
Key::TxnWrite(version, key), so that it can find the corresponding versions
and delete them before removing itself from the active set.

Consider the following example, where we have two ongoing transactions at
time T=2 and T=5, with some writes that are not yet committed marked in
parentheses.

Active set: [2, 5]

Time
5 (a5)
4  a4
3      b3      x
2         (x)     (e2)
1  a1      c1  d1
   a   b   c   d   e   Keys

Here, t2 will see a=a1, d=d1, e=e2 (it sees its own writes). t5 will see
a=a5, b=b3, c=c1. t2 does not see any newer versions, and t5 does not see
the tombstone at c@2 nor the value e=e2, because version=2 is in its active
set.

If t2 tries to write b=b2, it receives an error and must retry, because a
newer version exists. Similarly, if t5 tries to write e=e5, it receives an
error and must retry, because the version e=e2 is in its active set.

To commit, t2 can remove itself from the active set. A new transaction t6
starting after the commit will then see c as deleted and e=e2. t5 will still
not see any of t2's writes, because it's still in its local snapshot of the
active set at the time it began.

READ-ONLY AND TIME TRAVEL QUERIES
=================================

Since MVCC stores historical versions, it can trivially support time travel
queries where a transaction reads at a past timestamp and has a consistent
view of the database at that time.

This is done by a transaction simply using a past version, as if it had
started far in the past, ignoring newer versions like any other transaction.
This transaction cannot write, as it does not have a unique timestamp (the
original read-write transaction originally owned this timestamp).

The only wrinkle is that the time-travel query must also know what the active
set was at that version. Otherwise, it may see past transactions that committed
after that time, which were not visible to the original transaction that wrote
at that version. Similarly, if a time-travel query reads at a version that is
still active, it should not see its in-progress writes, and after it commits
a different time-travel query should not see those writes either, to maintain
version consistency.

To achieve this, every read-write transaction stores its active set snapshot
in the storage engine as well, as Key::TxnActiveSnapshot, such that later
time-travel queries can restore its original snapshot. Furthermore, a
time-travel query can only see versions below the snapshot version, otherwise
it could see spurious in-progress or since-committed versions.

In the following example, a time-travel query at version=3 would see a=a1,
c=c1, d=d1.

Time
5
4  a4
3      b3      x
2
1  a1      c1  d1
   a   b   c   d   Keys

Read-only queries work similarly to time-travel queries, with one exception:
they read at the next (current) version, i.e. Key::NextVersion, and use the
current active set, storing the snapshot in memory only. Read-only queries
do not increment the version sequence number in Key::NextVersion.

GARBAGE COLLECTION
==================

Normally, old versions would be garbage collected regularly, when they are
no longer needed by active transactions or time-travel queries. However,
toyDB does not implement garbage collection, instead keeping all history
forever, both out of laziness and also because it allows unlimited time
travel queries (it's a feature, not a bug!).

Mirrors `src/storage/mvcc.rs`.
"""

import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar

from toydb import encoding
from toydb.encoding import bincode, keycode, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error, errdata, errinput
from toydb.fmt import debug, debug_tuple
from toydb.ops import Bound, Range
from toydb.storage import engine as storage_engine
from toydb.storage.engine import Engine

T = TypeVar("T")
E = TypeVar("E", bound=Engine)

Version = int
"""An MVCC version represents a logical timestamp. Each version belongs to a
separate read/write transaction. The latest version is incremented when a
new read-write transaction begins. A `u64` in Rust."""

VERSION_MAX: Version = 2**64 - 1
"""The largest possible version, `u64::MAX`."""


class Mutex(Generic[T]):
    """A value protected by a mutex, like Rust's `Arc<Mutex<T>>`. The value
    is accessed by locking it:

        with mutex.lock() as value:
            ...
    """

    def __init__(self, value: T) -> None:
        self._value = value
        self._lock = threading.Lock()

    @contextmanager
    def lock(self) -> Iterator[T]:
        """Locks the mutex and yields the value. The mutex is released when
        the `with` block exits. It is not reentrant."""
        with self._lock:
            yield self._value


class Key(encoding.Key):
    """MVCC keys, using the Keycode encoding which preserves the ordering and
    grouping of keys."""

    NextVersion: ClassVar[type["NextVersion"]]
    TxnActive: ClassVar[type["TxnActive"]]
    TxnActiveSnapshot: ClassVar[type["TxnActiveSnapshot"]]
    TxnWrite: ClassVar[type["TxnWrite"]]
    Version: ClassVar[type["KeyVersion"]]
    Unversioned: ClassVar[type["Unversioned"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case NextVersion():
                serializer.serialize_unit_variant(0)
            case TxnActive(version):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_u64(version)
            case TxnActiveSnapshot(version):
                serializer.serialize_newtype_variant(2)
                serializer.serialize_u64(version)
            case TxnWrite(version, key):
                serializer.serialize_tuple_variant(3)
                serializer.serialize_u64(version)
                serializer.serialize_bytes(key)
            case KeyVersion(key, version):
                serializer.serialize_tuple_variant(4)
                serializer.serialize_bytes(key)
                serializer.serialize_u64(version)
            case Unversioned(key):
                serializer.serialize_newtype_variant(5)
                serializer.serialize_bytes(key)
            case _:
                raise TypeError(f"unknown key {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Key":
        match deserializer.deserialize_enum():
            case 0:
                return NextVersion()
            case 1:
                return TxnActive(deserializer.deserialize_u64())
            case 2:
                return TxnActiveSnapshot(deserializer.deserialize_u64())
            case 3:
                return TxnWrite(deserializer.deserialize_u64(), deserializer.deserialize_bytes())
            case 4:
                return KeyVersion(deserializer.deserialize_bytes(), deserializer.deserialize_u64())
            case 5:
                return Unversioned(deserializer.deserialize_bytes())
            case index:
                raise deserializer.invalid_variant(index, 6)


@dataclass(frozen=True)
class NextVersion(Key):
    """The next available version."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return "NextVersion"


@dataclass(frozen=True)
class TxnActive(Key):
    """Active (uncommitted) transactions by version."""

    version: Version

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("TxnActive", [self.version], pretty)


@dataclass(frozen=True)
class TxnActiveSnapshot(Key):
    """A snapshot of the active set at each version. Only written for
    versions where the active set is non-empty (excluding itself)."""

    version: Version

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("TxnActiveSnapshot", [self.version], pretty)


@dataclass(frozen=True)
class TxnWrite(Key):
    """Keeps track of all keys written to by an active transaction (identified
    by its version), in case it needs to roll back."""

    version: Version
    key: bytes

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("TxnWrite", [self.version, self.key], pretty)


@dataclass(frozen=True)
class KeyVersion(Key):
    """A versioned key/value pair. This is `Key::Version` in Rust: use it as
    `Key.Version`, since the bare name is taken by the `Version` type."""

    key: bytes
    version: Version

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Version", [self.key, self.version], pretty)


@dataclass(frozen=True)
class Unversioned(Key):
    """Unversioned non-transactional key/value pairs, mostly used for metadata.
    These exist separately from versioned keys, i.e. the unversioned key
    "foo" is entirely independent of the versioned key "foo@7"."""

    key: bytes

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Unversioned", [self.key], pretty)


Key.NextVersion = NextVersion
Key.TxnActive = TxnActive
Key.TxnActiveSnapshot = TxnActiveSnapshot
Key.TxnWrite = TxnWrite
Key.Version = KeyVersion
Key.Unversioned = Unversioned


class KeyPrefix(encoding.Key):
    """MVCC key prefixes, for prefix scans. These must match the keys above,
    including the enum variant index."""

    NextVersion: ClassVar[type["KeyPrefixNextVersion"]]
    TxnActive: ClassVar[type["KeyPrefixTxnActive"]]
    TxnActiveSnapshot: ClassVar[type["KeyPrefixTxnActiveSnapshot"]]
    TxnWrite: ClassVar[type["KeyPrefixTxnWrite"]]
    Version: ClassVar[type["KeyPrefixVersion"]]
    Unversioned: ClassVar[type["KeyPrefixUnversioned"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case KeyPrefixNextVersion():
                serializer.serialize_unit_variant(0)
            case KeyPrefixTxnActive():
                serializer.serialize_unit_variant(1)
            case KeyPrefixTxnActiveSnapshot():
                serializer.serialize_unit_variant(2)
            case KeyPrefixTxnWrite(version):
                serializer.serialize_newtype_variant(3)
                serializer.serialize_u64(version)
            case KeyPrefixVersion(key):
                serializer.serialize_newtype_variant(4)
                serializer.serialize_bytes(key)
            case KeyPrefixUnversioned():
                serializer.serialize_unit_variant(5)
            case _:
                raise TypeError(f"unknown key prefix {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "KeyPrefix":
        match deserializer.deserialize_enum():
            case 0:
                return KeyPrefixNextVersion()
            case 1:
                return KeyPrefixTxnActive()
            case 2:
                return KeyPrefixTxnActiveSnapshot()
            case 3:
                return KeyPrefixTxnWrite(deserializer.deserialize_u64())
            case 4:
                return KeyPrefixVersion(deserializer.deserialize_bytes())
            case 5:
                return KeyPrefixUnversioned()
            case index:
                raise deserializer.invalid_variant(index, 6)


@dataclass(frozen=True)
class KeyPrefixNextVersion(KeyPrefix):
    def fmt_debug(self, pretty: bool = False) -> str:
        return "NextVersion"


@dataclass(frozen=True)
class KeyPrefixTxnActive(KeyPrefix):
    def fmt_debug(self, pretty: bool = False) -> str:
        return "TxnActive"


@dataclass(frozen=True)
class KeyPrefixTxnActiveSnapshot(KeyPrefix):
    def fmt_debug(self, pretty: bool = False) -> str:
        return "TxnActiveSnapshot"


@dataclass(frozen=True)
class KeyPrefixTxnWrite(KeyPrefix):
    version: Version

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("TxnWrite", [self.version], pretty)


@dataclass(frozen=True)
class KeyPrefixVersion(KeyPrefix):
    key: bytes

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Version", [self.key], pretty)


@dataclass(frozen=True)
class KeyPrefixUnversioned(KeyPrefix):
    def fmt_debug(self, pretty: bool = False) -> str:
        return "Unversioned"


KeyPrefix.NextVersion = KeyPrefixNextVersion
KeyPrefix.TxnActive = KeyPrefixTxnActive
KeyPrefix.TxnActiveSnapshot = KeyPrefixTxnActiveSnapshot
KeyPrefix.TxnWrite = KeyPrefixTxnWrite
KeyPrefix.Version = KeyPrefixVersion
KeyPrefix.Unversioned = KeyPrefixUnversioned


class MVCC(Generic[E]):
    """An MVCC-based transactional key-value engine. It wraps an underlying storage
    engine that's used for raw key/value storage.

    While it supports any number of concurrent transactions, individual read or
    write operations are executed sequentially, serialized via a mutex. There
    are two reasons for this: the storage engine itself is not thread-safe,
    requiring serialized access, and the Raft state machine that manages the
    MVCC engine applies commands one at a time from the Raft log, which will
    serialize them anyway.
    """

    def __init__(self, engine: E) -> None:
        """Creates a new MVCC engine with the given storage engine."""
        self.engine: Mutex[E] = Mutex(engine)

    def begin(self) -> "Transaction[E]":
        """Begins a new read-write transaction."""
        return Transaction.begin(self.engine)

    def begin_read_only(self) -> "Transaction[E]":
        """Begins a new read-only transaction at the latest version."""
        return Transaction.begin_read_only(self.engine, None)

    def begin_as_of(self, version: Version) -> "Transaction[E]":
        """Begins a new read-only transaction as of the given version."""
        return Transaction.begin_read_only(self.engine, version)

    def resume(self, state: "TransactionState") -> "Transaction[E]":
        """Resumes a transaction from the given transaction state."""
        return Transaction.resume(self.engine, state)

    def get_unversioned(self, key: bytes) -> bytes | None:
        """Fetches the value of an unversioned key."""
        with self.engine.lock() as engine:
            return engine.get(Key.Unversioned(key).encode())

    def set_unversioned(self, key: bytes, value: bytes) -> None:
        """Sets the value of an unversioned key."""
        with self.engine.lock() as engine:
            engine.set(Key.Unversioned(key).encode(), value)

    def status(self) -> "Status":
        """Returns the status of the MVCC and storage engines."""
        with self.engine.lock() as engine:
            value = engine.get(Key.NextVersion().encode())
            versions = bincode.deserialize(serde.U64, value) - 1 if value is not None else 0
            active_txns = sum(1 for _ in engine.scan_prefix(KeyPrefix.TxnActive().encode()))
            return Status(versions=versions, active_txns=active_txns, storage=engine.status())


@dataclass(frozen=True)
class Status(encoding.Value):
    """MVCC engine status."""

    versions: int
    """The total number of MVCC versions (i.e. read-write transactions)."""
    active_txns: int
    """Number of currently active transactions."""
    storage: storage_engine.Status
    """The storage engine."""

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u64(self.versions)
        serializer.serialize_u64(self.active_txns)
        self.storage.serialize(serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Status":
        deserializer.deserialize_struct()
        return cls(
            versions=deserializer.deserialize_u64(),
            active_txns=deserializer.deserialize_u64(),
            storage=storage_engine.Status.deserialize(deserializer),
        )


@dataclass
class TransactionState(encoding.Value):
    """A Transaction's state, which determines its write version and isolation. It
    is separate from Transaction to allow it to be passed around independently
    of the engine. The main motivation for this is that it can be exported via
    Transaction.state(), (de)serialized, and later used to instantiate a new
    functionally equivalent Transaction via Transaction.resume(). This allows
    passing the transaction between the storage engine and SQL engine
    (potentially running on a different node) across the Raft state machine
    boundary.
    """

    version: Version
    """The version this transaction is running at. Only one read-write
    transaction can run at a given version, since this identifies its
    writes."""
    read_only: bool
    """If true, the transaction is read only."""
    active: set[Version] = field(default_factory=set)
    """The set of concurrent active (uncommitted) transactions, as of the start
    of this transaction. Their writes should be invisible to this
    transaction even if they're writing at a lower version, since they're
    not committed yet."""

    def is_visible(self, version: Version) -> bool:
        """Checks whether the given version is visible to this transaction.

        Future versions, and versions belonging to active transactions as of
        the start of this transaction, are never visible.

        Read-write transactions see their own writes at their version.

        Read-only queries only see versions below the transaction's version,
        excluding the version itself. This is to ensure time-travel queries see
        a consistent version both before and after any active transaction at
        that version commits its writes. See the module documentation for
        details.
        """
        if version in self.active:
            return False
        elif self.read_only:
            return version < self.version
        else:
            return version <= self.version

    def clone(self) -> "TransactionState":
        """Returns a copy of the state."""
        return TransactionState(self.version, self.read_only, set(self.active))

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u64(self.version)
        serializer.serialize_bool(self.read_only)
        ACTIVE.serialize(self.active, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "TransactionState":
        deserializer.deserialize_struct()
        return cls(
            version=deserializer.deserialize_u64(),
            read_only=deserializer.deserialize_bool(),
            active=ACTIVE.deserialize(deserializer),
        )


ACTIVE: serde.Serde[set[Version]] = serde.BTreeSet(serde.U64)
"""The encoding of an active set, a `BTreeSet<Version>`."""

VALUE: serde.Serde[bytes | None] = serde.Option(serde.Bytes)
"""The encoding of a versioned value, an `Option<Vec<u8>>` where None is a
deletion tombstone."""


class Transaction(Generic[E]):
    """An MVCC transaction."""

    def __init__(self, engine: Mutex[E], state: TransactionState) -> None:
        self.engine = engine
        """The underlying engine, shared by all transactions."""
        self._state = state
        """The transaction state."""

    @classmethod
    def begin(cls, engine: Mutex[E]) -> "Transaction[E]":
        """Begins a new transaction in read-write mode. This will allocate a new
        version that the transaction can write at, add it to the active set, and
        record its active snapshot for time-travel queries."""
        with engine.lock() as session:
            # Allocate a new version to write at.
            value = session.get(Key.NextVersion().encode())
            version = bincode.deserialize(serde.U64, value) if value is not None else 1
            session.set(Key.NextVersion().encode(), bincode.serialize(version + 1, serde.U64))

            # Fetch the current set of active transactions, persist it for
            # time-travel queries if non-empty, then add this txn to it.
            active = cls.scan_active(session)
            if active:
                session.set(
                    Key.TxnActiveSnapshot(version).encode(), bincode.serialize(active, ACTIVE)
                )
            session.set(Key.TxnActive(version).encode(), b"")

        return cls(engine, TransactionState(version=version, read_only=False, active=active))

    @classmethod
    def begin_read_only(cls, engine: Mutex[E], as_of: Version | None) -> "Transaction[E]":
        """Begins a new read-only transaction. If version is given it will see the
        state as of the beginning of that version (ignoring writes at that
        version). In other words, it sees the same state as the read-write
        transaction at that version saw when it began."""
        with engine.lock() as session:
            # Fetch the latest version.
            value = session.get(Key.NextVersion().encode())
            version = bincode.deserialize(serde.U64, value) if value is not None else 1

            # If requested, create the transaction as of a past version, restoring
            # the active snapshot as of the beginning of that version. Otherwise,
            # use the latest version and get the current, real-time snapshot.
            active: set[Version] = set()
            if as_of is not None:
                if as_of >= version:
                    raise errinput(f"version {as_of} does not exist")
                version = as_of
                value = session.get(Key.TxnActiveSnapshot(version).encode())
                if value is not None:
                    active = bincode.deserialize(ACTIVE, value)
            else:
                active = cls.scan_active(session)

        return cls(engine, TransactionState(version=version, read_only=True, active=active))

    @classmethod
    def resume(cls, engine: Mutex[E], s: TransactionState) -> "Transaction[E]":
        """Resumes a transaction from the given state."""
        # For read-write transactions, verify that the transaction is still
        # active before making further writes.
        if not s.read_only:
            with engine.lock() as session:
                if session.get(Key.TxnActive(s.version).encode()) is None:
                    raise errinput(f"no active transaction at version {s.version}")
        return cls(engine, s)

    @staticmethod
    def scan_active(session: Engine) -> set[Version]:
        """Fetches the set of currently active transactions."""
        active: set[Version] = set()
        for key, _ in session.scan_prefix(KeyPrefix.TxnActive().encode()):
            match Key.decode(key):
                case TxnActive(version):
                    active.add(version)
                case decoded:
                    raise errdata(f"expected TxnActive key, got {debug(decoded)}")
        return active

    def version(self) -> Version:
        """Returns the version the transaction is running at."""
        return self._state.version

    def read_only(self) -> bool:
        """Returns whether the transaction is read-only."""
        return self._state.read_only

    def state(self) -> TransactionState:
        """Returns the transaction's state. This can be used to instantiate a
        functionally equivalent transaction via resume()."""
        return self._state

    def commit(self) -> None:
        """Commits the transaction, by removing it from the active set. This will
        immediately make its writes visible to subsequent transactions. Also
        removes its TxnWrite records, which are no longer needed.

        NB: commit does not flush writes to durable storage, since we rely on
        the Raft log for persistence.
        """
        if self._state.read_only:
            return
        with self.engine.lock() as engine:
            prefix = KeyPrefix.TxnWrite(self._state.version).encode()
            remove = [key for key, _ in engine.scan_prefix(prefix)]
            for key in remove:
                engine.delete(key)
            engine.delete(Key.TxnActive(self._state.version).encode())

    def rollback(self) -> None:
        """Rolls back the transaction, by undoing all written versions and removing
        it from the active set. The active set snapshot is left behind, since
        this is needed for time travel queries at this version."""
        if self._state.read_only:
            return
        with self.engine.lock() as engine:
            rollback: list[bytes] = []
            prefix = KeyPrefix.TxnWrite(self._state.version).encode()
            for key, _ in engine.scan_prefix(prefix):
                match Key.decode(key):
                    case TxnWrite(_, userkey):
                        # the version
                        rollback.append(Key.Version(userkey, self._state.version).encode())
                    case decoded:
                        raise errdata(f"expected TxnWrite, got {debug(decoded)}")
                rollback.append(key)  # the TxnWrite record
            for key in rollback:
                engine.delete(key)
            engine.delete(Key.TxnActive(self._state.version).encode())  # remove from active set

    def delete(self, key: bytes) -> None:
        """Deletes a key."""
        self.write_version(key, None)

    def set(self, key: bytes, value: bytes) -> None:
        """Sets a value for a key."""
        self.write_version(key, value)

    def write_version(self, key: bytes, value: bytes | None) -> None:
        """Writes a new version for a key at the transaction's version. None writes
        a deletion tombstone. If a write conflict is found (either a newer or
        uncommitted version), a serialization error is returned.  Replacing our
        own uncommitted write is fine."""
        if self._state.read_only:
            raise Error.ReadOnly()
        with self.engine.lock() as engine:
            # Check for write conflicts, i.e. if the latest key is invisible to us
            # (either a newer version, or an uncommitted version in our past). We
            # can only conflict with the latest key, since all transactions enforce
            # the same invariant.
            start = Key.Version(
                key, min(self._state.active) if self._state.active else self._state.version + 1
            ).encode()
            end = Key.Version(key, VERSION_MAX).encode()
            last: tuple[bytes, bytes] | None = None
            for last in engine.scan((Bound.Included(start), Bound.Included(end))):
                pass
            if last is not None:
                match Key.decode(last[0]):
                    case KeyVersion(_, version):
                        if not self._state.is_visible(version):
                            raise Error.Serialization()
                    case decoded:
                        raise errdata(f"expected Key::Version got {debug(decoded)}")

            # Write the new version and its write record.
            #
            # NB: TxnWrite contains the provided user key, not the encoded engine
            # key, since we can construct the engine key using the version.
            engine.set(Key.TxnWrite(self._state.version, key).encode(), b"")
            engine.set(
                Key.Version(key, self._state.version).encode(), bincode.serialize(value, VALUE)
            )

    def get(self, key: bytes) -> bytes | None:
        """Fetches a key's value, or None if it does not exist."""
        with self.engine.lock() as engine:
            start = Key.Version(key, 0).encode()
            end = Key.Version(key, self._state.version).encode()
            scan = engine.scan((Bound.Included(start), Bound.Included(end))).rev()
            for versionkey, value in scan:
                match Key.decode(versionkey):
                    case KeyVersion(_, version):
                        if self._state.is_visible(version):
                            return bincode.deserialize(VALUE, value)
                    case decoded:
                        raise errdata(f"expected Key::Version got {debug(decoded)}")
            return None

    def scan(self, range: Range[bytes]) -> "ScanIterator[E]":
        """Returns an iterator over the latest visible key/value pairs at the
        transaction's version."""
        start: Bound[bytes]
        match range[0]:
            case Bound.Excluded(k):
                start = Bound.Excluded(Key.Version(k, VERSION_MAX).encode())
            case Bound.Included(k):
                start = Bound.Included(Key.Version(k, 0).encode())
            case _:
                start = Bound.Included(Key.Version(b"", 0).encode())
        end: Bound[bytes]
        match range[1]:
            case Bound.Excluded(k):
                end = Bound.Excluded(Key.Version(k, 0).encode())
            case Bound.Included(k):
                end = Bound.Included(Key.Version(k, VERSION_MAX).encode())
            case _:
                end = Bound.Excluded(KeyPrefix.Unversioned().encode())
        return ScanIterator(self.engine, self.state().clone(), (start, end))

    def scan_prefix(self, prefix: bytes) -> "ScanIterator[E]":
        """Scans keys under a given prefix."""
        # Normally, KeyPrefix::Version will only match all versions of the
        # exact given key. We want all keys maching the prefix, so we chop off
        # the Keycode byte slice terminator 0x0000 at the end.
        encoded = KeyPrefix.Version(prefix).encode()[:-2]
        range = keycode.prefix_range(encoded)
        return ScanIterator(self.engine, self.state().clone(), range)


class ScanIterator(Iterator[tuple[bytes, bytes]], Generic[E]):
    """An iterator over the latest live and visible key/value pairs for the txn.

    The (single-threaded) engine is shared via mutex, and holding the mutex for
    the lifetime of the iterator can cause deadlocks (e.g. when the local SQL
    engine pulls from two tables concurrently during a join). Instead, we pull
    and buffer a batch of rows at a time, and release the mutex in between.

    This does not support reverse scans, since the SQL layer doesn't currently
    need it.
    """

    BUFFER_SIZE: ClassVar[int] = 32
    """The number of live key/value pairs to pull from the engine each time we
    lock it. The tests use 2 to exercise the buffering code."""

    def __init__(self, engine: Mutex[E], txn: TransactionState, range: Range[bytes]) -> None:
        """Creates a new scan iterator."""
        self.engine = engine
        """The engine."""
        self.txn = txn
        """The transaction state."""
        self.buffer: deque[tuple[bytes, bytes]] = deque()
        """A buffer of live and visible key/value pairs to emit."""
        self.remainder: Range[bytes] | None = range
        """The remaining range after the buffer."""

    def clone(self) -> "ScanIterator[E]":
        """Returns an independent copy of the iterator, sharing the engine."""
        clone = ScanIterator(self.engine, self.txn.clone(), (Bound.Unbounded(), Bound.Unbounded()))
        clone.buffer = deque(self.buffer)
        clone.remainder = self.remainder
        return clone

    def fill_buffer(self) -> None:
        """Fills the buffer, if there's any pending items."""
        # Check if there's anything to buffer.
        if len(self.buffer) >= self.BUFFER_SIZE:
            return
        range = self.remainder
        if range is None:
            return
        self.remainder = None
        range_end = range[1]

        with self.engine.lock() as engine:
            iter = Peekable(VersionIterator(self.txn, engine.scan(range)))
            for key, _, value in iter:
                # If the next key equals this one, we're not at the latest version.
                peeked = iter.peek()
                if peeked is not None and peeked[0] == key:
                    continue

                # Decode the value, and skip deleted keys (tombstones).
                decoded = bincode.deserialize(VALUE, value)
                if decoded is None:
                    continue
                self.buffer.append((key, decoded))

                # If we filled the buffer, save the remaining range (if any) and
                # return. peek() has already buffered next(), so pull it.
                if len(self.buffer) == self.BUFFER_SIZE:
                    following = next(iter, None)
                    if following is not None:
                        # We have to re-encode it as a raw engine key, since we
                        # only have access to the decoded MVCC user key.
                        next_key, version, _ = following
                        range_start = Bound.Included(Key.Version(next_key, version).encode())
                        self.remainder = (range_start, range_end)
                    return

    def __next__(self) -> tuple[bytes, bytes]:
        if not self.buffer:
            self.fill_buffer()
        if not self.buffer:
            raise StopIteration
        return self.buffer.popleft()


class VersionIterator(Iterator[tuple[bytes, Version, bytes]]):
    """An iterator that decodes raw engine key/value pairs into MVCC key/value
    versions, and skips invisible versions. Helper for ScanIterator."""

    def __init__(self, txn: TransactionState, inner: storage_engine.ScanIterator) -> None:
        """Creates a new MVCC version iterator for the given engine iterator."""
        self.txn = txn
        """The transaction the scan is running in."""
        self.inner = inner
        """The inner engine scan iterator."""

    def try_next(self) -> tuple[bytes, Version, bytes] | None:
        """Returns the next visible key/version/value tuple, if any."""
        for key, value in self.inner:
            match Key.decode(key):
                case KeyVersion(userkey, version):
                    pass
                case decoded:
                    raise errdata(f"expected Key::Version got {debug(decoded)}")
            if not self.txn.is_visible(version):
                continue
            return userkey, version, value
        return None

    def __next__(self) -> tuple[bytes, Version, bytes]:
        item = self.try_next()
        if item is None:
            raise StopIteration
        return item


class Peekable(Iterator[T]):
    """An iterator that can look at the next item without consuming it, like
    Rust's `std::iter::Peekable`."""

    def __init__(self, iter: Iterator[T]) -> None:
        self.iter = iter
        self.peeked: list[T] = []

    def peek(self) -> T | None:
        """Returns the next item without consuming it, or None at the end."""
        if not self.peeked:
            for item in self.iter:
                self.peeked.append(item)
                break
        return self.peeked[0] if self.peeked else None

    def __next__(self) -> T:
        if self.peeked:
            return self.peeked.pop()
        return next(self.iter)
