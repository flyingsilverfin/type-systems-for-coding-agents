"""Raft nodes. Mirrors `src/raft/node.rs`."""

import itertools
import logging
import queue
import random
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar, overload

NodeID = int
"""A node ID, unique within a cluster. Assigned manually when started."""

Term = int
"""A leader term number. Increases monotonically on elections."""

Ticks = int
"""A logical clock interval as number of ticks."""

# NB: the aliases above are defined before these imports, since `log` and
# `message` import them from this module in turn.
from toydb.error import Error, Panic, assert_eq, assert_ne, errinput  # noqa: E402
from toydb.fmt import debug, debug_option  # noqa: E402
from toydb.ops import Bound  # noqa: E402
from toydb.raft import ELECTION_TIMEOUT_RANGE, HEARTBEAT_INTERVAL, MAX_APPEND_ENTRIES  # noqa: E402
from toydb.raft.log import Index, Log  # noqa: E402
from toydb.raft.message import (  # noqa: E402
    Envelope,
    Message,
    ReadSequence,
    Request,
    RequestID,
    Response,
    Status,
)
from toydb.raft.state import State  # noqa: E402

logger = logging.getLogger(__name__)


@dataclass
class Options:
    """Raft node options."""

    heartbeat_interval: Ticks = HEARTBEAT_INTERVAL
    """The number of ticks between leader heartbeats."""
    election_timeout_range: range = ELECTION_TIMEOUT_RANGE
    """The range of randomized election timeouts for followers and candidates."""
    max_append_entries: int = MAX_APPEND_ENTRIES
    """Maximum number of entries to send in a single Append message."""


class Node(ABC):
    """A Raft node with a dynamic role. This implements the Raft distributed
    consensus protocol, see the `raft` module documentation for more info.

    The node is driven synchronously by processing inbound messages via `step()`
    and by advancing time via `tick()`. These methods consume the node and
    return a new one with a possibly different role. Outbound messages are sent
    via the given `tx` queue, and must be delivered to peers or clients.

    This is the public interface to the node, with a closed set of roles. It
    wraps the `RawNode` types, which implement the actual node logic. It allows
    ergonomic use across role transitions since it can represent all roles,
    e.g.: `node = node.step(msg)`.
    """

    Candidate: ClassVar[type["NodeCandidate"]]
    Follower: ClassVar[type["NodeFollower"]]
    Leader: ClassVar[type["NodeLeader"]]

    node: "RawNodeCandidate | RawNodeFollower | RawNodeLeader"

    @classmethod
    def new(
        cls,
        id: NodeID,
        peers: set[NodeID],
        log: Log,
        state: State,
        tx: "queue.Queue[Envelope]",
        opts: Options,
    ) -> "Node":
        """Creates a new Raft node. It starts as a leaderless follower, waiting to
        hear from a leader or otherwise transitioning to candidate and
        campaigning for leadership. In the case of a single-node cluster (no
        peers), the node immediately transitions to leader when created."""
        node = RawNodeFollower.new(id, peers, log, state, tx, opts)
        # If this is a single-node cluster, become leader immediately.
        if node.cluster_size() == 1:
            return node.into_candidate().into_leader().into()
        return node.into()

    def id(self) -> NodeID:
        """Returns the node's ID."""
        match self:
            case NodeCandidate(node):
                return node.id
            case NodeFollower(node):
                return node.id
            case NodeLeader(node):
                return node.id
            case _:
                raise Panic(f"unknown node {self!r}")

    def term(self) -> Term:
        """Returns the node's term."""
        match self:
            case NodeCandidate(node):
                return node.term()
            case NodeFollower(node):
                return node.term()
            case NodeLeader(node):
                return node.term()
            case _:
                raise Panic(f"unknown node {self!r}")

    def step(self, msg: Envelope) -> "Node":
        """Processes an inbound message."""
        peers = self.node.peers
        assert_eq(msg.to, self.id(), f"message to other node: {debug(msg)}")
        assert msg.from_ in peers or msg.from_ == self.id(), f"unknown sender: {debug(msg)}"
        logger.debug("Stepping %r", msg)

        match self:
            case NodeCandidate(node):
                return node.step(msg)
            case NodeFollower(node):
                return node.step(msg)
            case NodeLeader(node):
                return node.step(msg)
            case _:
                raise Panic(f"unknown node {self!r}")

    def tick(self) -> "Node":
        """Advances time by a tick."""
        match self:
            case NodeCandidate(node):
                return node.tick()
            case NodeFollower(node):
                return node.tick()
            case NodeLeader(node):
                return node.tick()
            case _:
                raise Panic(f"unknown node {self!r}")


@dataclass(frozen=True)
class NodeCandidate(Node):
    """A candidate campaigns for leadership."""

    node: "RawNodeCandidate"


@dataclass(frozen=True)
class NodeFollower(Node):
    """A follower replicates entries from a leader."""

    node: "RawNodeFollower"


@dataclass(frozen=True)
class NodeLeader(Node):
    """A leader processes client requests and replicates entries to followers."""

    node: "RawNodeLeader"


