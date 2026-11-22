//! EXPORT: exports a table's rows, or the whole database, as text lines in
//! CSV, JSON (one object per row) or SQL (CREATE TABLE and INSERT statements).
//!
//! Exports read what the given transaction sees, in primary key order, with
//! columns in table order.

use std::collections::HashSet;

use itertools::Itertools as _;

use crate::encoding::csv;
use crate::encoding::json::Json;
use crate::errinput;
use crate::error::Result;
use crate::sql::engine::Transaction;
use crate::sql::json;
use crate::sql::parser::ast::ExportFormat;
use crate::sql::parser::format_ident;
use crate::sql::types::{Row, Table, Value};

/// The result of an export.
pub struct Export {
    /// The exported lines.
    pub lines: Vec<String>,
    /// The number of exported table rows.
    pub rows: u64,
}

/// Exports the given table, or all tables if None (only as SQL).
pub fn export(txn: &impl Transaction, table: Option<&str>, format: ExportFormat) -> Result<Export> {
    let Some(table) = table else {
        return match format {
            ExportFormat::Sql => export_database(txn),
            ExportFormat::Csv | ExportFormat::Json => {
                errinput!("EXPORT without a table requires AS SQL")
            }
        };
    };
    let table = txn.must_get_table(table)?;
    let rows: Vec<Row> = txn.scan(&table.name, None)?.try_collect()?;
    let lines = match format {
        ExportFormat::Csv => csv_lines(&table, &rows),
        ExportFormat::Json => json_lines(&table, &rows),
        ExportFormat::Sql => sql_lines(&table, &rows),
    };
    Ok(Export { lines, rows: rows.len() as u64 })
}

/// Exports every table as SQL, ordered such that tables come after the tables
/// they reference (other than themselves), and otherwise by name. A cycle of
/// references is broken by taking the first remaining table by name. The
/// views follow the tables, each after the views it depends on, and otherwise
/// by name.
fn export_database(txn: &impl Transaction) -> Result<Export> {
    let mut remaining = txn.list_tables()?; // in name order
    let mut exported = HashSet::new();
    let mut export = Export { lines: Vec::new(), rows: 0 };
    while !remaining.is_empty() {
        let next = remaining
            .iter()
            .position(|table| {
                table.columns.iter().filter_map(|c| c.references.as_ref()).all(|reference| {
                    reference == &table.name || exported.contains(reference.as_str())
                })
            })
            .unwrap_or(0);
        let table = remaining.remove(next);
        let rows: Vec<Row> = txn.scan(&table.name, None)?.try_collect()?;
        export.lines.extend(sql_lines(&table, &rows));
        export.rows += rows.len() as u64;
        exported.insert(table.name);
    }

    let mut remaining = txn.list_views()?; // in name order
    while !remaining.is_empty() {
        let next = remaining
            .iter()
            .position(|view| {
                view.dependencies.iter().all(|dependency| exported.contains(dependency.as_str()))
            })
            .expect("views form a cycle");
        let view = remaining.remove(next);
        export.lines.push(format!("{view};"));
        exported.insert(view.name);
    }
    Ok(export)
}

/// Exports table rows as CSV: a header with the column names, then one line
/// per row.
fn csv_lines(table: &Table, rows: &[Row]) -> Vec<String> {
    let header = csv::format_line(table.columns.iter().map(|c| Some(&c.name)));
    let rows = rows.iter().map(|row| csv::format_line(row.iter().map(csv_field)));
    std::iter::once(header).chain(rows).collect()
}

/// Formats a value as a CSV field: None (an unquoted empty field) for NULL,
/// the string itself for strings, otherwise the value as displayed in query
/// results.
fn csv_field(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        value => Some(value.to_string()),
    }
}

/// Exports table rows as JSON lines: one object per row, with the column
/// values as members.
fn json_lines(table: &Table, rows: &[Row]) -> Vec<String> {
    rows.iter().map(|row| json_line(table, row)).collect()
}

/// Formats a table row as a JSON line: an object with the column values as
/// members.
pub fn json_line(table: &Table, row: &Row) -> String {
    let members = table.columns.iter().zip(row);
    Json::object(members.map(|(c, v)| (c.name.as_str(), json::value(v)))).to_string()
}

/// Exports a table as SQL: its CREATE TABLE statement (the schema display,
/// with defaults as SQL literals), then an INSERT statement per row. Executing
/// the statements re-creates the table.
fn sql_lines(table: &Table, rows: &[Row]) -> Vec<String> {
    let mut schema = String::new();
    table.write_create_table(&mut schema, sql_literal).expect("writing to string failed");
    schema.push(';');

    let name = format_ident(&table.name);
    let inserts = rows.iter().map(|row| {
        format!("INSERT INTO {name} VALUES ({});", row.iter().map(sql_literal).join(", "))
    });
    schema.lines().map(str::to_string).chain(inserts).collect()
}

/// Formats a value as a SQL literal, i.e. SQL text that evaluates to exactly
/// this value.
pub fn sql_literal(value: &Value) -> String {
    match value {
        Value::Null => "NULL".to_string(),
        Value::Boolean(true) => "TRUE".to_string(),
        Value::Boolean(false) => "FALSE".to_string(),
        // The smallest integer has no literal, since -9223372036854775808 is
        // parsed as the negation of an out-of-range integer.
        Value::Integer(i64::MIN) => format!("{} - 1", i64::MIN + 1),
        Value::Integer(i) => i.to_string(),
        Value::Float(f) if f.is_nan() => "NAN".to_string(),
        Value::Float(f) if f.is_infinite() && f.is_sign_positive() => "INFINITY".to_string(),
        Value::Float(f) if f.is_infinite() => "-INFINITY".to_string(),
        Value::Float(_) => value.to_string(),
        Value::String(s) => format!("'{}'", s.replace('\'', "''")),
    }
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;

    #[test_case(Value::Null => "NULL"; "null")]
    #[test_case(Value::Boolean(true) => "TRUE"; "boolean")]
    #[test_case(Value::Integer(-7) => "-7"; "integer")]
    #[test_case(Value::Integer(i64::MIN) => "-9223372036854775807 - 1"; "integer min")]
    #[test_case(Value::Float(0.0) => "0.0"; "float")]
    #[test_case(Value::Float(1.5e-7) => "1.5e-7"; "float exponent")]
    #[test_case(Value::Float(f64::NAN) => "NAN"; "float nan")]
    #[test_case(Value::Float(f64::INFINITY) => "INFINITY"; "float infinity")]
    #[test_case(Value::Float(f64::NEG_INFINITY) => "-INFINITY"; "float negative infinity")]
    #[test_case(Value::String(r#"it's "x" \ é"#.into()) => r#"'it''s "x" \ é'"#; "string")]
    fn literal(value: Value) -> String {
        sql_literal(&value)
    }

    #[test_case(Value::Null => None; "null")]
    #[test_case(Value::String("".into()) => Some("".into()); "empty string")]
    #[test_case(Value::Float(f64::NEG_INFINITY) => Some("-inf".into()); "float")]
    #[test_case(Value::Boolean(false) => Some("FALSE".into()); "boolean")]
    fn csv(value: Value) -> Option<String> {
        csv_field(&value)
    }
}
