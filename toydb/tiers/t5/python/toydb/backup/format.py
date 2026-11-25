"""The backup file format, version 1, and its listing.

The file is `TOYDBBAK`, a u16 version, records (a u8 kind, a u32 length, the
payload), an END record with the number of records, and the CRC-32 of
everything before it. Integers are big-endian; a string is a u32 length and
UTF-8.

* A data type is written as its type tag: BOOLEAN 1, INTEGER 2, FLOAT 3,
  STRING 4. A data type added later gets the next unused tag, in the order the
  types are added.
* A value is written as NULL (tag 0) or as its type's tag and a payload:
  BOOLEAN one byte, INTEGER eight bytes, FLOAT its eight IEEE 754 bytes,
  STRING a string, a value of a data type added later its display in query
  results as a string.
* Records: USER, MEMBER, TABLE, CHECK, ROW, VIEW, MATVIEW, GRANT, POLICY, END,
  written in this order (tables after the tables they reference, each with its
  checks and rows; views and materialized views after those they depend on). A
  USER holds the stored password hash.

Reading checks the header, the version and the CRC before the records; an
unknown record kind, type tag or value tag is an error.
"""

import enum
import struct
import zlib
from abc import ABC
from collections.abc import Iterator
from dataclasses import dataclass
from typing import ClassVar

from toydb.error import Error, InvalidInput, errinput, panic
from toydb.sql.engine.changes import format_table
from toydb.sql.types import Column, DataType, Row, Table, Value
from toydb.sql.types.check import Check
from toydb.sql.types.policy import Policy
from toydb.sql.types.user import Grant, Privilege, User

MAGIC = b"TOYDBBAK"
"""The first bytes of a backup file."""

VERSION = 1
"""The format version."""

HEADER_SIZE = len(MAGIC) + 2
"""The size of the magic bytes and the version."""

CHECKSUM_SIZE = 4
"""The size of the CRC at the end of the file."""

NULL_TAG = 0
"""The value tag of NULL."""

TYPE_TAGS: dict[DataType, int] = {
    DataType.Boolean: 1,
    DataType.Integer: 2,
    DataType.Float: 3,
    DataType.String: 4,
}
"""The type tags of the data types."""

DATATYPES: dict[int, DataType] = {tag: datatype for datatype, tag in TYPE_TAGS.items()}
"""The data types, by type tag."""


class Kind(enum.IntEnum):
    """A record kind."""

    User = 1
    Member = 2
    Table = 3
    Check = 4
    Row = 5
    View = 6
    Grant = 7
    Policy = 8
    MatView = 9
    End = 255


class Record(ABC):
    """A backup record."""

    User: ClassVar[type["UserRecord"]]
    Member: ClassVar[type["MemberRecord"]]
    Table: ClassVar[type["TableRecord"]]
    Check: ClassVar[type["CheckRecord"]]
    Row: ClassVar[type["RowRecord"]]
    View: ClassVar[type["ViewRecord"]]
    Grant: ClassVar[type["GrantRecord"]]
    Policy: ClassVar[type["PolicyRecord"]]
    MatView: ClassVar[type["MatViewRecord"]]


@dataclass(frozen=True)
class UserRecord(Record):
    """A user, with its stored password hash. Use it as `Record.User`."""

    user: User


@dataclass(frozen=True)
class MemberRecord(Record):
    """A membership. Use it as `Record.Member`."""

    member: str
    user: str
    """The user that member is a member of."""


@dataclass(frozen=True)
class TableRecord(Record):
    """A table definition and its owner. Use it as `Record.Table`."""

    table: Table
    owner: str


@dataclass(frozen=True)
class CheckRecord(Record):
    """A CHECK constraint of a table. Use it as `Record.Check`."""

    table: str
    check: Check


@dataclass(frozen=True)
class RowRecord(Record):
    """A table row. Use it as `Record.Row`."""

    table: str
    row: Row


@dataclass(frozen=True)
class ViewRecord(Record):
    """A view definition and its owner. Use it as `Record.View`."""

    name: str
    owner: str
    columns: list[str]
    query: str


@dataclass(frozen=True)
class GrantRecord(Record):
    """The privileges granted to a user on a table or view. Use it as
    `Record.Grant`."""

    grant: Grant


@dataclass(frozen=True)
class PolicyRecord(Record):
    """A row-level security policy. Use it as `Record.Policy`."""

    policy: Policy


@dataclass(frozen=True)
class MatViewRecord(Record):
    """A materialized view definition and its owner. Use it as
    `Record.MatView`."""

    name: str
    owner: str
    columns: list[str]
    query: str


Record.User = UserRecord
Record.Member = MemberRecord
Record.Table = TableRecord
Record.Check = CheckRecord
Record.Row = RowRecord
Record.View = ViewRecord
Record.Grant = GrantRecord
Record.Policy = PolicyRecord
Record.MatView = MatViewRecord

