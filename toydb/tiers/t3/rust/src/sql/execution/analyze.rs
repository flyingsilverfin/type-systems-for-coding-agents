//! ANALYZE and SHOW STATISTICS: computes and shows column statistics, which
//! are stored in the catalog (see `sql::types::TableStatistics`).

use super::StatementResult;
use crate::error::{Error, Result};
use crate::sql::engine::Transaction;
use crate::sql::types::{ColumnStatistics, Label, Row, TableStatistics, Value};

/// ANALYZE: computes and stores statistics for the given table, or all tables
/// if None, from the rows visible to the transaction. Returns the names of the
/// analyzed tables, in order.
pub fn analyze(txn: &impl Transaction, table: Option<&str>) -> Result<Vec<String>> {
    if txn.state().read_only {
        return Err(Error::ReadOnly);
    }
    // Tables are listed in name order.
    let tables = match table {
        Some(table) => vec![txn.must_get_table(table)?],
        None => txn.list_tables()?,
    };
    let mut names = Vec::with_capacity(tables.len());
    for table in tables {
        let rows = txn.scan(&table.name, None)?;
        let statistics = TableStatistics::compute(&table, rows)?;
        txn.set_table_statistics(&table.name, statistics)?;
        names.push(table.name);
    }
    Ok(names)
}

/// SHOW STATISTICS: returns a table's column statistics as a SELECT result,
/// with one row per column in table order. Columns without statistics have
/// NULL statistics.
pub fn show_statistics(txn: &impl Transaction, table: &str) -> Result<StatementResult> {
    let table = txn.must_get_table(table)?;
    let statistics = txn.get_table_statistics(&table.name)?;

    let columns = [
        "column",
        "rows",
        "nulls",
        "distinct",
        "min",
        "max",
        "top",
        "top_count",
        "avg_length",
    ]
    .into_iter()
    .map(|name| Label::Unqualified(name.to_string()))
    .collect();

    let rows = table
        .columns
        .iter()
        .map(|column| {
            let name = Value::String(column.name.clone());
            let column_statistics = statistics
                .as_ref()
                .and_then(|s| s.columns.get(&column.name).map(|c| (s.rows, c)));
            match column_statistics {
                Some((rows, statistics)) => statistics_row(name, rows, statistics),
                None => [vec![name], vec![Value::Null; 8]].concat(),
            }
        })
        .collect();

    Ok(StatementResult::Select { columns, rows })
}

/// Builds a SHOW STATISTICS row for a column.
fn statistics_row(name: Value, rows: u64, statistics: &ColumnStatistics) -> Row {
    let ColumnStatistics { nulls, distinct, min, max, top, avg_length } = statistics;
    let (top, top_count) = match top {
        Some((value, count)) => (value.clone(), Value::Integer(*count as i64)),
        None => (Value::Null, Value::Null),
    };
    vec![
        name,
        Value::Integer(rows as i64),
        Value::Integer(*nulls as i64),
        Value::Integer(*distinct as i64),
        min.clone().unwrap_or(Value::Null),
        max.clone().unwrap_or(Value::Null),
        top,
        top_count,
        avg_length.map_or(Value::Null, Value::Float),
    ]
}
