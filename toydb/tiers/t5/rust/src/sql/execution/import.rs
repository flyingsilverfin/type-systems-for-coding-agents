//! IMPORT: imports rows into a table from CSV or JSON lines.
//!
//! All lines are read and checked first, in order, and the first problem is
//! reported. The rows are then inserted by a single INSERT plan, exactly as
//! `INSERT INTO <table> (<columns>) VALUES (<values>)` would insert them, with
//! the CSV header or the JSON object's member names as the column list: all rows
//! are completed with the default values of the other columns, so the usual
//! INSERT constraints and errors apply.

use std::sync::LazyLock;

use itertools::Itertools as _;
use regex::Regex;

use super::ExecutionResult;
use super::executor::build_row;
use crate::encoding::csv;
use crate::error::Result;
use crate::sql::engine::Transaction;
use crate::sql::parser::ast::ImportFormat;
use crate::sql::planner::{Node, Plan};
use crate::sql::types::{DataType, Expression, Table, Value};
use crate::{errdata, errinput};

/// A row read from an import line: column indexes and their values.
pub type ImportRow = (Vec<usize>, Vec<Value>);

/// Imports lines into a table as the given user, returning the number of
/// inserted rows.
pub fn import(
    txn: &impl Transaction,
    table: &str,
    format: ImportFormat,
    lines: &[String],
    user: &str,
) -> Result<u64> {
    let table = txn.must_get_table(table)?;
    let rows = match format {
        ImportFormat::Csv => read_csv(&table, lines)?,
        ImportFormat::Json => read_json(&table, lines)?,
    };
    insert(txn, table, rows, user)
}

/// Inserts rows of column values into a table as the given user.
pub fn insert(
    txn: &impl Transaction,
    table: Table,
    rows: Vec<ImportRow>,
    user: &str,
) -> Result<u64> {
    // Build the complete rows like INSERT does (with defaults) before
    // inserting any of them, and insert them with an INSERT plan.
    let rows: Vec<Vec<Expression>> = rows
        .into_iter()
        .map(|(columns, values)| {
            let column_map = columns.iter().enumerate().map(|(i, column)| (*column, i)).collect();
            build_row(&table, Some(&column_map), values)
        })
        .map_ok(|row| row.into_iter().map(Expression::Constant).collect())
        .try_collect()?;
    let plan = Plan::Insert { table, column_map: None, source: Node::Values { rows } };
    match plan.execute(txn, user, None)? {
        ExecutionResult::Insert { count } => Ok(count),
        _ => errdata!("unexpected INSERT result"),
    }
}

/// Reads CSV lines. The first line is a header with column names, and each
/// following line is a row with values for these columns. Unquoted empty
/// fields are NULL.
fn read_csv(table: &Table, lines: &[String]) -> Result<Vec<ImportRow>> {
    let mut lines = lines.iter().enumerate().map(|(i, line)| (i + 1, line));

    // Look up the header columns.
    let Some((_, header)) = lines.next() else {
        return Ok(Vec::new());
    };
    let Some(header) = csv::parse_line(header) else {
        return errinput!("invalid CSV on line 1");
    };
    let mut columns = Vec::with_capacity(header.len());
    for name in header {
        let name = name.unwrap_or_default();
        let index = lookup_column(table, &name)?;
        if columns.contains(&index) {
            return errinput!("column {name} given multiple times");
        }
        columns.push(index);
    }

    // Read the rows.
    let mut rows = Vec::new();
    for (number, line) in lines {
        let Some(fields) = csv::parse_line(line) else {
            return errinput!("invalid CSV on line {number}");
        };
        if fields.len() != columns.len() {
            return errinput!(
                "wrong number of fields on line {number}: expected {}, found {}",
                columns.len(),
                fields.len()
            );
        }
        let values = fields
            .into_iter()
            .zip(&columns)
            .map(|(field, &index)| match field {
                None => Ok(Value::Null),
                Some(text) => match parse_value(&text, table.columns[index].datatype) {
                    Some(value) => Ok(value),
                    None => invalid_value(table, index, number),
                },
            })
            .try_collect()?;
        rows.push((columns.clone(), values));
    }
    Ok(rows)
}

