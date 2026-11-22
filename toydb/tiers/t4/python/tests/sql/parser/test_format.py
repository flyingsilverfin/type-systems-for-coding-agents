"""Tests for toydb.sql.parser.format."""

import unittest

from toydb.sql.parser import Parser, ast
from toydb.sql.parser.format import (
    format_expr,
    format_literal,
    format_name,
    format_one_line,
    format_statement,
)

# Statements and their canonical lines.
STATEMENTS: list[tuple[str, list[str]]] = [
    # Transactions.
    ("begin transaction read write", ["BEGIN"]),
    ("BEGIN READ ONLY AS OF SYSTEM TIME 007", ["BEGIN READ ONLY AS OF SYSTEM TIME 7"]),
    ("commit", ["COMMIT"]),
    ("rollback;", ["ROLLBACK"]),
    # Schema statements.
    (
        "create table T (ID int primary key, v varchar not null default 'x' index, "
        "r int references T, b bool null unique)",
        [
            "CREATE TABLE t (",
            "  id INTEGER PRIMARY KEY,",
            "  v STRING NOT NULL DEFAULT 'x' INDEX,",
            "  r INTEGER REFERENCES t,",
            "  b BOOLEAN NULL UNIQUE",
            ")",
        ],
    ),
    ("CREATE TABLE t (id INT PRIMARY KEY)", ["CREATE TABLE t (", "  id INTEGER PRIMARY KEY", ")"]),
    ("drop table if exists T", ["DROP TABLE IF EXISTS t"]),
    # Writes.
    (
        "insert into t (id, V) values (1, 'a'), (-2, NULL)",
        ["INSERT INTO t (id, v) VALUES", "  (1, 'a'),", "  (-2, NULL)"],
    ),
    ("INSERT INTO t VALUES (1)", ["INSERT INTO t VALUES", "  (1)"]),
    (
        "update t set z = default, a = a + 1 where id = 1",
        ["UPDATE t", "SET a = a + 1, z = DEFAULT", "WHERE id = 1"],
    ),
    ("DELETE FROM t", ["DELETE FROM t"]),
    ("delete from t where id >= 10", ["DELETE FROM t", "WHERE id >= 10"]),
    # Queries.
    ("SELECT 1", ["SELECT 1"]),
    (
        "select id, NAME n from Item i left outer join \"Order\" o on o.item_id = i.id, extra "
        "where i.price >= 1.50 and not o.qty <> 0 group by id, n having count(*) > 1 "
        "order by n asc, 2 desc limit 10 offset 2",
        [
            "SELECT id, name AS n",
            "FROM item AS i",
            '  LEFT JOIN "Order" AS o ON o.item_id = i.id, extra',
            "WHERE i.price >= 1.5 AND NOT o.qty != 0",
            "GROUP BY id, n",
            "HAVING count(*) > 1",
            "ORDER BY n, 2 DESC",
            "LIMIT 10",
            "OFFSET 2",
        ],
    ),
    (
        "SELECT * FROM a CROSS JOIN b INNER JOIN c ON c.id = b.id RIGHT JOIN d ON TRUE, e",
        [
            "SELECT *",
            "FROM a",
            "  CROSS JOIN b",
            "  JOIN c ON c.id = b.id",
            "  RIGHT JOIN d ON TRUE, e",
        ],
    ),
    ("SELECT * FROM a x, b y", ["SELECT *", "FROM a AS x, b AS y"]),
    # EXPLAIN and the tier-3 statements.
    ("explain select * from t", ["EXPLAIN", "SELECT *", "FROM t"]),
    ("show statements", ["SHOW STATEMENTS"]),
    ("analyze", ["ANALYZE"]),
    ("ANALYZE T", ["ANALYZE t"]),
    ("show statistics T", ["SHOW STATISTICS t"]),
    ("export as sql", ["EXPORT AS SQL"]),
    ("EXPORT t AS CSV", ["EXPORT t AS CSV"]),
    (
        "import into t from csv 'id,v', '1,it''s'",
        ["IMPORT INTO t FROM CSV 'id,v', '1,it''s'"],
    ),
    # The tier-4 statements.
    ("format select 1", ["FORMAT", "SELECT 1"]),
    (
        'create view V (a, "B") as select id, v from t where id > 1',
        ['CREATE VIEW v (a, "B") AS', "SELECT id, v", "FROM t", "WHERE id > 1"],
    ),
    ("CREATE VIEW v AS SELECT 1", ["CREATE VIEW v AS", "SELECT 1"]),
    ("drop view if exists V", ["DROP VIEW IF EXISTS v"]),
    ("DROP VIEW v", ["DROP VIEW v"]),
    ("show views", ["SHOW VIEWS"]),
    ('describe "T"', ['DESCRIBE "T"']),
    ("describe select 1", ["DESCRIBE", "SELECT 1"]),
    ("estimate delete from t", ["ESTIMATE", "DELETE FROM t"]),
    (
        "prepare P (int, text) as select * from t where id = $1 and v = $02",
        ["PREPARE p (INTEGER, STRING) AS", "SELECT *", "FROM t", "WHERE id = $1 AND v = $2"],
    ),
    ("PREPARE q AS SELECT 1", ["PREPARE q AS", "SELECT 1"]),
    ("execute P (1, -2.5, 'x', NULL)", ["EXECUTE p (1, -2.5, 'x', NULL)"]),
    ("EXECUTE q", ["EXECUTE q"]),
    ("deallocate P", ["DEALLOCATE p"]),
    ("show prepared", ["SHOW PREPARED"]),
    ("lint lint update t set v = 1", ["LINT", "LINT", "UPDATE t", "SET v = 1"]),
    ("show changes", ["SHOW CHANGES"]),
    ("SHOW CHANGES SINCE 007", ["SHOW CHANGES SINCE 7"]),
]

