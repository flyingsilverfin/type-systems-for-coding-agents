"""Formats raw keys and values, recursively where necessary. Handles both both
Raft, MVCC, SQL, and raw binary data.

The Rust formatters are zero-sized types with static methods, parameterized
over the inner formatter (e.g. `MVCC::<SQL>::key(key)`). In Python they
are ordinary objects wrapping the inner formatter: `MVCC(SQL()).key(key)`.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

import toydb.sql.engine
import toydb.sql.types
from toydb import sql
from toydb.encoding import bincode, serde
from toydb.error import Error, Panic
from toydb.fmt import debug, escape_default
from toydb.storage import mvcc


class Formatter(ABC):
    """Formats encoded keys and values."""

    @abstractmethod
    def key(self, key: bytes) -> str:
        """Formats a key."""

    @abstractmethod
    def value(self, key: bytes, value: bytes) -> str:
        """Formats a value. Also takes the key to determine the kind of value."""

    def key_value(self, key: bytes, value: bytes) -> str:
        """Formats a key/value pair."""
        return self.key_maybe_value(key, value)

    def key_maybe_value(self, key: bytes, value: bytes | None) -> str:
        """Formats a key/value pair, where the value may not exist."""
        fmtkey = self.key(key)
        fmtvalue = "None" if value is None else self.value(key, value)
        return f"{fmtkey} → {fmtvalue}"


class Raw(Formatter):
    """Formats raw byte slices without any decoding."""

    def key(self, key: bytes) -> str:
        return self.bytes(key)

    def value(self, key: bytes, value: bytes) -> str:
        return self.bytes(value)

    # NB: defined last, since it shadows the builtin bytes type in the class.
    @staticmethod
    def bytes(bytes: bytes) -> str:
        """Formats raw bytes as escaped ASCII strings."""
        return f'"{escape_default(bytes)}"'


@dataclass(frozen=True)
class MVCC(Formatter):
    """Formats MVCC keys/values. Dispatches to the inner formatter to format
    the inner key/value."""

    inner: Formatter

    def key(self, key: bytes) -> str:
        try:
            decoded = mvcc.Key.decode(key)
        except Error:
            return Raw().key(key)  # invalid key
        match decoded:
            case mvcc.Key.TxnWrite(version, innerkey):
                return f"mvcc:TxnWrite({version}, {self.inner.key(innerkey)})"
            case mvcc.Key.Version(innerkey, version):
                return f"mvcc:Version({self.inner.key(innerkey)}, {version})"
            case mvcc.Key.Unversioned(innerkey):
                return f"mvcc:Unversioned({self.inner.key(innerkey)})"
            case mvcc.Key.NextVersion() | mvcc.Key.TxnActive() | mvcc.Key.TxnActiveSnapshot():
                return f"mvcc:{debug(decoded)}"
            case _:
                raise Panic(f"unknown key {decoded!r}")

    def value(self, key: bytes, value: bytes) -> str:
        try:
            decoded = mvcc.Key.decode(key)
        except Error:
            return Raw.bytes(value)  # invalid key
        try:
            match decoded:
                case mvcc.Key.NextVersion():
                    # A mvcc::Version.
                    return str(bincode.deserialize(serde.U64, value))
                case mvcc.Key.TxnActiveSnapshot():
                    active = bincode.deserialize(serde.BTreeSet(serde.U64), value)
                    return f"{{{','.join(str(version) for version in sorted(active))}}}"
                case mvcc.Key.TxnActive() | mvcc.Key.TxnWrite():
                    return Raw.bytes(value)
                case mvcc.Key.Version(userkey, _):
                    uservalue = bincode.deserialize(serde.Option(serde.Bytes), value)
                    if uservalue is None:
                        return "None"
                    return self.inner.value(userkey, uservalue)
                case mvcc.Key.Unversioned(userkey):
                    return self.inner.value(userkey, value)
                case _:
                    raise Panic(f"unknown key {decoded!r}")
        except Error:
            return Raw.bytes(value)


class SQL(Formatter):
    """Formats SQL keys/values."""

    @staticmethod
    def values(values: Iterable["sql.types.Value"]) -> str:
        """Formats a list of SQL values."""
        return ",".join(str(value) for value in values)

    @staticmethod
    def schema(table: "sql.types.Table") -> str:
        """Formats a table schema."""
        # Put it all on a single line.
        return re.sub(r"\n\s*", " ", str(table))

    def key(self, key: bytes) -> str:
        try:
            decoded = sql.engine.Key.decode(key)
        except Error:
            return Raw().key(key)  # invalid key
        match decoded:
            case sql.engine.Key.Table(name):
                return f"sql:Table({name})"
            case sql.engine.Key.Index(table, column, value):
                return f"sql:Index({table}.{column}, {value})"
            case sql.engine.Key.Row(table, id):
                return f"sql:Row({table}, {id})"
            case _:
                raise Panic(f"unknown key {decoded!r}")

    def value(self, key: bytes, value: bytes) -> str:
        try:
            decoded = sql.engine.Key.decode(key)
        except Error:
            return Raw().key(value)
        try:
            match decoded:
                case sql.engine.Key.Table():
                    return self.schema(bincode.deserialize(sql.types.Table, value))
                case sql.engine.Key.Row():
                    # A sql::types::Row.
                    row = bincode.deserialize(serde.Vec(sql.types.Value), value)
                    return self.values(row)
                case sql.engine.Key.Index():
                    # A BTreeSet<sql::types::Value>. Decoded as a list (which has
                    # the same encoding) to retain the set's sort order.
                    index = bincode.deserialize(serde.Vec(sql.types.Value), value)
                    return self.values(index)
                case _:
                    raise Panic(f"unknown key {decoded!r}")
        except Error:
            return Raw.bytes(value)
