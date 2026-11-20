"""Executes statement plans.

Mirrors `src/sql/execution/executor.rs`.
"""

from abc import ABC
from dataclasses import dataclass
from functools import cmp_to_key
from typing import ClassVar

from toydb.error import errinput, panic
from toydb.iter import Empty, FilterMap, IntoIter, Map, Skip, Take
from toydb.sql.engine import Transaction
from toydb.sql.execution.aggregator import Aggregator
from toydb.sql.execution.join import HashJoiner, NestedLoopJoiner
from toydb.sql.planner import Direction, Node, Plan
from toydb.sql.types import Expression, Label, Row, Rows, Table, Value
from toydb.storage.btree import BTreeMap


class Executor:
    """Executes statement plans.

    The plan root specifies the action to take (e.g. SELECT, INSERT, UPDATE,
    etc). It has a nested tree of child nodes that process rows.

    Nodes are executed recursively, and return row iterators. Parent nodes
    recursively pull input rows from their child nodes, process them, and pass
    them on to their parent node.

    Below is an example of an (unoptimized) query plan:

    SELECT title, released, genres.name AS genre
    FROM movies INNER JOIN genres ON movies.genre_id = genres.id
    WHERE released >= 2000
    ORDER BY released

    Select
    └─ Order: movies.released desc
       └─ Projection: movies.title, movies.released, genres.name as genre
          └─ Filter: movies.released >= 2000
             └─ NestedLoopJoin: inner on movies.genre_id = genres.id
                ├─ Scan: movies
                └─ Scan: genres

    Rows flow from the tree leaves to the root:

    1. Scan nodes read rows from movies and genres.
    2. NestedLoopJoin joins the rows from movies and genres.
    3. Filter discards rows with release dates older than 2000.
    4. Projection picks out the requested column values from the rows.
    5. Order sorts the rows by release date.
    6. Select returns the final rows to the client.
    """

    def __init__(self, txn: Transaction) -> None:
        """Creates a new executor."""
        self.txn = txn
        """The transaction used to execute the plan."""

    def execute(self, plan: Plan) -> "ExecutionResult":
        """Executes a plan, returning an execution result."""
        match plan:
            # CREATE TABLE
            case Plan.CreateTable(schema):
                name = schema.name
                self.txn.create_table(schema)
                return ExecutionResult.CreateTable(name)

            # DROP TABLE
            case Plan.DropTable(name, if_exists):
                existed = self.txn.drop_table(name, if_exists)
                return ExecutionResult.DropTable(name, existed)

            # DELETE
            case Plan.Delete(table_name, primary_key, source_node):
                source = self.execute_node(source_node)
                count = self.delete(table_name, primary_key, source)
                return ExecutionResult.Delete(count)

            # INSERT
            case Plan.Insert(table, column_map, source_node):
                source = self.execute_node(source_node)
                count = self.insert(table, column_map, source)
                return ExecutionResult.Insert(count)

            # SELECT
            case Plan.Select(root):
                columns = [root.column_label(i) for i in range(root.columns())]
                rows = self.execute_node(root)
                return ExecutionResult.Select(columns, rows)

            # UPDATE
            case Plan.Update(table, primary_key, source_node, expressions):
                source = self.execute_node(source_node)
                count = self.update(table.name, primary_key, source, expressions)
                return ExecutionResult.Update(count)

            case _:
                panic(f"unknown plan {plan!r}")

    def execute_node(self, node: Node) -> Rows:
        """Recursively executes a query plan node, returning a row iterator."""
        match node:
            # GROUP BY and aggregate functions.
            case Node.Aggregate(source_node, group_by, aggregates):
                source = self.execute_node(source_node)
                aggregator = Aggregator(group_by, aggregates)
                aggregator.add_rows(source)
                return aggregator.into_rows()

            # WHERE and similar filtering.
            case Node.Filter(source_node, predicate):
                source = self.execute_node(source_node)

                def keep(row: Row) -> Row | None:
                    match predicate.evaluate(row):
                        case Value.Boolean(True):
                            return row
                        case Value.Boolean(False) | Value.Null():
                            return None
                        case value:
                            raise errinput(f"filter returned {value}, expected boolean")

                return FilterMap(source, keep)

            # JOIN using a hash join.
            case Node.HashJoin(left_node, left_column, right_node, right_column, outer):
                right_columns = right_node.columns()
                left = self.execute_node(left_node)
                right = self.execute_node(right_node)
                return HashJoiner(left, left_column, right, right_column, right_columns, outer)

            # Looks up primary keys by secondary index values.
            case Node.IndexLookup(table, column, values):
                column_name = table.columns[column].name
                ids = list(self.txn.lookup_index(table.name, column_name, values))
                return IntoIter(self.txn.get(table.name, ids))

            # Looks up rows by primary key.
            case Node.KeyLookup(table, keys):
                return IntoIter(self.txn.get(table.name, keys))

            # LIMIT
            case Node.Limit(source_node, limit):
                return Take(self.execute_node(source_node), limit)

            # JOIN using a nested loop join.
            case Node.NestedLoopJoin(left_node, right_node, join_predicate, outer):
                right_columns = right_node.columns()
                left = self.execute_node(left_node)
                right = self.execute_node(right_node)
                return NestedLoopJoiner(left, right, right_columns, join_predicate, outer)

            # An empty row iterator.
            case Node.Nothing():
                return Empty()

            # OFFSET
            case Node.Offset(source_node, offset):
                return Skip(self.execute_node(source_node), offset)

            # ORDER BY
            case Node.Order(source_node, key):
                source = self.execute_node(source_node)
                return self.order(source, key)

            # Projects columns from the source, and evaluates expressions.
            case Node.Projection(source_node, expressions):
                source = self.execute_node(source_node)

                def project(row: Row) -> Row:
                    return [expr.evaluate(row) for expr in expressions]

                return Map(source, project)

            # Remaps source column indexes to new target column indexes.
            case Node.Remap(source_node, targets):
                source = self.execute_node(source_node)
                size = max((target + 1 for target in targets if target is not None), default=0)

                def remap(row: Row) -> Row:
                    assert len(row) == len(targets), "iterators of different lengths"
                    remapped: Row = [Value.Null()] * size
                    for target, value in zip(targets, row):
                        if target is not None:
                            remapped[target] = value
                    return remapped

                return Map(source, remap)

            # Scans a table, optionally filtering rows.
            case Node.Scan(table, scan_filter):
                return self.txn.scan(table.name, scan_filter)

            # Emits constant values.
            case Node.Values(rows):

                def evaluate(row: list[Expression]) -> Row:
                    return [expr.evaluate(None) for expr in row]

                return Map(IntoIter(rows), evaluate)

            case _:
                panic(f"unknown node {node!r}")

    def delete(self, table: str, primary_key: int, source: Rows) -> int:
        """DELETE: deletes rows, taking primary keys from the source at the
        given primary_key column index. Returns the number of rows deleted."""
        ids = [row[primary_key] for row in source]
        count = len(ids)
        self.txn.delete(table, ids)
        return count

    def insert(self, table: Table, column_map: dict[int, int] | None, source: Rows) -> int:
        """INSERT: inserts rows into a table from the given source.

        If given, column_map contains the mapping of table → source columns
        for all columns in source. Otherwise, every column in source
        corresponds to those in table, but a tail of source columns may be
        missing.
        """
        rows = [self.build_row(table, column_map, values) for values in source]
        count = len(rows)
        self.txn.insert(table.name, rows)
        return count

    @staticmethod
    def build_row(table: Table, column_map: dict[int, int] | None, values: Row) -> Row:
        """Builds a complete table row for INSERT from the given source
        values, filling in default values for missing columns. The column_map
        is as for insert()."""
        # Fast path: the row is already complete, with no column mapping.
        if len(values) == len(table.columns) and column_map is None:
            return values
        if len(values) > len(table.columns):
            raise errinput(f"too many values for table {table.name}")
        if column_map is not None and len(column_map) != len(values):
            raise errinput("column and value counts do not match")

        # Map source columns to table columns, and fill in default values.
        row: Row = []
        for i, column in enumerate(table.columns):
            if column_map is None and i < len(values):
                # Pass through the source column to the table column.
                row.append(values[i])
            elif column_map is not None and (vi := column_map.get(i)) is not None:
                # Map the source column to the table column.
                row.append(values[vi])
            elif column.default is not None:
                # Column not given in source, use the default.
                row.append(column.default)
            else:
                raise errinput(f"no value given for column {column.name} with no default")
        return row

    def update(
        self,
        table: str,
        primary_key: int,
        source: Rows,
        expressions: list[tuple[int, Expression]],
    ) -> int:
        """UPDATE: updates rows passed in from the source. Returns the number
        of rows updated."""
        updates: BTreeMap[Value, Row] = BTreeMap()
        for row in source:
            update = list(row)
            for column, expr in expressions:
                update[column] = expr.evaluate(row)
            id = row[primary_key]
            updates.insert(id, update)
        count = len(updates)
        self.txn.update(table, updates)
        return count

    @staticmethod
    def order(source: Rows, order: list[tuple[Expression, Direction]]) -> Rows:
        """Sorts the input rows."""
        # We can't sort with a key function directly, since we may have to
        # vary the sort direction of each expression. Collect the rows and
        # pre-computed sort keys into a list.
        rows: list[tuple[Row, list[Value]]] = [
            (row, [expr.evaluate(row) for expr, _ in order]) for row in source
        ]

        def compare(a: tuple[Row, list[Value]], b: tuple[Row, list[Value]]) -> int:
            for a_key, b_key, (_, dir) in zip(a[1], b[1], order):
                ordering = a_key.cmp(b_key)
                if dir == Direction.Descending:
                    ordering = -ordering
                if ordering != 0:
                    return ordering
            return 0

        rows.sort(key=cmp_to_key(compare))

        return IntoIter([row for row, _ in rows])


class ExecutionResult(ABC):
    """A plan execution result."""

    CreateTable: ClassVar[type["CreateTable"]]
    DropTable: ClassVar[type["DropTable"]]
    Delete: ClassVar[type["Delete"]]
    Insert: ClassVar[type["Insert"]]
    Update: ClassVar[type["Update"]]
    Select: ClassVar[type["Select"]]


@dataclass
class CreateTable(ExecutionResult):
    name: str


@dataclass
class DropTable(ExecutionResult):
    name: str
    existed: bool


@dataclass
class Delete(ExecutionResult):
    count: int


@dataclass
class Insert(ExecutionResult):
    count: int


@dataclass
class Update(ExecutionResult):
    count: int


@dataclass
class Select(ExecutionResult):
    columns: list[Label]
    rows: Rows


ExecutionResult.CreateTable = CreateTable
ExecutionResult.DropTable = DropTable
ExecutionResult.Delete = Delete
ExecutionResult.Insert = Insert
ExecutionResult.Update = Update
ExecutionResult.Select = Select
