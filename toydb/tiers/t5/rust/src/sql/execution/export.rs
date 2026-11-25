//! EXPORT: exports a table's rows, or the whole database, as text lines in
//! CSV, JSON (one object per row) or SQL (CREATE TABLE and INSERT statements).
//!
//! Exports read what the given transaction sees, in primary key order, with
//! columns in table order.

use std::fmt::Display;

use itertools::Itertools as _;

use super::policies;
use crate::encoding::csv;
use crate::encoding::json::Json;
use crate::errinput;
use crate::error::Result;
use crate::sql::engine::Transaction;
use crate::sql::json;
use crate::sql::parser::ast::ExportFormat;
use crate::sql::parser::format_ident;
use crate::sql::types::{Check, MaterializedView, Row, Table, Value, View};

/// The result of an export.
pub struct Export {
    /// The exported lines.
    pub lines: Vec<String>,
    /// The number of exported table rows.
    pub rows: u64,
}

/// Exports the given table, or all tables if None (only as SQL), as the given
/// user.
pub fn export(
    txn: &impl Transaction,
    table: Option<&str>,
    format: ExportFormat,
    user: &str,
) -> Result<Export> {
    let Some(table) = table else {
        return match format {
            ExportFormat::Sql => export_database(txn, user),
            ExportFormat::Csv | ExportFormat::Json => {
                errinput!("EXPORT without a table requires AS SQL")
            }
        };
    };
    let table = txn.must_get_table(table)?;
    let rows: Vec<Row> = policies::scan(txn, &table, user)?.try_collect()?;
    let lines = match format {
        ExportFormat::Csv => csv_lines(&table, &rows),
        ExportFormat::Json => json_lines(&table, &rows),
        ExportFormat::Sql => sql_lines(&table, &txn.get_checks(&table.name)?, &rows),
    };
    Ok(Export { lines, rows: rows.len() as u64 })
}

/// Exports every table as SQL, ordered such that tables come after the tables
/// they reference (other than themselves), and otherwise by name. A cycle of
/// references is broken by taking the first remaining table by name. The
/// views and materialized views follow the tables, each after the views and
/// materialized views it depends on, and otherwise by name.
fn export_database(txn: &impl Transaction, user: &str) -> Result<Export> {
    let mut export = Export { lines: Vec::new(), rows: 0 };
    for table in table_order(txn.list_tables()?) {
        let rows: Vec<Row> = policies::scan(txn, &table, user)?.try_collect()?;
        export.lines.extend(sql_lines(&table, &txn.get_checks(&table.name)?, &rows));
        export.rows += rows.len() as u64;
    }
    for view in view_order(txn.list_views()?, txn.list_materialized_views()?) {
        export.lines.push(format!("{view};"));
    }
    Ok(export)
}

/// A view or materialized view definition.
#[derive(Clone, Debug, PartialEq)]
pub enum ViewDefinition {
    View(View),
    Materialized(MaterializedView),
}

impl ViewDefinition {
    /// Returns the view's name.
    pub fn name(&self) -> &str {
        match self {
            Self::View(view) => &view.name,
            Self::Materialized(view) => &view.name,
        }
    }

    /// Returns the names that the view's query reads.
    fn dependencies(&self) -> &[String] {
        match self {
            Self::View(view) => &view.dependencies,
            Self::Materialized(view) => &view.dependencies,
        }
    }
}

/// Formats the definition as its CREATE statement.
impl Display for ViewDefinition {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::View(view) => view.fmt(f),
            Self::Materialized(view) => view.fmt(f),
        }
    }
}

/// Returns the tables in the order of EXPORT AS SQL.
pub fn table_order(mut remaining: Vec<Table>) -> Vec<Table> {
    remaining.sort_by(|a, b| a.name.cmp(&b.name));
    let mut ordered: Vec<Table> = Vec::with_capacity(remaining.len());
    while !remaining.is_empty() {
        let next = remaining
            .iter()
            .position(|table| {
                table.columns.iter().filter_map(|c| c.references.as_ref()).all(|reference| {
                    reference == &table.name || ordered.iter().any(|t| &t.name == reference)
                })
            })
            .unwrap_or(0);
        ordered.push(remaining.remove(next));
    }
    ordered
}

