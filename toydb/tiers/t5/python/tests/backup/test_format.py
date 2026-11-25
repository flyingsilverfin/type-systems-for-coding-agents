"""Tests for toydb.backup.format.

The format is mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext5/bk_*` (see `tests/sql/test_mod.py`); these tests
cover the encoding of values and records, the checks of the reader and the
listing and comparison of backups directly.
"""

import math
import struct
import unittest
import zlib

from toydb.backup import format
from toydb.backup.format import Decoder, Encoder, Reader, Record
from toydb.error import Error
from toydb.sql.types import Column, DataType, Table, Value
from toydb.sql.types.check import Check
from toydb.sql.types.policy import Policy
from toydb.sql.types.user import Grant, Privilege, User

TABLE = Table(
    "t",
    0,
    [
        Column("id", DataType.Integer, False, None, True, False, None),
        Column("v", DataType.String, True, Value.Null(), False, True, "t"),
    ],
)

RECORDS: list[Record] = [
    Record.User(User("alice", False, "md5abc")),
    Record.User(User("bob", True, None)),
    Record.Member("alice", "bob"),
    Record.Table(TABLE, "alice"),
    Record.Check("t", Check("t_check1", "id > 0")),
    Record.Row("t", [Value.Integer(1), Value.String("a")]),
    Record.Row("t", [Value.Integer(2), Value.Null()]),
    Record.View("v", "admin", ["id"], "SELECT id FROM t"),
    Record.MatView("m", "alice", ["id", "n"], "SELECT id, 1 FROM v"),
    Record.Grant(Grant("t", "bob", [Privilege.Select, Privilege.Delete])),
    Record.Policy(Policy("t", "p", "alice", "id = 1", "id > 0")),
    Record.Policy(Policy("t", "q", None, "TRUE", None)),
]


def file(*records: bytes, count: int | None = None, trailing: bytes = b"") -> bytes:
    """Builds a backup file from encoded records (a kind, length and payload
    each), with an END record of the given count (the number of records by
    default) and a valid CRC."""
    body = b"".join(records)
    end = struct.pack(">BII", 255, 4, len(records) if count is None else count)
    data = format.MAGIC + struct.pack(">H", 1) + body + end + trailing
    return data + struct.pack(">I", zlib.crc32(data))


def record(kind: int, payload: bytes) -> bytes:
    """Encodes a record."""
    return struct.pack(">BI", kind, len(payload)) + payload


def string(value: str) -> bytes:
    """Encodes a string."""
    return struct.pack(">I", len(value.encode())) + value.encode()


class TestValues(unittest.TestCase):
    def encode(self, value: Value) -> bytes:
        encoder = Encoder()
        encoder.value(value)
        return bytes(encoder.output)

    def test_encode(self) -> None:
        """Values are their type tag and payload, NULL the tag 0."""
        cases = [
            (Value.Null(), "00"),
            (Value.Boolean(True), "0101"),
            (Value.Boolean(False), "0100"),
            (Value.Integer(-1), "02ffffffffffffffff"),
            (Value.Integer(-(2**63)), "028000000000000000"),
            (Value.Float(1.5), "033ff8000000000000"),
            (Value.Float(-math.inf), "03fff0000000000000"),
            (Value.String("é"), "0400000002c3a9"),
            (Value.String(""), "0400000000"),
        ]
        for value, expect in cases:
            with self.subTest(value=value):
                self.assertEqual(self.encode(value).hex(), expect)
                self.assertEqual(Decoder(bytes.fromhex(expect)).value(), value)

    def test_datatypes(self) -> None:
        """Every data type has a type tag that reads back as the type."""
        for datatype in DataType:
            with self.subTest(datatype=datatype):
                encoder = Encoder()
                encoder.datatype(datatype)
                self.assertEqual(Decoder(bytes(encoder.output)).datatype(), datatype)
        self.assertEqual(format.TYPE_TAGS[DataType.Boolean], 1)
        self.assertEqual(format.TYPE_TAGS[DataType.String], 4)

    def test_nan(self) -> None:
        """NaN keeps its bits."""
        data = self.encode(Value.Float(math.nan))
        self.assertEqual(data.hex(), "037ff8000000000000")
        value = Decoder(data).value()
        assert isinstance(value, Value.Float)
        self.assertTrue(math.isnan(value.value))

    def test_errors(self) -> None:
        """Unknown tags, truncated payloads and invalid UTF-8 are errors."""
        cases = [
            ("09", Decoder.value, "unknown value tag 9"),
            ("05", Decoder.datatype, "unknown type tag 5"),
            ("0200", Decoder.value, "truncated"),
            ("", Decoder.value, "truncated"),
            ("0400000002ff", Decoder.value, "truncated"),
            ("0400000001ff", Decoder.value, "invalid string"),
        ]
        for data, read, reason in cases:
            with self.subTest(data=data):
                with self.assertRaises(Error) as cm:
                    read(Decoder(bytes.fromhex(data)))
                self.assertEqual(str(cm.exception), f"invalid input: invalid backup: {reason}")