Node.Candidate = NodeCandidate
Node.Follower = NodeFollower
Node.Leader = NodeLeader


class Role(ABC):
    """Marker base class for a Raft role: leader, follower, or candidate."""


R = TypeVar("R", bound=Role)


class RawNode(ABC, Generic[R]):
    """A Raft node with role R.

    This implements the typestate pattern, where individual node states (roles)
    are encoded as `RawNode[Role]`. See http://cliffle.com/blog/rust-typestate/.

    The role-specific methods (the Rust `impl RawNode<Follower>` blocks, etc.)
    are implemented by one subclass per role: `RawNodeFollower`,
    `RawNodeCandidate` and `RawNodeLeader`.
    """

    def __init__(
        self,
        id: NodeID,
        peers: set[NodeID],
        log: Log,
        state: State,
        tx: "queue.Queue[Envelope]",
        opts: Options,
        role: R,
    ) -> None:
        self.id = id
        """The node ID. Must be unique in the cluster."""
        self.peers = peers
        """The IDs of the other nodes in the cluster. Does not change while
        running. Can change on restart, but all nodes must have the same set of
        nodes, otherwise it can result in multiple leaders (split brain)."""
        self.log = log
        """The Raft log, which stores client commands to be executed."""
        self.state = state
        """The Raft state machine, which executes client commands from the log."""
        self.tx = tx
        """Queue for sending outbound messages to other nodes."""
        self.opts = opts
        """Node options."""
        self.role = role
        """Role-specific state."""

    @abstractmethod
    def into(self) -> Node:
        """Wraps the node in the `Node` variant for its role (the Rust
        `From<RawNode<R>> for Node` implementations)."""

    @overload
    def into_role(self, role: "Candidate") -> "RawNodeCandidate": ...

    @overload
    def into_role(self, role: "Follower") -> "RawNodeFollower": ...

    @overload
    def into_role(self, role: "Leader") -> "RawNodeLeader": ...

    def into_role(
        self, role: "Candidate | Follower | Leader"
    ) -> "RawNodeCandidate | RawNodeFollower | RawNodeLeader":
        """Helper for role transitions."""
        args = (self.id, self.peers, self.log, self.state, self.tx, self.opts)
        match role:
            case Candidate():
                return RawNodeCandidate(*args, role)
            case Follower():
                return RawNodeFollower(*args, role)
            case Leader():
                return RawNodeLeader(*args, role)
            case _:
                raise TypeError(f"unknown role {role!r}")

    def term(self) -> Term:
        """Returns the node's current term."""
        return self.log.get_term_vote()[0]

    def cluster_size(self) -> int:
        """Returns the cluster size as number of nodes."""
        return len(self.peers) + 1

    def quorum_size(self) -> int:
        """Returns the cluster quorum size (strict majority)."""
        return self.cluster_size() // 2 + 1

    def quorum_value(self, values: list[int]) -> int:
        """Returns the quorum value (i.e. median) of the given unsorted list. It
        must have the same length as the cluster size."""
        assert_eq(len(values), self.cluster_size(), "vector size must match cluster size")
        return sorted(values, reverse=True)[self.quorum_size() - 1]

    def random_election_timeout(self) -> Ticks:
        """Generates a random election timeout."""
        return random.choice(self.opts.election_timeout_range)

    def send(self, to: NodeID, message: Message) -> None:
        """Sends a message to the given recipient."""
        self.send_via(self.tx, Envelope(from_=self.id, to=to, term=self.term(), message=message))

    @staticmethod
    def send_via(tx: "queue.Queue[Envelope]", msg: Envelope) -> None:
        """Sends a message via the given queue."""
        logger.debug("Sending %r", msg)
        tx.put(msg)

    def broadcast(self, message: Message) -> None:
        """Broadcasts a message to all peers."""
        # Send in increasing ID order for test determinism.
        for id in sorted(self.peers):
            self.send(id, message)


@dataclass
class Follower(Role):
    """A follower replicates log entries from a leader and forwards client requests
    to it. Nodes start as leaderless followers, until they either discover a
    leader or hold an election."""

    leader: NodeID | None
    """The leader, or None if we're a leaderless follower."""
    election_timeout: Ticks
    """The leader_seen timeout before triggering an election."""
    leader_seen: Ticks = 0
    """The number of ticks since the last message from the leader."""
    forwarded: set[RequestID] = field(default_factory=set)
    """Local client requests that have been forwarded to the leader. These are
    aborted on leader/term changes."""