# Statements and their one-line forms.
ONE_LINE: list[tuple[str, str]] = [
    (
        "create table T (ID int primary key, v varchar not null default 'x' index, "
        "r int references T)",
        "CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING NOT NULL DEFAULT 'x' INDEX, "
        "r INTEGER REFERENCES t)",
    ),
    (
        "explain insert into t (id, v) values (1, 'a'), (-2, NULL)",
        "EXPLAIN INSERT INTO t (id, v) VALUES (1, 'a'), (-2, NULL)",
    ),
    (
        "SELECT a.x FROM a JOIN b ON a.id = b.id LEFT JOIN c ON TRUE, d WHERE x > 1 LIMIT 1",
        "SELECT a.x FROM a JOIN b ON a.id = b.id LEFT JOIN c ON TRUE, d WHERE x > 1 LIMIT 1",
    ),
    (
        "PREPARE p (INT) AS CREATE TABLE t (id INT PRIMARY KEY DEFAULT $1)",
        "PREPARE p (INTEGER) AS CREATE TABLE t (id INTEGER PRIMARY KEY DEFAULT $1)",
    ),
    ("SELECT '(', ')'", "SELECT '(', ')'"),
]

# Expressions and their canonical text, written as SELECT items.
EXPRESSIONS: list[tuple[str, str]] = [
    # Literals keep their values.
    ("1, -1, 007", "1, -1, 7"),
    ("1.50, 2E3, 1e100, 1.5e-7, 1e16, 0.1", "1.5, 2000.0, 1e100, 1.5e-7, 1e16, 0.1"),
    ("INFINITY, -INFINITY, NAN", "INFINITY, -INFINITY, NAN"),
    ("TRUE, false, null", "TRUE, FALSE, NULL"),
    ("'it''s', '', 'a\"b'", "'it''s', '', 'a\"b'"),
    # Parentheses exactly where the parser needs them.
    ("(a + b) * c, a + (b * c)", "(a + b) * c, a + b * c"),
    ("a - (b - c), (a - b) - c", "a - (b - c), a - b - c"),
    ("a ^ (b ^ c), (a ^ b) ^ c", "a ^ b ^ c, (a ^ b) ^ c"),
    ("-(a + 1), (-a) ^ 2, -(a ^ 2)", "-(a + 1), -a ^ 2, -(a ^ 2)"),
    ("(a!)!, -(a!), - -a, + a", "a!!, -(a!), --a, +a"),
    ("NOT (a = b), (NOT a) = b, NOT (a OR b)", "NOT a = b, (NOT a) = b, NOT (a OR b)"),
    ("(a = b) IS NULL, a = (b IS NULL)", "a = b IS NULL, a = (b IS NULL)"),
    # Operators as written.
    ("a <> 1, b IS NOT NULL, c >= 2, d <= 3", "a != 1, b IS NOT NULL, c >= 2, d <= 3"),
    ("e IS NAN, f IS NOT NAN, g LIKE 'x%'", "e IS NAN, f IS NOT NAN, g LIKE 'x%'"),
    # Names, functions and parameters.
    ('T.ID, "Name", "x""y", MAX(v), "Sqrt"(4)', 't.id, "Name", "x""y", max(v), "Sqrt"(4)'),
    ("count(*), $1 + $02", "count(*), $1 + $2"),
]


