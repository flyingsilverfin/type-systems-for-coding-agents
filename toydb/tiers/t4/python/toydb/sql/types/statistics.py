"""Column statistics, computed by ANALYZE and stored in the catalog. They
describe a table as it was when it was analyzed, and are not updated by later
writes.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.fmt import debug_f64
from toydb.sql.types.schema import Table
from toydb.sql.types.value import DataType, Row, Value


@dataclass
class ColumnStatistics:
    """Statistics for a single column."""

    nulls: int
    """The number of NULL values."""
    distinct: int
    """The number of distinct non-NULL values, as grouped by GROUP BY (i.e.
    using Value equality, where NaN equals NaN)."""
    min: Value | None
    """The smallest non-NULL value, in Value order (as used by ORDER BY), or
    None if there are no non-NULL values."""
    max: Value | None
    """The largest non-NULL value, in Value order, or None if there are no
    non-NULL values."""
    top: tuple[Value, int] | None
    """The most frequent non-NULL value and its number of rows, or None if
    there are no non-NULL values. Ties go to the smallest value."""
    avg_length: float | None
    """The average length in characters of the non-NULL values of a STRING
    column. None for other column types, or without non-NULL values."""

    @classmethod
    def compute(cls, datatype: DataType, values: Iterable[Value]) -> "ColumnStatistics":
        """Computes the statistics of a column's values."""
        # Sort the non-NULL values and count runs of equal values, like
        # ORDER BY sorts and GROUP BY groups them (by Value.cmp).
        nulls = 0
        ordered: list[Value] = []
        for value in values:
            if isinstance(value, Value.Null):
                nulls += 1
            else:
                ordered.append(value)
        if not ordered:
            return cls(nulls, 0, None, None, None, None)
        ordered.sort()
        groups: list[tuple[Value, int]] = []  # (value, count) in sort order
        for value in ordered:
            if groups and groups[-1][0].cmp(value) == 0:
                groups[-1] = (groups[-1][0], groups[-1][1] + 1)
            else:
                groups.append((value, 1))

        # max() returns the first maximum, i.e. the smallest of equally
        # frequent values.
        top = max(groups, key=lambda group: group[1])
        avg_length = None
        if datatype == DataType.String:
            length = sum(len(value.try_into_string()) for value in ordered)
            avg_length = length / len(ordered)
        return cls(
            nulls=nulls,
            distinct=len(groups),
            min=ordered[0],
            max=ordered[-1],
            top=top,
            avg_length=avg_length,
        )

    def __str__(self) -> str:
        def display(value: Value | None) -> str:
            return str(value) if value is not None else "None"

        output = f"nulls={self.nulls} distinct={self.distinct}"
        output += f" min={display(self.min)} max={display(self.max)}"
        if self.top is not None:
            value, count = self.top
            output += f" top={value}×{count}"
        else:
            output += " top=None"
        if self.avg_length is not None:
            output += f" avg_length={debug_f64(self.avg_length)}"
        return output

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u64(self.nulls)
        serializer.serialize_u64(self.distinct)
        serde.Option(Value).serialize(self.min, serializer)
        serde.Option(Value).serialize(self.max, serializer)
        _TOP.serialize(self.top, serializer)
        serde.Option(serde.F64).serialize(self.avg_length, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "ColumnStatistics":
        deserializer.deserialize_struct()
        return cls(
            nulls=deserializer.deserialize_u64(),
            distinct=deserializer.deserialize_u64(),
            min=serde.Option(Value).deserialize(deserializer),
            max=serde.Option(Value).deserialize(deserializer),
            top=_TOP.deserialize(deserializer),
            avg_length=serde.Option(serde.F64).deserialize(deserializer),
        )


@dataclass
class TableStatistics(encoding.Value):
    """The column statistics of a table, as computed by ANALYZE."""

    rows: int
    """The number of table rows."""
    columns: dict[str, ColumnStatistics]
    """Statistics for each column, keyed by column name. Columns without
    statistics are missing."""

    @classmethod
    def compute(cls, table: Table, rows: Iterable[Row]) -> "TableStatistics":
        """Computes statistics for the given table rows."""
        rows = list(rows)
        columns = {
            column.name: ColumnStatistics.compute(column.datatype, (row[i] for row in rows))
            for i, column in enumerate(table.columns)
        }
        return cls(rows=len(rows), columns=columns)

    def __str__(self) -> str:
        """Formats the statistics on a single line, e.g. for storage dumps,
        with the columns in name order."""
        output = f"rows={self.rows}"
        for name, stats in sorted(self.columns.items()):
            output += f" {name}({stats})"
        return output

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_u64(self.rows)
        _COLUMNS.serialize(self.columns, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "TableStatistics":
        deserializer.deserialize_struct()
        return cls(rows=deserializer.deserialize_u64(), columns=_COLUMNS.deserialize(deserializer))


_TOP: serde.Serde[tuple[Value, int] | None] = serde.Option(serde.Tuple(Value, serde.U64))
"""The encoding of the top value and its count, an `Option<(Value, u64)>`."""

_COLUMNS: serde.Serde[dict[str, ColumnStatistics]] = serde.BTreeMap(serde.String, ColumnStatistics)
"""The encoding of the column statistics, a `BTreeMap<String, ColumnStatistics>`."""
