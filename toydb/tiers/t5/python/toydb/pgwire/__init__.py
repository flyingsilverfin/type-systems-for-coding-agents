"""A server for a subset of the PostgreSQL frontend/backend protocol, version
3.0, so that PostgreSQL clients can use toydb. Each connection has a session
of its own, with the startup message's user as its login user; its statements
are executed exactly as those of a client connection.
"""

import logging
import socket
import threading
from typing import Any

from toydb import net
from toydb.error import Error
from toydb.pgwire.connection import Connection
from toydb.sql.engine import Engine

logger = logging.getLogger(__name__)


def serve(listener: socket.socket, engine: Engine[Any]) -> None:
    """Accepts PostgreSQL connections and serves each in a thread of its own."""
    while True:
        try:
            sock, addr = listener.accept()
        except OSError as error:
            logger.error("PostgreSQL client accept failed: %s", net.display_io_error(error))
            continue
        peer = net.display_addr(addr)

        def run(sock: socket.socket = sock, peer: str = peer) -> None:
            logger.debug("PostgreSQL client %s connected", peer)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try:
                with sock.makefile("rb") as reader, sock.makefile("wb") as writer:
                    Connection(engine, reader, writer).serve()
                logger.debug("PostgreSQL client %s disconnected", peer)
            except (Error, OSError) as error:
                logger.error("PostgreSQL client %s error: %s", peer, Error.from_exception(error))
            finally:
                sock.close()

        threading.Thread(target=run, daemon=True).start()