class RawNodeFollower(RawNode[Follower]):
    """A Raft node in the follower role (`RawNode<Follower>` in Rust)."""

    @classmethod
    def new(
        cls,
        id: NodeID,
        peers: set[NodeID],
        log: Log,
        state: State,
        tx: "queue.Queue[Envelope]",
        opts: Options,
    ) -> "RawNodeFollower":
        """Creates a new node as a leaderless follower."""
        if id in peers:
            raise errinput(f"node ID {id} can't be in peers")
        role = Follower(None, 0)
        node = cls(id, peers, log, state, tx, opts, role)
        node.role.election_timeout = node.random_election_timeout()

        # Apply any pending entries following restart. State machine writes are
        # not flushed to durable storage, so a tail of writes may be lost if
        # the host crashes or restarts. The Raft log is durable, so we can
        # always recover the state from it. We reapply any missing entries here
        # if that should happen.
        node.maybe_apply()
        return node

    def into(self) -> Node:
        return Node.Follower(self)

    def into_candidate(self) -> "RawNodeCandidate":
        """Transitions the follower into a candidate, by campaigning for
        leadership in a new term."""
        # Abort any forwarded requests. These must be retried with new leader.
        self.abort_forwarded()

        # Apply any pending log entries, so that we're caught up if we win.
        self.maybe_apply()

        # Become candidate and campaign.
        election_timeout = self.random_election_timeout()
        node = self.into_role(Candidate(election_timeout))
        node.campaign()

        term, vote = node.log.get_term_vote()
        assert node.id in node.role.votes, "candidate did not vote for self"
        assert_ne(term, 0, "candidate can't have term 0")
        assert_eq(vote, node.id, "log vote does not match self", debug_option)

        return node

    def into_follower(self, term: Term, leader: NodeID | None) -> "RawNodeFollower":
        """Transitions the follower into either a leaderless follower in a new term
        (e.g. if someone holds a new election) or a follower of a current leader."""
        assert_ne(term, 0, "can't become follower in term 0")

        # Abort any forwarded requests. These must be retried with new leader.
        self.abort_forwarded()

        if leader is not None:
            # We found a leader in the current term.
            assert leader in self.peers, "leader is not a peer"
            assert_eq(self.role.leader, None, "already have leader in term", debug_option)
            assert_eq(term, self.term(), "can't follow leader in different term")
            logger.info("Following leader %s in term %s", leader, term)
            self.role = Follower(leader, self.role.election_timeout)
        else:
            # We found a new term, but we don't know who the leader is yet.
            # We'll find out if we step a message from it.
            assert_ne(term, self.term(), "can't become leaderless follower in current term")
            logger.info("Discovered new term %s", term)
            self.log.set_term_vote(term, None)
            self.role = Follower(None, self.random_election_timeout())
        return self

    def step(self, msg: Envelope) -> Node:
        """Processes an inbound message."""
        # Past term: outdated peer, drop the message.
        if msg.term < self.term():
            logger.debug("Dropping message from past term: %r", msg)
            return self.into()
        # Future term: newer leader or candidate, become leaderless follower
        # and step the message.
        if msg.term > self.term():
            return self.into_follower(msg.term, None).step(msg)

        # Record when we last saw a message from the leader (if any).
        if msg.from_ == self.role.leader:
            self.role.leader_seen = 0

        match msg.message:
            # The leader sends periodic heartbeats. If we don't have a leader
            # yet, follow it. If the commit_index advances, apply commands.
            case Message.Heartbeat(last_index, commit_index, read_seq):
                assert commit_index <= last_index, "commit_index after last_index"

                # Make sure the heartbeat is from our leader, or follow it.
                if (leader := self.role.leader) is not None:
                    assert_eq(msg.from_, leader, "multiple leaders in term")
                else:
                    self.into_follower(msg.term, msg.from_)

                # Check if our log matches the leader's log up to last_index,
                # and respond to the heartbeat. last_index always has the
                # leader's term, since it only appends entries in its term.
                match_index = last_index if self.log.has(last_index, msg.term) else 0
                self.send(msg.from_, Message.HeartbeatResponse(match_index, read_seq))

                # Advance the commit index and apply entries. We can only do
                # this if we matched the leader's last_index, which implies
                # that the logs are identical up to match_index. This also
                # implies that the commit_index is present in our log.
                if match_index != 0 and commit_index > self.log.get_commit_index()[0]:
                    self.log.commit(commit_index)
                    self.maybe_apply()

            # Append log entries from the leader to the local log.
            case Message.Append(base_index, base_term, entries):
                if entries:
                    assert_eq(base_index, entries[0].index - 1, "base index mismatch")

                # Make sure the append is from our leader, or follow it.
                if (leader := self.role.leader) is not None:
                    assert_eq(msg.from_, leader, "multiple leaders in term")
                else:
                    self.into_follower(msg.term, msg.from_)

                # If the base entry matches our log, append the entries.
                if base_index == 0 or self.log.has(base_index, base_term):
                    match_index = entries[-1].index if entries else base_index
                    self.log.splice(entries)
                    self.send(
                        msg.from_, Message.AppendResponse(match_index=match_index, reject_index=0)
                    )
                else:
                    # Otherwise, reject the base index. If the local log is
                    # shorter than the base index, lower the reject index to
                    # skip all missing entries.
                    reject_index = min(base_index, self.log.get_last_index()[0] + 1)
                    self.send(
                        msg.from_, Message.AppendResponse(reject_index=reject_index, match_index=0)
                    )

            # Confirm the leader's read sequence number.
            case Message.Read(seq):
                # Make sure the read is from our leader, or follow it.
                if (leader := self.role.leader) is not None:
                    assert_eq(msg.from_, leader, "multiple leaders in term")
                else:
                    self.into_follower(msg.term, msg.from_)

                # Confirm the read.
                self.send(msg.from_, Message.ReadResponse(seq))

            # A candidate is requesting our vote. We only grant one per term.
            case Message.Campaign(last_index, last_term):
                # Don't vote if we already voted for someone else in this term.
                # We can repeat our vote for the same node though.
                _, vote = self.log.get_term_vote()
                if vote is not None and msg.from_ != vote:
                    self.send(msg.from_, Message.CampaignResponse(vote=False))
                    return self.into()

                # Only vote if the candidate's log is at least as long as ours.
                # At least one node in any quorum must have all committed
                # entries, and this ensures we'll only elect a leader that has
                # all committed entries. See section 5.4.1 in the Raft paper.
                log_index, log_term = self.log.get_last_index()
                if log_term > last_term or log_term == last_term and log_index > last_index:
                    self.send(msg.from_, Message.CampaignResponse(vote=False))
                    return self.into()

                # Grant the vote.
                logger.info("Voting for %s in term %s election", msg.from_, msg.term)
                self.log.set_term_vote(msg.term, msg.from_)
                self.send(msg.from_, Message.CampaignResponse(vote=True))

            # Forward client requests to the leader, or abort them if there is
            # none. These will not be retried, the client should use timeouts
            # instead.  Local client requests use our node ID as the sender.
            case Message.ClientRequest(id, _):
                assert_eq(msg.from_, self.id, "client request from other node")

                if (leader := self.role.leader) is not None:
                    logger.debug("Forwarding request to leader %s: %r", leader, msg)
                    self.role.forwarded.add(id)
                    self.send(leader, msg.message)
                else:
                    self.send(msg.from_, Message.ClientResponse(id, Error.Abort()))

            # Client responses from the leader are passed on to the client.
            case Message.ClientResponse(id, response):
                assert_eq(
                    msg.from_, self.role.leader, "client response from non-leader", debug_option
                )

                if id in self.role.forwarded:
                    self.role.forwarded.remove(id)
                    self.send(self.id, Message.ClientResponse(id, response))

            # We may receive a vote after we lost an election, ignore it.
            case Message.CampaignResponse():
                pass

            # We're not leader this term, so we shouldn't see these.
            case Message.HeartbeatResponse() | Message.AppendResponse() | Message.ReadResponse():
                raise Panic(f"follower received unexpected message {debug(msg)}")

            case _:
                raise Panic(f"unknown message {msg!r}")
        return self.into()

    def tick(self) -> Node:
        """Processes a logical clock tick."""
        # Campaign if we haven't heard from the leader in a while.
        self.role.leader_seen += 1
        if self.role.leader_seen >= self.role.election_timeout:
            return self.into_candidate().into()
        return self.into()

    def abort_forwarded(self) -> None:
        """Aborts all forwarded requests (e.g. on term/leader changes)."""
        # Sort by ID for test determinism.
        forwarded, self.role.forwarded = self.role.forwarded, set()
        for id in sorted(forwarded):
            logger.debug("Aborting forwarded request %s", id)
            self.send(self.id, Message.ClientResponse(id, Error.Abort()))

    def maybe_apply(self) -> None:
        """Applies any pending log entries."""
        for entry in self.log.scan_apply(self.state.get_applied_index()):
            logger.debug("Applying %r", entry)
            # Throw away the result, since only the leader responds to clients.
            # This includes errors -- any non-deterministic errors (e.g. IO
            # errors) must panic instead to avoid node divergence.
            try:
                self.state.apply(entry)
            except Error:
                pass