class TestFormat(unittest.TestCase):
    def test_statements(self) -> None:
        for statement, expect in STATEMENTS:
            with self.subTest(statement=statement):
                self.assertEqual(format_statement(Parser.parse(statement)), expect)

    def test_one_line(self) -> None:
        for statement, expect in ONE_LINE:
            with self.subTest(statement=statement):
                self.assertEqual(format_one_line(Parser.parse(statement)), expect)

    def test_expressions(self) -> None:
        for exprs, expect in EXPRESSIONS:
            with self.subTest(exprs=exprs):
                lines = format_statement(Parser.parse(f"SELECT {exprs}"))
                self.assertEqual(lines, [f"SELECT {expect}"])

    def test_expression(self) -> None:
        """A single expression is written by format_expr."""
        self.assertEqual(
            format_expr(Parser.parse_expr("NOT (A <> 1.50 OR b IS NOT NULL)")),
            "NOT (a != 1.5 OR b IS NOT NULL)",
        )
        self.assertEqual(
            format_expr(Parser.parse_expr("c >= -2E3 AND d = 'it''s'")),
            "c >= -2000.0 AND d = 'it''s'",
        )

    def test_round_trip(self) -> None:
        """Parsing the lines or the one-line form again gives the same statement."""
        for statement, _ in STATEMENTS + ONE_LINE:
            with self.subTest(statement=statement):
                parsed = Parser.parse(statement)
                self.assertEqual(Parser.parse("\n".join(format_statement(parsed))), parsed)
                self.assertEqual(Parser.parse(format_one_line(parsed)), parsed)

    def test_names(self) -> None:
        """Names are unquoted only if the lexer reads them back unchanged."""
        cases = [
            ("item", "item"),
            ("t2", "t2"),
            ("ab_1", "ab_1"),
            ("été", "été"),
            ("Item", '"Item"'),
            ("Été", '"Été"'),
            ("order", '"order"'),
            ("view", '"view"'),
            ("format", '"format"'),
            ("a b", '"a b"'),
            ('x"y', '"x""y"'),
            ("_x", '"_x"'),
            ("1a", '"1a"'),
            ("$1", '"$1"'),
            ("", '""'),
        ]
        for name, expect in cases:
            with self.subTest(name=name):
                self.assertEqual(format_name(name), expect)
                self.assertEqual(Parser.parse_expr(expect), ast.Expression.Column(None, name))

    def test_literals(self) -> None:
        """Literals are written as SQL text that reads back as the same literal."""
        cases: list[tuple[ast.Literal, str]] = [
            (ast.Literal.Null(), "NULL"),
            (ast.Literal.Boolean(True), "TRUE"),
            (ast.Literal.Integer(9223372036854775807), "9223372036854775807"),
            (ast.Literal.Float(0.0), "0.0"),
            (ast.Literal.Float(float("inf")), "INFINITY"),
            (ast.Literal.Float(float("nan")), "NAN"),
            (ast.Literal.Float(1e-5), "1e-5"),
            (ast.Literal.String("é ☃\\"), "'é ☃\\'"),
        ]
        for literal, expect in cases:
            with self.subTest(literal=literal):
                self.assertEqual(format_literal(literal), expect)
                self.assertEqual(Parser.parse_expr(expect), ast.Expression.Literal(literal))


if __name__ == "__main__":
    unittest.main()
