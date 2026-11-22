"""Tests for toydb.sql.execution.lint.

LINT is mostly covered by the SQL golden scripts in
`tests/sql/testscripts/ext4` (see `tests/sql/test_mod.py`); these tests cover
the rules, the resolution of column references and the order of the warnings
directly.
"""

import unittest

from toydb.sql.engine import Local
from toydb.sql.execution.lint import (
    LintWarning,
    Scope,
    Source,
    is_constant,
    lint,
    operands,
)
from toydb.sql.parser import Parser, ast
from toydb.sql.types import DataType, Value
from toydb.sql.types.view import View
from toydb.storage.memory import Memory

INTEGER, STRING = DataType.Integer, DataType.String


def expression(sql: str) -> ast.Expression:
    """Parses an expression."""
    statement = Parser.parse(f"SELECT {sql}")
    assert isinstance(statement, ast.Statement.Select)
    return statement.select[0][0]


class TestLint(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = Local(Memory())
        session = self.engine.session()
        session.execute("CREATE TABLE t (id INT PRIMARY KEY, s STRING, n INT, f FLOAT, b BOOL)")
        session.execute("CREATE TABLE u (id INT PRIMARY KEY, s STRING)")
        session.execute("CREATE VIEW v AS SELECT id, s AS name, NULL AS x FROM t")
        session.close()
        self.txn = self.engine.begin_read_only()

    def tearDown(self) -> None:
        self.txn.rollback()

    def lint(self, sql: str) -> list[tuple[str, str]]:
        """Lints a statement, returning the codes and messages."""
        return [(w.code, w.message) for w in lint(self.txn, Parser.parse(sql))]

    def codes(self, sql: str) -> list[str]:
        """Lints a statement, returning the codes."""
        return [code for code, _ in self.lint(sql)]

    def test_no_warnings(self) -> None:
        for sql in [
            "SELECT * FROM t WHERE id = 1",
            "SELECT 1",
            "INSERT INTO t (id) VALUES (1)",
            "DELETE FROM t WHERE id = 1",
            "UPDATE t SET n = 1 WHERE id = 1",
            "CREATE TABLE w (id INT PRIMARY KEY, n INT DEFAULT 1)",
            "SELECT * FROM t WHERE s IS NULL OR n IS NOT NULL OR f IS NOT NAN",
            "BEGIN",
            "DESCRIBE t",
            "SHOW CHANGES SINCE 1",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(self.lint(sql), [])

    def test_warning_row(self) -> None:
        warning = LintWarning("unfiltered_write", "DELETE without WHERE deletes every row of t")
        self.assertEqual(
            warning.row(),
            [
                Value.String("unfiltered_write"),
                Value.String("DELETE without WHERE deletes every row of t"),
            ],
        )

    def test_statements(self) -> None:
        """Write statements are checked, with names written so that they read
        back as the same name."""
        self.assertEqual(
            self.lint('UPDATE "Big Table" SET a = 1'),
            [("unfiltered_write", 'UPDATE without WHERE changes every row of "Big Table"')],
        )
        self.assertEqual(
            self.lint('DELETE FROM "order"'),
            [("unfiltered_write", 'DELETE without WHERE deletes every row of "order"')],
        )
        self.assertEqual(
            self.lint("INSERT INTO t VALUES (1), (2)"),
            [("implicit_columns", "INSERT without a column list depends on the column order of t")],
        )

    def test_contained_statements(self) -> None:
        """Contained statements are checked at any depth."""
        for sql in [
            "EXPLAIN DELETE FROM t",
            "FORMAT DELETE FROM t",
            "DESCRIBE DELETE FROM t",
            "ESTIMATE DELETE FROM t",
            "LINT LINT DELETE FROM t",
            "PREPARE p AS DELETE FROM t",
            "EXPLAIN LINT FORMAT DELETE FROM t",
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(self.codes(sql), ["unfiltered_write"])
        self.assertEqual(
            self.codes("CREATE VIEW w AS SELECT * FROM t WHERE TRUE"), ["constant_condition"]
        )
        self.assertEqual(self.codes("EXECUTE p (1 / 0, 2)"), ["division_by_zero"])

    def test_null_comparison(self) -> None:
        self.assertEqual(
            self.lint("SELECT n = NULL, NULL != s, NULL <= NULL, n IS NULL, n + NULL FROM t"),
            [
                ("null_comparison", "comparison with NULL is never true: n = NULL"),
                ("null_comparison", "comparison with NULL is never true: NULL != s"),
                ("null_comparison", "comparison with NULL is never true: NULL <= NULL"),
            ],
        )
        # Only the NULL constant itself, also in parentheses.
        self.assertEqual(
            self.lint("SELECT n = -NULL, n = (NULL) FROM t"),
            [("null_comparison", "comparison with NULL is never true: n = NULL")],
        )

    def test_type_mismatch(self) -> None:
        self.assertEqual(
            self.lint(
                "SELECT s = 1, 1.5 < s, n = f, n = 1.5, f >= 1, b != TRUE, b = 'x', "
                "n = -'x', s > +2, n = NULL FROM t"
            ),
            [
                ("type_mismatch", "STRING column s compared with INTEGER value 1"),
                ("type_mismatch", "STRING column s compared with FLOAT value 1.5"),
                ("type_mismatch", "BOOLEAN column b compared with STRING value 'x'"),
                ("type_mismatch", "INTEGER column n compared with STRING value -'x'"),
                ("type_mismatch", "STRING column s compared with INTEGER value +2"),
                ("null_comparison", "comparison with NULL is never true: n = NULL"),
            ],
        )

    def test_type_mismatch_parameters(self) -> None:
        """Parameters have the types of the PREPARE that contains them."""
        self.assertEqual(
            self.lint("PREPARE p (STRING, INT) AS SELECT * FROM t WHERE n = $1 AND s = $2 AND f = $3"),
            [
                ("type_mismatch", "INTEGER column n compared with STRING parameter $1"),
                ("type_mismatch", "STRING column s compared with INTEGER parameter $2"),
            ],
        )
        self.assertEqual(
            self.codes("PREPARE p (STRING) AS EXPLAIN SELECT * FROM t WHERE n = $1"),
            ["type_mismatch"],
        )
        self.assertEqual(self.lint("SELECT * FROM t WHERE n = $1"), [])

    def test_resolution(self) -> None:
        """Column references resolve against the FROM items, by alias or by
        name, and unqualified ones only if exactly one item has the column."""
        cases: list[tuple[str, list[str]]] = [
            ("SELECT * FROM t WHERE t.s = 1", ["STRING column t.s"]),
            ("SELECT * FROM t a WHERE a.s = 1 AND t.s = 1", ["STRING column a.s"]),
            ("SELECT * FROM t JOIN u ON t.id = u.id WHERE s = 1 AND n = 's'", ["INTEGER column n"]),
            ("SELECT * FROM t a JOIN t b ON a.id = b.id WHERE n = 's'", []),
            ("SELECT * FROM t, nosuch WHERE n = 's' AND nosuch.n = 's'", ["INTEGER column n"]),
            ("SELECT * FROM t x, u x WHERE x.n = 's' AND x.id = 's'", []),
            ("SELECT * FROM nosuch x, t x WHERE x.n = 's'", ["INTEGER column x.n"]),
            ("SELECT * FROM v WHERE name = 1 AND x = 1 AND id = 's'", ["STRING column name", "INTEGER column id"]),
            ("SELECT s AS a FROM t WHERE a = 1", []),
            ("UPDATE t SET s = 1 WHERE s = 1", ["STRING column s"]),
            ("DELETE FROM u WHERE id = 's'", ["INTEGER column id"]),
            ("INSERT INTO t (id, b) VALUES (1, s = 1)", []),
        ]
        for sql, expected in cases:
            with self.subTest(sql=sql):
                messages = [m for code, m in self.lint(sql) if code == "type_mismatch"]
                self.assertEqual([m.split(" compared")[0] for m in messages], expected)

    def test_like_without_wildcard(self) -> None:
        self.assertEqual(
            self.lint("SELECT s LIKE 'it''s', s LIKE '%', s LIKE '_', s LIKE s, s LIKE 1 FROM t"),
            [("like_without_wildcard", "LIKE without wildcards matches like =: s LIKE 'it''s'")],
        )

    def test_division_by_zero(self) -> None:
        self.assertEqual(
            self.lint("SELECT n / 0, n % 0, n / 0.0, n / -0, 0 / n, (n + 1) / 0 FROM t"),
            [
                ("division_by_zero", "division by zero: n / 0"),
                ("division_by_zero", "division by zero: n % 0"),
                ("division_by_zero", "division by zero: (n + 1) / 0"),
            ],
        )

    def test_constant_condition(self) -> None:
        cases: list[tuple[str, list[str]]] = [
            ("SELECT * FROM t WHERE 1 = 1", ["1 = 1"]),
            ("SELECT * FROM t WHERE sqrt(4) > 1 AND NOT FALSE", ["sqrt(4) > 1 AND NOT FALSE"]),
            ("SELECT * FROM t WHERE n > 1 OR TRUE", []),
            ("SELECT COUNT(*) FROM t HAVING COUNT(*) > 1", []),
            ("SELECT COUNT(*) FROM t HAVING TRUE", ["TRUE"]),
            ("SELECT * FROM t JOIN u ON 1 = 1 WHERE n = 1", ["1 = 1"]),
            ("DELETE FROM t WHERE FALSE", ["FALSE"]),
            ("UPDATE t SET n = 1 WHERE NULL", ["NULL"]),
            ("PREPARE p (BOOLEAN) AS SELECT * FROM t WHERE $1", []),
        ]
        for sql, expected in cases:
            with self.subTest(sql=sql):
                messages = [m for code, m in self.lint(sql) if code == "constant_condition"]
                self.assertEqual(messages, [f"condition is constant: {e}" for e in expected])

    def test_order_by_constant(self) -> None:
        self.assertEqual(
            self.lint("SELECT * FROM t ORDER BY 1, n, 'a' DESC, -2, sqrt(n), MAX(n), 1 + 1"),
            [
                ("order_by_constant", "ORDER BY a constant has no effect: 1"),
                ("order_by_constant", "ORDER BY a constant has no effect: 'a'"),
                ("order_by_constant", "ORDER BY a constant has no effect: -2"),
                ("order_by_constant", "ORDER BY a constant has no effect: 1 + 1"),
            ],
        )

    def test_order(self) -> None:
        """Warnings are in canonical text order: clause by clause, a part
        before the parts it contains, and in rule order for the same part."""
        self.assertEqual(
            self.codes(
                "SELECT n / 0 FROM t JOIN u ON NULL = 1 WHERE t.s = 1 GROUP BY 1 % 0 "
                "HAVING 1 = 1 ORDER BY 'a' LIKE 'b' LIMIT 1 / 0 OFFSET 2 / 0"
            ),
            [
                "division_by_zero",
                "null_comparison",
                "constant_condition",
                "type_mismatch",
                "division_by_zero",
                "constant_condition",
                "like_without_wildcard",
                "order_by_constant",
                "division_by_zero",
                "division_by_zero",
            ],
        )
        # The canonical text orders UPDATE assignments by column name.
        self.assertEqual(
            self.lint("UPDATE t SET s = 1 / 0, n = 2 / 0"),
            [
                ("unfiltered_write", "UPDATE without WHERE changes every row of t"),
                ("division_by_zero", "division by zero: 2 / 0"),
                ("division_by_zero", "division by zero: 1 / 0"),
            ],
        )
        self.assertEqual(
            self.lint("SELECT (n / 0) = NULL FROM t"),
            [
                ("null_comparison", "comparison with NULL is never true: n / 0 = NULL"),
                ("division_by_zero", "division by zero: n / 0"),
            ],
        )

    def test_operands(self) -> None:
        self.assertEqual(operands(expression("a + b")), [expression("a"), expression("b")])
        self.assertEqual(operands(expression("a IS NOT NULL")), [expression("a")])
        self.assertEqual(operands(expression("-a")), [expression("a")])
        self.assertEqual(operands(expression("f(a, 1)")), [expression("a"), expression("1")])
        self.assertEqual(operands(expression("a")), [])

    def test_is_constant(self) -> None:
        for sql, constant in [
            ("1", True),
            ("NOT (1 + 2 > sqrt(4))", True),
            ("a", False),
            ("t.a + 1", False),
            ("$1", False),
            ("count(*)", False),
            ("sqrt(a)", False),
            ("f(1, a)", False),
        ]:
            with self.subTest(sql=sql):
                self.assertEqual(is_constant(expression(sql)), constant)

    def test_unplannable_view(self) -> None:
        """A view whose query can't be planned doesn't resolve."""
        txn = self.engine.begin()
        txn.create_view(View("w", ["s"], "SELECT nosuch AS s FROM t", ["t"]))
        statement = Parser.parse("SELECT * FROM w, u WHERE s = 1 AND w.s = 1")
        self.assertEqual(
            [w.message for w in lint(txn, statement)],
            ["STRING column s compared with INTEGER value 1"],
        )
        txn.rollback()

    def test_scope(self) -> None:
        scope = Scope(
            [Source("t", None, {"id": INTEGER, "s": STRING}), Source("t", "x", {"id": INTEGER, "y": None})],
            [STRING],
        )
        self.assertEqual(scope.column_type("t", "s"), STRING)
        self.assertEqual(scope.column_type("x", "id"), INTEGER)
        self.assertIsNone(scope.column_type("x", "y"))
        self.assertIsNone(scope.column_type("x", "s"))
        self.assertIsNone(scope.column_type(None, "id"))
        self.assertEqual(scope.column_type(None, "s"), STRING)
        self.assertEqual(scope.parameter_type(1), STRING)
        self.assertIsNone(scope.parameter_type(2))
        self.assertIsNone(Scope([], None).parameter_type(1))


if __name__ == "__main__":
    unittest.main()