@dataclass
class Candidate(Role):
    """A candidate is campaigning to become a leader."""

    election_timeout: Ticks
    """Election timeout, in ticks."""
    votes: set[NodeID] = field(default_factory=set)
    """Votes received (including our own)."""
    election_duration: Ticks = 0
    """Ticks elapsed since election start."""


class RawNodeCandidate(RawNode[Candidate]):
    """A Raft node in the candidate role (`RawNode<Candidate>` in Rust)."""

    def into(self) -> Node:
        return Node.Candidate(self)

    def into_follower(self, term: Term, leader: NodeID | None) -> RawNodeFollower:
        """Transitions the candidate to a follower. We either lost the election and
        follow the winner, or we discovered a new term and step into it as a
        leaderless follower."""
        election_timeout = self.random_election_timeout()
        if leader is not None:
            # We lost the election, follow the winner.
            assert_eq(term, self.term(), "can't follow leader in different term")
            logger.info("Lost election, following leader %s in term %s", leader, term)
            return self.into_role(Follower(leader, election_timeout))
        else:
            # We found a new term, but we don't necessarily know who the leader
            # is yet. We'll find out when we step a message from it.
            assert_ne(term, self.term(), "can't become leaderless follower in current term")
            logger.info("Discovered new term %s", term)
            self.log.set_term_vote(term, None)
            return self.into_role(Follower(None, election_timeout))

    def into_leader(self) -> "RawNodeLeader":
        """Transitions the candidate to a leader. We won the election."""
        term, vote = self.log.get_term_vote()
        assert_ne(term, 0, "leaders can't have term 0")
        assert_eq(vote, self.id, "leader did not vote for self", debug_option)

        logger.info("Won election for term %s, becoming leader", term)
        peers = set(self.peers)
        last_index, _ = self.log.get_last_index()
        node = self.into_role(Leader.new(peers, last_index))

        # Propose an empty command when assuming leadership, to disambiguate
        # previous entries in the log. See section 5.4.2 in the Raft paper.
        # We do this prior to the heartbeat, to avoid a wasted replication
        # roundtrip if the heartbeat response indicates the peer is behind.
        node.propose(None)
        node.maybe_commit_and_apply()
        node.heartbeat()

        return node

    def step(self, msg: Envelope) -> Node:
        """Processes an inbound message."""
        # Past term: outdated peer, drop the message.
        if msg.term < self.term():
            logger.debug("Dropping message from past term: %r", msg)
            return self.into()
        # Future term: newer leader or candidate, become leaderless follower
        # and step the message.
        if msg.term > self.term():
            return self.into_follower(msg.term, None).step(msg)

        match msg.message:
            # If we received a vote, record it. If the vote gives us quorum,
            # assume leadership.
            case Message.CampaignResponse(vote=True):
                self.role.votes.add(msg.from_)
                if len(self.role.votes) >= self.quorum_size():
                    return self.into_leader().into()

            # We didn't get the vote. :(
            case Message.CampaignResponse(vote=False):
                pass

            # Don't grant votes for other candidates.
            case Message.Campaign():
                self.send(msg.from_, Message.CampaignResponse(vote=False))

            # If we hear from a leader in this term, we lost the election.
            # Follow it and step the message.
            case Message.Heartbeat() | Message.Append() | Message.Read():
                return self.into_follower(msg.term, msg.from_).step(msg)

            # Abort client requests while campaigning. The client must retry.
            case Message.ClientRequest(id, _):
                self.send(msg.from_, Message.ClientResponse(id, Error.Abort()))

            # We're not a leader in this term, nor are we forwarding requests,
            # so we shouldn't see these.
            case (
                Message.HeartbeatResponse()
                | Message.AppendResponse()
                | Message.ReadResponse()
                | Message.ClientResponse()
            ):
                raise Panic(f"unexpected message {debug(msg)}")

            case _:
                raise Panic(f"unknown message {msg!r}")
        return self.into()

    def tick(self) -> Node:
        """Processes a logical clock tick."""
        # If noone won this election, start a new one after a while.
        self.role.election_duration += 1
        if self.role.election_duration >= self.role.election_timeout:
            self.campaign()
        return self.into()

    def campaign(self) -> None:
        """Hold a new election by increasing the term, voting for ourself, and
        soliciting votes from all peers."""
        term = self.term() + 1
        logger.info("Starting new election for term %s", term)
        self.role = Candidate(self.random_election_timeout())
        self.role.votes.add(self.id)  # vote for ourself
        self.log.set_term_vote(term, self.id)

        last_index, last_term = self.log.get_last_index()
        self.broadcast(Message.Campaign(last_index, last_term))