ORDER: list[type[Record]] = [
    UserRecord,
    MemberRecord,
    TableRecord,
    CheckRecord,
    RowRecord,
    ViewRecord,
    MatViewRecord,
    GrantRecord,
    PolicyRecord,
]
"""The record types, in the order of a diff."""


def invalid(reason: str) -> InvalidInput:
    """Returns the error of an invalid backup."""
    return errinput(f"invalid backup: {reason}")


class Encoder:
    """Encodes the fields of a record payload."""

    def __init__(self) -> None:
        self.output = bytearray()

    def u8(self, value: int) -> None:
        self.output += struct.pack(">B", value)

    def u32(self, value: int) -> None:
        self.output += struct.pack(">I", value)

    def bool(self, value: bool) -> None:
        self.u8(1 if value else 0)

    def string(self, value: str) -> None:
        encoded = value.encode()
        self.u32(len(encoded))
        self.output += encoded

    def optional_string(self, value: str | None) -> None:
        if value is None:
            self.u8(0)
        else:
            self.u8(1)
            self.string(value)

    def datatype(self, datatype: DataType) -> None:
        """Writes the type tag of a data type."""
        self.u8(TYPE_TAGS[datatype])

    def value(self, value: Value) -> None:
        """Writes a value: its type tag and payload, or the NULL tag."""
        if (datatype := value.datatype()) is None:
            self.u8(NULL_TAG)
            return
        self.datatype(datatype)
        match value:
            case Value.Boolean(v):
                self.bool(v)
            case Value.Integer(v):
                self.output += struct.pack(">q", v)
            case Value.Float(v):
                self.output += struct.pack(">d", v)
            case Value.String(v):
                self.string(v)
            case _:
                panic(f"unknown value {value!r}")