/// Reads JSON lines. Each line is an object whose members give column values.
fn read_json(table: &Table, lines: &[String]) -> Result<Vec<ImportRow>> {
    let mut rows = Vec::with_capacity(lines.len());
    for (number, line) in lines.iter().enumerate().map(|(i, line)| (i + 1, line)) {
        // Members are kept in the order written (see `encoding::json`).
        let Ok(json) = serde_json::from_str::<serde_json::Value>(line) else {
            return errinput!("invalid JSON on line {number}");
        };
        let serde_json::Value::Object(object) = json else {
            return errinput!("line {number} is not a JSON object");
        };
        let mut columns = Vec::with_capacity(object.len());
        let mut values = Vec::with_capacity(object.len());
        for (name, json) in &object {
            let index = lookup_column(table, name)?;
            let Some(value) = json_value(json, table.columns[index].datatype) else {
                return invalid_value(table, index, number);
            };
            columns.push(index);
            values.push(value);
        }
        rows.push((columns, values));
    }
    Ok(rows)
}

/// Looks up a column index by its exact name.
fn lookup_column(table: &Table, name: &str) -> Result<usize> {
    match table.columns.iter().position(|c| c.name == name) {
        Some(index) => Ok(index),
        None => errinput!("unknown column {name} in table {}", table.name),
    }
}

/// Returns an invalid value error for the given column.
fn invalid_value<T>(table: &Table, index: usize, line: usize) -> Result<T> {
    let column = &table.columns[index];
    errinput!("invalid value for {} column {} on line {line}", column.datatype, column.name)
}

/// Converts a JSON value to a value of the given data type, or None if it is
/// not a valid value for the type. null is NULL for every type. JSON numbers
/// are read from their text: an INTEGER only from an integer without fraction
/// or exponent (not 2.0 or 2e0), a FLOAT from any number (infinite beyond the
/// float range). Values without a native JSON form, and only they, use the
/// typed form {"type":"FLOAT","value":"NaN"}.
fn json_value(json: &serde_json::Value, datatype: DataType) -> Option<Value> {
    use serde_json::Value as Json;

    if json.is_null() {
        return Some(Value::Null);
    }
    match datatype {
        DataType::Boolean => json.as_bool().map(Value::Boolean),
        // Numbers are parsed from their JSON text, so this only accepts
        // integers without fraction or exponent, e.g. not 2.0 or 2e0.
        DataType::Integer => json.as_number()?.as_i64().map(Value::Integer),
        // Numbers are parsed from their JSON text like FLOAT text, so numbers
        // beyond the float range become infinite (as_f64() rejects them).
        DataType::Float => match json {
            Json::Number(number) => number.to_string().parse().ok().map(Value::Float),
            // Only non-finite floats use the typed form.
            Json::Object(_) => typed_value(json, datatype)
                .filter(|value| matches!(value, Value::Float(f) if !f.is_finite())),
            _ => None,
        },
        DataType::String => json.as_str().map(|s| Value::String(s.to_string())),
    }
}

/// Converts a typed JSON value of the given data type, an object with exactly
/// the string members type (the data type) and value (the value text), to a
/// value, or None if it is not a valid typed value.
fn typed_value(json: &serde_json::Value, datatype: DataType) -> Option<Value> {
    let object = json.as_object()?;
    if object.len() != 2 {
        return None;
    }
    let r#type = object.get("type")?.as_str()?;
    let text = object.get("value")?.as_str()?;
    if r#type != datatype.to_string() {
        return None;
    }
    parse_value(text, datatype)
}