@dataclass
class Leader(Role):
    """A leader serves client requests and replicates the log to followers.
    If the leader loses leadership, all client requests are aborted."""

    progress: dict[NodeID, "Progress"]
    """Follower replication progress."""
    writes: dict[Index, "Write"] = field(default_factory=dict)
    """Tracks pending write requests by log index. Added when the write is
    proposed and appended to the leader's log, and removed when the command
    is applied to the state machine, returning the result to the client."""
    reads: deque["Read"] = field(default_factory=deque)
    """Tracks pending read requests. For linearizability, read requests are
    assigned a sequence number and only executed once a quorum of nodes have
    confirmed that we're still the leader. Otherwise, an old leader could
    serve stale reads if a new leader has been elected elsewhere."""
    read_seq: ReadSequence = 0
    """The read sequence number used for the last read. Initialized to 0 in
    this term, and incremented for every read command."""
    since_heartbeat: Ticks = 0
    """Number of ticks since last heartbeat."""

    @classmethod
    def new(cls, peers: set[NodeID], last_index: Index) -> "Leader":
        """Creates a new leader role."""
        next_index = last_index + 1
        progress = {p: Progress(next_index=next_index, match_index=0, read_seq=0) for p in peers}
        return cls(progress)


@dataclass
class Progress:
    """Per-follower replication progress (in this term)."""

    match_index: Index
    """The highest index where the follower's log is known to match the leader.
    Initialized to 0, increases monotonically."""
    next_index: Index
    """The next index to replicate to the follower. Initialized to
    last_index+1, decreased when probing log mismatches. Always in
    the range [match_index+1, last_index+1].

    Entries not yet sent are in the range [next_index, last_index].
    Entries not acknowledged are in the range [match_index+1, next_index)."""
    read_seq: ReadSequence
    """The last read sequence number confirmed by this follower. To avoid stale
    reads on leader changes, a read is only served once its sequence number
    is confirmed by a quorum."""

    def advance(self, match_index: Index) -> bool:
        """Attempts to advance a follower's match index, returning true if it did.
        If next_index is below it, it is advanced to the following index."""
        if match_index <= self.match_index:
            return False
        self.match_index = match_index
        self.next_index = max(self.next_index, match_index + 1)
        return True

    def advance_read(self, read_seq: ReadSequence) -> bool:
        """Attempts to advance a follower's read_seq, returning true if it did."""
        if read_seq <= self.read_seq:
            return False
        self.read_seq = read_seq
        return True

    def regress_next(self, next_index: Index) -> bool:
        """Attempts to regress a follower's next index to the given index, returning
        true if it did. Won't regress below match_index + 1."""
        if next_index >= self.next_index or self.next_index <= self.match_index + 1:
            return False
        self.next_index = max(next_index, self.match_index + 1)
        return True