/// Returns the views and materialized views in the order of EXPORT AS SQL.
pub fn view_order(views: Vec<View>, matviews: Vec<MaterializedView>) -> Vec<ViewDefinition> {
    let views = views.into_iter().map(ViewDefinition::View);
    let matviews = matviews.into_iter().map(ViewDefinition::Materialized);
    let mut remaining =
        views.chain(matviews).sorted_by(|a, b| a.name().cmp(b.name())).collect_vec();
    let mut ordered = Vec::with_capacity(remaining.len());
    while !remaining.is_empty() {
        let next = remaining
            .iter()
            .position(|view| {
                view.dependencies().iter().all(|dependency| {
                    remaining.iter().all(|other| other.name() != dependency)
                })
            })
            .expect("views form a cycle");
        ordered.push(remaining.remove(next));
    }
    ordered
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
/// with defaults as SQL literals, and its CHECK constraints), then an INSERT
/// statement per row. Executing the statements re-creates the table.
fn sql_lines(table: &Table, checks: &[Check], rows: &[Row]) -> Vec<String> {
    let mut schema = String::new();
    table.write_create_table(&mut schema, sql_literal).expect("writing to string failed");
    let mut lines = schema.lines().map(str::to_string).collect_vec();

    // Add the CHECK constraints before the closing parenthesis.
    let close = lines.pop().expect("no closing parenthesis");
    if !checks.is_empty() {
        lines.last_mut().expect("no columns").push(',');
        let last = checks.len() - 1;
        for (i, check) in checks.iter().enumerate() {
            let comma = if i < last { "," } else { "" };
            lines.push(format!("  {check}{comma}"));
        }
    }
    lines.push(close + ";");

    let name = format_ident(&table.name);
    let inserts = rows.iter().map(|row| {
        format!("INSERT INTO {name} VALUES ({});", row.iter().map(sql_literal).join(", "))
    });
    lines.into_iter().chain(inserts).collect()
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
    use crate::sql::types::{Column, DataType};

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

    #[test]
    fn order() {
        let table = |name: &str, references: &[&str]| {
            let column = |name: &str, references: Option<&str>| Column {
                name: name.into(),
                datatype: DataType::Integer,
                nullable: references.is_some(),
                default: references.map(|_| Value::Null),
                unique: references.is_none(),
                index: references.is_some(),
                references: references.map(str::to_string),
            };
            let mut columns = vec![column("id", None)];
            columns.extend(references.iter().map(|r| column(&format!("{r}_id"), Some(r))));
            Table { name: name.into(), primary_key: 0, columns }
        };
        let tables = vec![table("c", &["b"]), table("a", &["a", "c"]), table("b", &[])];
        let names = table_order(tables).into_iter().map(|t| t.name).collect::<Vec<_>>();
        assert_eq!(names, vec!["b", "c", "a"]);

        let view = |name: &str, dependencies: &[&str]| View {
            name: name.into(),
            columns: vec!["id".into()],
            query: "SELECT 1 AS id".into(),
            dependencies: dependencies.iter().map(|d| d.to_string()).collect(),
        };
        let views = vec![view("z", &["t"]), view("a", &["m", "t"]), view("m", &["z"])];
        let ordered = view_order(views, Vec::new());
        let names = ordered.iter().map(|v| v.name()).collect::<Vec<_>>();
        assert_eq!(names, vec!["z", "m", "a"]);

        // Materialized views are ordered together with the views.
        let views = vec![view("b", &["mb"]), view("v", &["t"])];
        let matview = |name: &str, dependencies: &[&str]| MaterializedView {
            name: name.into(),
            columns: vec!["id".into()],
            query: "SELECT 1 AS id".into(),
            dependencies: dependencies.iter().map(|d| d.to_string()).collect(),
        };
        let matviews = vec![matview("mb", &["v"]), matview("ma", &["t"])];
        let ordered = view_order(views, matviews);
        let names = ordered.iter().map(|v| v.name()).collect::<Vec<_>>();
        assert_eq!(names, vec!["ma", "v", "mb", "b"]);
        assert_eq!(ordered[0].to_string(), "CREATE MATERIALIZED VIEW ma (id) AS SELECT 1 AS id");
    }
}
