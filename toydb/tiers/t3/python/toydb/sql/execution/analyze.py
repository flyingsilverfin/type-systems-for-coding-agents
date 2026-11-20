"""Column statistics: ANALYZE computes and stores them in the catalog, SHOW
STATISTICS shows them. Both run in the caller's transaction, so statistics
are transactional like table schemas and rows."""

from toydb.error import Error
from toydb.sql.engine import Transaction
from toydb.sql.types import Label, Row, Value
from toydb.sql.types.statistics import TableStatistics

COLUMNS: list[Label] = [
    Label.Unqualified(name)
    for name in (
        "column",
        "rows",
        "nulls",
        "distinct",
        "min",
        "max",
        "top",
        "top_count",
        "avg_length",
    )
]
"""The SHOW STATISTICS result columns."""


def analyze(txn: Transaction, table: str | None) -> list[str]:
    """ANALYZE: computes and stores statistics for the given table, or all
    tables in name order if None, from the rows the transaction sees.
    Returns the names of the analyzed tables."""
    if txn.state().read_only:
        raise Error.ReadOnly()
    if table is not None:
        tables = [txn.must_get_table(table)]
    else:
        tables = sorted(txn.list_tables(), key=lambda schema: schema.name)
    for schema in tables:
        statistics = TableStatistics.compute(schema, txn.scan(schema.name, None))
        txn.set_statistics(schema.name, statistics)
    return [schema.name for schema in tables]


def show_statistics(txn: Transaction, table: str) -> list[Row]:
    """SHOW STATISTICS: returns one row per table column with its statistics
    (see COLUMNS). Columns without statistics have NULL for all of them."""
    schema = txn.must_get_table(table)
    statistics = txn.get_statistics(schema.name)

    def optional(value: Value | None) -> Value:
        return value if value is not None else Value.Null()

    rows: list[Row] = []
    for column in schema.columns:
        name = Value.String(column.name)
        stats = statistics.columns.get(column.name) if statistics is not None else None
        if statistics is None or stats is None:
            rows.append([name] + [Value.Null()] * (len(COLUMNS) - 1))
            continue
        top: Value = Value.Null()
        top_count: Value = Value.Null()
        if stats.top is not None:
            top, top_count = stats.top[0], Value.Integer(stats.top[1])
        rows.append(
            [
                name,
                Value.Integer(statistics.rows),
                Value.Integer(stats.nulls),
                Value.Integer(stats.distinct),
                optional(stats.min),
                optional(stats.max),
                top,
                top_count,
                optional(Value.Float(stats.avg_length) if stats.avg_length is not None else None),
            ]
        )
    return rows
