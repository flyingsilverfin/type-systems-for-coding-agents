"""The SQL planner.

Mirrors `src/sql/planner/planner.rs`.
"""

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from toydb.error import Error, errinput, panic
from toydb.sql.parser import ast
from toydb.sql.planner.plan import Aggregate, Direction, Node, Plan, invert_remap
from toydb.sql.types import Column, Expression, Label, Table, Value

if TYPE_CHECKING:
    from toydb.sql.engine import Catalog


class Planner:
    """The planner builds an execution plan from a parsed Abstract Syntax Tree,
    using the catalog for schema information.

    To build the plan, it recursively traverses the AST and transforms AST
    nodes into plan nodes. The planner also resolves column names to column
    indexes, using a Scope to track currently visible columns and tables at
    each node.
    """

    def __init__(self, catalog: "Catalog") -> None:
        """Creates a new planner."""
        self.catalog = catalog

    def build(self, statement: ast.Statement) -> Plan:
        """Builds a plan for an AST statement."""
        match statement:
            case ast.Statement.CreateTable(name, columns):
                return self.build_create_table(name, columns)
            case ast.Statement.DropTable(name, if_exists):
                return self.build_drop_table(name, if_exists)

            case ast.Statement.Delete(table, where):
                return self.build_delete(table, where)
            case ast.Statement.Insert(table, columns, values):
                return self.build_insert(table, columns, values)
            case ast.Statement.Update(table, set, where):
                return self.build_update(table, set, where)
            case ast.Statement.Select(
                select, from_, where, group_by, having, order_by, offset, limit
            ):
                return self.build_select(
                    select, from_, where, group_by, having, order_by, offset, limit
                )

            # Transaction and explain statements are handled by Session.
            case _:
                panic(f"unexpected statement {statement!r}")

    def build_create_table(self, name: str, columns: list[ast.Column]) -> Plan:
        """Builds a CREATE TABLE plan."""
        # Most schema validation happens during execution via Table.validate().
        primary_key = next((i for i, c in enumerate(columns) if c.primary_key), None)
        if primary_key is None:
            raise errinput(f"no primary key for table {name}")
        if sum(1 for c in columns if c.primary_key) > 1:
            raise errinput(f"multiple primary keys for table {name}")

        def build_column(c: ast.Column) -> Column:
            nullable = c.nullable if c.nullable is not None else not c.primary_key
            default: Value | None
            if c.default is not None:
                default = self.build_constant_value(c.default)
            elif nullable:
                default = Value.Null()
            else:
                default = None
            return Column(
                name=c.name,
                datatype=c.datatype,
                nullable=nullable,
                default=default,
                unique=c.unique or c.primary_key,
                index=(c.index or c.unique or c.references is not None) and not c.primary_key,
                references=c.references,
            )

        schema = Table(
            name=name, primary_key=primary_key, columns=[build_column(c) for c in columns]
        )
        return Plan.CreateTable(schema=schema)

    def build_drop_table(self, name: str, if_exists: bool) -> Plan:
        """Builds a DROP TABLE plan."""
        return Plan.DropTable(name=name, if_exists=if_exists)

    def build_delete(self, table_name: str, where: ast.Expression | None) -> Plan:
        """Builds a DELETE plan."""
        table = self.catalog.must_get_table(table_name)
        scope = Scope.from_table(table)
        filter = self.build_expression(where, scope) if where is not None else None
        return Plan.Delete(
            table=table.name,
            primary_key=table.primary_key,
            source=Node.Scan(table=table, filter=filter, alias=None),
        )

    def build_insert(
        self,
        table_name: str,
        columns: list[str] | None,
        values: list[list[ast.Expression]],
    ) -> Plan:
        """Builds an INSERT plan."""
        table = self.catalog.must_get_table(table_name)
        column_map: dict[int, int] | None = None
        if columns is not None:
            column_map = {}
            for vindex, name in enumerate(columns):
                cindex = next((i for i, c in enumerate(table.columns) if c.name == name), None)
                if cindex is None:
                    raise errinput(f"unknown column {name} in table {table.name}")
                if cindex in column_map:
                    raise errinput(f"column {name} given multiple times")
                column_map[cindex] = vindex
        scope = Scope()
        rows = [[self.build_expression(expr, scope) for expr in exprs] for exprs in values]
        return Plan.Insert(table=table, column_map=column_map, source=Node.Values(rows=rows))

    def build_update(
        self,
        table_name: str,
        set: dict[str, ast.Expression | None],
        where: ast.Expression | None,
    ) -> Plan:
        """Builds an UPDATE plan."""
        table = self.catalog.must_get_table(table_name)
        scope = Scope.from_table(table)
        filter = self.build_expression(where, scope) if where is not None else None
        expressions: list[tuple[int, Expression]] = []
        for column, ast_expr in sorted(set.items()):  # ordered by column, like a BTreeMap
            index = scope.lookup_column(None, column)
            if ast_expr is not None:
                expr = self.build_expression(ast_expr, scope)
            elif (default := table.columns[index].default) is not None:
                expr = Expression.Constant(default)
            else:
                raise errinput(f"column {column} has no default value")
            expressions.append((index, expr))
        return Plan.Update(
            table=table,
            primary_key=table.primary_key,
            source=Node.Scan(table=table, filter=filter, alias=None),
            expressions=expressions,
        )

    def build_select(
        self,
        select: list[tuple[ast.Expression, str | None]],
        from_: list[ast.From],
        where: ast.Expression | None,
        group_by: list[ast.Expression],
        having: ast.Expression | None,
        order_by: list[tuple[ast.Expression, ast.Direction]],
        offset: ast.Expression | None,
        limit: ast.Expression | None,
    ) -> Plan:
        """Builds a SELECT plan."""
        scope = Scope()

        # Build FROM clause.
        node: Node
        if from_:
            node = self.build_from_clause(from_, scope)
        else:
            # For a constant SELECT, emit a single empty row to project with.
            # This allows using aggregate functions and WHERE as normal.
            node = Node.Values(rows=[[]])

        # Expand out SELECT * to all FROM columns if there are multiple SELECT
        # expressions or a GROUP BY clause (to ensure all columns are in GROUP
        # BY). For simplicity, expressions only supports scalar values, so we
        # special-case the * tuple here.
        if (ast.Expression.All(), None) in select:
            if node.columns() == 0:
                raise errinput("SELECT * requires a FROM clause")
            if len(select) > 1 or group_by:
                expanded: list[tuple[ast.Expression, str | None]] = []
                for expr, alias in select:
                    match expr:
                        case ast.Expression.All():
                            expanded.extend(
                                (node.column_label(i).into_ast_expression(), None)
                                for i in range(node.columns())
                            )
                        case _:
                            expanded.append((expr, alias))
                select = expanded

        # Build WHERE clause.
        if where is not None:
            predicate = self.build_expression(where, scope)
            node = Node.Filter(source=node, predicate=predicate)

        # Build aggregate functions and GROUP BY clause.
        aggregates = self.collect_aggregates(select, having, order_by)
        if group_by or aggregates:
            node, scope = self.build_aggregate(node, group_by, aggregates, scope)

        # Build SELECT clause. We can omit this for a trivial SELECT *.
        if select != [(ast.Expression.All(), None)]:
            # Prepare the post-projection scope.
            child_scope = scope.project(select)

            # Build the SELECT column expressions and aliases.
            expressions: list[Expression] = []
            aliases: list[Label] = []
            for expr, alias in select:
                expressions.append(self.build_expression(expr, scope))
                aliases.append(Label.from_name(alias))

            # Add hidden columns for HAVING and ORDER BY columns not in SELECT.
            hidden = self.build_select_hidden(having, order_by, scope, child_scope)
            aliases.extend(Label.None_() for _ in hidden)
            expressions.extend(hidden)

            scope = child_scope
            node = Node.Projection(source=node, expressions=expressions, aliases=aliases)

        # Build HAVING clause.
        if having is not None:
            if not scope.aggregates:
                raise errinput("HAVING requires GROUP BY or aggregate function")
            predicate = self.build_expression(having, scope)
            node = Node.Filter(source=node, predicate=predicate)

        # Build ORDER BY clause.
        if order_by:
            key = [
                (self.build_expression(expr, scope), Direction.from_ast(dir))
                for expr, dir in order_by
            ]
            node = Node.Order(source=node, key=key)

        # Build OFFSET clause.
        if offset is not None:
            match self.build_constant_value(offset):
                case Value.Integer(value) if value >= 0:
                    node = Node.Offset(source=node, offset=value)
                case value:
                    raise errinput(f"invalid offset {value}")

        # Build LIMIT clause.
        if limit is not None:
            match self.build_constant_value(limit):
                case Value.Integer(value) if value >= 0:
                    node = Node.Limit(source=node, limit=value)
                case value:
                    raise errinput(f"invalid limit {value}")

        # Remove any hidden columns before emitting the result.
        if (targets := scope.remap_hidden()) is not None:
            node = Node.Remap(source=node, targets=targets)

        return Plan.Select(node)

    def build_from_clause(self, from_: list[ast.From], scope: "Scope") -> Node:
        """Builds a FROM clause consisting of one or more items. Each item is
        either a table or a join of two or more tables. All items are
        implicitly joined, e.g. "SELECT * FROM a, b" is an implicit full join
        of a and b."""
        # Build the first FROM item. A FROM clause must have at least one.
        if not from_:
            raise errinput("no from items given")
        node = self.build_from(from_[0], scope)

        # Build and implicitly join additional items.
        for item in from_[1:]:
            right = self.build_from(item, scope)
            node = Node.NestedLoopJoin(left=node, right=right, predicate=None, outer=False)
        return node

    def build_from(self, from_: ast.From, parent_scope: "Scope") -> Node:
        """Builds FROM items, which can either be a single table or a chained
        join of multiple tables, e.g. "SELECT * FROM a LEFT JOIN b ON b.a_id =
        a.id"."""
        # Each from item is built in its own scope, such that a join node only
        # sees the columns of its children. It's then merged into the parent.
        scope = Scope()

        node: Node
        match from_:
            # A full table scan.
            case ast.From.Table(name, alias):
                table = self.catalog.must_get_table(name)
                scope.add_table(table, alias)
                node = Node.Scan(table=table, filter=None, alias=alias)

            # A two-way join. The left or right nodes may be chained joins.
            case ast.From.Join(left_from, right_from, type, ast_predicate):
                # Right joins are built as a left join then column swap.
                if type == ast.JoinType.Right:
                    left_from, right_from = right_from, left_from

                # Build the left and right nodes.
                left = self.build_from(left_from, scope)
                right = self.build_from(right_from, scope)
                left_size, right_size = left.columns(), right.columns()

                # Build the join node.
                predicate = (
                    self.build_expression(ast_predicate, scope)
                    if ast_predicate is not None
                    else None
                )
                outer = type.is_outer()
                node = Node.NestedLoopJoin(
                    left=left, right=right, predicate=predicate, outer=outer
                )

                # For right joins, swap the columns.
                if type == ast.JoinType.Right:
                    size = left_size + right_size
                    targets: list[int | None] = [(i + right_size) % size for i in range(size)]
                    scope = scope.remap(targets)
                    node = Node.Remap(source=node, targets=targets)

            case _:
                panic(f"unknown from item {from_!r}")

        parent_scope.merge(scope)
        return node

    def build_aggregate(
        self,
        source: Node,
        group_by: list[ast.Expression],
        aggregates: list[ast.Expression],
        scope: "Scope",
    ) -> tuple[Node, "Scope"]:
        """Builds an aggregate node, which computes aggregates for a set of
        GROUP BY buckets. The aggregate functions have been collected from the
        SELECT, HAVING, and ORDER BY clauses.

        The ast.Expression for each aggregate function and GROUP BY expression
        is tracked in the Scope and mapped to the column index. Later nodes
        (i.e. SELECT, HAVING, and ORDER BY) can look up the column index of
        aggregate expressions while building expressions. Consider e.g.:

        SELECT SUM(a) / COUNT(*) FROM t GROUP BY b % 10 HAVING b % 10 >= 5 ORDER BY MAX(c)

        This will build an Aggregate node for SUM(a), COUNT(*), MAX(c) bucketed
        by b % 10. The SELECT can look up up SUM(a) and COUNT(*) to compute the
        division, and HAVING can look up b % 10 to compute the predicate.

        Returns the node and its scope, which replaces the given scope (Rust
        assigns it through the `&mut Scope`).
        """
        # Construct a child scope with the group_by and aggregate AST
        # expressions, for lookups. Discard duplicate expressions.
        child_scope = scope.spawn()
        group_by = [e for e in group_by if child_scope.add_aggregate(e, scope) is not None]
        aggregates = [e for e in aggregates if child_scope.add_aggregate(e, scope) is not None]

        # Build the node from the remaining unique expressions.
        node = Node.Aggregate(
            source=source,
            group_by=[self.build_expression(expr, scope) for expr in group_by],
            aggregates=[self.build_aggregate_function(expr, scope) for expr in aggregates],
        )
        return node, child_scope

    @staticmethod
    def build_aggregate_function(expr: ast.Expression, scope: "Scope") -> Aggregate:
        """Builds an aggregate function from an AST expression."""
        if not isinstance(expr, ast.Expression.Function):
            panic("aggregate expression must be function")
        name, args = expr.name, expr.args
        if len(args) != 1:
            raise errinput(f"{name} takes 1 argument")
        if args[0].contains(Planner.is_aggregate_function):
            raise errinput("aggregate functions can't be nested")
        # Special-case COUNT(*) since expressions don't support tuples.
        match (name, args[0]):
            case ("count", ast.Expression.All()):
                argument = Expression.Constant(Value.Boolean(True))
            case (_, arg):
                argument = Planner.build_expression(arg, scope)
        match name:
            case "avg":
                return Aggregate.Average(argument)
            case "count":
                return Aggregate.Count(argument)
            case "min":
                return Aggregate.Min(argument)
            case "max":
                return Aggregate.Max(argument)
            case "sum":
                return Aggregate.Sum(argument)
            case _:
                raise errinput(f"unknown aggregate function {name}")

    @staticmethod
    def is_aggregate_function(expr: ast.Expression) -> bool:
        """Checks whether a given AST expression is an aggregate function."""
        match expr:
            case ast.Expression.Function(name, _):
                return name in ("avg", "count", "max", "min", "sum")
        return False

    @staticmethod
    def collect_aggregates(
        select: list[tuple[ast.Expression, str | None]],
        having: ast.Expression | None,
        order_by: list[tuple[ast.Expression, ast.Direction]],
    ) -> list[ast.Expression]:
        """Collects aggregate functions from SELECT, HAVING, and ORDER BY
        clauses."""
        exprs = [expr for expr, _ in select]
        if having is not None:
            exprs.append(having)
        exprs.extend(expr for expr, _ in order_by)
        aggregates: list[ast.Expression] = []
        for expr in exprs:
            expr.collect(Planner.is_aggregate_function, aggregates)
        return aggregates

    def build_select_hidden(
        self,
        having: ast.Expression | None,
        order_by: list[tuple[ast.Expression, ast.Direction]],
        scope: "Scope",
        child_scope: "Scope",
    ) -> list[Expression]:
        """Builds hidden columns for a projection to pass through columns that
        are used by downstream nodes. Consider e.g.:

        SELECT id FROM table ORDER BY value

        The ORDER BY node is evaluated after the SELECT projection (it may need
        to order on projected columns), but "value" isn't projected and thus
        isn't available to the ORDER BY node. We add a hidden "value" column to
        the projection to satisfy the ORDER BY.

        Hidden columns are tracked in the scope and stripped before the result
        is returned to the client.
        """
        hidden: list[Expression] = []

        def visit(expr: ast.Expression) -> bool:
            # If this is an aggregate or GROUP BY expression that isn't
            # already available in the child scope, add a hidden column.
            index = scope.lookup_aggregate(expr)
            if index is not None and child_scope.lookup_aggregate(expr) is None:
                child_scope.add_passthrough(scope, index, True)
                hidden.append(Expression.Column(index))
                return True

            # Look for column references that don't exist post-projection,
            # but that do exist in the parent, and add hidden columns.
            if not isinstance(expr, ast.Expression.Column):
                return True
            try:
                child_scope.lookup_column(expr.table, expr.column)
                return True
            except Error:
                pass
            try:
                index = scope.lookup_column(expr.table, expr.column)
            except Error:
                # If the parent lookup fails too (i.e. unknown column),
                # ignore the error. It will be surfaced during building.
                return True
            child_scope.add_passthrough(scope, index, True)
            hidden.append(Expression.Column(index))
            return True

        exprs = [having] if having is not None else []
        exprs.extend(expr for expr, _ in order_by)
        for expr in exprs:
            expr.walk(visit)
        return hidden

    @staticmethod
    def build_expression(expr: ast.Expression, scope: "Scope") -> Expression:
        """Builds an expression from an AST expression, looking up columns and
        aggregate expressions in the scope."""
        # Look up aggregate functions or GROUP BY expressions. These were added
        # to the scope when building the Aggregate node, if any.
        if (index := scope.lookup_aggregate(expr)) is not None:
            return Expression.Column(index)

        # Helper for building a child expression.
        def build(expr: ast.Expression) -> Expression:
            return Planner.build_expression(expr, scope)

        match expr:
            # For simplicity, expression evaluation only supports scalar
            # values, not compound types like tuples. Support for * is
            # therefore special-cased in SELECT and COUNT(*).
            case ast.Expression.All():
                raise errinput("unsupported use of *")
            case ast.Expression.Literal(literal):
                match literal:
                    case ast.Literal.Null():
                        return Expression.Constant(Value.Null())
                    case ast.Literal.Boolean(b):
                        return Expression.Constant(Value.Boolean(b))
                    case ast.Literal.Integer(i):
                        return Expression.Constant(Value.Integer(i))
                    case ast.Literal.Float(f):
                        return Expression.Constant(Value.Float(f))
                    case ast.Literal.String(s):
                        return Expression.Constant(Value.String(s))
                    case _:
                        panic(f"unknown literal {literal!r}")
            case ast.Expression.Column(table, name):
                return Expression.Column(scope.lookup_column(table, name))
            case ast.Expression.Function(name, args):
                match (name, len(args)):
                    # NB: aggregate functions are processed above.
                    case ("sqrt", 1):
                        return Expression.SquareRoot(build(args[0]))
                    case (name, n):
                        raise errinput(f"unknown function {name} with {n} arguments")
            case ast.Expression.Operator(op):
                match op:
                    case ast.Operator.And(lhs, rhs):
                        return Expression.And(build(lhs), build(rhs))
                    case ast.Operator.Not(inner):
                        return Expression.Not(build(inner))
                    case ast.Operator.Or(lhs, rhs):
                        return Expression.Or(build(lhs), build(rhs))

                    case ast.Operator.Equal(lhs, rhs):
                        return Expression.Equal(build(lhs), build(rhs))
                    case ast.Operator.GreaterThan(lhs, rhs):
                        return Expression.GreaterThan(build(lhs), build(rhs))
                    case ast.Operator.GreaterThanOrEqual(lhs, rhs):
                        return Expression.Or(
                            Expression.GreaterThan(build(lhs), build(rhs)),
                            Expression.Equal(build(lhs), build(rhs)),
                        )
                    case ast.Operator.Is(inner, literal):
                        built = build(inner)
                        match literal:
                            case ast.Literal.Null():
                                return Expression.Is(built, Value.Null())
                            case ast.Literal.Float(f) if math.isnan(f):
                                return Expression.Is(built, Value.Float(f))
                            case value:
                                panic(f"invalid IS value {value!r}")  # enforced by parser
                    case ast.Operator.LessThan(lhs, rhs):
                        return Expression.LessThan(build(lhs), build(rhs))
                    case ast.Operator.LessThanOrEqual(lhs, rhs):
                        return Expression.Or(
                            Expression.LessThan(build(lhs), build(rhs)),
                            Expression.Equal(build(lhs), build(rhs)),
                        )
                    case ast.Operator.Like(lhs, rhs):
                        return Expression.Like(build(lhs), build(rhs))
                    case ast.Operator.NotEqual(lhs, rhs):
                        return Expression.Not(Expression.Equal(build(lhs), build(rhs)))

                    case ast.Operator.Add(lhs, rhs):
                        return Expression.Add(build(lhs), build(rhs))
                    case ast.Operator.Divide(lhs, rhs):
                        return Expression.Divide(build(lhs), build(rhs))
                    case ast.Operator.Exponentiate(lhs, rhs):
                        return Expression.Exponentiate(build(lhs), build(rhs))
                    case ast.Operator.Factorial(inner):
                        return Expression.Factorial(build(inner))
                    case ast.Operator.Identity(inner):
                        return Expression.Identity(build(inner))
                    case ast.Operator.Remainder(lhs, rhs):
                        return Expression.Remainder(build(lhs), build(rhs))
                    case ast.Operator.Multiply(lhs, rhs):
                        return Expression.Multiply(build(lhs), build(rhs))
                    case ast.Operator.Negate(inner):
                        return Expression.Negate(build(inner))
                    case ast.Operator.Subtract(lhs, rhs):
                        return Expression.Subtract(build(lhs), build(rhs))
                    case _:
                        panic(f"unknown operator {op!r}")
            case _:
                panic(f"unknown expression {expr!r}")

    @staticmethod
    def build_constant_value(expr: ast.Expression) -> Value:
        """Builds a constant value from an AST expression by evaluating it.
        The expression can't contain column references or aggregate
        functions."""
        return Planner.build_expression(expr, Scope()).evaluate(None)


