"""Joins rows.

Mirrors `src/sql/execution/join.rs`.
"""

from toydb.error import Error, errinput
from toydb.iter import CloneIterator, Empty, IntoIter, Peekable
from toydb.sql.types import Expression, Row, Rows, Value


class NestedLoopJoiner(CloneIterator[Row]):
    """NestedLoopJoiner implements nested loop joins.

    For every row in the left source, iterate over the right source and join
    them. Rows are filtered on the join predicate, if given.

    If outer is true, and there are no matches in the right source for a row
    in the left source, a joined row with NULL values for the right source is
    returned (typically used for a LEFT JOIN).
    """

    def __init__(
        self,
        left: Rows,
        right: Rows,
        right_columns: int,
        predicate: Expression | None,
        outer: bool,
    ) -> None:
        """Creates a new nested loop joiner."""
        self.left: Peekable[Row] = Peekable(left)
        """The left source."""
        self.right = right
        """The right source."""
        self.right_original = right.clone()
        """The original right iterator state. Can be cloned to reset the
        right source to its original state."""
        self.right_columns = right_columns
        """The number of columns in the right source."""
        self.right_matched = False
        """True if a right match has been seen for the current left row."""
        self.predicate = predicate
        """The join predicate."""
        self.outer = outer
        """If true, emit a row when there is no match in the right source."""

    def clone(self) -> "NestedLoopJoiner":
        clone = NestedLoopJoiner(
            self.left.iter, self.right.clone(), self.right_columns, self.predicate, self.outer
        )
        clone.left = self.left.clone()
        clone.right_original = self.right_original.clone()
        clone.right_matched = self.right_matched
        return clone

    def try_next(self) -> Row | None:
        """Returns the next joined row, if any."""
        # While there is a valid left row, look for a right-hand match to return.
        while (left := self.peek_left()) is not None:
            # If there is a match in the remaining right rows, return it.
            for right in self.right:
                row = [*left, *right]
                if self.predicate is not None:
                    match self.predicate.evaluate(row):
                        case Value.Boolean(True):
                            pass
                        case Value.Boolean(False) | Value.Null():
                            continue
                        case v:
                            raise errinput(f"join predicate returned {v}, expected boolean")
                self.right_matched = True
                return row

            # If there was no right match for the left row, and this is an
            # outer join, emit a row with right NULLs.
            if not self.right_matched and self.outer:
                self.right_matched = True
                return [*left, *(Value.Null() for _ in range(self.right_columns))]

            # We reached the end of the right source. Reset it and move onto
            # the next left row.
            self.right = self.right_original.clone()
            self.right_matched = False
            next(self.left)

        # Otherwise, there's either a None or Err in left. Return it.
        return next(self.left, None)

    def peek_left(self) -> Row | None:
        """Peeks the next valid left row. Returns None if the left source is
        exhausted or its next row is an error."""
        try:
            return self.left.peek()
        except Error:
            return None

    def __next__(self) -> Row:
        if (row := self.try_next()) is None:
            raise StopIteration
        return row


class HashJoiner(CloneIterator[Row]):
    """HashJoiner implements hash joins.

    This builds a hash table of rows from the right source keyed on the join
    value, then iterates over the left source and looks up matching rows in
    the hash table.

    If outer is true, and there is no match in the right source for a row in
    the left source, a row with NULL values for the right source is emitted
    instead.
    """

    def __init__(
        self,
        left: Rows,
        left_column: int,
        right: Rows,
        right_column: int,
        right_columns: int,
        outer: bool,
    ) -> None:
        """Creates a new hash joiner."""
        # Build a hash map from the right source.
        right_map: dict[Value, list[Row]] = {}
        for row in right:
            value = row[right_column]
            if value.is_undefined():
                continue  # undefined will never match anything
            right_map.setdefault(value, []).append(row)

        self.left = left
        """The left source."""
        self.left_column = left_column
        """The left column to join on."""
        self.right = right_map
        """The right hash map to join on."""
        self.right_columns = right_columns
        """The number of columns in the right source."""
        self.outer = outer
        """If true, emit a row when there is no match in the right source."""
        self.pending: Rows = Empty()
        """Any pending matches to emit."""

    def clone(self) -> "HashJoiner":
        clone = HashJoiner(
            self.left.clone(), self.left_column, Empty(), 0, self.right_columns, self.outer
        )
        clone.right = self.right
        clone.pending = self.pending.clone()
        return clone

    def try_next(self) -> Row | None:
        """Returns the next joined row, if any."""
        # If there's a pending row stashed from a previous call, return it.
        if (row := next(self.pending, None)) is not None:
            return row

        # Find the next left row to join with.
        for left in self.left:
            if (right := self.right.get(left[self.left_column])) is not None:
                # Join with all right matches and stash them in pending.
                self.pending = IntoIter([[*left, *r] for r in right])
                return next(self.pending, None)
            elif self.outer:
                # If there is no match for the left row, but it's an outer
                # join, emit a row with right NULLs.
                return [*left, *(Value.Null() for _ in range(self.right_columns))]

        return None

    def __next__(self) -> Row:
        if (row := self.try_next()) is None:
            raise StopIteration
        return row
