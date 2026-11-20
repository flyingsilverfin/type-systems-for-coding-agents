"""Tests for toydb.sql.parser.ast.

The Rust module has no unit tests of its own (it is covered by the SQL golden
scripts).
"""

import math
import unittest

from toydb.fmt import debug
from toydb.sql.parser import Parser, ast


def function(name: str) -> "ast.Expression":
    return ast.Expression.Function(name, ())


class TestExpression(unittest.TestCase):
    def test_walk(self) -> None:
        expr = Parser.parse_expr("a + 1 > -b AND NOT c IS NULL OR f(d!, 2) LIKE 'x'")
        visited: list[str] = []

        def visit(expr: ast.Expression) -> bool:
            match expr:
                case ast.Expression.Operator(op):
                    visited.append(type(op).__name__)
                case ast.Expression.Column(_, name) | ast.Expression.Function(name, _):
                    visited.append(name)
                case ast.Expression.Literal(literal):
                    visited.append(debug(literal))
                case _:
                    visited.append(debug(expr))
            return True

        self.assertTrue(expr.walk(visit))
        self.assertEqual(
            " ".join(visited),
            # NB: only the first function argument is walked, see below.
            'Or And GreaterThan Add a Integer(1) Negate b Not Is c Like f Factorial d String("x")',
        )

        # Returning false halts the walk.
        visited.clear()
        self.assertFalse(expr.walk(lambda e: visit(e) and e != ast.Expression.Column(None, "b")))
        self.assertEqual(" ".join(visited), "Or And GreaterThan Add a Integer(1) Negate b")

    def test_walk_function(self) -> None:
        """Like in Rust, walking function arguments stops at the first
        argument that was walked in full, and a function without arguments
        returns false."""
        visited: list[str] = []

        def visit(expr: ast.Expression) -> bool:
            visited.append(debug(expr))
            return True

        self.assertTrue(Parser.parse_expr("f(1, 2)").walk(visit))
        self.assertEqual(visited[1:], ["Literal(Integer(1))"])
        self.assertFalse(Parser.parse_expr("f()").walk(visit))

    def test_contains(self) -> None:
        expr = Parser.parse_expr("a + 1 > -b AND NOT c IS NULL")
        self.assertTrue(expr.contains(lambda e: e == ast.Expression.Column(None, "c")))
        self.assertFalse(expr.contains(lambda e: e == ast.Expression.Column(None, "d")))
        self.assertTrue(expr.contains(lambda e: isinstance(e, ast.Expression.Literal)))
        self.assertFalse(expr.contains(lambda e: isinstance(e, ast.Expression.Function)))
        self.assertTrue(Parser.parse_expr("*").contains(lambda e: e == ast.Expression.All()))

    def test_collect(self) -> None:
        expr = Parser.parse_expr("sum(a) + 1 > -count(*) AND NOT max(min(b)) IS NULL OR f(g(c))!")
        exprs: list[ast.Expression] = []
        aggregates = ("sum", "count", "max", "min", "g")

        def is_aggregate(expr: ast.Expression) -> bool:
            match expr:
                case ast.Expression.Function(name, _):
                    return name in aggregates
                case _:
                    return False

        expr.collect(is_aggregate, exprs)
        # Does not recurse into matching expressions, i.e. min(b).
        self.assertEqual(
            [debug(expr) for expr in exprs],
            [
                'Function("sum", [Column(None, "a")])',
                'Function("count", [All])',
                'Function("max", [Function("min", [Column(None, "b")])])',
                'Function("g", [Column(None, "c")])',
            ],
        )

        # Appends to the given list.
        expr.collect(lambda e: e == ast.Expression.All(), exprs)
        self.assertEqual(len(exprs), 5)
        self.assertEqual(exprs[4], ast.Expression.All())

    def test_equality(self) -> None:
        """Expressions and literals are compared and hashed by value, with
        floats compared by their bits."""
        a, b = Parser.parse_expr("a + 1.5 * f(x, 'y')"), Parser.parse_expr("a+1.5*f(x,'y')")
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertNotEqual(a, Parser.parse_expr("a + 1.5 * f(x, 'z')"))
        self.assertEqual(len({a: 1, b: 2}), 1)

        literal = ast.Literal
        self.assertEqual(literal.Null(), literal.Null())
        self.assertEqual(literal.Float(math.nan), literal.Float(math.nan))
        self.assertEqual(hash(literal.Float(math.nan)), hash(literal.Float(math.nan)))
        self.assertNotEqual(literal.Float(math.nan), literal.Float(-math.nan))
        self.assertNotEqual(literal.Float(0.0), literal.Float(-0.0))
        self.assertEqual(literal.Float(1.0), literal.Float(1.0))
        self.assertNotEqual(literal.Float(1.0), literal.Integer(1))
        self.assertNotEqual(literal.Boolean(True), literal.Integer(1))
        self.assertNotEqual(literal.String("1"), literal.Integer(1))
        self.assertNotEqual(literal.Null(), literal.Boolean(False))
        self.assertEqual(Parser.parse_expr("x IS NAN"), Parser.parse_expr("x IS NAN"))
        self.assertEqual(function("f"), function("f"))
        self.assertNotEqual(function("f"), function("g"))

    def test_conversions(self) -> None:
        literal = ast.Literal.Integer(1)
        self.assertEqual(ast.Expression.from_literal(literal), ast.Expression.Literal(literal))
        operator = ast.Operator.Negate(ast.Expression.Literal(literal))
        self.assertEqual(ast.Expression.from_operator(operator), ast.Expression.Operator(operator))


class TestJoinType(unittest.TestCase):
    def test_is_outer(self) -> None:
        self.assertFalse(ast.JoinType.Cross.is_outer())
        self.assertFalse(ast.JoinType.Inner.is_outer())
        self.assertTrue(ast.JoinType.Left.is_outer())
        self.assertTrue(ast.JoinType.Right.is_outer())
        self.assertEqual(debug(ast.JoinType.Left), "Left")
        self.assertEqual(debug(ast.Direction.Ascending), "Ascending")


if __name__ == "__main__":
    unittest.main()
