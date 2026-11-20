"""Raft messages. Mirrors `src/raft/message.rs`."""

import uuid
from dataclasses import dataclass
from typing import ClassVar

from toydb import encoding, storage
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error
from toydb.fmt import Raw, debug, debug_struct, debug_tuple
from toydb.raft.log import Entry, Index

# NB: `node` defines these aliases before it imports this module in turn.
from toydb.raft.node import NodeID, Term

RequestID = uuid.UUID
"""A client request ID. Must be globally unique while in flight.

For simplicity, a random UUIDv4 is used. We could incorporate the
node/process/MAC ID and timestamp for better collision avoidance (e.g. via
UUIDv6) but it doesn't matter at this scale."""

ReadSequence = int
"""A read sequence number, used to confirm leadership for linearizable reads."""


class _RequestIDSerde:
    """Serializes a `RequestID` like the `uuid` crate does for binary formats:
    as its 16 bytes."""

    def serialize(self, value: RequestID, serializer: Serializer, /) -> None:
        serializer.serialize_bytes(value.bytes)

    def deserialize(self, deserializer: Deserializer, /) -> RequestID:
        data = deserializer.deserialize_bytes()
        if len(data) != 16:
            raise deserializer.custom(
                f"invalid length: expected 16 bytes, found {len(data)}"
            )
        return uuid.UUID(bytes=data)


_REQUEST_ID: serde.Serde[RequestID] = _RequestIDSerde()