/// Parses a value of the given data type from text, as written by EXPORT, or
/// returns None if the text is not a valid value. Text is never trimmed.
///
/// * BOOLEAN: true or false in any case.
/// * INTEGER: an optional -, then digits.
/// * FLOAT: NaN, inf, -inf, or an optional -, digits, an optional fraction,
///   and an optional exponent.
/// * STRING: any text.
pub fn parse_value(text: &str, datatype: DataType) -> Option<Value> {
    static INTEGER: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^-?[0-9]+$").unwrap());
    static FLOAT: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$").unwrap());

    match datatype {
        DataType::Boolean => match text.to_lowercase().as_str() {
            "true" => Some(Value::Boolean(true)),
            "false" => Some(Value::Boolean(false)),
            _ => None,
        },
        DataType::Integer if INTEGER.is_match(text) => text.parse().ok().map(Value::Integer),
        DataType::Integer => None,
        DataType::Float => match text {
            "NaN" => Some(Value::Float(f64::NAN)),
            "inf" => Some(Value::Float(f64::INFINITY)),
            "-inf" => Some(Value::Float(f64::NEG_INFINITY)),
            text if FLOAT.is_match(text) => text.parse().ok().map(Value::Float),
            _ => None,
        },
        DataType::String => Some(Value::String(text.to_string())),
    }
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;

    #[test_case("true", DataType::Boolean => Some(Value::Boolean(true)); "boolean")]
    #[test_case("fAlSe", DataType::Boolean => Some(Value::Boolean(false)); "boolean case")]
    #[test_case("1", DataType::Boolean => None; "boolean number")]
    #[test_case("007", DataType::Integer => Some(Value::Integer(7)); "integer leading zeros")]
    #[test_case("-0", DataType::Integer => Some(Value::Integer(0)); "integer negative zero")]
    #[test_case("-9223372036854775808", DataType::Integer => Some(Value::Integer(i64::MIN)); "integer min")]
    #[test_case("9223372036854775808", DataType::Integer => None; "integer overflow")]
    #[test_case("+1", DataType::Integer => None; "integer plus")]
    #[test_case("1.0", DataType::Integer => None; "integer fraction")]
    #[test_case(" 1", DataType::Integer => None; "integer space")]
    #[test_case("2.5e-3", DataType::Float => Some(Value::Float(0.0025)); "float exponent")]
    #[test_case("1E10", DataType::Float => Some(Value::Float(1e10)); "float upper exponent")]
    #[test_case("1", DataType::Float => Some(Value::Float(1.0)); "float integer")]
    #[test_case("inf", DataType::Float => Some(Value::Float(f64::INFINITY)); "float infinity")]
    #[test_case(".5", DataType::Float => None; "float no integer part")]
    #[test_case("5.", DataType::Float => None; "float no fraction digits")]
    #[test_case("Infinity", DataType::Float => None; "float infinity spelling")]
    #[test_case("nan", DataType::Float => None; "float nan case")]
    #[test_case("", DataType::String => Some(Value::String("".into())); "string empty")]
    #[test_case(" x ", DataType::String => Some(Value::String(" x ".into())); "string untrimmed")]
    fn parse(text: &str, datatype: DataType) -> Option<Value> {
        parse_value(text, datatype)
    }

    #[test]
    fn parse_nan() {
        assert!(matches!(parse_value("NaN", DataType::Float), Some(Value::Float(f)) if f.is_nan()));
    }

    #[test_case(r#"null"#, DataType::Integer => Some(Value::Null); "null")]
    #[test_case(r#"-0"#, DataType::Integer => Some(Value::Integer(0)); "integer negative zero")]
    #[test_case(r#"2.0"#, DataType::Integer => None; "integer fraction")]
    #[test_case(r#"2e0"#, DataType::Integer => None; "integer exponent")]
    #[test_case(r#"9223372036854775808"#, DataType::Integer => None; "integer overflow")]
    #[test_case(r#"9223372036854775808"#, DataType::Float => Some(Value::Float(9223372036854775808.0)); "float big")]
    #[test_case(r#"-1"#, DataType::Float => Some(Value::Float(-1.0)); "float integer")]
    #[test_case(r#"-1e400"#, DataType::Float => Some(Value::Float(f64::NEG_INFINITY)); "float overflow")]
    #[test_case(r#"1e-400"#, DataType::Float => Some(Value::Float(0.0)); "float underflow")]
    #[test_case(r#"{"value":"-inf","type":"FLOAT"}"#, DataType::Float => Some(Value::Float(f64::NEG_INFINITY)); "float typed")]
    #[test_case(r#"{"type":"FLOAT","value":"1.5"}"#, DataType::Float => None; "float typed finite")]
    #[test_case(r#"{"type":"FLOAT","value":"inf","x":1}"#, DataType::Float => None; "float typed extra member")]
    #[test_case(r#"{"type":"INTEGER","value":"inf"}"#, DataType::Float => None; "float typed wrong type")]
    #[test_case(r#"{"type":"STRING","value":"x"}"#, DataType::String => None; "string typed")]
    #[test_case(r#""1""#, DataType::Integer => None; "integer string")]
    #[test_case(r#"true"#, DataType::Boolean => Some(Value::Boolean(true)); "boolean")]
    #[test_case(r#"1"#, DataType::Boolean => None; "boolean number")]
    fn json(json: &str, datatype: DataType) -> Option<Value> {
        json_value(&serde_json::from_str(json).unwrap(), datatype)
    }
}
