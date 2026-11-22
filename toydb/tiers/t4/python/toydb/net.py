"""TCP networking helpers, standing in for `std::net` (`ToSocketAddrs`,
`TcpListener::bind`, `TcpStream::connect` and `SocketAddr` display).

Has no Rust module counterpart: the Rust code uses `std::net` directly. Like
`std::net`, these raise `OSError` (`std::io::Error`), which callers convert
to `Error.IO` via `Error.from_exception()` where the Rust code uses `?`.
"""

import socket

type Addr = str | tuple[str, int]
"""A socket address to resolve, like Rust's `impl ToSocketAddrs`: either a
`host:port` string or a `(host, port)` tuple."""


def to_socket_addrs(addr: Addr) -> list[tuple[int, tuple[str, int] | tuple[str, int, int, int]]]:
    """Resolves an address into the (family, sockaddr) pairs it refers to, in
    resolution order."""
    match addr:
        case str():
            host, sep, port = addr.rpartition(":")
            if not sep or not port.isdigit() or int(port) > 0xFFFF:
                raise OSError("invalid socket address")
            if host.startswith("[") and host.endswith("]"):
                host = host[1:-1]  # IPv6 literal
            resolved: tuple[str, int] = (host, int(port))
        case (host, port):
            resolved = (host, port)
    try:
        infos = socket.getaddrinfo(*resolved, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise OSError(f"failed to lookup address information: {error.strerror}") from error
    return [(family, sockaddr) for family, _, _, _, sockaddr in infos]


def connect(addr: Addr) -> socket.socket:
    """Opens a TCP connection to an address, trying each resolved address in
    turn like `TcpStream::connect`."""
    last_error: OSError | None = None
    for family, sockaddr in to_socket_addrs(addr):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.connect(sockaddr)
        except OSError as error:
            sock.close()
            last_error = error
            continue
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock
    if last_error is not None:
        raise last_error
    raise OSError("could not resolve to any addresses")


def bind(addr: Addr) -> socket.socket:
    """Binds a listening TCP socket to the first resolved address that
    succeeds, like `TcpListener::bind` (which also sets `SO_REUSEADDR`)."""
    last_error: OSError | None = None
    for family, sockaddr in to_socket_addrs(addr):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
            sock.listen(128)
        except OSError as error:
            sock.close()
            last_error = error
            continue
        return sock
    if last_error is not None:
        raise last_error
    raise OSError("could not resolve to any addresses")


def display_addr(sockaddr: tuple[str, int] | tuple[str, int, int, int]) -> str:
    """Formats a socket address like Rust's `SocketAddr` display, e.g.
    `127.0.0.1:9601` or `[::1]:9601`."""
    host, port = sockaddr[0], sockaddr[1]
    if ":" in host:
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def display_io_error(error: OSError) -> str:
    """Formats an OS error like Rust's `std::io::Error` display, e.g.
    `Connection refused (os error 111)`."""
    if error.errno is not None and error.strerror is not None:
        return f"{error.strerror} (os error {error.errno})"
    return str(error)
