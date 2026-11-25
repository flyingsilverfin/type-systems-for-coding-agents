"""Plan optimizers.

Mirrors `src/sql/planner/optimizer.rs`.
"""

from abc import ABC, abstractmethod

from toydb.sql.planner.plan import Node
from toydb.sql.types import Expression, Label, Value


class Optimizer(ABC):
    """A node optimizer, which recursively transforms a plan node to make plan
    execution more efficient where possible."""

    @abstractmethod
    def optimize(self, node: Node) -> Node:
        """Optimizes a node, returning the optimized node."""

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the optimizer like the Rust `#[derive(Debug)]`."""
        return type(self).__name__


class ConstantFolding(Optimizer):
    """Folds constant expressions by pre-evaluating them once now, instead of
    re-evaluating them for every row during execution."""

    def optimize(self, node: Node) -> Node:
        # Recursively transform expressions in the node tree. Post-order to
        # partially fold child expressions as far as possible, and avoid
        # quadratic costs.
        return node.transform(
            lambda node: node.transform_expressions(lambda expr: expr, self.fold),
            lambda node: node,
        )

    @staticmethod
    def fold(expr: Expression) -> Expression:
        """Folds constant expressions in a node."""
        Constant, Boolean = Expression.Constant, Value.Boolean

        # If the expression is constant, evaluate it.
        #
        # This is a very simple approach, which doesn't handle more complex
        # cases such as 1 + a - 2 (which would require rearranging the
        # expression as 1 - 2 + a to evaluate the 1 - 2 branch).
        #
        # TODO: consider doing something better.
        if not expr.contains(lambda expr: isinstance(expr, Expression.Column)):
            return Constant(expr.evaluate(None))

        # If the expression is a logical operator, and one of the sides is
        # constant, we may be able to evaluate it even if it has a column
        # reference. For example, a AND FALSE is always FALSE, regardless of
        # what a is.
        match expr:
            case Expression.And(lhs, rhs):
                match (lhs, rhs):
                    # If either side of an AND is false, the AND is false.
                    case (Constant(Boolean(False)), _) | (_, Constant(Boolean(False))):
                        return Constant(Boolean(False))
                    # If either side of an AND is true, the AND is redundant.
                    case (Constant(Boolean(True)), other) | (other, Constant(Boolean(True))):
                        return other
                    case _:
                        return expr

            case Expression.Or(lhs, rhs):
                match (lhs, rhs):
                    # If either side of an OR is true, the OR is true.
                    case (Constant(Boolean(True)), _) | (_, Constant(Boolean(True))):
                        return Constant(Boolean(True))
                    # If either side of an OR is false, the OR is redundant.
                    case (Constant(Boolean(False)), other) | (other, Constant(Boolean(False))):
                        return other
                    case _:
                        return expr

            case _:
                return expr


class FilterPushdown(Optimizer):
    """Pushes filter predicates down into child nodes where possible. In
    particular, this can perform filtering during storage scans (below Raft),
    instead of reading and transmitting all rows across the network before
    filtering, by pushing a predicate from a Filter node down into a Scan
    node."""

    def optimize(self, node: Node) -> Node:
        # Push down before descending, so we can keep recursively pushing down.
        return node.transform(self.push_filters, lambda node: node)

    @staticmethod
    def push_filters(node: Node) -> Node:
        """Pushes filter predicates down into child nodes where possible."""
        node = FilterPushdown.maybe_push_filter(node)
        node = FilterPushdown.maybe_push_join(node)
        return node

    @staticmethod
    def push_into(expr: Expression, target: Node) -> tuple[Node, Expression | None]:
        """Pushes an expression into a node if possible. Returns the target
        node with the expression pushed into it (Rust mutates the target in
        place, but nodes are immutable here), and the unpushed expression if
        it couldn't be pushed."""
        match target:
            case Node.Filter(source, predicate):
                return Node.Filter(source, Expression.And(expr, predicate)), None
            case Node.NestedLoopJoin(left, right, predicate, outer):
                if predicate is not None:
                    expr = Expression.And(expr, predicate)
                return Node.NestedLoopJoin(left, right, expr, outer), None
            case Node.Scan(table, filter, alias):
                if filter is not None:
                    expr = Expression.And(expr, filter)
                return Node.Scan(table, expr, alias), None
            # Unable to push down, just return the original expression.
            case _:
                return target, expr

    @staticmethod
    def maybe_push_filter(node: Node) -> Node:
        """Pushes a filter node predicate down into its source, if possible."""
        if not isinstance(node, Node.Filter):
            return node
        # Attempt to push the filter into the source, or return the original.
        source, predicate = FilterPushdown.push_into(node.predicate, node.source)
        if predicate is not None:
            return Node.Filter(source, predicate)
        # Push succeded, return the source that was pushed into. When we
        # replace this filter node with the source node, Node.transform() will
        # skip the source node since it now takes the place of the original
        # filter node. Transform the source manually.
        return FilterPushdown.push_filters(source)

    @staticmethod
    def maybe_push_join(node: Node) -> Node:
        """Pushes down parts of a join predicate into the left or right
        sources where possible."""
        if not isinstance(node, Node.NestedLoopJoin) or node.predicate is None:
            return node
        left, right, outer = node.left, node.right, node.outer
        left_columns = left.columns()

        # Convert the predicate into conjunctive normal form (an AND list).
        cnf = node.predicate.into_cnf_vec()

        # Push down expressions that don't reference both sources. Constant
        # expressions can be pushed down into both.
        push_left: list[Expression] = []
        push_right: list[Expression] = []
        predicate: list[Expression] = []
        for expr in cnf:
            ref_left, ref_right = False, False

            def visit(expr: Expression) -> bool:
                nonlocal ref_left, ref_right
                match expr:
                    case Expression.Column(index):
                        ref_left = ref_left or index < left_columns
                        ref_right = ref_right or index >= left_columns
                return not (ref_left and ref_right)  # exit once both are referenced

            expr.walk(visit)
            match (ref_left, ref_right):
                case (True, True):
                    predicate.append(expr)
                case (True, False):
                    push_left.append(expr)
                case (False, True):
                    push_right.append(expr)
                case _:
                    push_left.append(expr)
                    push_right.append(expr)

        # In the remaining cross-source expressions, look for equijoins where
        # one side also has constant value lookups. In this case we can copy
        # the constant lookups to the other side, to allow index lookups. This
        # commonly happens when joining a foreign key (which is indexed) on a
        # primary key, and we want to make use of the foreign key index, e.g.:
        #
        # SELECT m.name, g.name FROM movies m JOIN genres g ON m.genre_id = g.id AND g.id = 7;
        left_lookups: dict[int, int] = {  # column → push_left index
            column: i
            for i, expr in enumerate(push_left)
            if (column := expr.is_column_lookup()) is not None
        }
        right_lookups: dict[int, int] = {  # column → push_right index
            column: i
            for i, expr in enumerate(push_right)
            if (column := expr.is_column_lookup()) is not None
        }

        for expr in predicate:
            # Find equijoins.
            match expr:
                case Expression.Equal(Expression.Column(l), Expression.Column(r)):
                    pass
                case _:
                    continue

            # The lhs may be a reference to the right source; swap them.
            if l > r:
                l, r = r, l

            # Check if either side is a column lookup, and copy it over.
            if (i := left_lookups.get(l)) is not None:
                push_right.append(push_left[i].replace_column(l, r))
            if (i := right_lookups.get(r)) is not None:
                push_left.append(push_right[i].replace_column(r, l))

        # Push predicates down into the sources if possible.
        if (expr := Expression.and_vec(push_left)) is not None:
            left, unpushed = FilterPushdown.push_into(expr, left)
            if unpushed is not None:
                # Pushdown failed, put it back into the join predicate.
                predicate.append(unpushed)

        if (expr := Expression.and_vec(push_right)) is not None:
            # Right columns have indexes in the joined row; shift them left.
            expr = expr.shift_column(-left_columns)
            right, unpushed = FilterPushdown.push_into(expr, right)
            if unpushed is not None:
                # Pushdown failed, undo the column index shift.
                predicate.append(unpushed.shift_column(left_columns))

        # Leave any remaining predicates in the join node.
        return Node.NestedLoopJoin(left, right, Expression.and_vec(predicate), outer)


