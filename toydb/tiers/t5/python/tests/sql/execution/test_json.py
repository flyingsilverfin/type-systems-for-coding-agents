"""Tests for toydb.sql.execution.json."""

import math
import unittest

from toydb.encoding import json as jsontext
from toydb.sql.execution import StatementResult
from toydb.sql.execution.json import encode_result, expression_to_json, plan_to_json
from toydb.sql.parser import Parser, ast
from toydb.sql.planner import Node, Plan, Planner
from toydb.sql.types import Expression, Label, Table, Value
from toydb.storage import mvcc


class Labels:
    """Column labels for expressions: a, b, and #2 without a label."""

    def column_label(self, index: int) -> Label:
        return [Label.Qualified("t", "a"), Label.Unqualified("b"), Label.None_()][index]


class Catalog:
    """An in-memory catalog, populated by CREATE TABLE statements."""

    def __init__(self, *statements: str) -> None:
        self.tables: dict[str, Table] = {}
        for statement in statements:
            plan = Planner(self).build(Parser.parse(statement))
            assert isinstance(plan, Plan.CreateTable)
            self.tables[plan.schema.name] = plan.schema

    def get_table(self, table: str) -> Table | None:
        return self.tables.get(table)

    def must_get_table(self, table: str) -> Table:
        return self.tables[table]


CATALOG = Catalog(
    "CREATE TABLE item (id INT PRIMARY KEY, name STRING INDEX, qty INT)",
    "CREATE TABLE sale (id INT PRIMARY KEY, item_id INT REFERENCES item)",
)


def explain(statement: str) -> str:
    """Plans and optimizes a statement, returning the plan's JSON text."""
    plan = Planner(CATALOG).build(Parser.parse(statement)).optimize()
    return jsontext.encode(plan_to_json(plan))


class TestExpressions(unittest.TestCase):
    def test_expressions(self) -> None:
        a, b, c = Expression.Column(0), Expression.Column(1), Expression.Column(2)
        one = Expression.Constant(Value.Integer(1))
        cases: list[tuple[Expression, str]] = [
            (one, '{"constant":1}'),
            (Expression.Constant(Value.Float(math.nan)), '{"constant":{"type":"FLOAT","value":"NaN"}}'),
            (a, '{"column":0,"label":"t.a"}'),
            (b, '{"column":1,"label":"b"}'),
            (c, '{"column":2,"label":null}'),
            (
                Expression.Subtract(a, Expression.Subtract(b, one)),
                '{"op":"_ - _","args":[{"column":0,"label":"t.a"},'
                '{"op":"_ - _","args":[{"column":1,"label":"b"},{"constant":1}]}]}',
            ),
            (Expression.Not(Expression.Is(a, Value.Null())),
             '{"op":"NOT _","args":[{"op":"_ IS NULL","args":[{"column":0,"label":"t.a"}]}]}'),
            (Expression.Is(a, Value.Float(math.nan)),
             '{"op":"_ IS NAN","args":[{"column":0,"label":"t.a"}]}'),
            (Expression.Identity(Expression.Negate(a)),
             '{"op":"-_","args":[{"column":0,"label":"t.a"}]}'),
            (Expression.Factorial(a), '{"op":"_!","args":[{"column":0,"label":"t.a"}]}'),
            (Expression.SquareRoot(Expression.Add(a, one)),
             '{"op":"sqrt(_)","args":[{"op":"_ + _","args":[{"column":0,"label":"t.a"},{"constant":1}]}]}'),
            (Expression.Like(b, Expression.Constant(Value.String("x%"))),
             '{"op":"_ LIKE _","args":[{"column":1,"label":"b"},{"constant":"x%"}]}'),
        ]
        for expr, expect in cases:
            with self.subTest(expr=expr):
                self.assertEqual(jsontext.encode(expression_to_json(expr, Labels())), expect)


