"""Formats raw keys and values, recursively where necessary. Handles both both
Raft, MVCC, SQL, and raw binary data.

The Rust formatters are zero-sized types with static methods, parameterized
over the inner formatter (e.g. `Raft::<SQLCommand>::key(key)`). In Python they
are ordinary objects wrapping the inner formatter: `Raft(SQLCommand()).key(key)`.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import toydb.sql.engine
import toydb.sql.types
from toydb import sql
from toydb.encoding import bincode, serde
from toydb.error import Error, Panic
from toydb.fmt import debug, escape_default
from toydb.sql.types.check import CHECKS, Check
from toydb.sql.types.matview import MaterializedView
from toydb.sql.types.policy import Policy
from toydb.sql.types.statistics import TableStatistics
from toydb.sql.types.user import PRIVILEGES, User
from toydb.sql.types.view import View
from toydb.storage import mvcc

if TYPE_CHECKING:
    from toydb import raft


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
class Raft(Formatter):
    """Formats Raft log entries. Dispatches to the inner formatter to format
    each Raft command."""

    inner: Formatter

    def entry(self, entry: "raft.Entry") -> str:
        """Formats a Raft entry."""
        fmtcommand = "None" if entry.command is None else self.inner.value(b"", entry.command)
        return f"{entry.index}@{entry.term} {fmtcommand}"

    def key(self, key: bytes) -> str:
        from toydb import raft

        try:
            decoded = raft.Key.decode(key)
        except Error:
            return Raw().key(key)  # invalid key
        return f"raft:{debug(decoded)}"

    def value(self, key: bytes, value: bytes) -> str:
        from toydb import raft

        try:
            decoded = raft.Key.decode(key)
        except Error:
            return Raw().value(key, value)  # invalid key
        try:
            match decoded:
                case raft.Key.CommitIndex():
                    # A (raft::Index, raft::Term) tuple.
                    index, term = bincode.deserialize(serde.Tuple(serde.U64, serde.U64), value)
                    return f"{index}@{term}"
                case raft.Key.TermVote():
                    # A (raft::Term, Option<raft::NodeID>) tuple.
                    term, vote = bincode.deserialize(
                        serde.Tuple(serde.U64, serde.Option(serde.U8)), value
                    )
                    return f"term={term} vote={'None' if vote is None else vote}"
                case raft.Key.Entry():
                    return self.entry(bincode.deserialize(raft.Entry, value))
                case _:
                    raise Panic(f"unknown key {decoded!r}")
        except Error:
            return Raw.bytes(value)


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
    def checks(checks: list[Check]) -> str:
        """Formats a table's CHECK constraints."""
        return ", ".join(str(check) for check in checks)

    @staticmethod
    def schema(table: "sql.types.Table") -> str:
        """Formats a table schema."""
        # Put it all on a single line.
        return re.sub(r"\n\s*", " ", str(table))

    def key(self, key: bytes) -> str:
        # Special-case the Raft applied index key.
        if key == sql.engine.Raft.APPLIED_INDEX_KEY:
            return key.decode(errors="replace")
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
            case sql.engine.Key.Statistics(table):
                return f"sql:Statistics({table})"
            case sql.engine.Key.View(name):
                return f"sql:View({name})"
            case sql.engine.Key.User(name):
                return f"sql:User({name})"
            case sql.engine.Key.Membership(member, user):
                return f"sql:Membership({member}, {user})"
            case sql.engine.Key.Grant(object, grantee):
                return f"sql:Grant({object}, {grantee})"
            case sql.engine.Key.Owner(object):
                return f"sql:Owner({object})"
            case sql.engine.Key.Checks(table):
                return f"sql:Checks({table})"
            case sql.engine.Key.Policy(table, name):
                return f"sql:Policy({table}, {name})"
            case sql.engine.Key.MaterializedView(name):
                return f"sql:MaterializedView({name})"
            case sql.engine.Key.MaterializedViewRow(name, position):
                return f"sql:MaterializedViewRow({name}, {position})"
            case _:
                raise Panic(f"unknown key {decoded!r}")

    def value(self, key: bytes, value: bytes) -> str:
        # Special-case the applied_index key.
        if key == sql.engine.Raft.APPLIED_INDEX_KEY:
            try:
                # A raft::Index.
                return str(bincode.deserialize(serde.U64, value))
            except Error:
                pass

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
                case sql.engine.Key.Statistics():
                    return str(bincode.deserialize(TableStatistics, value))
                case sql.engine.Key.View():
                    return str(bincode.deserialize(View, value))
                case sql.engine.Key.User():
                    return str(bincode.deserialize(User, value))
                case sql.engine.Key.Membership():
                    return Raw.bytes(value)
                case sql.engine.Key.Grant():
                    privileges = bincode.deserialize(PRIVILEGES, value)
                    return ",".join(str(privilege) for privilege in privileges)
                case sql.engine.Key.Owner():
                    return bincode.deserialize(serde.String, value)
                case sql.engine.Key.Checks():
                    return SQL.checks(bincode.deserialize(CHECKS, value))
                case sql.engine.Key.Policy():
                    return str(bincode.deserialize(Policy, value))
                case sql.engine.Key.MaterializedView():
                    return str(bincode.deserialize(MaterializedView, value))
                case sql.engine.Key.MaterializedViewRow():
                    row = bincode.deserialize(serde.Vec(sql.types.Value), value)
                    return self.values(row)
                case _:
                    raise Panic(f"unknown key {decoded!r}")
        except Error:
            return Raw.bytes(value)


