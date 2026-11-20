"""Tests for Raft nodes. Mirrors the tests in `src/raft/node.rs`.

Most Raft tests are Goldenscripts under tests/raft/testscripts.
"""

import dataclasses
import io
import json
import tempfile
import unittest
import uuid
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from tests import goldenscript
from tests.goldenscript import CommandParser, Context, arg
from tests.raft import test_state as teststate
from tests.raft.test_state import Channel, KVCommand, KVResponse
from tests.storage.test_engine import Mirror
from toydb import raft
from toydb.encoding import bincode, serde
from toydb.error import Error, assert_eq
from toydb.fmt import debug, debug_option
from toydb.ops import Bound
from toydb.raft.log import Entry, Index, Key
from toydb.raft.message import Envelope, Message, Request, RequestID, Response
from toydb.raft.node import Node, NodeID, Options, RawNodeFollower, Term, Ticks
from toydb.storage.bitcask import BitCask
from toydb.storage.memory import Memory

# Don't fsync in tests (like `cfg!(test)` in Rust).
BitCask.FSYNC = False


def test_goldenscript(path: Path) -> None:
    """Runs the goldenscript tests in tests/raft/testscripts/node."""
    runner = TestRunner()
    try:
        goldenscript.run(runner, path)
    finally:
        runner.close()


def new_noop(id: NodeID, peers: set[NodeID]) -> RawNodeFollower:
    """Creates a noop node, with a noop state machine and transport."""
    log = raft.Log(Memory())
    state = teststate.Noop()
    tx: Channel[Envelope] = Channel()
    return RawNodeFollower.new(id, peers, log, state, tx, Options())


class RawNodeTests(unittest.TestCase):
    def test_quorum_size(self) -> None:
        """Tests RawNode.quorum_size() and cluster_size()."""
        cases = {1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4, 8: 5}
        for size, expect in cases.items():
            with self.subTest(size=size):
                node = new_noop(1, set(range(2, size + 1)))
                self.assertEqual(node.cluster_size(), size)
                self.assertEqual(node.quorum_size(), expect)

    def test_quorum_value(self) -> None:
        """Tests RawNode.quorum_value()."""
        cases = [
            ([1], 1),
            ([1, 3, 2], 2),
            ([4, 1, 3, 2], 2),
            ([1, 1, 1, 2, 2], 1),
            ([1, 1, 2, 2, 2], 2),
        ]
        for values, expect in cases:
            with self.subTest(values=values):
                size = len(values)
                node = new_noop(1, set(range(2, size + 1)))
                self.assertEqual(node.cluster_size(), size)
                self.assertEqual(node.quorum_value(values), expect)

    def test_goldenscripts(self) -> None:
        """Runs the Raft node goldenscripts."""
        scripts = Path(__file__).resolve().parent / "testscripts/node"
        for path in sorted(p for p in scripts.iterdir() if p.is_file()):
            with self.subTest(path=path):
                test_goldenscript(path)


# Test helpers for Node (an `impl Node` block in the Rust tests).


def dismantle(node: Node) -> tuple[raft.Log, raft.State]:
    return (node.node.log, node.node.state)


def get_applied_index(node: Node) -> Index:
    return node.node.state.get_applied_index()


def get_commit_index(node: Node) -> tuple[Index, Term]:
    return node.node.log.get_commit_index()


def get_last_index(node: Node) -> tuple[Index, Term]:
    return node.node.log.get_last_index()


def get_term_vote(node: Node) -> tuple[Term, NodeID | None]:
    return node.node.log.get_term_vote()


def options(node: Node) -> Options:
    return dataclasses.replace(node.node.opts)


def peers(node: Node) -> set[NodeID]:
    return set(node.node.peers)


def read(node: Node, command: bytes) -> bytes:
    return node.node.state.read(command)


def scan_log(node: Node) -> list[Entry]:
    return list(node.node.log.scan((Bound.Unbounded(), Bound.Unbounded())))


