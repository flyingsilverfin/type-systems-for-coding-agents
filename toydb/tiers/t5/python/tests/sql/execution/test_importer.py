"""Tests for toydb.sql.execution.importer and toydb.sql.execution.export.

Both are mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext`.
"""

import math
import unittest
from typing import Any

from toydb.error import Error
from toydb.sql.engine import Local
from toydb.sql.execution.export import export
from toydb.sql.execution.importer import import_lines, read_json_value
from toydb.sql.parser.ast import ExportFormat, ImportFormat
from toydb.sql.types import DataType, Value
from toydb.sql.types.user import ADMIN
from toydb.storage.memory import Memory


class TestReadJSONValue(unittest.TestCase):
    def test_values(self) -> None:
        cases: list[tuple[Any, DataType, Value | None]] = [
            (None, DataType.Integer, Value.Null()),
            (True, DataType.Boolean, Value.Boolean(True)),
            (1, DataType.Boolean, None),
            (2, DataType.Integer, Value.Integer(2)),
            (True, DataType.Integer, None),  # bool is an int in Python
            (2.0, DataType.Integer, None),
            (2**63, DataType.Integer, None),
            (2, DataType.Float, Value.Float(2.0)),
            (2**63, DataType.Float, Value.Float(9.223372036854776e18)),
            (10**400, DataType.Float, Value.Float(math.inf)),
            (False, DataType.Float, None),
            ({"type": "FLOAT", "value": "NaN"}, DataType.Float, Value.Float(math.nan)),
            ({"value": "-inf", "type": "FLOAT"}, DataType.Float, Value.Float(-math.inf)),
            ({"type": "FLOAT", "value": "1.5"}, DataType.Float, None),
            ({"type": "INTEGER", "value": "inf"}, DataType.Float, None),
            ({"type": "FLOAT", "value": "inf", "x": 1}, DataType.Float, None),
            ({"type": "FLOAT", "value": math.inf}, DataType.Float, None),
            ("x", DataType.String, Value.String("x")),
            ({"type": "STRING", "value": "x"}, DataType.String, None),
            (["x"], DataType.String, None),
        ]
        for member, datatype, expect in cases:
            with self.subTest(member=member, datatype=datatype):
                self.assertEqual(read_json_value(member, datatype), expect)


class TestImportExport(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING DEFAULT 'd', f FLOAT)")
        session.close()

    def test_roundtrip(self) -> None:
        """Exported CSV and JSON lines import as the same rows."""
        txn = self.engine.begin()
        count = import_lines(
            txn, "t", ImportFormat.Csv, ["v,id,f", '"a,b",1,NaN', ",2,", '"",3,-0'], ADMIN
        )
        self.assertEqual(count, 3)
        csv = export(txn, "t", ExportFormat.Csv, ADMIN)
        self.assertEqual(csv.lines, ["id,v,f", '1,"a,b",NaN', "2,,", '3,"",0.0'])
        self.assertEqual(csv.rows, 3)
        jsonl = export(txn, "t", ExportFormat.Json, ADMIN)
        txn.delete("t", [Value.Integer(i) for i in (1, 2, 3)])
        self.assertEqual(import_lines(txn, "t", ImportFormat.Json, jsonl.lines, ADMIN), 3)
        self.assertEqual(export(txn, "t", ExportFormat.Csv, ADMIN).lines, csv.lines)
        txn.rollback()

    def test_defaults(self) -> None:
        """Rows are inserted like INSERT, with the defaults of the other
        columns."""
        txn = self.engine.begin()
        import_lines(
            txn, "t", ImportFormat.Json, ['{"id":1}', '{"f":1.5,"id":2,"v":null}'], ADMIN
        )
        self.assertEqual(
            export(txn, "t", ExportFormat.Sql, ADMIN).lines[-2:],
            ["INSERT INTO t VALUES (1, 'd', NULL);", "INSERT INTO t VALUES (2, NULL, 1.5);"],
        )
        txn.rollback()

    def test_errors(self) -> None:
        cases: list[tuple[ImportFormat, list[str], str]] = [
            (ImportFormat.Csv, ["id,x"], "unknown column x in table t"),
            (ImportFormat.Csv, ["id,id"], "column id given multiple times"),
            (ImportFormat.Csv, ['id,"v'], "invalid CSV on line 1"),
            (ImportFormat.Csv, ["id", "1,2"], "wrong number of fields on line 2: expected 1, found 2"),
            (ImportFormat.Csv, ["id", "1", "a"], "invalid value for INTEGER column id on line 3"),
            (ImportFormat.Json, ["{}"], "no value given for column id with no default"),
            (ImportFormat.Json, ["{"], "invalid JSON on line 1"),
            (ImportFormat.Json, ["[]"], "line 1 is not a JSON object"),
            (ImportFormat.Json, ['{"id":1,"f":"x"}'], "invalid value for FLOAT column f on line 1"),
        ]
        for format, lines, message in cases:
            with self.subTest(lines=lines), self.assertRaises(Error) as cm:
                import_lines(self.engine.begin(), "t", format, lines, ADMIN)
            self.assertEqual(str(cm.exception), f"invalid input: {message}")


if __name__ == "__main__":
    unittest.main()
