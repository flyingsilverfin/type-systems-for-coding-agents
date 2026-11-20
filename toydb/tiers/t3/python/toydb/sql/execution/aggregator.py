"""Computes aggregates.

Mirrors `src/sql/execution/aggregator.rs`.
"""

from abc import ABC
from dataclasses import dataclass
from typing import ClassVar

from toydb.error import panic
from toydb.iter import IntoIter, Map
from toydb.sql.planner import Aggregate
from toydb.sql.types import Expression, Row, Rows, Value
from toydb.storage.btree import BTreeMap


@dataclass(frozen=True)
class Bucket:
    """A GROUP BY bucket, i.e. the values of the GROUP BY expressions. Buckets
    are ordered lexicographically by `Value.cmp`, like a Rust `Vec<Value>`."""

    values: tuple[Value, ...]

    def __lt__(self, other: "Bucket") -> bool:
        for a, b in zip(self.values, other.values):
            if (ordering := a.cmp(b)) != 0:
                return ordering < 0
        return len(self.values) < len(other.values)


class Aggregator:
    """Computes bucketed aggregates for input rows. For example, this query
    would compute COUNT and SUM aggregates bucketed by category and brand:

    SELECT COUNT(*), SUM(price) FROM products GROUP BY category, brand
    """

    def __init__(self, group_by: list[Expression], aggregates: list[Aggregate]) -> None:
        """Creates a new aggregator for the given GROUP BY buckets and
        aggregates."""
        self.group_by = group_by
        """GROUP BY expressions."""
        self.aggregates = aggregates
        """Aggregates to compute."""
        self.buckets: BTreeMap[Bucket, list[Accumulator]] = BTreeMap()
        """Accumulators indexed by group_by bucket."""

    def add(self, row: Row) -> None:
        """Adds a row to the aggregator."""
        # Compute the bucket values.
        bucket = Bucket(tuple(expr.evaluate(row) for expr in self.group_by))

        # Look up the bucket accumulators, or create a new bucket.
        accumulators = self.buckets.get(bucket)
        if accumulators is None:
            accumulators = [Accumulator.new(aggregate) for aggregate in self.aggregates]
            self.buckets.insert(bucket, accumulators)

        # Accumulate the evaluated values.
        for i, aggregate in enumerate(self.aggregates):
            accumulators[i] = accumulators[i].add(aggregate.expr().evaluate(row))

    def add_rows(self, rows: Rows) -> None:
        """Adds rows to the aggregator."""
        for row in rows:
            self.add(row)

    def into_rows(self) -> Rows:
        """Returns a row iterator over the aggregate result."""
        # If there were no rows and no group_by expressions, return a row of
        # empty accumulators (e.g. SELECT COUNT(*) FROM t WHERE FALSE).
        if self.buckets.is_empty() and not self.group_by:
            result = [Accumulator.new(aggregate).value() for aggregate in self.aggregates]
            return IntoIter([result])

        # Emit the group_by and aggregate values for each bucket.
        def emit(item: tuple[Bucket, list[Accumulator]]) -> Row:
            bucket, accumulators = item
            return [*bucket.values, *(acc.value() for acc in accumulators)]

        return Map(IntoIter(list(self.buckets)), emit)


class Accumulator(ABC):
    """Accumulates aggregate values. Accumulators are immutable: `add` returns
    the updated accumulator."""

    Average: ClassVar[type["Average"]]
    Count: ClassVar[type["Count"]]
    Max: ClassVar[type["Max"]]
    Min: ClassVar[type["Min"]]
    Sum: ClassVar[type["Sum"]]

    @staticmethod
    def new(aggregate: Aggregate) -> "Accumulator":
        """Creates a new accumulator from an aggregate kind."""
        match aggregate:
            case Aggregate.Average():
                return Average(count=0, sum=Value.Integer(0))
            case Aggregate.Count():
                return Count(0)
            case Aggregate.Max():
                return Max(None)
            case Aggregate.Min():
                return Min(None)
            case Aggregate.Sum():
                return Sum(None)
            case _:
                panic(f"unknown aggregate {aggregate!r}")

    def add(self, value: Value) -> "Accumulator":
        """Adds a value to the accumulator."""
        # Aggregates ignore NULL values.
        if value == Value.Null():
            return self
        match self:
            case Average(count, sum):
                return Average(count + 1, sum.checked_add(value))
            case Count(count):
                return Count(count + 1)
            case Max(None):
                return Max(value)
            case Max(current) if current is not None and value > current:
                return Max(value)
            case Max():
                return self
            case Min(None):
                return Min(value)
            case Min(current) if current is not None and value < current:
                return Min(value)
            case Min():
                return self
            case Sum(None):
                return Sum(Value.Integer(0).checked_add(value))
            case Sum(sum):
                return Sum(sum.checked_add(value))
            case _:
                panic(f"unknown accumulator {self!r}")

    def value(self) -> Value:
        """Returns the aggregate value."""
        match self:
            case Average(count=0):
                return Value.Null()
            case Average(count, sum):
                return sum.checked_div(Value.Integer(count))
            case Count(count):
                return Value.Integer(count)
            case Max(None) | Min(None) | Sum(None):
                return Value.Null()
            case Max(value) | Min(value) | Sum(value):
                return value
            case _:
                panic(f"unknown accumulator {self!r}")


@dataclass(frozen=True)
class Average(Accumulator):
    count: int
    sum: Value


@dataclass(frozen=True)
class Count(Accumulator):
    count: int


@dataclass(frozen=True)
class Max(Accumulator):
    value_: Value | None


@dataclass(frozen=True)
class Min(Accumulator):
    value_: Value | None


@dataclass(frozen=True)
class Sum(Accumulator):
    value_: Value | None


Accumulator.Average = Average
Accumulator.Count = Count
Accumulator.Max = Max
Accumulator.Min = Min
Accumulator.Sum = Sum
