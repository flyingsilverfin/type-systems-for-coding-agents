"""The toyDB server.

Mirrors `src/server.rs`.
"""

import logging
import queue
import socket
import threading
import time
import uuid
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import ClassVar

from toydb import encoding, net, raft, sql, storage
from toydb.encoding import bincode, serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import Error, panic
from toydb.fmt import debug, debug_tuple
from toydb.sql.engine.raft import ResponseSender
from toydb.sql.execution import StatementResult
from toydb.sql.execution.session import Session
from toydb.sql.types import Row, Table, Value

logger = logging.getLogger(__name__)

RAFT_PEER_CHANNEL_CAPACITY: int = 1000
"""The outbound Raft peer channel capacity. This buffers messages when a Raft
peer is slow or unavailable. Beyond this, messages will be dropped."""

RAFT_PEER_RETRY_INTERVAL: timedelta = timedelta(seconds=1)
"""The retry interval when connecting to a Raft peer."""

type RaftRequest = tuple[raft.Request, ResponseSender]
"""A client request to the local Raft node, along with a response channel."""

type RouteMessage = raft.Envelope | RaftRequest
"""An inbound message for the Raft router: either a message from a Raft peer,
or a request from a local SQL client."""


class Server:
    """A toyDB server. Routes messages to/from an inner Raft node.

    * Listens for inbound SQL connections from clients via TCP and passes
      requests to the local Raft node.

    * Listens for inbound Raft connections from other toyDB nodes via TCP and
      passes messages to the local Raft node.

    * Connects to other toyDB nodes via TCP and sends outbound Raft messages
      from the local Raft node.
    """

    def __init__(
        self,
        id: raft.NodeID,
        peers: dict[raft.NodeID, str],
        raft_log: raft.Log,
        raft_state: raft.State,
    ) -> None:
        """Creates a new toyDB server."""
        self.node_rx: queue.Queue[raft.Envelope] = queue.Queue()
        """Outbound messages from the Raft node."""
        self.node: raft.Node = raft.Node.new(
            id, set(peers), raft_log, raft_state, self.node_rx, raft.Options()
        )
        """The inner Raft node."""
        self.peers = peers
        """Raft peer IDs and addresses."""

    def serve(self, raft_addr: net.Addr, sql_addr: net.Addr) -> None:
        """Serves Raft and SQL requests indefinitely."""
        try:
            raft_listener = net.bind(raft_addr)
            sql_listener = net.bind(sql_addr)
        except OSError as error:
            raise Error.from_exception(error) from error
        logger.info(
            "Listening on %s (SQL) and %s (Raft)",
            net.display_addr(sql_listener.getsockname()),
            net.display_addr(raft_listener.getsockname()),
        )

        threads: list[threading.Thread] = []

        def spawn(target: Callable[[], None]) -> None:
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            threads.append(thread)

        id = self.node.id()
        # Python queues can't be selected on, so inbound peer messages and
        # client requests share a single inbox (raft_step_tx and
        # raft_request_tx in Rust).
        inbox: queue.Queue[RouteMessage] = queue.Queue()

        # Serve inbound Raft connections.
        spawn(lambda: Server.raft_accept(raft_listener, inbox))

        # Establish outbound Raft connections to peers.
        raft_peers_tx: dict[raft.NodeID, queue.Queue[raft.Envelope]] = {}
        for peer_id, addr in self.peers.items():
            raft_peer_tx: queue.Queue[raft.Envelope] = queue.Queue(RAFT_PEER_CHANNEL_CAPACITY)
            raft_peers_tx[peer_id] = raft_peer_tx
            spawn(lambda addr=addr, rx=raft_peer_tx: Server.raft_send_peer(addr, rx))

        # Route Raft messages between the local node, peers, and clients.
        node, node_rx = self.node, self.node_rx
        spawn(lambda: Server.raft_route(node, node_rx, inbox, raft_peers_tx))

        # Serve inbound SQL connections.
        sql_engine = sql.engine.Raft(inbox)
        spawn(lambda: Server.sql_accept(id, sql_listener, sql_engine))

        for thread in threads:
            thread.join()

    @staticmethod
    def raft_accept(listener: socket.socket, raft_step_tx: "queue.Queue[RouteMessage]") -> None:
        """Accepts new inbound Raft connections from peers and spawns threads
        routing inbound messages to the local Raft node."""
        while True:
            try:
                sock, addr = listener.accept()
            except OSError as error:
                logger.error("Raft peer accept failed: %s", net.display_io_error(error))
                continue
            peer = net.display_addr(addr)

            def receive(sock: socket.socket = sock, peer: str = peer) -> None:
                logger.debug("Raft peer %s connected", peer)
                try:
                    Server.raft_receive_peer(sock, raft_step_tx)
                    logger.debug("Raft peer %s disconnected", peer)
                except Error as error:
                    logger.error("Raft peer %s error: %s", peer, error)
                finally:
                    sock.close()

            threading.Thread(target=receive, daemon=True).start()

    @staticmethod
    def raft_receive_peer(sock: socket.socket, raft_step_tx: "queue.Queue[RouteMessage]") -> None:
        """Receives inbound messages from a peer via TCP, and queues them for
        stepping into the Raft node."""
        with sock.makefile("rb") as reader:
            while (message := raft.Envelope.maybe_decode_from(reader)) is not None:
                raft_step_tx.put(message)

    @staticmethod
    def raft_send_peer(addr: str, raft_node_rx: queue.Queue[raft.Envelope]) -> None:
        """Sends outbound messages to a peer via TCP. Retries indefinitely if
        the connection fails."""
        while True:
            try:
                sock = net.connect(addr)
            except OSError as error:
                error_message = net.display_io_error(error)
                logger.error("Failed connecting to Raft peer %s: %s", addr, error_message)
                time.sleep(RAFT_PEER_RETRY_INTERVAL.total_seconds())
                continue
            writer = sock.makefile("wb")
            try:
                while True:
                    message = raft_node_rx.get()
                    try:
                        message.encode_into(writer)
                        writer.flush()
                    except (Error, OSError) as error:
                        error = Error.from_exception(error)
                        logger.error("Failed sending to Raft peer %s: %s", addr, error)
                        break
            finally:
                # Like dropping a Rust BufWriter: closing flushes any buffered
                # bytes, and errors are ignored (the peer is already gone).
                for closable in (writer, sock):
                    try:
                        closable.close()
                    except OSError:
                        pass
            logger.debug("Disconnected from Raft peer %s", addr)

    @staticmethod
    def raft_route(
        node: raft.Node,
        node_rx: queue.Queue[raft.Envelope],
        inbox: "queue.Queue[RouteMessage]",
        peers_tx: dict[raft.NodeID, queue.Queue[raft.Envelope]],
    ) -> None:
        """Routes Raft messages:

        * node_rx: outbound messages from the local Raft node. Routed to peers
          via TCP, or to local clients via a response channel.

        * inbox: inbound messages from remote Raft peers, which are stepped
          into the local Raft node, and inbound requests from local SQL
          clients, which are stepped into the local Raft node as ClientRequest
          messages. Responses are returned via the provided response channel.
          (In Rust, these are separate peers_rx and request_rx channels.)

        * peers_tx: outbound per-peer channels sent via TCP connections.
          Messages from the local node's node_rx are sent here.

        Panics on any errors, since the Raft node can't recover from failed
        state transitions.
        """
        # Track response channels by request ID. The Raft node will emit
        # ClientResponse messages that we forward to the response channel.
        response_txs: dict[raft.RequestID, ResponseSender] = {}

        tick_interval = raft.TICK_INTERVAL.total_seconds()
        next_tick = time.monotonic() + tick_interval
        while True:
            # Wait for an inbound message until the next tick is due. A due
            # tick takes precedence, so a busy inbox can't starve ticks.
            inbound: RouteMessage | None = None
            if (timeout := next_tick - time.monotonic()) > 0:
                try:
                    inbound = inbox.get(timeout=timeout)
                except queue.Empty:
                    pass

            match inbound:
                # Periodically tick the node. Like a crossbeam ticker, the next
                # tick is due an interval after this one was handled (missed
                # ticks aren't delivered in a burst).
                case None:
                    next_tick = time.monotonic() + tick_interval
                    try:
                        node = node.tick()
                    except Error as error:
                        panic(f"tick failed: {debug(error)}")

                # Step messages from peers into the node.
                case raft.Envelope():
                    try:
                        node = node.step(inbound)
                    except Error as error:
                        panic(f"step failed: {debug(error)}")

                # Track inbound client requests and step them into the node.
                case (request, response_tx):
                    id = uuid.uuid4()
                    msg = raft.Envelope(
                        from_=node.id(),
                        to=node.id(),
                        term=node.term(),
                        message=raft.Message.ClientRequest(id=id, request=request),
                    )
                    try:
                        node = node.step(msg)
                    except Error as error:
                        panic(f"step failed: {debug(error)}")
                    response_txs[id] = response_tx

            # Send outbound messages from the node to the appropriate peer. If
            # we receive a client response addressed to the local node, forward
            # it to the waiting client via the response channel. The node only
            # emits messages while being stepped or ticked above.
            while True:
                try:
                    msg = node_rx.get_nowait()
                except queue.Empty:
                    break
                if msg.to == node.id() and isinstance(msg.message, raft.Message.ClientResponse):
                    if (response_tx := response_txs.pop(msg.message.id, None)) is not None:
                        response_tx.put(msg.message.response)
                    continue
                if (peer_tx := peers_tx.get(msg.to)) is None:
                    panic("unknown peer")
                try:
                    peer_tx.put_nowait(msg)
                except queue.Full:
                    logger.error("Raft peer channel full, dropping message")

    @staticmethod
    def sql_accept(id: raft.NodeID, listener: socket.socket, sql_engine: sql.engine.Raft) -> None:
        """Accepts new SQL client connections and spawns session threads for
        them."""
        while True:
            try:
                sock, addr = listener.accept()
            except OSError as error:
                logger.error("Client accept failed: %s", net.display_io_error(error))
                continue
            peer = net.display_addr(addr)
            session = sql_engine.session()

            def serve(
                sock: socket.socket = sock,
                peer: str = peer,
                session: Session[sql.engine.raft.Transaction] = session,
            ) -> None:
                logger.debug("Client %s connected", peer)
                try:
                    Server.sql_session(id, sock, session)
                    logger.debug("Client %s disconnected", peer)
                except Error as error:
                    logger.error("Client %s error: %s", peer, error)
                finally:
                    session.close()
                    sock.close()

            threading.Thread(target=serve, daemon=True).start()

    @staticmethod
    def sql_session(
        id: raft.NodeID,
        sock: socket.socket,
        session: Session[sql.engine.raft.Transaction],
    ) -> None:
        """Processes a client SQL session, executing SQL statements against
        the Raft node."""
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with sock.makefile("rb") as reader, sock.makefile("wb") as writer:
            while (request := Request.maybe_decode_from(reader)) is not None:
                # Execute request.
                logger.debug("Received request %s", debug(request))
                response: Response | Error
                try:
                    match request:
                        case Request.Execute(query):
                            response = Response.Execute(session.execute(query))
                        case Request.GetTable(table):
                            response = Response.GetTable(
                                session.with_txn(True, lambda txn: txn.must_get_table(table))
                            )
                        case Request.ListTables():
                            response = Response.ListTables(
                                session.with_txn(
                                    True, lambda txn: [t.name for t in txn.list_tables()]
                                )
                            )
                        case Request.Status():
                            status = session.status()
                            response = Response.Status(
                                Status(server=id, raft=status.raft, mvcc=status.mvcc)
                            )
                        case _:
                            panic(f"unknown request {request!r}")
                except Error as error:
                    response = error

                # Process response.
                name = "Err" if isinstance(response, Error) else "Ok"
                logger.debug("Returning response %s", debug_tuple(name, [response]))
                bincode.serialize_into(writer, response, RESPONSE_RESULT)
                try:
                    writer.flush()
                except OSError as error:
                    raise Error.from_exception(error) from error


