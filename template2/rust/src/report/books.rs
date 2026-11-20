//! Output of the book commands.
use std::collections::BTreeMap;

use super::{book_lines, NONE, NO_GENRE};
use crate::{
    model::{Book, Loan},
    repo::books::{Availability, BookSummary},
};

pub fn books(items: &[BookSummary]) -> Vec<String> {
    book_lines(items)
}

pub fn book(book: &Book, loans: &[Loan]) -> Vec<String> {
    let open: Vec<&Loan> = loans.iter().filter(|loan| loan.is_open()).collect();
    let mut borrowers: Vec<&str> = open.iter().map(|loan| loan.member_id.as_str()).collect();
    borrowers.sort_unstable();
    borrowers.dedup();
    let borrowers = if borrowers.is_empty() { NONE.to_owned() } else { borrowers.join(", ") };
    vec![
        format!("book {}: {}", book.isbn, book.title),
        format!("subtitle: {}", book.subtitle.as_deref().unwrap_or(NONE)),
        format!("year: {}", book.year),
        format!("genre: {}", book.genre.as_deref().unwrap_or(NONE)),
        format!("copies: {}", book.copies),
        format!("loans: {}", loans.len()),
        format!("on loan: {}", open.len()),
        format!("borrowers: {borrowers}"),
    ]
}

/// Number of books per genre, most common first.
pub fn stats(genres: &[Option<String>]) -> Vec<String> {
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for genre in genres {
        *counts.entry(genre.as_deref().unwrap_or(NO_GENRE)).or_default() += 1;
    }
    let mut ordered: Vec<(&str, usize)> = counts.into_iter().collect();
    ordered.sort_by(|a, b| b.1.cmp(&a.1).then(a.0.cmp(b.0)));
    ordered.iter().map(|(genre, count)| format!("{genre}: {count}")).collect()
}

/// Number of books per decade of publication, in chronological order.
pub fn decades(years: &[i64]) -> Vec<String> {
    let mut counts: BTreeMap<i64, usize> = BTreeMap::new();
    for year in years {
        *counts.entry(year.div_euclid(10) * 10).or_default() += 1;
    }
    counts.iter().map(|(decade, count)| format!("{decade}s: {count}")).collect()
}

pub fn genre(items: &[BookSummary]) -> Vec<String> {
    book_lines(items)
}

pub fn never_borrowed(items: &[BookSummary]) -> Vec<String> {
    book_lines(items)
}

pub fn available(availability: &Availability) -> Vec<String> {
    let on_loan: i64 = availability.loans.iter().filter(|loan| loan.is_open()).map(|_| 1).sum();
    let free = (availability.copies - on_loan).max(0);
    vec![format!("{} {}: {free} of {} available", availability.isbn, availability.title, availability.copies)]
}

/// Books whose title or subtitle contains `query`, ignoring case.
pub fn search(rows: &[(BookSummary, Option<String>)], query: &str) -> Vec<String> {
    let needle = query.to_lowercase();
    let matches = |text: &str| text.to_lowercase().contains(&needle);
    book_lines(
        rows.iter()
            .filter(|(book, subtitle)| matches(&book.title) || subtitle.as_deref().is_some_and(matches))
            .map(|(book, _)| book),
    )
}
