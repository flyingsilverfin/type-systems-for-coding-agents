//! Plain-text output: one formatting function per command, each returning the lines to print.
pub mod books;
pub mod loans;
pub mod members;

use crate::{model::Loan, repo::books::BookSummary};

/// Placeholder printed for a missing optional value.
pub const NONE: &str = "-";
/// Label for books without a genre; the `genre` command accepts it to select those books.
pub const NO_GENRE: &str = "(none)";

/// One line per book, ordered by title and then ISBN.
fn book_lines<'a>(books: impl IntoIterator<Item = &'a BookSummary>) -> Vec<String> {
    let mut ordered: Vec<&BookSummary> = books.into_iter().collect();
    ordered.sort_by(|a, b| (&a.title, &a.isbn).cmp(&(&b.title, &b.isbn)));
    ordered
        .iter()
        .map(|b| format!("{}  {} ({}) [{}]", b.isbn, b.title, b.year, b.genre.as_deref().unwrap_or(NONE)))
        .collect()
}

/// Order loans by due date, then loan id.
fn by_due(mut loans: Vec<&Loan>) -> Vec<&Loan> {
    loans.sort_by(|a, b| (a.due, &a.id).cmp(&(b.due, &b.id)));
    loans
}

/// Render an amount in cents as `units.cents`.
fn money(cents: i64) -> String {
    format!("{}.{:02}", cents.div_euclid(100), cents.rem_euclid(100))
}