class Request(encoding.Value, ABC):
    """A SQL client request."""

    Execute: ClassVar[type["RequestExecute"]]
    GetTable: ClassVar[type["RequestGetTable"]]
    ListTables: ClassVar[type["RequestListTables"]]
    Status: ClassVar[type["RequestStatus"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case RequestExecute(statement):
                serializer.serialize_newtype_variant(0)
                serializer.serialize_str(statement)
            case RequestGetTable(table):
                serializer.serialize_newtype_variant(1)
                serializer.serialize_str(table)
            case RequestListTables():
                serializer.serialize_unit_variant(2)
            case RequestStatus():
                serializer.serialize_unit_variant(3)
            case _:
                panic(f"unknown request {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Request":
        match deserializer.deserialize_enum():
            case 0:
                return RequestExecute(deserializer.deserialize_str())
            case 1:
                return RequestGetTable(deserializer.deserialize_str())
            case 2:
                return RequestListTables()
            case 3:
                return RequestStatus()
            case index:
                raise deserializer.invalid_variant(index, 4)


@dataclass(frozen=True)
class RequestExecute(Request):
    """Executes a SQL statement. This is `Request::Execute` in Rust: use it as
    `Request.Execute`."""

    statement: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Execute", [self.statement], pretty)


@dataclass(frozen=True)
class RequestGetTable(Request):
    """Fetches the given table schema. This is `Request::GetTable` in Rust:
    use it as `Request.GetTable`."""

    table: str

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("GetTable", [self.table], pretty)


@dataclass(frozen=True)
class RequestListTables(Request):
    """Lists all tables. This is `Request::ListTables` in Rust: use it as
    `Request.ListTables`."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return "ListTables"


@dataclass(frozen=True)
class RequestStatus(Request):
    """Returns server status. This is `Request::Status` in Rust: use it as
    `Request.Status`."""

    def fmt_debug(self, pretty: bool = False) -> str:
        return "Status"


Request.Execute = RequestExecute
Request.GetTable = RequestGetTable
Request.ListTables = RequestListTables
Request.Status = RequestStatus


ROW: serde.Serde[Row | None] = serde.Option(serde.Vec(Value))
"""The encoding of an `Option<Row>`."""

TABLE_NAMES: serde.Serde[list[str]] = serde.Vec(serde.String)
"""The encoding of a `Vec<String>` of table names."""


class Response(encoding.Value, ABC):
    """A SQL server response."""

    Execute: ClassVar[type["ResponseExecute"]]
    Row: ClassVar[type["ResponseRow"]]
    GetTable: ClassVar[type["ResponseGetTable"]]
    ListTables: ClassVar[type["ResponseListTables"]]
    Status: ClassVar[type["ResponseStatus"]]

    def serialize(self, serializer: Serializer) -> None:
        match self:
            case ResponseExecute(result):
                serializer.serialize_newtype_variant(0)
                result.serialize(serializer)
            case ResponseRow(row):
                serializer.serialize_newtype_variant(1)
                ROW.serialize(row, serializer)
            case ResponseGetTable(table):
                serializer.serialize_newtype_variant(2)
                table.serialize(serializer)
            case ResponseListTables(tables):
                serializer.serialize_newtype_variant(3)
                TABLE_NAMES.serialize(tables, serializer)
            case ResponseStatus(status):
                serializer.serialize_newtype_variant(4)
                status.serialize(serializer)
            case _:
                panic(f"unknown response {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Response":
        match deserializer.deserialize_enum():
            case 0:
                return ResponseExecute(StatementResult.deserialize(deserializer))
            case 1:
                return ResponseRow(ROW.deserialize(deserializer))
            case 2:
                return ResponseGetTable(Table.deserialize(deserializer))
            case 3:
                return ResponseListTables(TABLE_NAMES.deserialize(deserializer))
            case 4:
                return ResponseStatus(Status.deserialize(deserializer))
            case index:
                raise deserializer.invalid_variant(index, 5)


@dataclass(frozen=True)
class ResponseExecute(Response):
    """This is `Response::Execute` in Rust: use it as `Response.Execute`."""

    result: StatementResult

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Execute", [self.result], pretty)


@dataclass(frozen=True)
class ResponseRow(Response):
    """This is `Response::Row` in Rust: use it as `Response.Row`."""

    row: Row | None

    def fmt_debug(self, pretty: bool = False) -> str:
        row = "None" if self.row is None else debug_tuple("Some", [self.row], pretty)
        return f"Row({row})"


@dataclass(frozen=True)
class ResponseGetTable(Response):
    """This is `Response::GetTable` in Rust: use it as `Response.GetTable`."""

    table: Table

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("GetTable", [self.table], pretty)


@dataclass(frozen=True)
class ResponseListTables(Response):
    """This is `Response::ListTables` in Rust: use it as `Response.ListTables`."""

    tables: list[str]

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("ListTables", [self.tables], pretty)


@dataclass(frozen=True)
class ResponseStatus(Response):
    """This is `Response::Status` in Rust: use it as `Response.Status`."""

    status: "Status"

    def fmt_debug(self, pretty: bool = False) -> str:
        return debug_tuple("Status", [self.status], pretty)


Response.Execute = ResponseExecute
Response.Row = ResponseRow
Response.GetTable = ResponseGetTable
Response.ListTables = ResponseListTables
Response.Status = ResponseStatus


@dataclass(frozen=True)
class Status:
    """SQL server status."""

    server: raft.NodeID
    raft: raft.Status
    mvcc: storage.mvcc.Status

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u8(self.server)
        self.raft.serialize(serializer)
        self.mvcc.serialize(serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Status":
        deserializer.deserialize_struct()
        return cls(
            server=deserializer.deserialize_u8(),
            raft=raft.Status.deserialize(deserializer),
            mvcc=storage.mvcc.Status.deserialize(deserializer),
        )


RESPONSE_RESULT: serde.Result[Response] = serde.Result(Response)
"""The encoding of a `Result<Response>`, as sent to clients."""