class TestPlans(unittest.TestCase):
    def test_plans(self) -> None:
        cases: list[tuple[str, str]] = [
            (
                "SELECT * FROM item",
                '{"statement":"select","root":{"node":"scan","table":"item","alias":null,'
                '"filter":null}}',
            ),
            (
                "SELECT * FROM item WHERE id = 1 OR id = 2",
                '{"statement":"select","root":{"node":"key_lookup","table":"item","alias":null,'
                '"keys":[1,2]}}',
            ),
            (
                "SELECT * FROM item i WHERE name = 'x'",
                '{"statement":"select","root":{"node":"index_lookup","table":"item","alias":"i",'
                '"column":"name","values":["x"]}}',
            ),
            (
                "SELECT * FROM item WHERE FALSE",
                '{"statement":"select","root":{"node":"nothing"}}',
            ),
            (
                "SELECT 1",
                '{"statement":"select","root":{"node":"projection","expressions":'
                '[{"expr":{"constant":1},"alias":null}],"source":{"node":"values","rows":[[]]}}}',
            ),
            (
                "SELECT * FROM item JOIN sale ON item.id > sale.id",
                '{"statement":"select","root":{"node":"nested_loop_join","join":"inner",'
                '"predicate":{"op":"_ > _","args":[{"column":0,"label":"item.id"},'
                '{"column":3,"label":"sale.id"}]},'
                '"left":{"node":"scan","table":"item","alias":null,"filter":null},'
                '"right":{"node":"scan","table":"sale","alias":null,"filter":null}}}',
            ),
            (
                "INSERT INTO item (id) VALUES (1), (2)",
                '{"statement":"insert","table":"item","source":{"node":"values",'
                '"rows":[[{"constant":1}],[{"constant":2}]]}}',
            ),
            (
                "UPDATE item SET qty = qty + 1, name = DEFAULT",
                '{"statement":"update","table":"item","set":[{"column":"name","expr":'
                '{"constant":null}},{"column":"qty","expr":{"op":"_ + _","args":'
                '[{"column":2,"label":"item.qty"},{"constant":1}]}}],'
                '"source":{"node":"scan","table":"item","alias":null,"filter":null}}',
            ),
            (
                "DELETE FROM item WHERE qty IS NOT NULL",
                '{"statement":"delete","table":"item","source":{"node":"scan","table":"item",'
                '"alias":null,"filter":{"op":"NOT _","args":[{"op":"_ IS NULL","args":'
                '[{"column":2,"label":"item.qty"}]}]}}}',
            ),
            ("CREATE TABLE z (id INT PRIMARY KEY)", '{"statement":"create_table","table":"z"}'),
            ("DROP TABLE IF EXISTS z", '{"statement":"drop_table","table":"z"}'),
        ]
        for statement, expect in cases:
            with self.subTest(statement=statement):
                self.assertEqual(explain(statement), expect)

    def test_remap_null(self) -> None:
        """Remap columns that aren't mapped from the source are null."""
        source = Node.Values(rows=[[Expression.Constant(Value.Integer(1))]] )
        remap = Node.Remap(source=source, targets=[1])
        self.assertEqual(
            jsontext.encode(plan_to_json(Plan.Select(remap))),
            '{"statement":"select","root":{"node":"remap","columns":[null,'
            '{"column":0,"label":null}],"dropped":[],"source":{"node":"values",'
            '"rows":[[{"constant":1}]]}}}',
        )

    def test_view(self) -> None:
        """A View node has its name, its alias and its source, whose columns
        are labelled by the source."""
        source = Planner(CATALOG).build(Parser.parse("SELECT id FROM item"))
        assert isinstance(source, Plan.Select)
        cases: list[tuple[str | None, str]] = [(None, "null"), ("v2", '"v2"')]
        for alias, expect in cases:
            view = Node.View(name="v", alias=alias, column_names=["id"], source=source.root)
            filter = Node.Filter(view, Expression.Equal(Expression.Column(0), Expression.Column(0)))
            self.assertEqual(
                jsontext.encode(plan_to_json(Plan.Select(filter))),
                '{"statement":"select","root":{"node":"filter","predicate":{"op":"_ = _","args":'
                f'[{{"column":0,"label":"{alias or "v"}.id"}},{{"column":0,"label":"{alias or "v"}.id"}}]}},'
                f'"source":{{"node":"view","view":"v","alias":{expect},"source":'
                '{"node":"projection","expressions":[{"expr":{"column":0,"label":"item.id"},'
                '"alias":null}],"source":{"node":"scan","table":"item","alias":null,"filter":null}}}}}',
            )


    def test_system_table(self) -> None:
        """A SystemTable node has its name and its alias."""
        table = Table(name="toydb_users", primary_key=0, columns=[])
        node = Node.SystemTable(table=table, alias="u")
        self.assertEqual(
            jsontext.encode(plan_to_json(Plan.Select(node))),
            '{"statement":"select","root":{"node":"system_table","table":"toydb_users","alias":"u"}}',
        )