@dataclass(frozen=True)
class Envelope(encoding.Value):
    """A message envelope specifying the sender and receiver."""

    from_: NodeID
    """The sender (`from` in Rust, which is a Python keyword)."""
    term: Term
    """The sender's current term."""
    to: NodeID
    """The recipient."""
    message: "Message"
    """The message."""

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_struct(
            "Envelope",
            [("from", self.from_), ("term", self.term), ("to", self.to), ("message", self.message)],
            pretty,
        )

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u8(self.from_)
        serializer.serialize_u64(self.term)
        serializer.serialize_u8(self.to)
        self.message.serialize(serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Envelope":
        deserializer.deserialize_struct()
        return cls(
            from_=deserializer.deserialize_u8(),
            term=deserializer.deserialize_u64(),
            to=deserializer.deserialize_u8(),
            message=Message.deserialize(deserializer),
        )


class Message:
    """A message sent between Raft nodes. Messages are sent asynchronously (i.e.
    they are not request/response) and may be dropped or reordered.

    In practice, they are sent across a TCP connection and queue which ensures
    messages are not dropped or reordered as long as the connection remains
    intact. A message and its response are sent across separate TCP connections
    (outbound from the respective sender).
    """

    Campaign: ClassVar[type["Campaign"]]
    CampaignResponse: ClassVar[type["CampaignResponse"]]
    Heartbeat: ClassVar[type["Heartbeat"]]
    HeartbeatResponse: ClassVar[type["HeartbeatResponse"]]
    Append: ClassVar[type["Append"]]
    AppendResponse: ClassVar[type["AppendResponse"]]
    Read: ClassVar[type["MessageRead"]]
    ReadResponse: ClassVar[type["ReadResponse"]]
    ClientRequest: ClassVar[type["ClientRequest"]]
    ClientResponse: ClassVar[type["ClientResponse"]]

    def __repr__(self) -> str:
        return debug(self)

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case Campaign(last_index, last_term):
                serializer.serialize_struct_variant(0)
                serializer.serialize_u64(last_index)
                serializer.serialize_u64(last_term)
            case CampaignResponse(vote):
                serializer.serialize_struct_variant(1)
                serializer.serialize_bool(vote)
            case Heartbeat(last_index, commit_index, read_seq):
                serializer.serialize_struct_variant(2)
                serializer.serialize_u64(last_index)
                serializer.serialize_u64(commit_index)
                serializer.serialize_u64(read_seq)
            case HeartbeatResponse(match_index, read_seq):
                serializer.serialize_struct_variant(3)
                serializer.serialize_u64(match_index)
                serializer.serialize_u64(read_seq)
            case Append(base_index, base_term, entries):
                serializer.serialize_struct_variant(4)
                serializer.serialize_u64(base_index)
                serializer.serialize_u64(base_term)
                serde.Vec(Entry).serialize(entries, serializer)
            case AppendResponse(match_index, reject_index):
                serializer.serialize_struct_variant(5)
                serializer.serialize_u64(match_index)
                serializer.serialize_u64(reject_index)
            case MessageRead(seq):
                serializer.serialize_struct_variant(6)
                serializer.serialize_u64(seq)
            case ReadResponse(seq):
                serializer.serialize_struct_variant(7)
                serializer.serialize_u64(seq)
            case ClientRequest(id, request):
                serializer.serialize_struct_variant(8)
                _REQUEST_ID.serialize(id, serializer)
                request.serialize(serializer)
            case ClientResponse(id, response):
                serializer.serialize_struct_variant(9)
                _REQUEST_ID.serialize(id, serializer)
                serde.Result(Response).serialize(response, serializer)
            case _:
                raise TypeError(f"unknown message {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Message":
        index = deserializer.deserialize_enum()
        if index > 9:
            raise deserializer.invalid_variant(index, 10)
        deserializer.deserialize_struct_variant()
        match index:
            case 0:
                return Campaign(
                    last_index=deserializer.deserialize_u64(),
                    last_term=deserializer.deserialize_u64(),
                )
            case 1:
                return CampaignResponse(vote=deserializer.deserialize_bool())
            case 2:
                return Heartbeat(
                    last_index=deserializer.deserialize_u64(),
                    commit_index=deserializer.deserialize_u64(),
                    read_seq=deserializer.deserialize_u64(),
                )
            case 3:
                return HeartbeatResponse(
                    match_index=deserializer.deserialize_u64(),
                    read_seq=deserializer.deserialize_u64(),
                )
            case 4:
                return Append(
                    base_index=deserializer.deserialize_u64(),
                    base_term=deserializer.deserialize_u64(),
                    entries=serde.Vec(Entry).deserialize(deserializer),
                )
            case 5:
                return AppendResponse(
                    match_index=deserializer.deserialize_u64(),
                    reject_index=deserializer.deserialize_u64(),
                )
            case 6:
                return MessageRead(seq=deserializer.deserialize_u64())
            case 7:
                return ReadResponse(seq=deserializer.deserialize_u64())
            case 8:
                return ClientRequest(
                    id=_REQUEST_ID.deserialize(deserializer),
                    request=Request.deserialize(deserializer),
                )
            case _:
                return ClientResponse(
                    id=_REQUEST_ID.deserialize(deserializer),
                    response=serde.Result(Response).deserialize(deserializer),
                )


@dataclass(frozen=True, repr=False)
class Campaign(Message):
    """Candidates campaign for leadership by soliciting votes from peers.
    Votes will only be granted if the candidate's log is at least as
    up-to-date as the voter."""

    last_index: Index
    """The index of the candidate's last log entry."""
    last_term: Term
    """The term of the candidate's last log entry."""


@dataclass(frozen=True, repr=False)
class CampaignResponse(Message):
    """Followers may vote for a single candidate per term, but only if the
    candidate's log is at least as up-to-date as the follower. Candidates
    implicitly vote for themselves."""

    vote: bool
    """If true, the follower granted the candidate a vote. A false response
    isn't necessary, but is emitted for clarity."""


@dataclass(frozen=True, repr=False)
class Heartbeat(Message):
    """Leaders send periodic heartbeats. This serves several purposes:

    * Inform nodes about the leader, and prevent elections.
    * Detect lost appends and reads, as a retry mechanism.
    * Advance followers' commit indexes, so they can apply entries.

    The Raft paper does not have a distinct heartbeat message, and instead
    uses an empty AppendEntries RPC, but we choose to add one for better
    separation of concerns.
    """

    last_index: Index
    """The index of the leader's last log entry. The term is the leader's
    current term, since it appends a noop entry on election win. The
    follower compares this to its own log to determine if it's up-to-date."""
    commit_index: Index
    """The index of the leader's last committed log entry. Followers use
    this to advance their commit index and apply entries. It's only safe
    to commit this if the local log matches last_index, such that the
    follower's log is identical to the leader at the commit index."""
    read_seq: ReadSequence
    """The leader's latest read sequence number in this term."""


@dataclass(frozen=True, repr=False)
class HeartbeatResponse(Message):
    """Followers respond to leader heartbeats if they still consider it leader."""

    match_index: Index
    """If non-zero, the heartbeat's last_index which was matched in the
    follower's log. Otherwise, the follower is either divergent or
    lagging behind the leader."""
    read_seq: ReadSequence
    """The heartbeat's read sequence number."""


@dataclass(frozen=True, repr=False)
class Append(Message):
    """Leaders replicate log entries to followers by appending to their logs
    after the given base entry.

    If the base entry matches the follower's log then their logs are
    identical up to it (see section 5.3 in the Raft paper), and the entries
    can be appended -- possibly replacing conflicting entries. Otherwise,
    the append is rejected and the leader must retry an earlier base index
    until a common base is found.

    Empty appends messages (no entries) are used to probe follower logs for
    a common match index in the case of divergent logs, restarted nodes, or
    dropped messages. This is typically done by sending probes with a
    decrementing base index until a match is found, at which point the
    subsequent entries can be sent.
    """

    base_index: Index
    """The index of the log entry to append after."""
    base_term: Term
    """The term of the base entry."""
    entries: list[Entry]
    """Log entries to append. Must start at base_index + 1."""


@dataclass(frozen=True, repr=False)
class AppendResponse(Message):
    """Followers accept or reject appends from the leader depending on whether
    the base entry matches their log."""

    match_index: Index
    """If non-zero, the follower appended entries up to this index. The
    entire log up to this index is consistent with the leader. If no
    entries were sent (a probe), this will be the matching base index."""
    reject_index: Index
    """If non-zero, the follower rejected an append at this base index
    because the base index/term did not match its log. If the follower's
    log is shorter than the base index, the reject index will be lowered
    to the index after its last local index, to avoid probing each
    missing index."""


@dataclass(frozen=True, repr=False)
class MessageRead(Message):
    """Leaders need to confirm they are still the leader before serving reads,
    to guarantee linearizability in case a different leader has been
    estalished elsewhere. Read requests are served once the sequence number
    has been confirmed by a quorum.

    This is `Message::Read` in Rust: use it as `Message.Read`, since the bare
    name clashes with the `Request` and `Response` variants."""

    seq: ReadSequence

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_struct("Read", [("seq", self.seq)], pretty)


@dataclass(frozen=True, repr=False)
class ReadResponse(Message):
    """Followers confirm leadership at the read sequence numbers."""

    seq: ReadSequence


@dataclass(frozen=True, repr=False)
class ClientRequest(Message):
    """A client request. This can be submitted to the leader, or to a follower
    which will forward it to its leader. If there is no leader, or the
    leader or term changes, the request is aborted with an Error.Abort
    ClientResponse and the client must retry."""

    id: RequestID
    """The request ID. Must be globally unique for the request duration."""
    request: "Request"
    """The request itself."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_struct(
            "ClientRequest", [("id", Raw(str(self.id))), ("request", self.request)], pretty
        )


@dataclass(frozen=True, repr=False)
class ClientResponse(Message):
    """A client response."""

    id: RequestID
    """The ID of the original ClientRequest."""
    response: "Response | Error"
    """The response, or an error."""

    def fmt_debug(self, pretty: bool = False) -> str:
        name = "Err" if isinstance(self.response, Error) else "Ok"
        response = Raw(debug_tuple(name, [self.response], pretty))
        return debug_struct(
            "ClientResponse", [("id", Raw(str(self.id))), ("response", response)], pretty
        )


Message.Campaign = Campaign
Message.CampaignResponse = CampaignResponse
Message.Heartbeat = Heartbeat
Message.HeartbeatResponse = HeartbeatResponse
Message.Append = Append
Message.AppendResponse = AppendResponse
Message.Read = MessageRead
Message.ReadResponse = ReadResponse
Message.ClientRequest = ClientRequest
Message.ClientResponse = ClientResponse


class Request(encoding.Value):
    """A client request, typically passed through to the state machine."""

    Read: ClassVar[type["RequestRead"]]
    Write: ClassVar[type["RequestWrite"]]
    Status: ClassVar[type["RequestStatus"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case RequestRead(command):
                serializer.serialize_newtype_variant(0)
                serializer.serialize_bytes(command)
            case RequestWrite(command):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_bytes(command)
            case RequestStatus():
                serializer.serialize_unit_variant(2)
            case _:
                raise TypeError(f"unknown request {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Request":
        match deserializer.deserialize_enum():
            case 0:
                return RequestRead(deserializer.deserialize_bytes())
            case 1:
                return RequestWrite(deserializer.deserialize_bytes())
            case 2:
                return RequestStatus()
            case index:
                raise deserializer.invalid_variant(index, 3)


@dataclass(frozen=True)
class RequestRead(Request):
    """A state machine read command, executed via `State.read`. This is not
    replicated, and only evaluated on the leader."""

    command: bytes

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Read", [self.command], pretty)


@dataclass(frozen=True)
class RequestWrite(Request):
    """A state machine write command, executed via `State.apply`. This is
    replicated across all nodes, and must produce a deterministic result."""

    command: bytes

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Write", [self.command], pretty)


@dataclass(frozen=True)
class RequestStatus(Request):
    """Requests Raft cluster status from the leader."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return "Status"


Request.Read = RequestRead
Request.Write = RequestWrite
Request.Status = RequestStatus


class Response(encoding.Value):
    """A client response. This will be wrapped in a Result for error handling."""

    Read: ClassVar[type["ResponseRead"]]
    Write: ClassVar[type["ResponseWrite"]]
    Status: ClassVar[type["ResponseStatus"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case ResponseRead(result):
                serializer.serialize_newtype_variant(0)
                serializer.serialize_bytes(result)
            case ResponseWrite(result):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_bytes(result)
            case ResponseStatus(status):
                serializer.serialize_newtype_variant(2)
                status.serialize(serializer)
            case _:
                raise TypeError(f"unknown response {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Response":
        match deserializer.deserialize_enum():
            case 0:
                return ResponseRead(deserializer.deserialize_bytes())
            case 1:
                return ResponseWrite(deserializer.deserialize_bytes())
            case 2:
                return ResponseStatus(Status.deserialize(deserializer))
            case index:
                raise deserializer.invalid_variant(index, 3)


@dataclass(frozen=True)
class ResponseRead(Response):
    """A state machine read result."""

    result: bytes

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Read", [self.result], pretty)


@dataclass(frozen=True)
class ResponseWrite(Response):
    """A state machine write result."""

    result: bytes

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Write", [self.result], pretty)


@dataclass(frozen=True)
class ResponseStatus(Response):
    """The current Raft leader status."""

    status: "Status"

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Status", [self.status], pretty)


Response.Read = ResponseRead
Response.Write = ResponseWrite
Response.Status = ResponseStatus


@dataclass(frozen=True)
class Status:
    """Raft cluster status. Generated by the leader."""

    leader: NodeID
    """The current Raft leader, which generated this status."""
    term: Term
    """The current Raft term."""
    match_index: dict[NodeID, Index]
    """The match indexes of all nodes, indicating replication progress. Sorted
    by node ID (a BTreeMap in Rust), for test determinism."""
    commit_index: Index
    """The current commit index."""
    applied_index: Index
    """The current applied index."""
    storage: storage.Status
    """The log storage engine status."""

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u8(self.leader)
        serializer.serialize_u64(self.term)
        serde.BTreeMap(serde.U8, serde.U64).serialize(self.match_index, serializer)
        serializer.serialize_u64(self.commit_index)
        serializer.serialize_u64(self.applied_index)
        self.storage.serialize(serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Status":
        deserializer.deserialize_struct()
        return cls(
            leader=deserializer.deserialize_u8(),
            term=deserializer.deserialize_u64(),
            match_index=serde.BTreeMap(serde.U8, serde.U64).deserialize(deserializer),
            commit_index=deserializer.deserialize_u64(),
            applied_index=deserializer.deserialize_u64(),
            storage=storage.Status.deserialize(deserializer),
        )