class IndexLookup(Optimizer):
    """Uses a primary key or secondary index lookup where possible."""

    def optimize(self, node: Node) -> Node:
        # Recursively transform expressions in the node tree. Post-order to
        # partially fold child expressions as far as possible, and avoid
        # quadratic costs.
        return node.transform(self.index_lookup, lambda node: node)

    @staticmethod
    def index_lookup(node: Node) -> Node:
        """Rewrites a filtered scan node into a key or index lookup if
        possible."""
        # Only handle scan filters. Assume FilterPushdown has pushed filters
        # into scan nodes first.
        if not isinstance(node, Node.Scan) or node.filter is None:
            return node
        table, alias = node.table, node.alias

        # Convert the filter into conjunctive normal form (a list of ANDs).
        cnf = node.filter.into_cnf_vec()

        # Find the first expression that's either a primary key or secondary
        # index lookup. We could be more clever here, but this is fine.
        for i, expr in enumerate(cnf):
            column = expr.is_column_lookup()
            if column is not None and (column == table.primary_key or table.columns[column].index):
                break
        else:
            # No index lookups found, return the original node.
            return node

        # Extract the lookup values and expression from the cnf list.
        values = cnf.pop(i).into_column_values(column)

        # Build the primary key or secondary index lookup node.
        if column == table.primary_key:
            node = Node.KeyLookup(table=table, keys=values, alias=alias)
        else:
            node = Node.IndexLookup(table=table, column=column, values=values, alias=alias)

        # If there's any remaining CNF expressions, add a filter node for them.
        if (predicate := Expression.and_vec(cnf)) is not None:
            node = Node.Filter(source=node, predicate=predicate)

        return node


