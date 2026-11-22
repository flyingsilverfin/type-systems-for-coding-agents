"""SQL execution plans.

Mirrors `src/sql/planner/plan.rs`.
"""

import enum
from abc import ABC
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar

from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import panic
from toydb.sql.parser import ast
from toydb.sql.types import Expression, Label, Table, Value

if TYPE_CHECKING:
    from toydb.sql.engine import Catalog, Transaction
    from toydb.sql.execution import ExecutionResult


class Plan(ABC):
    """A statement execution plan.

    The plan root specifies the action to take (e.g. SELECT, INSERT, UPDATE,
    etc). It has a nested tree of child nodes that stream an process rows.

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

    Plans are immutable: `optimize` returns a new plan.
    """

    CreateTable: ClassVar[type["CreateTable"]]
    DropTable: ClassVar[type["DropTable"]]
    Delete: ClassVar[type["Delete"]]
    Insert: ClassVar[type["Insert"]]
    Update: ClassVar[type["Update"]]
    Select: ClassVar[type["Select"]]

    @staticmethod
    def build(statement: ast.Statement, catalog: "Catalog") -> "Plan":
        """Builds a plan from an AST statement."""
        from toydb.sql.planner.planner import Planner

        return Planner(catalog).build(statement)

    def execute(self, txn: "Transaction") -> "ExecutionResult":
        """Executes the plan."""
        from toydb.sql.execution import Executor

        return Executor(txn).execute(self)

    def optimize(self) -> "Plan":
        """Optimizes the plan. See OPTIMIZERS for the list of optimizers."""
        from toydb.sql.planner.optimizer import OPTIMIZERS

        def optimize(node: Node) -> Node:
            for opt in OPTIMIZERS:
                node = opt.optimize(node)
            return node

        match self:
            case CreateTable() | DropTable():
                return self
            case Delete(source=source) | Insert(source=source) | Update(source=source):
                return replace(self, source=optimize(source))
            case Select(root):
                return Select(optimize(root))
            case _:
                panic(f"unknown plan {self!r}")

    def __str__(self) -> str:
        """Formats the plan as an EXPLAIN tree."""
        match self:
            case CreateTable(schema):
                return f"CreateTable: {schema.name}"
            case DropTable(name=table):
                return f"DropTable: {table}"
            case Delete(table=table, source=source):
                return f"Delete: {table}" + source.format("", False, True)
            case Insert(table=table, source=source):
                return f"Insert: {table.name}" + source.format("", False, True)
            case Update(table=table, source=source, expressions=expressions):
                formatted = ", ".join(
                    f"{table.columns[i].name}={expr.display(source)}" for i, expr in expressions
                )
                return f"Update: {table.name} ({formatted})" + source.format("", False, True)
            case Select(root):
                return root.format("", True, True)
            case _:
                panic(f"unknown plan {self!r}")

    def serialize(self, serializer: Serializer) -> None:
        """Serializes the plan, like `#[derive(Serialize)]`."""
        match self:
            case CreateTable(schema):
                serializer.serialize_struct_variant(0)
                schema.serialize(serializer)
            case DropTable(name, if_exists):
                serializer.serialize_struct_variant(1)
                serializer.serialize_str(name)
                serializer.serialize_bool(if_exists)
            case Delete(table, primary_key, source):
                serializer.serialize_struct_variant(2)
                serializer.serialize_str(table)
                serializer.serialize_u64(primary_key)
                source.serialize(serializer)
            case Insert(table, column_map, source):
                serializer.serialize_struct_variant(3)
                table.serialize(serializer)
                _COLUMN_MAP.serialize(column_map, serializer)
                source.serialize(serializer)
            case Update(table, primary_key, source, expressions):
                serializer.serialize_struct_variant(4)
                table.serialize(serializer)
                serializer.serialize_u64(primary_key)
                source.serialize(serializer)
                _UPDATE_EXPRESSIONS.serialize(expressions, serializer)
            case Select(root):
                serializer.serialize_newtype_variant(5)
                root.serialize(serializer)
            case _:
                panic(f"unknown plan {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Plan":
        """Deserializes a plan, like `#[derive(Deserialize)]`."""
        match deserializer.deserialize_enum():
            case 0:
                return CreateTable(schema=Table.deserialize(deserializer))
            case 1:
                return DropTable(
                    name=deserializer.deserialize_str(),
                    if_exists=deserializer.deserialize_bool(),
                )
            case 2:
                return Delete(
                    table=deserializer.deserialize_str(),
                    primary_key=deserializer.deserialize_u64(),
                    source=Node.deserialize(deserializer),
                )
            case 3:
                return Insert(
                    table=Table.deserialize(deserializer),
                    column_map=_COLUMN_MAP.deserialize(deserializer),
                    source=Node.deserialize(deserializer),
                )
            case 4:
                return Update(
                    table=Table.deserialize(deserializer),
                    primary_key=deserializer.deserialize_u64(),
                    source=Node.deserialize(deserializer),
                    expressions=_UPDATE_EXPRESSIONS.deserialize(deserializer),
                )
            case 5:
                return Select(Node.deserialize(deserializer))
            case index:
                raise deserializer.invalid_variant(index, 6)


@dataclass(frozen=True)
class CreateTable(Plan):
    """A CREATE TABLE plan. Creates a new table with the given schema. Errors
    if the table already exists or the schema is invalid."""

    schema: Table


@dataclass(frozen=True)
class DropTable(Plan):
    """A DROP TABLE plan. Drops the given table. Errors if the table does not
    exist, unless if_exists is true."""

    name: str
    if_exists: bool


@dataclass(frozen=True)
class Delete(Plan):
    """A DELETE plan. Deletes rows in table that match the rows from source.
    primary_key specifies the primary key column index in the source rows."""

    table: str
    primary_key: int
    source: "Node"


@dataclass(frozen=True)
class Insert(Plan):
    """An INSERT plan. Inserts rows from source (typically a Values node) into
    table. If column_map is given, it maps table → source column indexes and
    must have one entry for every column in source. Table columns not
    present in source will get the column's default value if set, or error."""

    table: Table
    column_map: dict[int, int] | None
    source: "Node"


@dataclass(frozen=True)
class Update(Plan):
    """An UPDATE plan. Updates rows in table that match the rows from source,
    where primary_key specifies the primary key column index in the source
    rows. The given column/expression pairs specify the row updates to make,
    evaluated using the existing source row, which must be a complete row
    from the update table."""

    table: Table
    primary_key: int
    source: "Node"
    expressions: list[tuple[int, Expression]]


@dataclass(frozen=True)
class Select(Plan):
    """A SELECT plan. Recursively executes the query plan tree and returns the
    resulting rows."""

    root: "Node"


Plan.CreateTable = CreateTable
Plan.DropTable = DropTable
Plan.Delete = Delete
Plan.Insert = Insert
Plan.Update = Update
Plan.Select = Select


class Node(ABC):
    """A query plan node. Returns a row iterator, and can be nested.

    Nodes are immutable: `transform` and the optimizers return new nodes.
    """

    Aggregate: ClassVar[type["NodeAggregate"]]
    Filter: ClassVar[type["Filter"]]
    HashJoin: ClassVar[type["HashJoin"]]
    IndexLookup: ClassVar[type["IndexLookup"]]
    KeyLookup: ClassVar[type["KeyLookup"]]
    Limit: ClassVar[type["Limit"]]
    NestedLoopJoin: ClassVar[type["NestedLoopJoin"]]
    Nothing: ClassVar[type["Nothing"]]
    Offset: ClassVar[type["Offset"]]
    Order: ClassVar[type["Order"]]
    Projection: ClassVar[type["Projection"]]
    Remap: ClassVar[type["Remap"]]
    Scan: ClassVar[type["Scan"]]
    Values: ClassVar[type["Values"]]
    View: ClassVar[type["View"]]

    def columns(self) -> int:
        """Returns the number of columns emitted by the node."""
        match self:
            # Source nodes emit all table columns.
            case IndexLookup(table=table) | KeyLookup(table=table) | Scan(table=table):
                return len(table.columns)

            # These nodes modify the set of columns.
            case NodeAggregate(group_by=group_by, aggregates=aggregates):
                return len(aggregates) + len(group_by)
            case Projection(expressions=expressions):
                return len(expressions)
            case Remap(targets=targets):
                return max((i + 1 for i in targets if i is not None), default=0)

            # Join nodes emit the combined columns.
            case HashJoin(left=left, right=right) | NestedLoopJoin(left=left, right=right):
                return left.columns() + right.columns()

            # Constant nodes have a predefined number of columns.
            case Nothing(columns):
                return len(columns)
            case Values(rows):
                return len(rows[0]) if rows else 0

            # Views emit the view's columns.
            case View(column_names=column_names):
                return len(column_names)

            # Simple nodes just pass through the source columns.
            case Filter(source=source) | Limit(source=source) | Offset(source=source) | Order(
                source=source
            ):
                return source.columns()

            case _:
                panic(f"unknown node {self!r}")

    def column_label(self, index: int) -> Label:
        """Returns a label for a column, if any, by tracing the column through
        the plan tree. Only used for query result headers and plan display
        purposes, not to look up expression columns (see Scope)."""
        match self:
            # Source nodes use the table/column name.
            case (
                IndexLookup(table=table, alias=alias)
                | KeyLookup(table=table, alias=alias)
                | Scan(table=table, alias=alias)
            ):
                if index >= len(table.columns):
                    panic("index out of bounds")
                name = alias if alias is not None else table.name
                return Label.Qualified(name, table.columns[index].name)

            # Views use the view/column name.
            case View(name=view, alias=alias, column_names=column_names):
                if index >= len(column_names):
                    panic("index out of bounds")
                name = alias if alias is not None else view
                return Label.Qualified(name, column_names[index])

            # These nodes rearrange columns. Route them to the correct upstream
            # column where appropriate.
            case NodeAggregate(source=source, group_by=group_by):
                if index < len(group_by):
                    match group_by[index]:
                        case Expression.Column(column):
                            return source.column_label(column)
                return Label.None_()
            case Projection(source, expressions, aliases):
                if index < len(aliases) and not isinstance(aliases[index], Label.None_):
                    # Aliased columns use the alias.
                    return aliases[index]
                if index < len(expressions):
                    match expressions[index]:
                        # Unaliased column references route to the source.
                        case Expression.Column(column):
                            return source.column_label(column)
                # Unaliased expressions don't have a name.
                return Label.None_()
            case Remap(source, targets):
                if index in targets:
                    return source.column_label(targets.index(index))
                return Label.None_()

            # Joins dispatch to the appropriate source.
            case HashJoin(left=left, right=right) | NestedLoopJoin(left=left, right=right):
                if index < left.columns():
                    return left.column_label(index)
                return right.column_label(index - left.columns())

            # Simple nodes just dispatch to the source.
            case Filter(source=source) | Limit(source=source) | Offset(source=source) | Order(
                source=source
            ):
                return source.column_label(index)

            # Nothing nodes contain the original columns of replaced nodes.
            case Nothing(columns):
                return columns[index] if index < len(columns) else Label.None_()

            # And some don't have any names at all.
            case Values():
                return Label.None_()

            case _:
                panic(f"unknown node {self!r}")

    def transform(
        self, before: Callable[["Node"], "Node"], after: Callable[["Node"], "Node"]
    ) -> "Node":
        """Recursively transforms query nodes depth-first by applying the given
        closures before and after descending. The closures may raise errors."""

        def xform(node: Node) -> Node:
            return node.transform(before, after)

        node = before(self)
        match node:
            case (
                NodeAggregate(source=source)
                | Filter(source=source)
                | Limit(source=source)
                | Offset(source=source)
                | Order(source=source)
                | Projection(source=source)
                | Remap(source=source)
                | View(source=source)
            ):
                node = replace(node, source=xform(source))
            case HashJoin(left=left, right=right) | NestedLoopJoin(left=left, right=right):
                node = replace(node, left=xform(left), right=xform(right))

            case IndexLookup() | KeyLookup() | Nothing() | Scan() | Values():
                pass

            case _:
                panic(f"unknown node {node!r}")
        return after(node)

    def transform_expressions(
        self,
        before: Callable[[Expression], Expression],
        after: Callable[[Expression], Expression],
    ) -> "Node":
        """Recursively transforms all node expressions by calling the given
        closures on them before and after descending."""
        match self:
            case Filter(source, predicate):
                return Filter(source, predicate.transform(before, after))
            case NestedLoopJoin(left, right, predicate, outer) if predicate is not None:
                return NestedLoopJoin(left, right, predicate.transform(before, after), outer)
            case Order(source, key):
                return Order(source, [(expr.transform(before, after), dir) for expr, dir in key])
            case Projection(source, expressions, aliases):
                expressions = [expr.transform(before, after) for expr in expressions]
                return Projection(source, expressions, aliases)
            case Scan(table, filter, alias) if filter is not None:
                return Scan(table, filter.transform(before, after), alias)
            case Values(rows):
                return Values([[expr.transform(before, after) for expr in row] for row in rows])

            case (
                NodeAggregate()
                | HashJoin()
                | IndexLookup()
                | KeyLookup()
                | Limit()
                | NestedLoopJoin()
                | Nothing()
                | Offset()
                | Remap()
                | Scan()
                | View()
            ):
                return self

            case _:
                panic(f"unknown node {self!r}")

    def __str__(self) -> str:
        return self.format("", True, True)

    def format(self, prefix: str, root: bool, last_child: bool) -> str:
        """Recursively formats the node. Prefix is used for tree branch lines.
        root is true if this is the root (first) node, and last_child is true
        if this is the last child node of the parent."""
        f = ""

        # If this is not the root node, emit a newline after the previous node.
        # This avoids a spurious newline at the end of the plan.
        if not root:
            f += "\n"

        # Prefix the node with a tree branch line. Modify the prefix for any
        # child nodes we'll recurse into.
        if not last_child:
            f += f"{prefix}├─ "
            prefix = f"{prefix}│  "
        elif not root:
            f += f"{prefix}└─ "
            prefix = f"{prefix}   "
        else:
            f += prefix

        # Format the node.
        match self:
            case NodeAggregate(source, group_by, aggregates):
                formatted = ", ".join(
                    [str(expr.display(source)) for expr in group_by]
                    + [agg.format(source) for agg in aggregates]
                )
                f += f"Aggregate: {formatted}"
                f += source.format(prefix, False, True)

            case Filter(source, predicate):
                f += f"Filter: {predicate.display(source)}"
                f += source.format(prefix, False, True)

            case HashJoin(left, left_column, right, right_column, outer):
                kind = "outer" if outer else "inner"
                match left.column_label(left_column):
                    case Label.None_():
                        left_label = f"left #{left_column}"
                    case label:
                        left_label = str(label)
                match right.column_label(right_column):
                    case Label.None_():
                        right_label = f"right #{right_column}"
                    case label:
                        right_label = str(label)
                f += f"HashJoin: {kind} on {left_label} = {right_label}"
                f += left.format(prefix, False, False)
                f += right.format(prefix, False, True)

            case IndexLookup(table, column, values, alias):
                name = table.columns[column].name
                f += f"IndexLookup: {table.name}.{name}"
                if alias is not None:
                    f += f" as {alias}.{name}"
                if values and len(values) < 10:
                    f += " ({})".format(", ".join(str(value) for value in values))
                else:
                    f += f" ({len(values)} values)"

            case KeyLookup(table, keys, alias):
                f += f"KeyLookup: {table.name}"
                if alias is not None:
                    f += f" as {alias}"
                if keys and len(keys) < 10:
                    f += " ({})".format(", ".join(str(key) for key in keys))
                else:
                    f += f" ({len(keys)} keys)"

            case Limit(source, limit):
                f += f"Limit: {limit}"
                f += source.format(prefix, False, True)

            case NestedLoopJoin(left, right, predicate, outer):
                kind = "outer" if outer else "inner"
                f += f"NestedLoopJoin: {kind}"
                if predicate is not None:
                    f += f" on {predicate.display(self)}"
                f += left.format(prefix, False, False)
                f += right.format(prefix, False, True)

            case Nothing():
                f += "Nothing"

            case Offset(source, offset):
                f += f"Offset: {offset}"
                f += source.format(prefix, False, True)

            case Order(source, key):
                orders = ", ".join(f"{expr.display(source)} {dir}" for expr, dir in key)
                f += f"Order: {orders}"
                f += source.format(prefix, False, True)

            case Projection(source, expressions, aliases):
                formatted_exprs: list[str] = []
                for i, expr in enumerate(expressions):
                    if i < len(aliases) and not isinstance(aliases[i], Label.None_):
                        formatted_exprs.append(f"{expr.display(source)} as {aliases[i]}")
                    else:
                        formatted_exprs.append(str(expr.display(source)))
                f += "Projection: {}".format(", ".join(formatted_exprs))
                f += source.format(prefix, False, True)

            case Remap(source, targets):

                def format_label(index: int) -> str:
                    match source.column_label(index):
                        case Label.None_():
                            return f"#{index}"
                        case label:
                            return str(label)

                remap = ", ".join(
                    format_label(from_) if from_ is not None else "Null"
                    for from_ in invert_remap(targets)
                )
                f += f"Remap: {remap}"
                dropped = ", ".join(
                    format_label(i) for i, target in enumerate(targets) if target is None
                )
                if dropped:
                    f += f" (dropped: {dropped})"
                f += source.format(prefix, False, True)

            case Scan(table, filter, alias):
                f += f"Scan: {table.name}"
                if alias is not None:
                    f += f" as {alias}"
                if filter is not None:
                    f += f" ({filter.display(self)})"

            case Values(rows):
                f += "Values: "
                if len(rows) == 1 and not rows[0]:
                    f += "blank row"
                elif len(rows) == 1:
                    f += ", ".join(str(expr.display(self)) for expr in rows[0])
                else:
                    f += f"{len(rows)} rows"

            case View(name, alias, _, source):
                f += f"View: {name}"
                if alias is not None:
                    f += f" as {alias}"
                f += source.format(prefix, False, True)

            case _:
                panic(f"unknown node {self!r}")
        return f

    def serialize(self, serializer: Serializer) -> None:
        """Serializes the node, like `#[derive(Serialize)]`."""
        match self:
            case NodeAggregate(source, group_by, aggregates):
                serializer.serialize_struct_variant(0)
                source.serialize(serializer)
                serde.Vec(Expression).serialize(group_by, serializer)
                serde.Vec(Aggregate).serialize(aggregates, serializer)
            case Filter(source, predicate):
                serializer.serialize_struct_variant(1)
                source.serialize(serializer)
                predicate.serialize(serializer)
            case HashJoin(left, left_column, right, right_column, outer):
                serializer.serialize_struct_variant(2)
                left.serialize(serializer)
                serializer.serialize_u64(left_column)
                right.serialize(serializer)
                serializer.serialize_u64(right_column)
                serializer.serialize_bool(outer)
            case IndexLookup(table, column, values, alias):
                serializer.serialize_struct_variant(3)
                table.serialize(serializer)
                serializer.serialize_u64(column)
                serde.Vec(Value).serialize(values, serializer)
                serde.Option(serde.String).serialize(alias, serializer)
            case KeyLookup(table, keys, alias):
                serializer.serialize_struct_variant(4)
                table.serialize(serializer)
                serde.Vec(Value).serialize(keys, serializer)
                serde.Option(serde.String).serialize(alias, serializer)
            case Limit(source, limit):
                serializer.serialize_struct_variant(5)
                source.serialize(serializer)
                serializer.serialize_u64(limit)
            case NestedLoopJoin(left, right, predicate, outer):
                serializer.serialize_struct_variant(6)
                left.serialize(serializer)
                right.serialize(serializer)
                serde.Option(Expression).serialize(predicate, serializer)
                serializer.serialize_bool(outer)
            case Nothing(columns):
                serializer.serialize_struct_variant(7)
                serde.Vec(Label).serialize(columns, serializer)
            case Offset(source, offset):
                serializer.serialize_struct_variant(8)
                source.serialize(serializer)
                serializer.serialize_u64(offset)
            case Order(source, key):
                serializer.serialize_struct_variant(9)
                source.serialize(serializer)
                _ORDER_KEY.serialize(key, serializer)
            case Projection(source, expressions, aliases):
                serializer.serialize_struct_variant(10)
                source.serialize(serializer)
                serde.Vec(Expression).serialize(expressions, serializer)
                serde.Vec(Label).serialize(aliases, serializer)
            case Remap(source, targets):
                serializer.serialize_struct_variant(11)
                source.serialize(serializer)
                _REMAP_TARGETS.serialize(targets, serializer)
            case Scan(table, filter, alias):
                serializer.serialize_struct_variant(12)
                table.serialize(serializer)
                serde.Option(Expression).serialize(filter, serializer)
                serde.Option(serde.String).serialize(alias, serializer)
            case Values(rows):
                serializer.serialize_struct_variant(13)
                serde.Vec(serde.Vec(Expression)).serialize(rows, serializer)
            case View(name, alias, column_names, source):
                serializer.serialize_struct_variant(14)
                serializer.serialize_str(name)
                serde.Option(serde.String).serialize(alias, serializer)
                serde.Vec(serde.String).serialize(column_names, serializer)
                source.serialize(serializer)
            case _:
                panic(f"unknown node {self!r}")

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Node":
        """Deserializes a node, like `#[derive(Deserialize)]`."""
        match deserializer.deserialize_enum():
            case 0:
                return NodeAggregate(
                    source=Node.deserialize(deserializer),
                    group_by=serde.Vec(Expression).deserialize(deserializer),
                    aggregates=serde.Vec(Aggregate).deserialize(deserializer),
                )
            case 1:
                return Filter(
                    source=Node.deserialize(deserializer),
                    predicate=Expression.deserialize(deserializer),
                )
            case 2:
                return HashJoin(
                    left=Node.deserialize(deserializer),
                    left_column=deserializer.deserialize_u64(),
                    right=Node.deserialize(deserializer),
                    right_column=deserializer.deserialize_u64(),
                    outer=deserializer.deserialize_bool(),
                )
            case 3:
                return IndexLookup(
                    table=Table.deserialize(deserializer),
                    column=deserializer.deserialize_u64(),
                    values=serde.Vec(Value).deserialize(deserializer),
                    alias=serde.Option(serde.String).deserialize(deserializer),
                )
            case 4:
                return KeyLookup(
                    table=Table.deserialize(deserializer),
                    keys=serde.Vec(Value).deserialize(deserializer),
                    alias=serde.Option(serde.String).deserialize(deserializer),
                )
            case 5:
                return Limit(
                    source=Node.deserialize(deserializer),
                    limit=deserializer.deserialize_u64(),
                )
            case 6:
                return NestedLoopJoin(
                    left=Node.deserialize(deserializer),
                    right=Node.deserialize(deserializer),
                    predicate=serde.Option(Expression).deserialize(deserializer),
                    outer=deserializer.deserialize_bool(),
                )
            case 7:
                return Nothing(columns_=serde.Vec(Label).deserialize(deserializer))
            case 8:
                return Offset(
                    source=Node.deserialize(deserializer),
                    offset=deserializer.deserialize_u64(),
                )
            case 9:
                return Order(
                    source=Node.deserialize(deserializer),
                    key=_ORDER_KEY.deserialize(deserializer),
                )
            case 10:
                return Projection(
                    source=Node.deserialize(deserializer),
                    expressions=serde.Vec(Expression).deserialize(deserializer),
                    aliases=serde.Vec(Label).deserialize(deserializer),
                )
            case 11:
                return Remap(
                    source=Node.deserialize(deserializer),
                    targets=_REMAP_TARGETS.deserialize(deserializer),
                )
            case 12:
                return Scan(
                    table=Table.deserialize(deserializer),
                    filter=serde.Option(Expression).deserialize(deserializer),
                    alias=serde.Option(serde.String).deserialize(deserializer),
                )
            case 13:
                return Values(rows=serde.Vec(serde.Vec(Expression)).deserialize(deserializer))
            case 14:
                return View(
                    name=deserializer.deserialize_str(),
                    alias=serde.Option(serde.String).deserialize(deserializer),
                    column_names=serde.Vec(serde.String).deserialize(deserializer),
                    source=Node.deserialize(deserializer),
                )
            case index:
                raise deserializer.invalid_variant(index, 15)


@dataclass(frozen=True)
class NodeAggregate(Node):
    """Aggregates values for the given group_by buckets, across all rows in
    the source node. The group_by columns are emitted first, followed by the
    aggregate columns, in the given order. This is the Rust `Node::Aggregate`,
    available as `Node.Aggregate`."""

    source: Node
    group_by: list[Expression]
    aggregates: list["Aggregate"]


@dataclass(frozen=True)
class Filter(Node):
    """Filters source rows, by discarding rows for which the predicate
    evaluates to false."""

    source: Node
    predicate: Expression


@dataclass(frozen=True)
class HashJoin(Node):
    """Joins the left and right sources on the given columns by building an
    in-memory hashmap of the right source and looking up matches for each
    row in the left source. When outer is true (e.g. LEFT JOIN), a left row
    without a right match is emitted anyway, with NULLs for the right row."""

    left: Node
    left_column: int
    right: Node
    right_column: int
    outer: bool


@dataclass(frozen=True)
class IndexLookup(Node):
    """Looks up the given values in a secondary index and emits matching rows.
    NULL and NaN values are considered equal, to allow IS NULL and IS NAN
    index lookups, as is -0.0 and 0.0."""

    table: Table
    column: int
    values: list[Value]
    alias: str | None


@dataclass(frozen=True)
class KeyLookup(Node):
    """Looks up the given primary keys and emits their rows."""

    table: Table
    keys: list[Value]
    alias: str | None


@dataclass(frozen=True)
class Limit(Node):
    """Only emits the first limit rows from the source, discards the rest."""

    source: Node
    limit: int


@dataclass(frozen=True)
class NestedLoopJoin(Node):
    """Joins the left and right sources on the given predicate by buffering
    the right source and iterating over it for every row in the left source.
    When outer is true (e.g. LEFT JOIN), a left row without a right match is
    emitted anyway, with NULLs for the right row."""

    left: Node
    right: Node
    predicate: Expression | None
    outer: bool


@dataclass(frozen=True)
class Nothing(Node):
    """Nothing does not emit anything, and is used to short-circuit nodes that
    can't emit anything during optimization. It retains the column names of
    any replaced nodes for results headers and plan formatting."""

    columns_: list[Label]
    """The Rust field `columns`, which would shadow `Node.columns()` in Python."""


@dataclass(frozen=True)
class Offset(Node):
    """Discards the first offset rows from source, emits the rest."""

    source: Node
    offset: int


@dataclass(frozen=True)
class Order(Node):
    """Sorts the source rows by the given sort key. Buffers the entire row set
    in memory."""

    source: Node
    key: list[tuple[Expression, "Direction"]]


@dataclass(frozen=True)
class Projection(Node):
    """Projects the input rows by evaluating the given expressions. Aliases
    are only used when displaying the plan."""

    source: Node
    expressions: list[Expression]
    aliases: list[Label]


@dataclass(frozen=True)
class Remap(Node):
    """Remaps source columns to the given target column index, or None to drop
    the column. Unspecified target columns yield Value.Null. The source →
    target mapping ensures a source column can only be mapped to a single
    target column, allowing the value to be moved rather than cloned."""

    source: Node
    targets: list[int | None]


@dataclass(frozen=True)
class Scan(Node):
    """A full table scan, with an optional pushed-down filter. The schema is
    used during plan optimization. The alias is only used for formatting."""

    table: Table
    filter: Expression | None
    alias: str | None


@dataclass(frozen=True)
class Values(Node):
    """A constant set of values."""

    rows: list[list[Expression]]


@dataclass(frozen=True)
class View(Node):
    """A view: emits the rows of the view's query plan, with the view's column
    names. The alias is only used for formatting."""

    name: str
    alias: str | None
    column_names: list[str]
    source: Node


Node.Aggregate = NodeAggregate
Node.Filter = Filter
Node.HashJoin = HashJoin
Node.IndexLookup = IndexLookup
Node.KeyLookup = KeyLookup
Node.Limit = Limit
Node.NestedLoopJoin = NestedLoopJoin
Node.Nothing = Nothing
Node.Offset = Offset
Node.Order = Order
Node.Projection = Projection
Node.Remap = Remap
Node.Scan = Scan
Node.Values = Values
Node.View = View


class Aggregate(ABC):
    """An aggregate function."""

    Average: ClassVar[type["Average"]]
    Count: ClassVar[type["Count"]]
    Max: ClassVar[type["Max"]]
    Min: ClassVar[type["Min"]]
    Sum: ClassVar[type["Sum"]]

    def format(self, node: Node) -> str:
        match self:
            case Average(expr):
                return f"avg({expr.display(node)})"
            case Count(expr):
                return f"count({expr.display(node)})"
            case Max(expr):
                return f"max({expr.display(node)})"
            case Min(expr):
                return f"min({expr.display(node)})"
            case Sum(expr):
                return f"sum({expr.display(node)})"
            case _:
                panic(f"unknown aggregate {self!r}")

    def expr(self) -> Expression:
        """Returns the inner expression."""
        match self:
            case Average(expr) | Count(expr) | Max(expr) | Min(expr) | Sum(expr):
                return expr
            case _:
                panic(f"unknown aggregate {self!r}")

    def serialize(self, serializer: Serializer) -> None:
        """Serializes the aggregate, like `#[derive(Serialize)]`."""
        serializer.serialize_newtype_variant(_AGGREGATES.index(type(self)))
        self.expr().serialize(serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Aggregate":
        """Deserializes an aggregate, like `#[derive(Deserialize)]`."""
        index = deserializer.deserialize_enum()
        if index >= len(_AGGREGATES):
            raise deserializer.invalid_variant(index, len(_AGGREGATES))
        return _AGGREGATES[index](Expression.deserialize(deserializer))


@dataclass(frozen=True)
class Average(Aggregate):
    expression: Expression


@dataclass(frozen=True)
class Count(Aggregate):
    expression: Expression


@dataclass(frozen=True)
class Max(Aggregate):
    expression: Expression


@dataclass(frozen=True)
class Min(Aggregate):
    expression: Expression


@dataclass(frozen=True)
class Sum(Aggregate):
    expression: Expression


_AGGREGATES: tuple[type[Average | Count | Max | Min | Sum], ...] = (Average, Count, Max, Min, Sum)
"""The aggregate variants, in Rust declaration order (for serialization)."""

Aggregate.Average = Average
Aggregate.Count = Count
Aggregate.Max = Max
Aggregate.Min = Min
Aggregate.Sum = Sum


class Direction(enum.Enum):
    """A sort order direction."""

    Ascending = 0
    Descending = 1

    def __str__(self) -> str:
        match self:
            case Direction.Ascending:
                return "asc"
            case Direction.Descending:
                return "desc"

    @staticmethod
    def from_ast(dir: ast.Direction) -> "Direction":
        """Converts an AST direction. Mirrors the Rust `From<ast::Direction>
        for Direction`."""
        match dir:
            case ast.Direction.Ascending:
                return Direction.Ascending
            case ast.Direction.Descending:
                return Direction.Descending

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_unit_variant(self.value)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Direction":
        index = deserializer.deserialize_enum()
        if index >= len(cls):
            raise deserializer.invalid_variant(index, len(cls))
        return cls(index)


def invert_remap(targets: list[int | None]) -> list[int | None]:
    """Inverts a Remap targets list to a list of source indexes, with None
    for columns that weren't targeted."""
    size = max((i + 1 for i in targets if i is not None), default=0)
    sources: list[int | None] = [None] * size
    for from_, to in enumerate(targets):
        if to is not None:
            sources[to] = from_
    return sources


# Serialization descriptors for compound plan fields.
_COLUMN_MAP = serde.Option(serde.HashMap(serde.U64, serde.U64))
_UPDATE_EXPRESSIONS: serde.Serde[list[tuple[int, Expression]]] = serde.Vec(
    serde.Tuple(serde.U64, Expression)
)
_ORDER_KEY: serde.Serde[list[tuple[Expression, Direction]]] = serde.Vec(
    serde.Tuple(Expression, Direction)
)
_REMAP_TARGETS = serde.Vec(serde.Option(serde.U64))
