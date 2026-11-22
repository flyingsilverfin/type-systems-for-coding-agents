"""The Raft log. Mirrors `src/raft/log.rs`."""

import collections.abc
from dataclasses import dataclass
from typing import ClassVar

from toydb import encoding, storage
from toydb.encoding import bincode, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Panic
from toydb.fmt import debug, debug_tuple
from toydb.ops import Bound, Range

# NB: `node` defines these aliases before it imports this module in turn.
from toydb.raft.node import NodeID, Term

Index = int
"""A log index (entry position). Starts at 1. 0 indicates no index."""

_U64_MAX = (1 << 64) - 1

_TERM_VOTE: serde.Serde[tuple[int, int | None]] = serde.Tuple(serde.U64, serde.Option(serde.U8))  # type: ignore[assignment]
"""The value of `Key.TermVote`: a `(Term, Option<NodeID>)` tuple."""

_COMMIT_INDEX: serde.Serde[tuple[int, int]] = serde.Tuple(serde.U64, serde.U64)  # type: ignore[assignment]
"""The value of `Key.CommitIndex`: an `(Index, Term)` tuple."""


@dataclass(frozen=True)
class Entry(encoding.Value):
    """A log entry containing a state machine command."""

    index: Index
    """The entry index.

    We could omit the index in the encoded value, since it's also stored in
    the key, but we keep it simple."""
    term: Term
    """The term in which the entry was added."""
    command: bytes | None
    """The state machine command. None (noop) commands are used during leader
    election to commit old entries, see section 5.4.2 in the Raft paper."""

    def __repr__(self) -> str:
        return debug(self)

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u64(self.index)
        serializer.serialize_u64(self.term)
        serde.Option(serde.Bytes).serialize(self.command, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Entry":
        deserializer.deserialize_struct()
        return cls(
            index=deserializer.deserialize_u64(),
            term=deserializer.deserialize_u64(),
            command=serde.Option(serde.Bytes).deserialize(deserializer),
        )


class Key(encoding.Key):
    """A log storage key."""

    Entry: ClassVar[type["KeyEntry"]]
    TermVote: ClassVar[type["TermVote"]]
    CommitIndex: ClassVar[type["CommitIndex"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case KeyEntry(index):
                serializer.serialize_newtype_variant(0)
                serializer.serialize_u64(index)
            case TermVote():
                serializer.serialize_unit_variant(1)
            case CommitIndex():
                serializer.serialize_unit_variant(2)
            case _:
                raise TypeError(f"unknown key {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Key":
        match deserializer.deserialize_enum():
            case 0:
                return KeyEntry(deserializer.deserialize_u64())
            case 1:
                return TermVote()
            case 2:
                return CommitIndex()
            case index:
                raise deserializer.invalid_variant(index, 3)


@dataclass(frozen=True)
class KeyEntry(Key):
    """A log entry, storing the term and command. This is `Key::Entry` in
    Rust: use it as `Key.Entry`, since the bare name is taken by `Entry`."""

    index: Index

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Entry", [self.index], pretty)


@dataclass(frozen=True)
class TermVote(Key):
    """Stores the current term and vote (if any)."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return "TermVote"


@dataclass(frozen=True)
class CommitIndex(Key):
    """Stores the current commit index (if any)."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return "CommitIndex"


Key.Entry = KeyEntry
Key.TermVote = TermVote
Key.CommitIndex = CommitIndex


class Log:
    """The Raft log stores a sequence of arbitrary commands (typically writes) that
    are replicated across nodes and applied sequentially to the local state
    machine. Each entry contains an index, command, and the term in which the
    leader proposed it. Commands may be noops (None), which are added when a
    leader is elected (see section 5.4.2 in the Raft paper). For example:

    Index | Term | Command
    ------|------|------------------------------------------------------
      1   |   1  | None
      2   |   1  | CREATE TABLE table (id INT PRIMARY KEY, value STRING)
      3   |   1  | INSERT INTO table VALUES (1, 'foo')
      4   |   2  | None
      5   |   2  | UPDATE table SET value = 'bar' WHERE id = 1
      6   |   2  | DELETE FROM table WHERE id = 1

    Note that this is for illustration only, and the actual toyDB Raft commands
    are not SQL statements but lower-level write operations.

    A key/value store is used to store the log entries on disk, keyed by index,
    along with a few other metadata keys (e.g. who we voted for in this term).

    In the steady state, the log is append-only: when a client submits a
    command, the leader appends it to its own log (via `Log.append`) and
    replicates it to followers who append it to their logs (via `Log.splice`).
    When an index has been replicated to a majority of nodes it becomes
    committed, making the log immutable up to that index and guaranteeing that
    all nodes will eventually contain it. Nodes keep track of the commit index
    via `Log.commit` and apply committed commands to the state machine.

    However, uncommitted entries can be replaced or removed. A leader may append
    entries to its log, but then be unable to reach consensus on them (e.g.
    because it is unable to communicate with a majority of nodes). If a
    different leader is elected and writes different commands to those same
    indexes, then the uncommitted entries will be replaced with entries from the
    new leader once the old leader (or a follower) discovers it.

    The Raft log has the following invariants:

    * Entry indexes are contiguous starting at 1 (no index gaps).
    * Entry terms never decrease from the previous entry.
    * Entry terms are at or below the current term.
    * Appended entries are durable (flushed to disk).
    * Appended entries use the current term.
    * Committed entries are never changed or removed (no log truncation).
    * Committed entries will eventually be replicated to all nodes.
    * Entries with the same index/term contain the same command.
    * If two logs contain a matching index/term, all previous entries
      are identical (see section 5.3 in the Raft paper).
    """

    def __init__(self, engine: storage.Engine) -> None:
        """Initializes a log using the given storage engine."""
        self.engine = engine
        """The underlying storage engine."""

        # Load some initial in-memory state from disk.
        term, vote = 0, None
        if (value := engine.get(Key.TermVote().encode())) is not None:
            term, vote = bincode.deserialize(_TERM_VOTE, value)
        last_index, last_term = 0, 0
        last = None
        for last in engine.scan_dyn(
            (
                Bound.Included(Key.Entry(0).encode()),
                Bound.Included(Key.Entry(_U64_MAX).encode()),
            )
        ):
            pass
        if last is not None:
            entry = Entry.decode(last[1])
            last_index, last_term = entry.index, entry.term
        commit_index, commit_term = 0, 0
        if (value := engine.get(Key.CommitIndex().encode())) is not None:
            commit_index, commit_term = bincode.deserialize(_COMMIT_INDEX, value)

        self.term: Term = term
        """The current term."""
        self.vote: NodeID | None = vote
        """Our leader vote in the current term, if any."""
        self.last_index: Index = last_index
        """The index of the last stored entry."""
        self.last_term: Term = last_term
        """The term of the last stored entry."""
        self.commit_index: Index = commit_index
        """The index of the last committed entry."""
        self.commit_term: Term = commit_term
        """The term of the last committed entry."""
        self.fsync = True  # fsync by default
        """If true, fsync entries to disk when appended. This is mandated by Raft,
        but comes with a hefty performance penalty (especially since we don't
        optimize for it by batching entries before fsyncing). Disabling it will
        yield much better write performance, but may lose data on crashes, which
        in some scenarios can cause log entries to become "uncommitted" and
        state machines diverging."""

    def enable_fsync(self, fsync: bool) -> None:
        """Controls whether to fsync writes. Disabling this may violate Raft
        guarantees, see comment on fsync attribute."""
        self.fsync = fsync

    def get_commit_index(self) -> tuple[Index, Term]:
        """Returns the commit index and term."""
        return (self.commit_index, self.commit_term)

    def get_last_index(self) -> tuple[Index, Term]:
        """Returns the last log index and term."""
        return (self.last_index, self.last_term)

    def get_term_vote(self) -> tuple[Term, NodeID | None]:
        """Returns the current term (0 if none) and vote."""
        return (self.term, self.vote)

    def set_term_vote(self, term: Term, vote: NodeID | None) -> None:
        """Stores the current term and cast vote (if any). Enforces that the term
        does not regress, and that we only vote for one node in a term. append()
        will use this term, and splice() can't write entries beyond it."""
        assert term > 0, "can't set term 0"
        assert term >= self.term, f"term regression {self.term} → {term}"
        assert term > self.term or self.vote is None or vote == self.vote, "can't change vote"

        if term == self.term and vote == self.vote:
            return
        self.engine.set(Key.TermVote().encode(), bincode.serialize((term, vote), _TERM_VOTE))
        # Always fsync, even with Log.fsync = false. Term changes are rare, so
        # this doesn't materially affect performance, and double voting could
        # lead to multiple leaders and split brain which is really bad.
        self.engine.flush()
        self.term = term
        self.vote = vote

    def append(self, command: bytes | None) -> Index:
        """Appends a command to the log at the current term, and flushes it to
        disk, returning its index. None implies a noop command, typically after
        Raft leader changes."""
        assert self.term > 0, "can't append entry in term 0"
        entry = Entry(index=self.last_index + 1, term=self.term, command=command)
        self.engine.set(Key.Entry(entry.index).encode(), entry.encode())
        if self.fsync:
            self.engine.flush()
        self.last_index = entry.index
        self.last_term = entry.term
        return entry.index

    def commit(self, index: Index) -> Index:
        """Commits entries up to and including the given index. The index must
        exist and be at or after the current commit index."""
        entry = self.get(index)
        if entry is None:
            raise Panic(f"commit index {index} does not exist")
        if entry.index < self.commit_index:
            raise Panic(f"commit index regression {self.commit_index} → {entry.index}")
        if entry.index == self.commit_index:
            return index
        term = entry.term
        self.engine.set(Key.CommitIndex().encode(), bincode.serialize((index, term), _COMMIT_INDEX))
        # NB: the commit index doesn't need to be fsynced, since the entries
        # are fsynced and the commit index can be recovered from the quorum.
        self.commit_index = index
        self.commit_term = term
        return index

    def get(self, index: Index) -> Entry | None:
        """Fetches an entry at an index, or None if it does not exist."""
        value = self.engine.get(Key.Entry(index).encode())
        return Entry.decode(value) if value is not None else None

    def has(self, index: Index, term: Term) -> bool:
        """Checks if the log contains an entry with the given index and term."""
        # Fast path: check against last_index. This is the common case when
        # followers process appends or heartbeats.
        if index == 0 or index > self.last_index:
            return False
        if (index, term) == (self.last_index, self.last_term):
            return True
        entry = self.get(index)
        return entry is not None and entry.term == term

    def scan(self, range: Range[Index]) -> "Iterator":
        """Returns an iterator over log entries in the given index range."""
        start: Bound[bytes]
        end: Bound[bytes]
        match range[0]:
            case Bound.Excluded(index):
                start = Bound.Excluded(Key.Entry(index).encode())
            case Bound.Included(index):
                start = Bound.Included(Key.Entry(index).encode())
            case _:
                start = Bound.Included(Key.Entry(0).encode())
        match range[1]:
            case Bound.Excluded(index):
                end = Bound.Excluded(Key.Entry(index).encode())
            case Bound.Included(index):
                end = Bound.Included(Key.Entry(index).encode())
            case _:
                end = Bound.Included(Key.Entry(_U64_MAX).encode())
        return Iterator(self.engine.scan_dyn((start, end)))

    def scan_apply(self, applied_index: Index) -> "Iterator":
        """Returns an iterator over entries that are ready to apply, starting after
        the current applied index up to the commit index."""
        # NB: we don't assert that commit_index >= applied_index, because the
        # local commit index is not flushed to durable storage -- if lost on
        # restart, it can be recovered from the logs of a quorum.
        if applied_index >= self.commit_index:
            return Iterator(iter(()))
        return self.scan((Bound.Included(applied_index + 1), Bound.Included(self.commit_index)))

    def splice(self, entries: list[Entry]) -> Index:
        """Splices a set of entries into the log and flushes it to disk. New
        indexes will be appended. Overlapping indexes with the same term must be
        equal and will be ignored. Overlapping indexes with different terms will
        truncate the existing log at the first conflict and then splice the new
        entries.

        The entries must have contiguous indexes and equal/increasing terms, and
        the first entry must be in the range [1,last_index+1] with a term at or
        above the previous (base) entry's term and at or below the current term.
        """
        if not entries:
            return self.last_index  # empty input is noop
        first, last = entries[0], entries[-1]

        # Check that the entries are well-formed.
        assert first.index > 0 and first.term > 0, "spliced entry has index or term 0"
        assert all(
            a.index + 1 == b.index for a, b in zip(entries, entries[1:])
        ), "spliced entries are not contiguous"
        assert all(
            a.term <= b.term for a, b in zip(entries, entries[1:])
        ), "spliced entries have term regression"

        # Check that the entries connect to the existing log (if any), and that the
        # term doesn't regress.
        assert last.term <= self.term, f"splice term {last.term} beyond current {self.term}"
        base = self.get(first.index - 1)
        if base is not None:
            if first.term < base.term:
                raise Panic(f"splice term regression {base.term} → {first.term}")
        elif first.index != 1:
            raise Panic(f"first index {first.index} must touch existing log")

        # Skip entries that are already in the log.
        skip = 0
        for entry in self.scan((Bound.Included(first.index), Bound.Included(last.index))):
            # [skip] is ok, because the scan has the same size as entries.
            assert entry.index == entries[skip].index, f"index mismatch at {debug(entry)}"
            if entry.term != entries[skip].term:
                break
            assert entry.command == entries[skip].command, f"command mismatch at {debug(entry)}"
            skip += 1
        entries = entries[skip:]

        # If all entries already exist then we're done.
        if not entries:
            return self.last_index
        first = entries[0]

        # Write the entries that weren't already in the log, and remove the
        # tail of the old log if any. We can't write below the commit index,
        # since these entries must be immutable.
        assert first.index > self.commit_index, "spliced entries below commit index"

        for entry in entries:
            self.engine.set(Key.Entry(entry.index).encode(), entry.encode())
        for index in range(last.index + 1, self.last_index + 1):
            self.engine.delete(Key.Entry(index).encode())
        if self.fsync:
            self.engine.flush()

        self.last_index = last.index
        self.last_term = last.term
        return self.last_index

    def status(self) -> storage.Status:
        """Returns log engine status."""
        return self.engine.status()


class Iterator(collections.abc.Iterator[Entry]):
    """A log entry iterator."""

    def __init__(self, inner: collections.abc.Iterator[tuple[bytes, bytes]]) -> None:
        self.inner = inner

    def __next__(self) -> Entry:
        _, value = next(self.inner)
        return Entry.decode(value)