def envelope_from_json(raw: str) -> Envelope:
    """Parses a message envelope from JSON, in the format that `serde_json`
    uses for the Rust types. Unknown fields are ignored."""
    value = json.loads(raw)
    [(name, fields)] = value["message"].items()
    variant = getattr(Message, name, None)
    if not (isinstance(variant, type) and issubclass(variant, Message)):
        raise ValueError(f"unknown variant `{name}`")
    if name == "Append":
        fields["entries"] = [
            Entry(e["index"], e["term"], None if e["command"] is None else bytes(e["command"]))
            for e in fields["entries"]
        ]
    elif name in ("ClientRequest", "ClientResponse"):
        raise ValueError(f"unsupported variant `{name}`")
    message = variant(**{f.name: fields[f.name] for f in dataclasses.fields(variant)})
    return Envelope(from_=value["from"], term=value["term"], to=value["to"], message=message)


class Command(ABC):
    """Commands accepted by the TestRunner."""


@dataclass
class Campaign(Command):
    """Campaigns nodes."""

    _0: list[NodeID] = arg(optional=True)
    """The node IDs, or empty to select all nodes."""


@dataclass
class Cluster(Command):
    """Creates a Raft cluster."""

    nodes: int = arg(key=True)
    """The number of nodes to create."""
    leader: NodeID | None = arg(key=True)
    """The node to promote as leader."""
    heartbeat_interval: Ticks | None = arg(key=True)
    """The heartbeat interval in ticks."""
    election_timeout: Ticks | None = arg(key=True)
    """The fixed election timeout in ticks."""
    max_append_entries: int | None = arg(key=True)
    """The maximum entries per append message."""


@dataclass
class Deliver(Command):
    """Delivers pending messages to nodes."""

    from_: NodeID | None = arg(key="from")
    """The sender to deliver messages from, or None to deliver
    from all senders."""
    ids: list[NodeID] = arg(optional=True)
    """The recipient node IDs, or empty to deliver to all nodes."""


@dataclass
class Get(Command):
    """Sends a read request to a node."""

    _0: NodeID
    """The node ID."""
    _1: str
    """The key to fetch."""


@dataclass
class Heal(Command):
    """Heals partitions for nodes."""

    _0: list[NodeID] = arg(optional=True)
    """The node IDs, or empty to heal all nodes."""


@dataclass
class Heartbeat(Command):
    """Sends heartbeats from nodes."""

    _0: list[NodeID] = arg(optional=True)
    """The node IDs, or empty to select all nodes."""


@dataclass
class Log(Command):
    """Displays node logs."""

    _0: list[NodeID] = arg(optional=True)
    """The node IDs, or empty to select all nodes."""


@dataclass
class Partition(Command):
    """Partitions nodes from the cluster."""

    _0: list[NodeID]
    """The node IDs to partition."""


@dataclass
class Put(Command):
    """Sends a write request to a node."""

    _0: NodeID
    """The node ID."""
    _1: list[tuple[str, str]]
    """The key/value pair to write. Only 1 is allowed."""


@dataclass
class Restart(Command):
    """Restarts nodes."""

    applied_index: Index | None = arg(key=True)
    """The state machine index to restore."""
    commit_index: Index | None = arg(key=True)
    """The log commit index to restore."""
    ids: list[NodeID] = arg(optional=True)
    """The node IDs, or empty to restart all nodes."""


@dataclass
class Stabilize(Command):
    """Stabilizes nodes by repeatedly delivering messages
    until no more messages are pending."""

    heartbeat: bool = arg(key=True, optional=True)
    """Whether to send a final leader heartbeat."""
    ids: list[NodeID] = arg(optional=True)
    """The node IDs, or empty to stabilize all nodes."""


@dataclass
class State(Command):
    """Displays node state machines."""

    _0: list[NodeID] = arg(optional=True)
    """The node IDs, or empty for all nodes."""


@dataclass
class Status(Command):
    """Displays node status."""

    request: bool = arg(key=True, optional=True)
    """Whether to fetch status through a client request."""
    ids: list[NodeID] = arg(optional=True)
    """The node IDs, or empty for all nodes."""


@dataclass
class Step(Command):
    """Steps a message on a node."""

    _0: NodeID
    """The node ID."""
    _1: str
    """The serialized message."""


@dataclass
class Tick(Command):
    """Ticks nodes."""

    _0: list[NodeID] = arg(optional=True)
    """The node IDs, or empty for all nodes."""


