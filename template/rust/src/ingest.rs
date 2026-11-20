//! Load members.csv, books.csv and loans.csv into the database.
use std::path::Path;

use chrono::NaiveDate;

use crate::{
    csv::{read_csv, Record},
    db::{quote, Db},
    error::{AppError, Result},
};

#[derive(Debug, Default)]
pub struct LoadStats {
    pub members: usize,
    pub books: usize,
    pub loans: usize,
}

fn field<'a>(record: &'a Record, key: &str, file: &str, line: usize) -> Result<&'a str> {
    match record.get(key).map(String::as_str) {
        Some(value) if !value.is_empty() => Ok(value),
        _ => Err(AppError::Csv { file: file.to_owned(), line, message: format!("missing required field '{key}'") }),
    }
}

fn optional<'a>(record: &'a Record, key: &str) -> Option<&'a str> {
    record.get(key).map(String::as_str).filter(|value| !value.is_empty())
}

fn parse_date(text: &str, file: &str, line: usize) -> Result<NaiveDate> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .map_err(|_| AppError::Csv { file: file.to_owned(), line, message: format!("invalid date '{text}'") })
}

fn parse_int(text: &str, file: &str, line: usize) -> Result<i64> {
    text.parse::<i64>()
        .map_err(|_| AppError::Csv { file: file.to_owned(), line, message: format!("invalid integer '{text}'") })
}

pub fn member_query(record: &Record, line: usize) -> Result<String> {
    let f = "members.csv";
    let member_id = field(record, "member_id", f, line)?;
    let name = field(record, "name", f, line)?;
    let joined = parse_date(field(record, "joined", f, line)?, f, line)?;
    let mut parts = vec![format!(
        "$m isa member, has member-id {}, has name {}, has joined {}",
        quote(member_id),
        quote(name),
        joined.format("%Y-%m-%d")
    )];
    if let Some(email) = optional(record, "email") {
        parts.push(format!("has email {}", quote(email)));
    }
    Ok(format!("insert {};", parts.join(", ")))
}

pub fn book_query(record: &Record, line: usize) -> Result<String> {
    let f = "books.csv";
    let isbn = field(record, "isbn", f, line)?;
    let title = field(record, "title", f, line)?;
    let year = parse_int(field(record, "year", f, line)?, f, line)?;
    let copies = parse_int(field(record, "copies", f, line)?, f, line)?;
    let mut parts = vec![format!(
        "$b isa book, has isbn {}, has title {}, has year {year}, has copies {copies}",
        quote(isbn),
        quote(title)
    )];
    if let Some(genre) = optional(record, "genre") {
        parts.push(format!("has genre {}", quote(genre)));
    }
    Ok(format!("insert {};", parts.join(", ")))
}

pub fn loan_query(record: &Record, line: usize) -> Result<String> {
    let f = "loans.csv";
    let loan_id = field(record, "loan_id", f, line)?;
    let member_id = field(record, "member_id", f, line)?;
    let isbn = field(record, "isbn", f, line)?;
    let borrowed = parse_date(field(record, "borrowed", f, line)?, f, line)?;
    let due = parse_date(field(record, "due", f, line)?, f, line)?;
    let mut attrs = vec![
        format!("has loan-id {}", quote(loan_id)),
        format!("has borrowed {}", borrowed.format("%Y-%m-%d")),
        format!("has due {}", due.format("%Y-%m-%d")),
    ];
    if let Some(returned) = optional(record, "returned") {
        attrs.push(format!("has returned {}", parse_date(returned, f, line)?.format("%Y-%m-%d")));
    }
    Ok(format!(
        "match $m isa member, has member-id {}; $b isa book, has isbn {};\ninsert $l isa loan, links (borrower: $m, item: $b), {};",
        quote(member_id),
        quote(isbn),
        attrs.join(", ")
    ))
}

pub fn load_dir(db: &Db, directory: &Path) -> Result<LoadStats> {
    let mut stats = LoadStats::default();
    let mut queries = Vec::new();
    for (line, record) in read_csv(&directory.join("members.csv"))? {
        queries.push(member_query(&record, line)?);
        stats.members += 1;
    }
    for (line, record) in read_csv(&directory.join("books.csv"))? {
        queries.push(book_query(&record, line)?);
        stats.books += 1;
    }
    db.write(&queries)?;
    let mut loans = Vec::new();
    for (line, record) in read_csv(&directory.join("loans.csv"))? {
        loans.push(loan_query(&record, line)?);
    }
    db.write(&loans)?;
    stats.loans = loans.len();
    Ok(stats)
}