class TestRecords(unittest.TestCase):
    def test_roundtrip(self) -> None:
        """Encoded records decode to the same records, with an END record of
        their count."""
        data = format.encode(RECORDS)
        self.assertEqual(format.decode(data), RECORDS)
        self.assertEqual(format.verify(data), len(RECORDS))

    def test_empty(self) -> None:
        """An empty backup is the header, the END record and the CRC."""
        data = format.encode([])
        self.assertEqual(data.hex(), "544f59444242414b" "0001" "ff0000000400000000" "407b665b")
        self.assertEqual(format.decode(data), [])

    def test_encoding(self) -> None:
        """The payloads of the records."""
        cases: list[tuple[Record, str]] = [
            (Record.User(User("a", True, None)), "01" + "0000000161" + "01" + "00"),
            (Record.Member("a", "b"), "02" + "0000000161" + "0000000162"),
            (Record.Check("t", Check("c", "x")), "04" + "0000000174" + "0000000163" + "0000000178"),
            (Record.Row("t", [Value.Boolean(True)]), "05" + "0000000174" + "00000001" + "0101"),
            (
                Record.View("v", "a", ["x"], "q"),
                "06" + "0000000176" + "0000000161" + "00000001" + "0000000178" + "0000000171",
            ),
            (
                Record.MatView("m", "a", ["x"], "q"),
                "09" + "000000016d" + "0000000161" + "00000001" + "0000000178" + "0000000171",
            ),
            (
                Record.Grant(Grant("t", "a", [Privilege.Insert, Privilege.Update])),
                "07" + "0000000174" + "0000000161" + "06",
            ),
            (
                Record.Policy(Policy("t", "p", None, "x", "y")),
                "08" + "0000000174" + "0000000170" + "00" + "0000000178" + "01" + "0000000179",
            ),
        ]
        for value, expect in cases:
            with self.subTest(record=value):
                kind, payload = format.encode_record(value)
                self.assertEqual(f"{kind:02x}" + payload.hex(), expect)

    def test_table(self) -> None:
        """A TABLE record has the table's definition and owner."""
        kind, payload = format.encode_record(Record.Table(TABLE, "alice"))
        self.assertEqual(kind, format.Kind.Table)
        expect = (
            string("t")
            + string("alice")
            + struct.pack(">II", 0, 2)
            + string("id")
            + bytes.fromhex("02 00 00 01 00 00")
            + string("v")
            + bytes.fromhex("04 01 01 00 00 01 01")
            + string("t")
        )
        self.assertEqual(payload, expect)