class TestRunner(goldenscript.Runner[Command]):
    """Runs Raft goldenscript tests. See run() for available commands."""

    __test__ = False  # not a unittest/pytest test class

    PARSER: ClassVar[CommandParser[Command]] = CommandParser.derive(Command)

    def __init__(self) -> None:
        self.ids: list[NodeID] = []
        """IDs of all cluster nodes, in order."""
        self.nodes: dict[NodeID, Node] = {}
        """The cluster nodes, keyed by node ID."""
        self.nodes_rx: dict[NodeID, Channel[Envelope]] = {}
        """Outbound send queues from each node."""
        self.nodes_pending: dict[NodeID, list[Envelope]] = {}
        """Inbound receive queues to each node, to be stepped."""
        self.applied_rx: dict[NodeID, Channel[Entry]] = {}
        """Applied log entries for each node, after state machine application."""
        self.disconnected: dict[NodeID, set[NodeID]] = {}
        """Network partitions (sender → receivers). A symmetric (bidirectional)
        partition needs an entry from each side."""
        self.requests: dict[RequestID, Request] = {}
        """In-flight client requests."""
        self.next_request_id = 1
        """The request ID to use for the next client request."""
        self.tempdir = tempfile.TemporaryDirectory(prefix="toydb")
        """Temporary directory (deleted when closed)."""
        self._bitcasks: list[BitCask] = []

    def close(self) -> None:
        """Closes the log engines and removes the temporary directory."""
        for bitcask in self._bitcasks:
            bitcask.close()
        self.tempdir.cleanup()

    def parse_command(self, command: goldenscript.Command) -> Command:
        return self.PARSER.try_from(command)

    def run(self, command: Command, context: Context) -> str:
        """Runs a goldenscript command."""
        output = io.StringIO()
        match command:
            case Campaign(ids):
                ids = self.validate_ids_or_all(ids)
                self.campaign(ids, output)

            case Cluster(nodes, leader, heartbeat_interval, election_timeout, max_append_entries):
                opts = Options()
                if heartbeat_interval is not None:
                    opts.heartbeat_interval = heartbeat_interval
                if election_timeout is not None:
                    opts.election_timeout_range = range(election_timeout, election_timeout + 1)
                if max_append_entries is not None:
                    opts.max_append_entries = max_append_entries
                self.cluster(nodes, leader, opts, output)

            case Deliver(from_, ids):
                ids = self.validate_ids_or_all(ids)
                self.deliver(ids, from_, output)

            case Get(id, key):
                request = Request.Read(KVCommand.Get(key).encode())
                self.request(id, request, output)

            case Heal(ids):
                ids = self.validate_ids_or_all(ids)
                self.heal(ids, output)

            case Heartbeat(ids):
                ids = self.validate_ids_or_all(ids)
                self.heartbeat(ids, output)

            case Log(ids):
                ids = self.validate_ids_or_all(ids)
                self.log(ids, output)

            case Partition(ids):
                self.validate_ids(ids)
                self.partition(ids, output)

            case Put(id, values):
                if len(values) != 1:
                    raise ValueError("must specify one key/value pair")
                [(key, value)] = values
                request = Request.Write(KVCommand.Put(key, value).encode())
                self.request(id, request, output)

            case Restart(applied_index, commit_index, ids):
                ids = self.validate_ids_or_all(ids)
                self.restart(ids, commit_index, applied_index, output)

            case Stabilize(heartbeat, ids):
                ids = self.validate_ids_or_all(ids)
                self.stabilize(ids, heartbeat, output)

            case State(ids):
                ids = self.validate_ids_or_all(ids)
                self.state(ids, output)

            case Status(request, ids):
                ids = self.validate_ids_or_all(ids)
                if request:
                    if len(ids) != 1:
                        raise ValueError("request=true requires 1 node ID")
                    self.request(ids[0], Request.Status(), output)
                else:
                    self.status(ids, output)

            case Step(id, raw):
                msg = envelope_from_json(raw)
                self.transition(id, lambda n: n.step(msg), output)

            case Tick(ids):
                ids = self.validate_ids_or_all(ids)
                for id in ids:
                    self.transition(id, lambda n: n.tick(), output)

            case _:
                raise TypeError(f"unknown command {command!r}")
        return output.getvalue()

    def add_node(self, id: NodeID, peers: set[NodeID], opts: Options) -> None:
        """Creates a new empty node and inserts it."""
        # Use both a BitCask and a Memory engine, and mirror operations
        # across them, for added engine test coverage.
        path = Path(self.tempdir.name) / f"{id}.log"
        bitcask = BitCask.new(path)
        self._bitcasks.append(bitcask)
        memory = Memory()
        engine = Mirror(bitcask, memory)
        log = raft.Log(engine)
        state = teststate.KV()
        self.add_node_with(id, peers, log, state, opts)

    def add_node_with(
        self, id: NodeID, peers: set[NodeID], log: raft.Log, state: raft.State, opts: Options
    ) -> None:
        """Creates a new node with the given log and state and inserts it."""
        node_rx: Channel[Envelope] = Channel()
        applied_rx: Channel[Entry] = Channel()
        state = teststate.Emit(state, applied_rx)
        self.nodes[id] = Node.new(id, peers, log, state, node_rx, opts)
        self.nodes_rx[id] = node_rx
        self.nodes_pending[id] = []
        # Replacing a channel drops (disconnects) the previous one.
        if (old_applied_rx := self.applied_rx.get(id)) is not None:
            old_applied_rx.close()
        self.applied_rx[id] = applied_rx
        self.disconnected[id] = set()

    def campaign(self, ids: list[NodeID], output: io.StringIO) -> None:
        """Transitions nodes to candidates and campaign in a new term."""

        def campaign(node: Node) -> Node:
            match node:
                case Node.Candidate(candidate):
                    candidate.campaign()
                    return candidate.into()
                case Node.Follower(follower):
                    return follower.into_candidate().into()
                case Node.Leader(leader):
                    term = leader.term()
                    return leader.into_follower(term + 1).into_candidate().into()
                case _:
                    raise TypeError(f"unknown node {node!r}")

        for id in ids:
            self.transition(id, campaign, output)

    def cluster(
        self, nodes: int, leader: NodeID | None, opts: Options, output: io.StringIO
    ) -> None:
        """Creates a Raft cluster."""
        if self.ids:
            raise ValueError("cluster already exists")
        if nodes == 0:
            raise ValueError("cluster can't have 0 nodes")

        self.ids = list(range(1, nodes + 1))

        for id in self.ids:
            peers = {i for i in self.ids if i != id}
            self.add_node(id, peers, dataclasses.replace(opts))

        # Promote leader if requested. Suppress output.
        if leader is not None:
            quiet = io.StringIO()
            node = self.nodes.pop(leader, None)
            if not isinstance(node, Node.Follower):
                raise ValueError(f"invalid leader {leader}")
            self.nodes[leader] = node.node.into_candidate().into_leader().into()
            self.receive(leader, quiet)
            self.stabilize(self.ids, True, quiet)

        # Drain any initial applied entries.
        for applied_rx in self.applied_rx.values():
            applied_rx.try_iter()

        # Output final cluster status.
        self.status(self.ids, output)

    def deliver(self, ids: list[NodeID], from_: NodeID | None, output: io.StringIO) -> int:
        """Delivers pending messages to the given nodes. If from is given, only
        delivers messages from that node. Returns the number of delivered
        messages."""
        # Take a snapshot of the pending queues before delivering any
        # messages. This avoids outbound messages in response to delivery
        # being delivered to higher node IDs in the same loop, which can
        # give unintuitive results.
        step: list[Envelope] = []
        for id in ids:
            pending = self.nodes_pending.pop(id, None)
            if pending is None:
                raise ValueError(f"unknown node {id}")
            deliver = [msg for msg in pending if from_ is None or from_ == msg.from_]
            requeue = [msg for msg in pending if not (from_ is None or from_ == msg.from_)]
            self.nodes_pending[id] = requeue
            step.extend(deliver)

        delivered = len(step)
        for msg in step:
            self.transition(msg.to, lambda node, msg=msg: node.step(msg), output)
        return delivered

    def heal(self, ids: list[NodeID], output: io.StringIO) -> None:
        """Heals the given partitioned nodes, restoring connectivity with all
        other nodes."""
        for id in ids:
            self.disconnected[id] = set()
            for peers in self.disconnected.values():
                peers.discard(id)
        output.write(self.format_disconnected(self.disconnected))

    def heartbeat(self, ids: list[NodeID], output: io.StringIO) -> None:
        """Emits a heartbeat from the given leader nodes."""
        for id in ids:
            node = self.nodes.get(id)
            if not isinstance(node, Node.Leader):
                raise ValueError(f"{id} is not a leader")
            node.node.heartbeat()
            self.receive(id, output)

    def log(self, ids: list[NodeID], output: io.StringIO) -> None:
        """Outputs the current log contents for the given nodes."""
        for id in ids:
            node = self.get_node(id)
            nodefmt = self.format_node(node)
            last_index, last_term = get_last_index(node)
            commit_index, commit_term = get_commit_index(node)
            term, vote = get_term_vote(node)
            output.write(
                f"{nodefmt} term={term} last={last_index}@{last_term} "
                f"commit={commit_index}@{commit_term} vote={debug_option(vote)}\n"
            )
            for entry in scan_log(node):
                output.write(f"{nodefmt} entry {self.format_entry(entry)}\n")

    def partition(self, ids: list[NodeID], output: io.StringIO) -> None:
        """Partitions the given nodes from all other nodes in the cluster
        (bidirectionally). The given nodes can communicate with each other
        unless they were previously partitioned."""
        partitioned = set(ids)
        for id in sorted(partitioned):
            for peer in self.ids:
                if peer not in partitioned:
                    self.disconnected.setdefault(id, set()).add(peer)
                    self.disconnected.setdefault(peer, set()).add(id)
        output.write(self.format_disconnected(self.disconnected))

    def receive(self, id: NodeID, output: io.StringIO) -> int:
        """Receives outbound messages from a node, prints them, and queues them
        for delivery. Returns the number of received messages."""
        rx = self.nodes_rx.get(id)
        if rx is None:
            raise ValueError(f"unknown node {id}")
        count = 0
        for msg in rx.try_iter():
            count += 1
            from_, term, to = msg.from_, msg.term, msg.to  # simplify formatting
            msgfmt = self.format_message(msg.message)

            # If the peer is disconnected, drop the message and output it.
            if msg.to in self.disconnected[msg.from_]:
                output.write(f"n{from_}@{term} ⇥ n{to} {self.format_strikethrough(msgfmt)}\n")
                continue

            # Intercept and output client responses.
            if msg.from_ == msg.to:
                if not isinstance(msg.message, Message.ClientResponse):
                    raise ValueError(f"invalid self-addressed message: {debug(msg)}")
                output.write(f"n{from_}@{term} → c{to} {msgfmt}\n")
                request = self.requests.pop(msg.message.id, None)
                if request is None:
                    raise ValueError("unknown request id")
                output.write(
                    f"c{to}@{term} {self.format_request(request)} ⇒ "
                    f"{self.format_response(msg.message.response)}\n"
                )
                continue

            # Output the message and queue it for delivery.
            output.write(f"n{from_}@{term} → n{to} {msgfmt}\n")
            if msg.to not in self.nodes_pending:
                raise ValueError(f"unknown node {to}")
            self.nodes_pending[msg.to].append(msg)
        return count

    def request(self, id: NodeID, request: Request, output: io.StringIO) -> None:
        """Submits a client request via the given node."""
        request_id = uuid.UUID(int=self.next_request_id)
        self.next_request_id += 1
        self.requests[request_id] = request

        term = self.get_node(id).term()
        msg = Envelope(
            from_=id, to=id, term=term, message=Message.ClientRequest(request_id, request)
        )
        output.write(f"c{id}@{term} → n{id} {self.format_message(msg.message)}\n")
        self.transition(id, lambda n: n.step(msg), output)

    def restart(
        self,
        ids: list[NodeID],
        commit_index: Index | None,
        applied_index: Index | None,
        output: io.StringIO,
    ) -> None:
        """Restarts the given nodes. If commit_index or applied_index are
        given, the log commit index or state machine will regress."""
        for id in ids:
            node = self.nodes.pop(id, None)
            if node is None:
                raise ValueError(f"unknown node {id}")
            node_peers = peers(node)
            opts = options(node)
            log, state = dismantle(node)
            log = raft.Log(log.engine)  # reset log

            # If requested, regress the commit index.
            if commit_index is not None:
                if commit_index > log.get_commit_index()[0]:
                    raise ValueError(f"commit_index={commit_index} beyond current")
                entry = log.get(commit_index)
                if entry is not None:
                    commit_term = entry.term
                elif commit_index == 0:
                    commit_term = 0
                else:
                    raise ValueError(f"unknown commit_index={commit_index}")
                log.engine.set(
                    Key.CommitIndex().encode(),
                    bincode.serialize(
                        (commit_index, commit_term), serde.Tuple(serde.U64, serde.U64)
                    ),
                )
                # Reset the log again.
                log = raft.Log(log.engine)

            # If requested, wipe the state machine and reapply up to the
            # requested applied index.
            if applied_index is not None:
                if applied_index > log.get_commit_index()[0]:
                    raise ValueError(f"applied_index={applied_index} beyond commit")
                state = teststate.KV()
                for entry in log.scan((Bound.Unbounded(), Bound.Included(applied_index))):
                    try:
                        state.apply(entry)
                    except Error:
                        pass  # apply errors are returned to client
                assert_eq(state.get_applied_index(), applied_index, "wrong applied index")

            # Add node, and run a noop transition to output applied entries.
            self.add_node_with(id, node_peers, log, state, opts)
            self.transition(id, lambda n: n, output)
        # Output restarted node status.
        self.status(ids, output)

    def stabilize(self, ids: list[NodeID], heartbeat: bool, output: io.StringIO) -> None:
        """Stabilizes the given nodes by repeatedly delivering pending messages
        until no new messages are generated. If heartbeat is true, leaders
        then emit a heartbeat and restabilize again, e.g. to propagate the
        commit index."""
        while self.deliver(ids, None, output) > 0:
            pass
        # If requested, heartbeat the current leader (with the highest
        # term) and re-stabilize the nodes.
        if heartbeat:
            by_term = sorted(self.nodes.values(), key=lambda n: n.term(), reverse=True)
            leader = next((n for n in by_term if isinstance(n, Node.Leader)), None)
            if leader is not None:
                self.heartbeat([leader.id()], output)
                self.stabilize(ids, False, output)

    def state(self, ids: list[NodeID], output: io.StringIO) -> None:
        """Outputs the current state machine for the given nodes."""
        for id in ids:
            node = self.get_node(id)
            nodefmt = self.format_node(node)
            applied_index = get_applied_index(node)
            raw = read(node, KVCommand.Scan().encode())
            response = KVResponse.decode(raw)
            if not isinstance(response, KVResponse.Scan):
                raise ValueError("unexpected scan response")
            output.write(f"{nodefmt} applied={applied_index}\n")
            for key, value in response.kvs.items():
                output.write(f"{nodefmt} state {key}={value}\n")

    def status(self, ids: list[NodeID], output: io.StringIO) -> None:
        """Outputs status for the given nodes."""
        for id in ids:
            node = self.get_node(id)
            last_index, last_term = get_last_index(node)
            commit_index, commit_term = get_commit_index(node)
            applied_index = get_applied_index(node)
            output.write(
                f"{self.format_node_role(node)} last={last_index}@{last_term} "
                f"commit={commit_index}@{commit_term} applied={applied_index}"
            )
            if isinstance(node, Node.Leader):
                progress = " ".join(
                    f"{id}:{pr.match_index}→{pr.next_index}"
                    for id, pr in sorted(node.node.role.progress.items())
                )
                output.write(f" progress={{{progress}}}")
            output.write("\n")

    def transition(self, id: NodeID, f: Callable[[Node], Node], output: io.StringIO) -> None:
        """Applies a node transition (typically a step or tick), and outputs
        relevant changes."""
        node = self.nodes.pop(id, None)
        if node is None:
            raise ValueError(f"unknown node {id}")

        # Fetch pre-transition info.
        old_noderole = self.format_node_role(node)
        old_commit_index, _ = get_commit_index(node)
        old_entries = scan_log(node)

        # Apply the transition.
        node = f(node)

        # Fetch post-transition info.
        nodefmt = self.format_node(node)
        noderole = self.format_node_role(node)
        commit_index, commit_term = get_commit_index(node)

        entries = scan_log(node)
        skip = 0
        while (
            skip < len(entries)
            and skip < len(old_entries)
            and entries[skip].term == old_entries[skip].term
        ):
            skip += 1
        appended = entries[skip:]

        self.nodes[id] = node

        # Output relevant changes.
        if old_noderole != noderole:
            output.write(f"{old_noderole} ⇨ {noderole}\n")
        for entry in appended:
            output.write(f"{nodefmt} append {self.format_entry(entry)}\n")
        if old_commit_index != commit_index:
            output.write(f"{nodefmt} commit {commit_index}@{commit_term}\n")
        for entry in self.applied_rx[id].try_iter():
            output.write(f"{nodefmt} apply {self.format_entry(entry)}\n")

        # Receive any outbound messages.
        self.receive(id, output)

    def get_node(self, id: NodeID) -> Node:
        """Fetches a node, or errors if it doesn't exist."""
        node = self.nodes.get(id)
        if node is None:
            raise ValueError(f"unknown node {id}")
        return node

    def validate_ids(self, ids: list[NodeID]) -> None:
        """Validates node IDs, by checking that they exist in the cluster."""
        for id in ids:
            if id not in self.nodes:
                raise ValueError(f"unknown node {id}")

    def validate_ids_or_all(self, ids: list[NodeID]) -> list[NodeID]:
        """Validates node IDs, or returns all node IDs if none were given."""
        self.validate_ids(ids)
        if not ids:
            return list(self.ids)
        return list(ids)

    @staticmethod
    def format_disconnected(disconnected: dict[NodeID, set[NodeID]]) -> str:
        """Formats network partitions."""
        # Return early if the cluster is fully connected.
        if all(not peers for peers in disconnected.values()):
            return "{} fully connected\n".format(" ".join(f"n{id}" for id in sorted(disconnected)))

        output = ""

        # Separate symmetric and asymmetric partitions.
        symmetric: dict[NodeID, set[NodeID]] = {}
        asymmetric: dict[NodeID, set[NodeID]] = {}
        for id, peers in sorted(disconnected.items()):
            for peer in sorted(peers):
                if id in disconnected[peer]:
                    symmetric.setdefault(id, set()).add(peer)
                else:
                    asymmetric.setdefault(id, set()).add(peer)

        # Anchor the symmetric partitions at the node with the largest number
        # of disconnects, otherwise the smallest (first) ID.
        for id, peers in [(id, sorted(peers)) for id, peers in symmetric.items()]:
            for peer in peers:
                # Recompute the peer set sizes for each iteration, since we
                # modify the peer set below.
                size = len(symmetric.get(id, ()))
                peer_size = len(symmetric.get(peer, ()))
                # If this peer set is the smallest (or we're the higher ID),
                # remove the entry. We may no longer be in the map.
                if (size < peer_size or size == peer_size and id > peer) and id in symmetric:
                    symmetric[id].discard(peer)
                    if not symmetric[id]:
                        del symmetric[id]

        # The values (sets) correspond to the RHS of a partition. Let's
        # group the LHS of the partition as well, from smallest to largest,
        # separately for symmetric and asymmetric partitions. The list
        # contains (LHS, RHS, symmetric) groupings for each partition.
        grouped: list[tuple[set[NodeID], set[NodeID], bool]] = []
        partitions = [(id, peers, True) for id, peers in symmetric.items()]
        partitions += [(id, peers, False) for id, peers in asymmetric.items()]
        for id, peers, symm in sorted(partitions, key=lambda p: (p[0], not p[2])):
            # Look for an existing LHS group with the same RHS, and insert
            # this node into it. Otherwise, create a new LHS group.
            for lhs, rhs, s in grouped:
                if peers == rhs and symm == s:
                    lhs.add(id)
                    break
            else:
                grouped.append(({id}, peers, symm))

        # Display the groups.
        for lhs, rhs, symm in grouped:
            fmtlhs = " ".join(f"n{id}" for id in sorted(lhs))
            sep = "⇹" if symm else "⇥"
            fmtrhs = " ".join(f"n{id}" for id in sorted(rhs))
            output += f"{fmtlhs} {sep} {fmtrhs}\n"

        return output

    @staticmethod
    def format_entry(entry: Entry) -> str:
        """Formats an entry."""
        command = "None" if entry.command is None else str(KVCommand.decode(entry.command))
        return f"{entry.index}@{entry.term} {command}"

    @staticmethod
    def format_message(msg: Message) -> str:
        """Formats a message."""
        match msg:
            case Message.Campaign(last_index, last_term):
                return f"Campaign last={last_index}@{last_term}"
            case Message.CampaignResponse(vote):
                return f"CampaignResponse vote={'true' if vote else 'false'}"
            case Message.Heartbeat(last_index, commit_index, read_seq):
                return (
                    f"Heartbeat last_index={last_index} commit_index={commit_index} "
                    f"read_seq={read_seq}"
                )
            case Message.HeartbeatResponse(match_index, read_seq):
                return f"HeartbeatResponse match_index={match_index} read_seq={read_seq}"
            case Message.Append(base_index, base_term, entries):
                ent = " ".join(f"{e.index}@{e.term}" for e in entries)
                return f"Append base={base_index}@{base_term} [{ent}]"
            case Message.AppendResponse(match_index, reject_index):
                match (match_index, reject_index):
                    case (0, 0):
                        raise AssertionError("match_index and reject_index both 0")
                    case (match_index, 0):
                        return f"AppendResponse match_index={match_index}"
                    case (0, reject_index):
                        return f"AppendResponse reject_index={reject_index}"
                    case _:
                        raise AssertionError("match_index and reject_index both set")
            case Message.Read(seq):
                return f"Read seq={seq}"
            case Message.ReadResponse(seq):
                return f"ReadResponse seq={seq}"
            case Message.ClientRequest(id, request):
                match request:
                    case Request.Read(v):
                        fmtrequest = f"read 0x{v.hex()}"
                    case Request.Write(v):
                        fmtrequest = f"write 0x{v.hex()}"
                    case _:
                        fmtrequest = "status"
                return f"ClientRequest id=0x{_trim_start(id.hex, '00')} {fmtrequest}"
            case Message.ClientResponse(id, response):
                match response:
                    case Response.Read(v):
                        fmtresponse = f"read 0x{v.hex()}"
                    case Response.Write(v):
                        fmtresponse = f"write 0x{v.hex()}"
                    case Response.Status(v):
                        fmtresponse = f"status {debug(v)}"
                    case Error():
                        fmtresponse = f"Error::{response.fmt_debug(pretty=True)}"
                    case _:
                        raise TypeError(f"unknown response {response!r}")
                return f"ClientResponse id=0x{_trim_start(id.hex, '00')} {fmtresponse}"
            case _:
                raise TypeError(f"unknown message {msg!r}")

    @staticmethod
    def format_node(node: Node) -> str:
        """Formats a node identifier."""
        return f"n{node.id()}@{node.term()}"

    @staticmethod
    def format_node_role(node: Node) -> str:
        """Formats a node identifier with role."""
        match node:
            case Node.Candidate():
                role = "candidate"
            case Node.Follower(follower):
                leader = follower.role.leader
                role = f"follower({'' if leader is None else f'n{leader}'})"
            case Node.Leader():
                role = "leader"
            case _:
                raise TypeError(f"unknown node {node!r}")
        return f"{TestRunner.format_node(node)} {role}"

    @staticmethod
    def format_request(request: Request) -> str:
        """Formats a request."""
        match request:
            case Request.Read(c) | Request.Write(c):
                return str(KVCommand.decode(c))
            case _:
                return "status"

    @staticmethod
    def format_response(response: Response | Error) -> str:
        """Formats a response."""
        match response:
            case Response.Read(r) | Response.Write(r):
                return str(KVResponse.decode(r))
            case Response.Status(status):
                return debug(status, pretty=True)
            case Error():
                return f"Error::{debug(response)} ({response})"
            case _:
                raise TypeError(f"unknown response {response!r}")

    @staticmethod
    def format_strikethrough(s: str) -> str:
        """Strike-through formats the given string using a Unicode combining stroke."""
        return "".join(c + "̶" for c in s)


def _trim_start(s: str, prefix: str) -> str:
    """Removes all repetitions of a prefix, like Rust's `trim_start_matches`."""
    while prefix and s.startswith(prefix):
        s = s.removeprefix(prefix)
    return s


if __name__ == "__main__":
    unittest.main()