class HashJoin(Optimizer):
    """Uses a hash join instead of a nested loop join for single-column
    equijoins."""

    def optimize(self, node: Node) -> Node:
        return node.transform(self.hash_join, lambda node: node)

    @staticmethod
    def hash_join(node: Node) -> Node:
        """Rewrites a nested loop join into a hash join if possible."""
        match node:
            # If this is a single-column equijoin, use a hash join.
            case Node.NestedLoopJoin(
                left,
                right,
                Expression.Equal(Expression.Column(left_column), Expression.Column(right_column)),
                outer,
            ):
                # The LHS column may be a column in the right table; swap them.
                if right_column < left_column:
                    left_column, right_column = right_column, left_column
                # The NestedLoopJoin predicate uses column indexes in the
                # joined row, while the HashJoin uses column indexes in each
                # individual table. Adjust the RHS column reference.
                right_column -= left.columns()
                return Node.HashJoin(left, left_column, right, right_column, outer)
            # Otherwise, retain the nested loop join.
            case _:
                return node


class ShortCircuit(Optimizer):
    """Short-circuits useless nodes and expressions (for example a Filter node
    that always evaluates to false), by removing them and/or replacing them
    with Nothing nodes that yield no rows."""

    def optimize(self, node: Node) -> Node:
        # Post-order transform, to pull Nothing nodes upwards in the tree.
        return node.transform(lambda node: node, self.short_circuit)

    @staticmethod
    def short_circuit(node: Node) -> Node:
        """Short-circuits useless nodes. Assumes the node has already been
        optimized by ConstantFolding."""
        Constant, Boolean, Null = Expression.Constant, Value.Boolean, Value.Null

        match node:
            # Filter nodes that always yield true are unnecessary: remove them.
            case Node.Filter(source, Constant(Boolean(True))):
                node = source

            # Predicates that always yield true are unnecessary: remove them.
            case Node.Scan(table, Constant(Boolean(True)), alias):
                node = Node.Scan(table=table, filter=None, alias=alias)
            case Node.NestedLoopJoin(left, right, Constant(Boolean(True)), outer):
                node = Node.NestedLoopJoin(left=left, right=right, predicate=None, outer=outer)

            # Remove noop projections that simply pass through the source columns.
            case Node.Projection(source, expressions, aliases) if (
                source.columns() == len(expressions)
                and all(alias == Label.None_() for alias in aliases)
                and all(expr == Expression.Column(i) for i, expr in enumerate(expressions))
            ):
                node = source

            case _:
                pass

        # Short-circuit nodes that don't produce anything by replacing them
        # with a Nothing node.
        match node:
            case Node.Filter(predicate=Constant(Boolean(False) | Null())):
                is_empty = True
            case Node.IndexLookup(values=values) if not values:
                is_empty = True
            case Node.KeyLookup(keys=keys) if not keys:
                is_empty = True
            case Node.Limit(limit=0):
                is_empty = True
            case Node.NestedLoopJoin(predicate=Constant(Boolean(False) | Null())):
                is_empty = True
            case Node.Scan(filter=Constant(Boolean(False) | Null())):
                is_empty = True
            case Node.Values(rows) if not rows:
                is_empty = True

            # Nodes that pull from a Nothing node can't produce anything.
            #
            # NB: does not short-circuit aggregation, since an aggregation over 0
            # rows should produce a result.
            case (
                Node.Filter(source=Node.Nothing())
                | Node.HashJoin(left=Node.Nothing())
                | Node.HashJoin(right=Node.Nothing())
                | Node.NestedLoopJoin(left=Node.Nothing())
                | Node.NestedLoopJoin(right=Node.Nothing())
                | Node.Offset(source=Node.Nothing())
                | Node.Order(source=Node.Nothing())
                | Node.Projection(source=Node.Nothing())
            ):
                is_empty = True

            case _:
                is_empty = False

        if is_empty:
            return Node.Nothing([node.column_label(i) for i in range(node.columns())])

        return node


OPTIMIZERS: list[Optimizer] = [
    ConstantFolding(),
    FilterPushdown(),
    IndexLookup(),
    HashJoin(),
    ShortCircuit(),
]
"""The set of optimizers, and the order in which they are applied."""