class TestReader(unittest.TestCase):
    def error(self, data: bytes) -> str:
        with self.assertRaises(Error) as cm:
            format.decode(data)
        return str(cm.exception).removeprefix("invalid input: invalid backup: ")

    def test_header(self) -> None:
        """The header, the version and the CRC are checked in this order."""
        valid = format.encode([])
        self.assertEqual(self.error(valid[:13]), "not a toydb backup")
        self.assertEqual(self.error(b"TOYDBBAX" + valid[8:]), "not a toydb backup")
        self.assertEqual(self.error(valid[:9] + b"\x02" + valid[10:]), "unsupported version 2")
        self.assertEqual(self.error(valid[:-1] + b"\x00"), "checksum mismatch")
        self.assertEqual(self.error(valid[:14]), "checksum mismatch")
        self.assertEqual(Reader(valid).version, 1)

    def test_records(self) -> None:
        """Invalid records, END records and missing END records."""
        user = record(1, string("alice") + b"\x00\x00")
        self.assertEqual(format.decode(file(user)), [Record.User(User("alice", False, None))])
        self.assertEqual(self.error(file(record(42, b""))), "unknown record kind 42")
        self.assertEqual(self.error(file(record(1, string("alice")))), "truncated")
        self.assertEqual(self.error(file(user, count=2)), "wrong record count")
        self.assertEqual(self.error(file(user, trailing=b"\x00")), "data after end record")
        data = format.MAGIC + struct.pack(">H", 1) + user
        self.assertEqual(self.error(data + struct.pack(">I", zlib.crc32(data))), "truncated")
        self.assertEqual(self.error(file(struct.pack(">BI", 1, 100))), "truncated")

    def test_records_lazily(self) -> None:
        """The reader yields the records before an invalid one."""
        user = record(1, string("alice") + b"\x00\x00")
        reader = Reader(file(user, record(7, b"")))
        reader.check_checksum()
        records = reader.records()
        self.assertEqual(next(records), Record.User(User("alice", False, None)))
        with self.assertRaises(Error):
            next(records)


class TestList(unittest.TestCase):
    def test_list(self) -> None:
        """A line per record, the header and the END record."""
        data = format.encode(RECORDS)
        self.assertEqual(
            format.list_backup(data),
            [
                "toydb backup version 1",
                "user alice password",
                "user bob superuser",
                "member alice of bob",
                "table t owner alice: CREATE TABLE t (id INTEGER PRIMARY KEY, "
                "v STRING DEFAULT NULL INDEX REFERENCES t)",
                "check t.t_check1: id > 0",
                "row t: 1, 'a'",
                "row t: 2, NULL",
                "view v owner admin (id): SELECT id FROM t",
                "materialized view m owner alice (id, n): SELECT id, 1 FROM v",
                "grant t to bob: SELECT, DELETE",
                "policy t.p to alice: id = 1 with check id > 0",
                "policy t.q: TRUE",
                f"end 12 records, checksum {zlib.crc32(data[:-4]):08x}",
            ],
        )

    def test_invalid(self) -> None:
        """The lines before the problem, then the error."""
        user = record(1, string("alice") + b"\x01\x00")
        self.assertEqual(
            format.list_backup(file(user, count=0)),
            [
                "toydb backup version 1",
                "user alice superuser",
                "Error: invalid input: invalid backup: wrong record count",
            ],
        )
        self.assertEqual(
            format.list_backup(b"TOYDBBAK\x00\x09\x00\x00\x00\x00"),
            ["Error: invalid input: invalid backup: unsupported version 9"],
        )


class TestDiff(unittest.TestCase):
    def test_identical(self) -> None:
        data = format.encode(RECORDS)
        self.assertEqual(format.diff(data, data), [])

    def test_diff(self) -> None:
        """Records of one backup only, and records that differ, by kind and
        identity."""
        a = format.encode(RECORDS)
        b = format.encode(
            [
                Record.User(User("alice", False, "md5other")),
                Record.Table(TABLE, "alice"),
                Record.Row("t", [Value.Integer(2), Value.String("b")]),
                Record.Row("t", [Value.Integer(10), Value.Null()]),
                Record.View("v", "admin", ["id"], "SELECT id FROM t"),
                Record.Grant(Grant("t", "bob", [Privilege.Select, Privilege.Delete])),
                Record.Policy(Policy("t", "p", "alice", "id = 1", "id > 0")),
                Record.Policy(Policy("t", "q", None, "TRUE", None)),
            ]
        )
        self.assertEqual(
            format.diff(a, b),
            [
                "- user alice password",
                "+ user alice password",
                "- user bob superuser",
                "- member alice of bob",
                "- check t.t_check1: id > 0",
                "- row t: 1, 'a'",
                "- row t: 2, NULL",
                "+ row t: 2, 'b'",
                "+ row t: 10, NULL",
                "- materialized view m owner alice (id, n): SELECT id, 1 FROM v",
            ],
        )

    def test_invalid(self) -> None:
        """An invalid file is an error."""
        with self.assertRaises(Error) as cm:
            format.diff(format.encode([]), b"")
        self.assertEqual(str(cm.exception), "invalid input: invalid backup: not a toydb backup")


if __name__ == "__main__":
    unittest.main()
