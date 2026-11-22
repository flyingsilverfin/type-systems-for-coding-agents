"""Tests for toydb.sql.execution.digest."""

import unittest

from toydb.sql.execution.digest import StatementDigests, digest
from toydb.sql.parser import Parser
from toydb.sql.types import Value

# Statements and their digests.
CASES: list[tuple[str, str]] = [
    # The examples of the specification.
    (
        "select ID, v as \"the name\" from T x where x.id >= 2 and not (v <> 'a' or v is not null) "
        "order by 1 asc, v desc limit 5 offset 1",
        'SELECT id, v AS "the name" FROM t AS x WHERE x.id >= ? AND NOT (v != ? OR v IS NOT NULL) '
        "ORDER BY ?, v DESC LIMIT ? OFFSET ?",
    ),
    (
        "SELECT t.v, u.w FROM t INNER JOIN u ON u.t_id = t.id LEFT OUTER JOIN t t2 ON t2.id = u.id",
        "SELECT t.v, u.w FROM t JOIN u ON u.t_id = t.id LEFT JOIN t AS t2 ON t2.id = u.id",
    ),
    (
        "SELECT COUNT(*), Sum(n) FROM t GROUP BY n % 2",
        "SELECT count(*), sum(n) FROM t GROUP BY n % ?",
    ),
    (
        "insert into T values (1, 'x', 15), (2, NULL, -1)",
        "INSERT INTO t VALUES (?, ?, ?), (?, ?, -?)",
    ),
    (
        "update t set v = default, n = n + 1 where id = 3",
        "UPDATE t SET n = n + ?, v = DEFAULT WHERE id = ?",
    ),
    (
        "CREATE TABLE u (k INT NOT NULL DEFAULT 1 PRIMARY KEY, r INT INDEX REFERENCES u, "
        "b BOOL NULL UNIQUE)",
        "CREATE TABLE u (k INTEGER PRIMARY KEY NOT NULL DEFAULT ?, r INTEGER INDEX REFERENCES u, "
        "b BOOLEAN NULL UNIQUE)",
    ),
    (
        "BEGIN TRANSACTION READ ONLY AS OF SYSTEM TIME 3",
        "BEGIN READ ONLY AS OF SYSTEM TIME ?",
    ),
    # Transactions and EXPLAIN.
    ("begin read write;", "BEGIN"),
    ("COMMIT", "COMMIT"),
    ("rollback", "ROLLBACK"),
    ("EXPLAIN select * from t where id = 1", "EXPLAIN SELECT * FROM t WHERE id = ?"),
    # Schema statements.
    ("drop table if exists T", "DROP TABLE IF EXISTS t"),
    (
        'CREATE TABLE "a b" ("Name" TEXT PRIMARY KEY, "key" DOUBLE DEFAULT -1.5)',
        'CREATE TABLE "a b" (Name STRING PRIMARY KEY, "key" FLOAT DEFAULT -?)',
    ),
    # Writes.
    ("DELETE FROM t", "DELETE FROM t"),
    ("insert into t (a, B) values (1, 'x')", "INSERT INTO t (a, b) VALUES (?, ?)"),
    # Literals of every kind.
    (
        "SELECT 'x', 1.5, TRUE, FALSE, NULL, -3, NAN, -INFINITY",
        "SELECT ?, ?, ?, ?, ?, -?, ?, -?",
    ),
    # Joins and FROM lists.
    (
        "SELECT * FROM a CROSS JOIN b, c z RIGHT OUTER JOIN d ON z.id = d.id",
        "SELECT * FROM a CROSS JOIN b, c AS z RIGHT JOIN d ON z.id = d.id",
    ),
    # Quoted names.
    (
        'SELECT "select"."from", "x""y" FROM "select"',
        'SELECT "select"."from", "x""y" FROM "select"',
    ),
    # The extension statements.
    ("show statements", "SHOW STATEMENTS"),
    ("analyze", "ANALYZE"),
    ('ANALYZE "T"', "ANALYZE T"),
    ("show statistics T", "SHOW STATISTICS t"),
    ("export t as csv", "EXPORT t AS CSV"),
    ("EXPORT AS SQL", "EXPORT AS SQL"),
    ("import into t from json '{}', '{\"a\":1}'", "IMPORT INTO t FROM JSON ?, ?"),
    # The tier-4 statements, with parameters written as themselves.
    ("format select 1", "FORMAT SELECT ?"),
    ("FORMAT FORMAT SELECT 'a'", "FORMAT FORMAT SELECT ?"),
    (
        'create view V (a, "B") as select id from t where id > 1',
        "CREATE VIEW v (a, B) AS SELECT id FROM t WHERE id > ?",
    ),
    ("CREATE VIEW v AS SELECT * FROM t", "CREATE VIEW v AS SELECT * FROM t"),
    ("drop view if exists V", "DROP VIEW IF EXISTS v"),
    ("show views", "SHOW VIEWS"),
    ('describe "T"', "DESCRIBE T"),
    ("describe select 1 + 1", "DESCRIBE SELECT ? + ?"),
    ("estimate select * from t where id = 1", "ESTIMATE SELECT * FROM t WHERE id = ?"),
    (
        "prepare P (int, text) as update t set v = $2 where id = $01 and n < 3",
        "PREPARE p (INTEGER, STRING) AS UPDATE t SET v = $2 WHERE id = $1 AND n < ?",
    ),
    ("PREPARE q AS SELECT 1", "PREPARE q AS SELECT ?"),
    ("execute P (1, -2.5, NULL)", "EXECUTE p (?, -?, ?)"),
    ("EXECUTE q", "EXECUTE q"),
    ("deallocate P", "DEALLOCATE p"),
    ("show prepared", "SHOW PREPARED"),
    ("lint delete from t", "LINT DELETE FROM t"),
    ("show changes", "SHOW CHANGES"),
    ("SHOW CHANGES SINCE 3", "SHOW CHANGES SINCE ?"),
]