@dataclass(frozen=True)
class Write:
    """A pending client write request."""

    from_: NodeID
    """The node which submitted the write."""
    id: RequestID
    """The write request ID."""


@dataclass(frozen=True)
class Read:
    """A pending client read request."""

    seq: ReadSequence
    """The sequence number of this read."""
    from_: NodeID
    """The node which submitted the read."""
    id: RequestID
    """The read request ID."""
    command: bytes
    """The read command."""


class RawNodeLeader(RawNode[Leader]):
    """A Raft node in the leader role (`RawNode<Leader>` in Rust)."""

    def into(self) -> Node:
        return Node.Leader(self)

    def into_follower(self, term: Term) -> RawNodeFollower:
        """Transitions the leader into a follower. This can only happen if we
        discover a new term, so we become a leaderless follower. Stepping the
        received message may then follow a new leader, if there is one."""
        assert term > self.term(), "leader can only become follower in later term"
        logger.info("Discovered new term %s", term)

        # Abort in-flight requests. The client must retry. Sort the requests
        # by ID for test determinism.
        writes, self.role.writes = self.role.writes, {}
        for write in sorted(writes.values(), key=lambda w: w.id):
            self.send(write.from_, Message.ClientResponse(write.id, Error.Abort()))
        reads, self.role.reads = self.role.reads, deque()
        for read in sorted(reads, key=lambda r: r.id):
            self.send(read.from_, Message.ClientResponse(read.id, Error.Abort()))

        self.log.set_term_vote(term, None)
        election_timeout = self.random_election_timeout()
        return self.into_role(Follower(None, election_timeout))

    def step(self, msg: Envelope) -> Node:
        """Processes an inbound message."""
        # Past term: outdated peer, drop the message.
        if msg.term < self.term():
            logger.debug("Dropping message from past term: %r", msg)
            return self.into()
        # Future term: become leaderless follower and step the message.
        if msg.term > self.term():
            return self.into_follower(msg.term).step(msg)

        match msg.message:
            # A follower received our heartbeat and confirms our leadership.
            # We may be able to execute new reads, and we may find that the
            # follower's log is lagging and requires us to catch it up.
            case Message.HeartbeatResponse(match_index, read_seq):
                last_index, _ = self.log.get_last_index()
                assert match_index <= last_index, "future match index"
                assert read_seq <= self.role.read_seq, "future read sequence number"

                # If the read sequence number advances, try to execute reads.
                if self.progress(msg.from_).advance_read(read_seq):
                    self.maybe_read()

                # If the follower didn't match our last index, an append to it
                # must have failed (or it's catching up). Probe it to discover
                # a matching entry and start replicating. Move next_index back
                # to last_index since the follower just told us it doesn't have
                # it (or a previous last_index).
                if match_index == 0:
                    self.progress(msg.from_).regress_next(last_index)
                    self.maybe_send_append(msg.from_, True)

                # If the follower's match index advances, an append response
                # got lost. Try to commit and apply.
                #
                # We don't need to eagerly send any pending entries, since any
                # proposals made after this heartbeat was sent should have been
                # eagerly replicated in steady state. If not, the next
                # heartbeat will trigger a probe above.
                if self.progress(msg.from_).advance(match_index):
                    self.maybe_commit_and_apply()

            # A follower appended our log entries (or a probe found a match).
            # Record its progress and attempt to commit and apply.
            case Message.AppendResponse(match_index=match_index, reject_index=0) if match_index > 0:
                last_index, _ = self.log.get_last_index()
                assert match_index <= last_index, "future match index"

                if self.progress(msg.from_).advance(match_index):
                    self.maybe_commit_and_apply()

                # Eagerly send any further pending entries. This may be a
                # successful probe response, or the peer may be lagging and
                # we're catching it up one MAX_APPEND_ENTRIES batch at a time.
                self.maybe_send_append(msg.from_, False)

            # A follower confirmed our read sequence number. If it advances,
            # try to execute reads.
            case Message.ReadResponse(seq):
                if self.progress(msg.from_).advance_read(seq):
                    self.maybe_read()

            # A follower rejected an append because the base entry in
            # reject_index did not match its log. Probe the previous entry by
            # sending an empty append until we find a common base.
            #
            # This linear probing can be slow with long divergent logs, but we
            # keep it simple. See also section 5.3 in the Raft paper.
            case Message.AppendResponse(reject_index=reject_index, match_index=0) if (
                reject_index > 0
            ):
                last_index, _ = self.log.get_last_index()
                assert reject_index <= last_index, "future reject index"

                # If the rejected base index is at or below the match index,
                # the rejection is stale and can be ignored.
                if reject_index <= self.progress(msg.from_).match_index:
                    return self.into()

                # Probe below the reject index, if we haven't already moved
                # next_index below it. This avoids sending duplicate probes
                # (heartbeats will trigger retries if they're lost).
                if self.progress(msg.from_).regress_next(reject_index):
                    self.maybe_send_append(msg.from_, True)

            # AppendResponses must set either match_index or reject_index.
            case Message.AppendResponse():
                raise Panic(f"invalid message {debug(msg)}")

            # A client submitted a write request. Propose it, and wait until
            # it's replicated and applied to the state machine before returning
            # the response to the client.
            case Message.ClientRequest(id, Request.Write(command)):
                index = self.propose(command)
                self.role.writes[index] = Write(from_=msg.from_, id=id)
                if self.cluster_size() == 1:
                    self.maybe_commit_and_apply()

            # A client submitted a read request. To ensure linearizability, we
            # must confirm that we are still the leader by sending the read's
            # sequence number and wait for quorum confirmation.
            case Message.ClientRequest(id, Request.Read(command)):
                self.role.read_seq += 1
                read = Read(seq=self.role.read_seq, from_=msg.from_, id=id, command=command)
                self.role.reads.append(read)
                self.broadcast(Message.Read(self.role.read_seq))
                if self.cluster_size() == 1:
                    self.maybe_read()

            # A client submitted a status command.
            case Message.ClientRequest(id, Request.Status()):
                response: Response | Error
                try:
                    response = Response.Status(self.status())
                except Error as error:
                    response = error
                self.send(msg.from_, Message.ClientResponse(id, response))

            # Don't grant any votes (we've already voted for ourself).
            case Message.Campaign():
                self.send(msg.from_, Message.CampaignResponse(vote=False))

            # Votes can come in after we won the election, ignore them.
            case Message.CampaignResponse():
                pass

            # There can't be another leader in this term.
            case Message.Heartbeat() | Message.Append() | Message.Read():
                raise Panic(f"saw other leader {msg.from_} in term {msg.term}")

            # Leaders don't proxy client requests.
            case Message.ClientResponse():
                raise Panic(f"unexpected message {debug(msg)}")

            case _:
                raise Panic(f"unknown message {msg!r}")

        return self.into()

    def tick(self) -> Node:
        """Processes a logical clock tick."""
        # Send periodic heartbeats.
        self.role.since_heartbeat += 1
        if self.role.since_heartbeat >= self.opts.heartbeat_interval:
            self.heartbeat()
        return self.into()

    def heartbeat(self) -> None:
        """Broadcasts a heartbeat to all peers."""
        last_index, last_term = self.log.get_last_index()
        commit_index, _ = self.log.get_commit_index()
        read_seq = self.role.read_seq
        assert_eq(last_term, self.term(), "leader's last_term not in current term")

        self.role.since_heartbeat = 0
        self.broadcast(Message.Heartbeat(last_index, commit_index, read_seq))

    def propose(self, command: bytes | None) -> Index:
        """Proposes a command for consensus by appending it to our log and
        replicating it to peers. If successful, it will eventually be committed
        and applied to the state machine."""
        index = self.log.append(command)
        for peer in sorted(self.peers):
            # Eagerly send the entry to the peer if it's in steady state and
            # we've sent all previous entries. Otherwise, the peer is lagging
            # and we're probing past entries for a match.
            if index == self.progress(peer).next_index:
                self.maybe_send_append(peer, False)
        return index

    def maybe_commit_and_apply(self) -> Index:
        """Commits new entries that have been replicated to a quorum and applies
        them to the state machine, returning results to clients."""
        # Determine the new commit index by quorum.
        last_index, _ = self.log.get_last_index()
        commit_index = self.quorum_value(
            [p.match_index for p in self.role.progress.values()] + [last_index]
        )

        # If the commit index doesn't advance, do nothing. We don't assert on
        # this, since the quorum value may regress e.g. following a restart or
        # leader change where followers are initialized with match index 0.
        old_index, old_term = self.log.get_commit_index()
        if commit_index <= old_index:
            return old_index

        # We can only safely commit an entry from our own term (see section
        # 5.4.2 in Raft paper).
        entry = self.log.get(commit_index)
        if entry is None:
            raise Panic(f"commit index {commit_index} missing")
        if entry.term != self.term():
            return old_index

        # Commit entries.
        self.log.commit(commit_index)

        # Apply entries and respond to clients.
        term = self.term()
        for entry in self.log.scan_apply(self.state.get_applied_index()):
            logger.debug("Applying %r", entry)
            write = self.role.writes.pop(entry.index, None)
            response: Response | Error
            try:
                response = Response.Write(self.state.apply(entry))
            except Error as error:
                response = error

            if write is not None:
                message = Message.ClientResponse(write.id, response)
                self.send_via(
                    self.tx, Envelope(from_=self.id, term=term, to=write.from_, message=message)
                )

        # If the commit term changed, there may be pending reads waiting for us
        # to commit and apply an entry from our own term. Execute them.
        if old_term != self.term():
            self.maybe_read()

        return commit_index

    def maybe_read(self) -> None:
        """Executes any ready read requests, where a quorum have confirmed that
        we're still the leader for the read sequences."""
        if not self.role.reads:
            return

        # It's only safe to read if we've committed and applied an entry from
        # our own term (the leader appends an entry when elected). Otherwise we
        # may be behind on application and serve stale reads.
        commit_index, commit_term = self.log.get_commit_index()
        applied_index = self.state.get_applied_index()
        if commit_term < self.term() or applied_index < commit_index:
            return

        # Determine the maximum read sequence confirmed by quorum.
        quorum_read_seq = self.quorum_value(
            [p.read_seq for p in self.role.progress.values()] + [self.role.read_seq]
        )

        # Execute ready reads. The deque is ordered by read_seq, so we
        # can keep pulling until we hit quorum_read_seq.
        while self.role.reads:
            if self.role.reads[0].seq > quorum_read_seq:
                break
            read = self.role.reads.popleft()
            response: Response | Error
            try:
                response = Response.Read(self.state.read(read.command))
            except Error as error:
                response = error
            self.send(read.from_, Message.ClientResponse(read.id, response))

    def maybe_send_append(self, peer: NodeID, probe: bool) -> None:
        """Sends a batch of pending log entries to a follower, in the
        [next_index,last_index] range. Limited by max_append_entries.

        If probe is true, we're trying to find a log index on the follower where
        it matches our log. To do this, we send an empty append probe with
        base_index of next_index-1. If the follower confirms the base_index
        matches its log, the actual entries are sent next -- otherwise,
        next_index is decremented and another probe is sent until a match is
        found. See section 5.3 in the Raft paper.

        The probe is skipped if the follower is up-to-date (according to
        match_index and last_index). If the probe's base_index has already been
        confirmed via match_index, an actual append is sent instead.
        """
        last_index, _ = self.log.get_last_index()
        progress = self.progress(peer)
        assert_ne(progress.next_index, 0, "invalid next_index")
        assert progress.next_index > progress.match_index, "invalid next_index <= match_index"
        assert progress.match_index <= last_index, "invalid match_index > last_index"
        assert progress.next_index <= last_index + 1, "invalid next_index > last_index + 1"

        # If the peer is caught up, there's no point sending an append.
        if progress.match_index == last_index:
            return

        # If a probe was requested, but the base_index has already been
        # confirmed via match_index, there is no point in probing. Just send
        # the entries instead.
        probe = probe and progress.next_index > progress.match_index + 1

        # If there are no pending entries, and this is not a probe, there's
        # nothing more to send until we get a response from the follower.
        if progress.next_index > last_index and not probe:
            return

        # Fetch the base and entries.
        match progress.next_index:
            case 0:
                raise Panic(f"next_index=0 for node {peer}")
            case 1:
                base_index, base_term = 0, 0  # first entry, there is no base
            case next:
                base = self.log.get(next - 1)
                assert base is not None, "missing base entry"
                base_index, base_term = base.index, base.term
        if probe:
            entries = []
        else:
            scan = self.log.scan((Bound.Included(progress.next_index), Bound.Unbounded()))
            entries = list(itertools.islice(scan, self.opts.max_append_entries))

        # Optimistically assume the entries will be accepted by the follower,
        # and bump next_index to avoid resending them until a response.
        if entries:
            progress.next_index = entries[-1].index + 1

        logger.debug("Replicating %s entries with base %s to %s", len(entries), base_index, peer)
        self.send(peer, Message.Append(base_index, base_term, entries))

    def status(self) -> Status:
        """Generates cluster status."""
        match_index = {id: p.match_index for id, p in self.role.progress.items()}
        match_index[self.id] = self.log.get_last_index()[0]
        return Status(
            leader=self.id,
            term=self.term(),
            match_index=dict(sorted(match_index.items())),
            commit_index=self.log.get_commit_index()[0],
            applied_index=self.state.get_applied_index(),
            storage=self.log.status(),
        )

    def progress(self, id: NodeID) -> Progress:
        """Returns a node's progress. Convenience method."""
        progress = self.role.progress.get(id)
        assert progress is not None, "unknown node"
        return progress