class SQLCommand(Formatter):
    """Formats SQL Raft write commands, from the Raft log."""

    def key(self, key: bytes) -> str:
        # There is no key, since these are wrapped in a Raft log entry.
        raise Panic("SQL commands don't have a key")

    def value(self, key: bytes, value: bytes) -> str:
        Write = sql.engine.Write
        try:
            write = Write.decode(value)
        except Error:
            return Raw.bytes(value)

        match write:
            case Write.Begin():
                txn = None
            case Write.Commit(txn) | Write.Rollback(txn):
                pass
            case (
                Write.Delete(txn=txn)
                | Write.Insert(txn=txn)
                | Write.Update(txn=txn)
                | Write.CreateTable(txn=txn)
                | Write.DropTable(txn=txn)
                | Write.SetStatistics(txn=txn)
                | Write.CreateView(txn=txn)
                | Write.DropView(txn=txn)
                | Write.CreateUser(txn=txn)
                | Write.DropUser(txn=txn)
                | Write.SetMembership(txn=txn)
                | Write.SetGrant(txn=txn)
                | Write.SetOwner(txn=txn)
                | Write.SetChecks(txn=txn)
                | Write.CreatePolicy(txn=txn)
                | Write.DropPolicy(txn=txn)
                | Write.CreateMatView(txn=txn)
                | Write.RefreshMatView(txn=txn)
                | Write.DropMatView(txn=txn)
            ):
                pass
            case _:
                raise Panic(f"unknown write {write!r}")
        fmttxn = f"t{txn.version} " if txn is not None and not txn.read_only else ""

        match write:
            case Write.Begin():
                fmtcommand = "BEGIN"
            case Write.Commit():
                fmtcommand = "COMMIT"
            case Write.Rollback():
                fmtcommand = "ROLLBACK"
            case Write.Delete(table=table, ids=ids):
                fmtcommand = f"DELETE {table} {','.join(str(id) for id in ids)}"
            case Write.Insert(table=table, rows=rows):
                fmtrows = " ".join(f"({SQL.values(row)})" for row in rows)
                fmtcommand = f"INSERT {table} {fmtrows}"
            case Write.Update(table=table, rows=rows):
                fmtrows = " ".join(f"{id}→({SQL.values(row)})" for id, row in rows)
                fmtcommand = f"UPDATE {table} {fmtrows}"
            case Write.CreateTable(schema=schema):
                fmtcommand = SQL.schema(schema)
            case Write.DropTable(table=table):
                fmtcommand = f"DROP TABLE {table}"
            case Write.SetStatistics(table=table, statistics=statistics):
                fmtcommand = f"STATISTICS {table} {statistics}"
            case Write.CreateView(view=view):
                fmtcommand = str(view)
            case Write.DropView(view=view):
                fmtcommand = f"DROP VIEW {view}"
            case Write.CreateUser(user=user):
                fmtcommand = f"CREATE USER {user}"
            case Write.DropUser(user=user):
                fmtcommand = f"DROP USER {user}"
            case Write.SetMembership(member=member, user=user, is_member=is_member):
                fmtcommand = f"{'' if is_member else 'NO '}MEMBERSHIP {member} OF {user}"
            case Write.SetGrant(object=object, grantee=grantee, privileges=privileges):
                fmtprivileges = ",".join(str(privilege) for privilege in privileges)
                fmtcommand = f"PRIVILEGES {object} {grantee} ({fmtprivileges})"
            case Write.SetOwner(object=object, owner=owner):
                fmtcommand = f"OWNER {object} {owner}"
            case Write.SetChecks(table=table, checks=checks):
                fmtcommand = f"CHECKS {table} ({SQL.checks(checks)})"
            case Write.CreatePolicy(policy=policy):
                fmtcommand = str(policy)
            case Write.DropPolicy(table=table, name=name):
                fmtcommand = f"DROP POLICY {name} ON {table}"
            case Write.CreateMatView(matview=matview, rows=rows):
                fmtrows = " ".join(f"({SQL.values(row)})" for row in rows)
                fmtcommand = f"{matview} ROWS {fmtrows}"
            case Write.RefreshMatView(name=name, rows=rows):
                fmtrows = " ".join(f"({SQL.values(row)})" for row in rows)
                fmtcommand = f"REFRESH MATERIALIZED VIEW {name} ROWS {fmtrows}"
            case Write.DropMatView(name=name):
                fmtcommand = f"DROP MATERIALIZED VIEW {name}"
            case _:
                raise Panic(f"unknown write {write!r}")
        return f"{fmttxn}{fmtcommand}"
