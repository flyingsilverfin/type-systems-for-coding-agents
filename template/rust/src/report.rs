//! Plain-text output. Output formats are part of the CLI contract: see README.md.
use std::collections::BTreeMap;

use chrono::NaiveDate;

use crate::model::{Book, Loan, Member};

pub const NONE: &str = "-";

pub fn by_due(mut loans: Vec<&Loan>) -> Vec<&Loan> {
    loans.sort_by(|a, b| (a.due, &a.id).cmp(&(b.due, &b.id)));
    loans
}

pub fn books(items: &[Book]) -> Vec<String> {
    let mut ordered: Vec<&Book> = items.iter().collect();
    ordered.sort_by(|a, b| (&a.title, &a.isbn).cmp(&(&b.title, &b.isbn)));
    ordered
        .iter()
        .map(|b| format!("{}  {} ({}) [{}]", b.isbn, b.title, b.year, b.genre.as_deref().unwrap_or(NONE)))
        .collect()
}

pub fn member(m: &Member, loans: &[Loan]) -> Vec<String> {
    let open_loans = by_due(loans.iter().filter(|loan| loan.is_open()).collect());
    let mut lines = vec![
        format!("member {}: {}", m.id, m.name),
        format!("email: {}", m.email.as_deref().unwrap_or(NONE)),
        format!("joined: {}", m.joined.format("%Y-%m-%d")),
        format!("open loans: {}", open_loans.len()),
    ];
    lines.extend(
        open_loans.iter().map(|loan| format!("  {} {} due {}", loan.id, loan.isbn, loan.due.format("%Y-%m-%d"))),
    );
    lines
}

pub fn overdue(loans: &[Loan], as_of: NaiveDate) -> Vec<String> {
    let late = by_due(loans.iter().filter(|loan| loan.is_open() && loan.due < as_of).collect());
    late.iter()
        .map(|loan| {
            format!(
                "{} {} {} due {} ({} days)",
                loan.id,
                loan.member_id,
                loan.isbn,
                loan.due.format("%Y-%m-%d"),
                (as_of - loan.due).num_days()
            )
        })
        .collect()
}

pub fn genre_stats(items: &[Book]) -> Vec<String> {
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for b in items {
        *counts.entry(b.genre.as_deref().unwrap_or("(none)")).or_default() += 1;
    }
    let mut ordered: Vec<(&str, usize)> = counts.into_iter().collect();
    ordered.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    ordered.iter().map(|(genre, count)| format!("{genre}: {count}")).collect()
}