# Expressions and their digests, written as SELECT items.
EXPRESSIONS: list[tuple[str, str]] = [
    ("a + b * c", "a + b * c"),
    ("(a + b) * c", "(a + b) * c"),
    ("a - (b - c)", "a - (b - c)"),
    ("(a - b) - c", "a - b - c"),
    ("a ^ b ^ c", "a ^ b ^ c"),
    ("(a ^ b) ^ c", "(a ^ b) ^ c"),
    ("-a ^ 2", "-a ^ ?"),
    ("-(a ^ 2)", "-(a ^ ?)"),
    ("- -a", "--a"),
    ("+-a", "+-a"),
    ("(a!)!", "a!!"),
    ("-(a!)", "-(a!)"),
    ("(a + 1)!", "(a + ?)!"),
    ("NOT (p AND q)", "NOT (p AND q)"),
    ("(NOT p) = q", "(NOT p) = q"),
    ("NOT (p = q)", "NOT p = q"),
    ("(p = q) = p", "p = q = p"),
    ("p = (q = p)", "p = (q = p)"),
    ("(p = q) IS NULL", "p = q IS NULL"),
    ("p = (q IS NULL)", "p = (q IS NULL)"),
    ("NOT (a IS NULL)", "NOT a IS NULL"),
    ("((a)) IS NOT NAN", "a IS NOT NAN"),
    ("(p AND q) IS NOT NULL", "(p AND q) IS NOT NULL"),
    ("a >= b AND b <= c OR a <> c", "a >= b AND b <= c OR a != c"),
    ("(s LIKE 'a%') = p", "s LIKE ? = p"),
    ("SQRT((a + b))", "sqrt(a + b)"),
    ("p OR (q OR p)", "p OR (q OR p)"),
    ("(p OR q) AND p", "(p OR q) AND p"),
]


class TestDigest(unittest.TestCase):
    def test_statements(self) -> None:
        for statement, expect in CASES:
            with self.subTest(statement=statement):
                self.assertEqual(digest(Parser.parse(statement)), expect)

    def test_expressions(self) -> None:
        for expr, expect in EXPRESSIONS:
            with self.subTest(expr=expr):
                self.assertEqual(digest(Parser.parse(f"SELECT {expr}")), f"SELECT {expect}")


class TestStatementDigests(unittest.TestCase):
    def test_record(self) -> None:
        """Statements are listed in order of first execution."""
        digests = StatementDigests()
        digests.record("SELECT ?", 1)
        digests.record_error("SELECT * FROM t")
        digests.record("SELECT ?", 2)
        self.assertEqual(
            digests.rows(),
            [
                [Value.String("SELECT ?"), Value.Integer(2), Value.Integer(0), Value.Integer(3)],
                [
                    Value.String("SELECT * FROM t"),
                    Value.Integer(1),
                    Value.Integer(1),
                    Value.Integer(0),
                ],
            ],
        )


if __name__ == "__main__":
    unittest.main()