class TestResults(unittest.TestCase):
    def test_results(self) -> None:
        state = mvcc.TransactionState(version=3, read_only=True, active=set())
        cases: list[tuple[StatementResult, str]] = [
            (StatementResult.Begin(state), '{"result":"begin","version":3,"read_only":true}'),
            (StatementResult.Commit(3), '{"result":"commit","version":3}'),
            (StatementResult.Rollback(3), '{"result":"rollback","version":3}'),
            (StatementResult.CreateTable("T"), '{"result":"create_table","table":"T"}'),
            (
                StatementResult.DropTable("t", False),
                '{"result":"drop_table","table":"t","existed":false}',
            ),
            (StatementResult.Delete(1), '{"result":"delete","count":1}'),
            (StatementResult.Insert(2), '{"result":"insert","count":2}'),
            (StatementResult.Update(0), '{"result":"update","count":0}'),
            (
                StatementResult.Select(
                    [Label.Qualified("t", "id"), Label.Unqualified("n"), Label.None_()],
                    [[Value.Integer(1), Value.String("a"), Value.Float(-math.inf)]],
                ),
                '{"result":"select","columns":["t.id","n",null],'
                '"rows":[[1,"a",{"type":"FLOAT","value":"-inf"}]]}',
            ),
            (StatementResult.Analyze(["B", "a"]), '{"result":"analyze","tables":["B","a"]}'),
            (
                StatementResult.Export(ast.ExportFormat.Csv, ["id", '"a,b"']),
                '{"result":"export","format":"csv","lines":["id","\\"a,b\\""]}',
            ),
            (
                StatementResult.Format(["SELECT *", "FROM t"]),
                '{"result":"format","lines":["SELECT *","FROM t"]}',
            ),
            (StatementResult.CreateView("V"), '{"result":"create_view","view":"V"}'),
            (
                StatementResult.DropView("v", True),
                '{"result":"drop_view","view":"v","existed":true}',
            ),
            (StatementResult.Prepare("p"), '{"result":"prepare","name":"p"}'),
            (StatementResult.Deallocate("p"), '{"result":"deallocate","name":"p"}'),
            (StatementResult.CreateUser("u"), '{"result":"create_user","user":"u"}'),
            (
                StatementResult.DropUser("u", False),
                '{"result":"drop_user","user":"u","existed":false}',
            ),
            (StatementResult.Grant("t"), '{"result":"grant","table":"t"}'),
            (StatementResult.Grant(None), '{"result":"grant"}'),
            (StatementResult.Revoke("t"), '{"result":"revoke","table":"t"}'),
            (StatementResult.Revoke(None), '{"result":"revoke"}'),
            (StatementResult.SetRole("u"), '{"result":"set_role","user":"u"}'),
            (StatementResult.ResetRole("admin"), '{"result":"reset_role","user":"admin"}'),
            (
                StatementResult.CreatePolicy("t", "p"),
                '{"result":"create_policy","table":"t","policy":"p"}',
            ),
            (
                StatementResult.DropPolicy("t", "p", False),
                '{"result":"drop_policy","table":"t","policy":"p","existed":false}',
            ),
            (
                StatementResult.CreateMaterializedView("m", 3),
                '{"result":"create_materialized_view","view":"m","count":3}',
            ),
            (
                StatementResult.RefreshMaterializedView("m", 0),
                '{"result":"refresh_materialized_view","view":"m","count":0}',
            ),
            (
                StatementResult.DropMaterializedView("m", True),
                '{"result":"drop_materialized_view","view":"m","existed":true}',
            ),
        ]
        for result, expect in cases:
            with self.subTest(result=result):
                self.assertEqual(encode_result(result), expect)


if __name__ == "__main__":
    unittest.main()
