//! Minimal CSV reader: header row, comma separated, double-quoted fields may contain commas and "" escapes.
use std::{collections::HashMap, fs, path::Path};

use crate::error::{AppError, Result};

pub type Record = HashMap<String, String>;

fn csv_error(file: &str, line: usize, message: impl Into<String>) -> AppError {
    AppError::Csv { file: file.to_owned(), line, message: message.into() }
}

pub fn parse_line(line: &str, file: &str, line_no: usize) -> Result<Vec<String>> {
    let mut fields = Vec::new();
    let mut current = String::new();
    let mut in_quotes = false;
    let mut chars = line.chars().peekable();
    while let Some(ch) = chars.next() {
        if in_quotes {
            if ch == '"' {
                if chars.peek() == Some(&'"') {
                    current.push('"');
                    chars.next();
                } else {
                    in_quotes = false;
                }
            } else {
                current.push(ch);
            }
        } else if ch == '"' {
            in_quotes = true;
        } else if ch == ',' {
            fields.push(std::mem::take(&mut current));
        } else {
            current.push(ch);
        }
    }
    if in_quotes {
        return Err(csv_error(file, line_no, "unterminated quoted field"));
    }
    fields.push(current);
    Ok(fields)
}

/// Return (line number, record) pairs. Empty fields are returned as empty strings.
pub fn read_csv(path: &Path) -> Result<Vec<(usize, Record)>> {
    let file = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let text = fs::read_to_string(path).map_err(|_| csv_error(&file, 0, "file not found"))?;
    let mut lines = text.lines();
    let header = match lines.next() {
        Some(line) => parse_line(line, &file, 1)?,
        None => return Err(csv_error(&file, 0, "missing header row")),
    };
    let mut records = Vec::new();
    for (index, line) in lines.enumerate() {
        let line_no = index + 2;
        if line.trim().is_empty() {
            continue;
        }
        let fields = parse_line(line, &file, line_no)?;
        if fields.len() != header.len() {
            return Err(csv_error(
                &file,
                line_no,
                format!("expected {} fields, found {}", header.len(), fields.len()),
            ));
        }
        records.push((line_no, header.iter().cloned().zip(fields).collect()));
    }
    Ok(records)
}