@dataclass
class Scope:
    """A scope maps column/table names to input column indexes, for lookups
    during expression construction. It also tracks aggregate and GROUP BY
    expressions, as well as hidden columns (e.g. ORDER BY columns that aren't
    projected in the SELECT clause).

    When building expressions, the scope is used to resolve column names to
    column indexes, which are placed in the plan and used during execution.
    Expression evaluation generally happens in the context of an input row.
    This row may come directly from a single table, or it may be the result of
    a long chain of joins and projections. The scope keeps track of which
    columns are currently visible and what names they have.

    `Scope()` creates a new, empty scope.
    """

    columns: list[Label] = field(default_factory=list)
    """The currently visible columns. If empty, only constant expressions can
    be used (no column references)."""
    tables: set[str] = field(default_factory=set)
    """Index of currently visible tables, by query name (e.g. may be aliased)."""
    qualified: dict[tuple[str, str], int] = field(default_factory=dict)
    """Index of fully qualified table.column names to column indexes. Qualified
    names are always unique within a scope."""
    unqualified: dict[str, list[int]] = field(default_factory=dict)
    """Index of unqualified column names to column indexes. If a name points
    to multiple columns, lookups will fail with an ambiguous name error."""
    aggregates: dict[ast.Expression, int] = field(default_factory=dict)
    """Index of aggregate and GROUP BY expressions to column indexes. This is
    used to track output columns of Aggregate nodes and look them up from
    expressions in downstream SELECT, HAVING, and ORDER BY clauses. If the
    node contains an (inner) Aggregate node, this is never empty."""
    hidden: set[int] = field(default_factory=set)
    """Hidden columns. These are used to pass e.g. ORDER BY and HAVING
    expressions through SELECT projection nodes if the expressions aren't
    already projected. They should be removed before emitting results."""

    @classmethod
    def from_table(cls, table: Table) -> "Scope":
        """Creates a scope from a table, using the table's original name."""
        scope = cls()
        scope.add_table(table, None)
        return scope

    def spawn(self) -> "Scope":
        """Creates a new child scope that inherits from the parent scope."""
        child = Scope()
        child.tables = set(self.tables)  # retain table names
        return child

    def add_table(self, table: Table, alias: str | None) -> None:
        """Adds a table to the scope. The label is either the table's original
        name or an alias, and must be unique. All table columns are added, in
        order."""
        name = alias if alias is not None else table.name
        if name in self.tables:
            raise errinput(f"duplicate table name {name}")
        for column in table.columns:
            self.add_column(Label.Qualified(name, column.name))
        self.tables.add(name)

    def add_column(self, label: Label) -> int:
        """Appends a column with the given label to the scope. Returns the
        column index."""
        index = len(self.columns)
        match label:
            case Label.Qualified(table, column):
                self.qualified[(table, column)] = index
        match label:
            case Label.Qualified(_, name) | Label.Unqualified(name):
                self.unqualified.setdefault(name, []).append(index)
        self.columns.append(label)
        return index

    def lookup_column(self, table: str | None, name: str) -> int:
        """Looks up a column index by name, if possible."""
        fmtname = f"{table}.{name}" if table is not None else name
        if not self.columns:
            raise errinput(f"expression must be constant, found column {fmtname}")
        if table is not None:
            if table not in self.tables:
                raise errinput(f"unknown table {table}")
            if (index := self.qualified.get((table, name))) is not None:
                return index
        elif (indexes := self.unqualified.get(name)) is not None:
            if len(indexes) > 1:
                raise errinput(f"ambiguous column {name}")
            return indexes[0]
        if self.aggregates:
            raise errinput(
                f"column {fmtname} must be used in an aggregate or GROUP BY expression"
            )
        raise errinput(f"unknown column {fmtname}")

    def add_aggregate(self, expr: ast.Expression, parent: "Scope") -> int | None:
        """Adds an aggregate expression to the scope, returning the new column
        index or None if the expression already exists. This is either an
        aggregate function or a GROUP BY expression, used to look up the
        aggregate output column from e.g. SELECT, HAVING, and ORDER BY."""
        if expr in self.aggregates:
            return None
        # If this is a simple column reference (i.e. GROUP BY foo), pass
        # through the column label from the parent scope for lookups.
        label: Label = Label.None_()
        match expr:
            case ast.Expression.Column(table, column):
                # Ignore errors, they will be emitted when building the expression.
                try:
                    label = parent.columns[parent.lookup_column(table, column)]
                except Error:
                    pass
        index = self.add_column(label)
        self.aggregates[expr] = index
        return index

    def lookup_aggregate(self, expr: ast.Expression) -> int | None:
        """Looks up an aggregate column index by aggregate function or GROUP
        BY expression."""
        return self.aggregates.get(expr)

    def add_passthrough(self, parent: "Scope", parent_index: int, hide: bool) -> int:
        """Adds a column that passes through a column from the parent scope,
        retaining its properties. If hide is true, the column is hidden."""
        index = self.add_column(parent.columns[parent_index])
        for expr, i in parent.aggregates.items():
            if i == parent_index:
                self.aggregates.setdefault(expr, index)
        if hide or parent_index in parent.hidden:
            self.hidden.add(index)
        return index

    def merge(self, scope: "Scope") -> None:
        """Merges two scopes, by appending the given scope to self."""
        for table in scope.tables:
            if table in self.tables:
                raise errinput(f"duplicate table name {table}")
            self.tables.add(table)
        offset = len(self.columns)
        for label in scope.columns:
            self.add_column(label)
        for expr, index in scope.aggregates.items():
            self.aggregates.setdefault(expr, index + offset)
        self.hidden.update(index + offset for index in scope.hidden)

    def project(self, expressions: list[tuple[ast.Expression, str | None]]) -> "Scope":
        """Projects the scope via the given expressions and aliases, creating
        a new child scope with one column per expression. These may be a
        simple column reference (e.g. "SELECT a, b FROM table"), which passes
        through the corresponding column from the original scope and retains
        its qualified and unqualified names. Otherwise, for non-trivial column
        references, a new column is created for the expression. Explicit
        aliases may be given."""
        child = self.spawn()
        for expr, alias in expressions:
            # Use the alias if given, or look up any column references.
            label: Label = Label.None_()
            if alias is not None:
                label = Label.Unqualified(alias)
            else:
                match expr:
                    case ast.Expression.Column(table, column):
                        # Ignore errors, they will be surfaced in build_expression().
                        try:
                            label = self.columns[self.lookup_column(table, column)]
                        except Error:
                            pass
            index = child.add_column(label)
            # If this is an aggregate query, then all projected expressions
            # must also be aggregates by definition (an aggregate node can only
            # emit aggregate functions or GROUP BY expressions).
            if self.aggregates:
                child.aggregates.setdefault(expr, index)
        return child

    def remap(self, targets: list[int | None]) -> "Scope":
        """Remaps the scope using the given targets."""
        child = self.spawn()
        for index in invert_remap(targets):
            if index is not None:
                child.add_passthrough(self, index, False)
        return child

    def remove_hidden(self) -> set[int] | None:
        """Removes hidden columns from the scope, returning their indexes or
        None if no columns are hidden."""
        if not self.hidden:
            return None
        hidden, self.hidden = self.hidden, set()
        self.columns = [label for i, label in enumerate(self.columns) if i not in hidden]
        self.qualified = {k: i for k, i in self.qualified.items() if i not in hidden}
        self.unqualified = {
            name: retained
            for name, indexes in self.unqualified.items()
            if (retained := [i for i in indexes if i not in hidden])
        }
        self.aggregates = {e: i for e, i in self.aggregates.items() if i not in hidden}
        return hidden

    def remap_hidden(self) -> list[int | None] | None:
        """Removes hidden columns from the scope and returns the remaining
        column indexes as a Remap targets list, or None if no columns are
        hidden. A Remap targets list maps parent column indexes to child
        column indexes, or None if a column should be dropped."""
        size = len(self.columns)
        hidden = self.remove_hidden()
        if hidden is None:
            return None
        targets: list[int | None] = [None] * size
        index = 0
        for old_index in range(size):
            if old_index not in hidden:
                targets[old_index] = index
                index += 1
        return targets