class Decoder:
    """Decodes the fields of a record payload, or the records of a file."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def at_end(self) -> bool:
        """Returns true if all bytes have been read."""
        return self.position >= len(self.data)

    def take(self, length: int) -> bytes:
        """Reads the given number of bytes."""
        if self.position + length > len(self.data):
            raise invalid("truncated")
        data = self.data[self.position : self.position + length]
        self.position += length
        return data

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        value: int = struct.unpack(">I", self.take(4))[0]
        return value

    def bool(self) -> bool:
        return self.u8() != 0

    def string(self) -> str:
        data = self.take(self.u32())
        try:
            return data.decode()
        except UnicodeDecodeError:
            raise invalid("invalid string") from None

    def optional_string(self) -> str | None:
        return self.string() if self.bool() else None

    def datatype(self) -> DataType:
        """Reads a type tag, erroring on an unknown tag."""
        tag = self.u8()
        if (datatype := DATATYPES.get(tag)) is None:
            raise invalid(f"unknown type tag {tag}")
        return datatype

    def value(self) -> Value:
        """Reads a value, erroring on an unknown tag."""
        tag = self.u8()
        if tag == NULL_TAG:
            return Value.Null()
        if (datatype := DATATYPES.get(tag)) is None:
            raise invalid(f"unknown value tag {tag}")
        match datatype:
            case DataType.Boolean:
                return Value.Boolean(self.bool())
            case DataType.Integer:
                return Value.Integer(struct.unpack(">q", self.take(8))[0])
            case DataType.Float:
                return Value.Float(struct.unpack(">d", self.take(8))[0])
            case DataType.String:
                return Value.String(self.string())
            case _:
                panic(f"unknown data type {datatype}")


def encode_record(record: Record) -> tuple[Kind, bytes]:
    """Encodes a record as its kind and payload."""
    payload = Encoder()
    match record:
        case Record.User(user):
            kind = Kind.User
            payload.string(user.name)
            payload.bool(user.superuser)
            payload.optional_string(user.password)
        case Record.Member(member, user):
            kind = Kind.Member
            payload.string(member)
            payload.string(user)
        case Record.Table(table, owner):
            kind = Kind.Table
            payload.string(table.name)
            payload.string(owner)
            payload.u32(table.primary_key)
            payload.u32(len(table.columns))
            for column in table.columns:
                payload.string(column.name)
                payload.datatype(column.datatype)
                payload.bool(column.nullable)
                payload.bool(column.default is not None)
                if column.default is not None:
                    payload.value(column.default)
                payload.bool(column.unique)
                payload.bool(column.index)
                payload.optional_string(column.references)
        case Record.Check(table, check):
            kind = Kind.Check
            payload.string(table)
            payload.string(check.name)
            payload.string(check.condition)
        case Record.Row(table, row):
            kind = Kind.Row
            payload.string(table)
            payload.u32(len(row))
            for value in row:
                payload.value(value)
        case Record.View(name, owner, columns, query):
            kind = Kind.View
            payload.string(name)
            payload.string(owner)
            payload.u32(len(columns))
            for column in columns:
                payload.string(column)
            payload.string(query)
        case Record.MatView(name, owner, columns, query):
            kind = Kind.MatView
            payload.string(name)
            payload.string(owner)
            payload.u32(len(columns))
            for column in columns:
                payload.string(column)
            payload.string(query)
        case Record.Grant(grant):
            kind = Kind.Grant
            payload.string(grant.object)
            payload.string(grant.grantee)
            payload.u8(sum(1 << privilege.value for privilege in set(grant.privileges)))
        case Record.Policy(policy):
            kind = Kind.Policy
            payload.string(policy.table)
            payload.string(policy.name)
            payload.optional_string(policy.user)
            payload.string(policy.condition)
            payload.optional_string(policy.check)
        case _:
            panic(f"unknown record {record!r}")
    return kind, bytes(payload.output)


def decode_record(kind: int, payload: Decoder) -> Record:
    """Decodes a record payload of the given kind."""
    match kind:
        case Kind.User:
            name, superuser = payload.string(), payload.bool()
            return Record.User(User(name=name, superuser=superuser, password=payload.optional_string()))
        case Kind.Member:
            return Record.Member(payload.string(), payload.string())
        case Kind.Table:
            name, owner = payload.string(), payload.string()
            primary_key = payload.u32()
            columns: list[Column] = []
            for _ in range(payload.u32()):
                column_name, datatype, nullable = payload.string(), payload.datatype(), payload.bool()
                default = payload.value() if payload.bool() else None
                columns.append(
                    Column(
                        name=column_name,
                        datatype=datatype,
                        nullable=nullable,
                        default=default,
                        unique=payload.bool(),
                        index=payload.bool(),
                        references=payload.optional_string(),
                    )
                )
            return Record.Table(Table(name=name, primary_key=primary_key, columns=columns), owner)
        case Kind.Check:
            table, name = payload.string(), payload.string()
            return Record.Check(table, Check(name=name, condition=payload.string()))
        case Kind.Row:
            table = payload.string()
            return Record.Row(table, [payload.value() for _ in range(payload.u32())])
        case Kind.View:
            name, owner = payload.string(), payload.string()
            columns = [payload.string() for _ in range(payload.u32())]
            return Record.View(name, owner, columns, payload.string())
        case Kind.MatView:
            name, owner = payload.string(), payload.string()
            columns = [payload.string() for _ in range(payload.u32())]
            return Record.MatView(name, owner, columns, payload.string())
        case Kind.Grant:
            object, grantee, bits = payload.string(), payload.string(), payload.u8()
            privileges = [privilege for privilege in Privilege if bits & 1 << privilege.value]
            return Record.Grant(Grant(object=object, grantee=grantee, privileges=privileges))
        case Kind.Policy:
            table, name, user = payload.string(), payload.string(), payload.optional_string()
            condition, check = payload.string(), payload.optional_string()
            return Record.Policy(
                Policy(table=table, name=name, user=user, condition=condition, check=check)
            )
        case _:
            raise invalid(f"unknown record kind {kind}")


class Writer:
    """Writes a backup file."""

    def __init__(self) -> None:
        self.output = bytearray(MAGIC)
        self.output += struct.pack(">H", VERSION)
        self.count = 0
        """The number of records written."""

    def write(self, record: Record) -> None:
        """Writes a record."""
        kind, payload = encode_record(record)
        self.write_payload(kind, payload)
        self.count += 1

    def write_payload(self, kind: Kind, payload: bytes) -> None:
        self.output += struct.pack(">BI", kind, len(payload))
        self.output += payload

    def finish(self) -> bytes:
        """Writes the END record and the CRC, returning the file."""
        self.write_payload(Kind.End, struct.pack(">I", self.count))
        self.output += struct.pack(">I", zlib.crc32(self.output))
        return bytes(self.output)


def encode(records: list[Record]) -> bytes:
    """Encodes records as a backup file."""
    writer = Writer()
    for record in records:
        writer.write(record)
    return writer.finish()


class Reader:
    """Reads a backup file. Creating a reader checks the header and the
    version."""

    def __init__(self, data: bytes) -> None:
        if len(data) < HEADER_SIZE + CHECKSUM_SIZE or not data.startswith(MAGIC):
            raise invalid("not a toydb backup")
        self.version: int = struct.unpack(">H", data[len(MAGIC) : HEADER_SIZE])[0]
        """The format version."""
        if self.version != VERSION:
            raise invalid(f"unsupported version {self.version}")
        self.data = data
        self.checksum: int = struct.unpack(">I", data[-CHECKSUM_SIZE:])[0]
        """The CRC stored at the end of the file."""
        self.count: int | None = None
        """The number of records of the END record, once read."""

    def check_checksum(self) -> None:
        """Checks the CRC."""
        if zlib.crc32(self.data[:-CHECKSUM_SIZE]) != self.checksum:
            raise invalid("checksum mismatch")

    def records(self) -> Iterator[Record]:
        """Reads the records up to the END record, checking the END record."""
        body = Decoder(self.data[HEADER_SIZE:-CHECKSUM_SIZE])
        count = 0
        while not body.at_end():
            kind, length = body.u8(), body.u32()
            payload = Decoder(body.take(length))
            if kind == Kind.End:
                end_count = payload.u32()
                if not body.at_end():
                    raise invalid("data after end record")
                if end_count != count:
                    raise invalid("wrong record count")
                self.count = end_count
                return
            yield decode_record(kind, payload)
            count += 1
        raise invalid("truncated")


def decode(data: bytes) -> list[Record]:
    """Decodes a backup file, checking all of it."""
    reader = Reader(data)
    reader.check_checksum()
    return list(reader.records())


def format_record(record: Record) -> str:
    """Formats a record as its line of toybackup list."""
    match record:
        case Record.User(user):
            line = f"user {user.name}"
            if user.superuser:
                line += " superuser"
            if user.password is not None:
                line += " password"
            return line
        case Record.Member(member, user):
            return f"member {member} of {user}"
        case Record.Table(table, owner):
            return f"table {table.name} owner {owner}: {format_table(table)}"
        case Record.Check(table, check):
            return f"check {table}.{check.name}: {check.condition}"
        case Record.Row(table, row):
            return f"row {table}: {', '.join(str(value) for value in row)}"
        case Record.View(name, owner, columns, query):
            return f"view {name} owner {owner} ({', '.join(columns)}): {query}"
        case Record.MatView(name, owner, columns, query):
            return f"materialized view {name} owner {owner} ({', '.join(columns)}): {query}"
        case Record.Grant(grant):
            privileges = ", ".join(str(privilege) for privilege in sorted(grant.privileges))
            return f"grant {grant.object} to {grant.grantee}: {privileges}"
        case Record.Policy(policy):
            line = f"policy {policy.table}.{policy.name}"
            if policy.user is not None:
                line += f" to {policy.user}"
            line += f": {policy.condition}"
            if policy.check is not None:
                line += f" with check {policy.check}"
            return line
        case _:
            panic(f"unknown record {record!r}")


def list_backup(data: bytes) -> list[str]:
    """Returns the lines of toybackup list: one per record, values displayed
    as in query results."""
    lines: list[str] = []
    try:
        reader = Reader(data)
        lines.append(f"toydb backup version {reader.version}")
        reader.check_checksum()
        for record in reader.records():
            lines.append(format_record(record))
        lines.append(f"end {reader.count} records, checksum {reader.checksum:08x}")
    except Error as error:
        lines.append(f"Error: {error}")
    return lines


def verify(data: bytes) -> int:
    """Checks a backup file, returning its number of records."""
    return len(decode(data))


type Identity = tuple[object, ...]
"""The identity of a record: its position in the diff order and its key."""


def identities(records: list[Record]) -> dict[Identity, Record]:
    """Returns the records of a backup by identity."""
    primary_keys: dict[str, int] = {}
    found: dict[Identity, Record] = {}
    for record in records:
        match record:
            case Record.User(user):
                key: tuple[object, ...] = (user.name,)
            case Record.Member(member, user):
                key = (member, user)
            case Record.Table(table):
                primary_keys[table.name] = table.primary_key
                key = (table.name,)
            case Record.Check(table, check):
                key = (table, check.name)
            case Record.Row(table, row):
                primary_key = primary_keys.get(table, 0)
                key = (table, *row[primary_key : primary_key + 1])
            case Record.View(name) | Record.MatView(name):
                key = (name,)
            case Record.Grant(grant):
                key = (grant.object, grant.grantee)
            case Record.Policy(policy):
                key = (policy.table, policy.name)
            case _:
                panic(f"unknown record {record!r}")
        found[(ORDER.index(type(record)), *key)] = record
    return found


def diff(a: bytes, b: bytes) -> list[str]:
    """Returns the lines of toybackup diff: the records of each identity that
    is in one backup only, or differs, by record kind and identity."""
    old, new = identities(decode(a)), identities(decode(b))
    lines: list[str] = []
    for identity in sorted(old.keys() | new.keys()):  # type: ignore[type-var]
        before, after = old.get(identity), new.get(identity)
        if before == after:
            continue
        if before is not None:
            lines.append(f"- {format_record(before)}")
        if after is not None:
            lines.append(f"+ {format_record(after)}")
    return lines
