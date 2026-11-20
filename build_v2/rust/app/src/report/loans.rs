//! Output of the loan commands.
use std::collections::BTreeMap;

use chrono::NaiveDate;

use super::by_due;
use crate::model::Loan;

/// All of a member's loans in borrowing order, with their status.
pub fn history(member_id: &str, loans: &[Loan]) -> Vec<String> {
    let mut ordered: Vec<&Loan> = loans.iter().collect();
    ordered.sort_by(|a, b| (a.borrowed, &a.id).cmp(&(b.borrowed, &b.id)));
    let mut lines = vec![format!("history {member_id}: {} loans", ordered.len())];
    lines.extend(ordered.iter().map(|loan| {
        let status = match (loan.lost, loan.returned) {
            (Some(lost), _) => format!("lost {lost}"),
            (None, Some(returned)) => format!("returned {returned}"),
            (None, None) => "open".to_owned(),
        };
        format!("  {} {} borrowed {} due {} {status}", loan.id, loan.isbn, loan.borrowed, loan.due)
    }));
    lines
}

/// Open loans that were due before `as_of`, with how many days they are overdue.
pub fn overdue(loans: &[Loan], as_of: NaiveDate) -> Vec<String> {
    by_due(loans.iter().filter(|loan| loan.is_open() && loan.due < as_of).collect())
        .iter()
        .map(|loan| {
            let days = (as_of - loan.due).num_days();
            format!("{} {} {} due {} ({days} days)", loan.id, loan.member_id, loan.isbn, loan.due)
        })
        .collect()
}

/// Open loans among the given loans, which the repository has already restricted to the due-date window.
pub fn due_soon(loans: &[Loan]) -> Vec<String> {
    by_due(loans.iter().filter(|loan| loan.is_open()).collect())
        .iter()
        .map(|loan| format!("{} {} {} due {}", loan.id, loan.member_id, loan.isbn, loan.due))
        .collect()
}

/// Returned loans that came back after their due date, latest first.
pub fn returned_late(loans: &[Loan]) -> Vec<String> {
    let mut late: Vec<(&Loan, NaiveDate, i64)> = loans
        .iter()
        .filter(|loan| loan.is_returned())
        .filter_map(|loan| loan.returned.map(|returned| (loan, returned, (returned - loan.due).num_days())))
        .filter(|(_, _, days)| *days > 0)
        .collect();
    late.sort_by(|a, b| b.2.cmp(&a.2).then(a.0.id.cmp(&b.0.id)));
    late.iter()
        .map(|(loan, returned, days)| format!("{} {} returned {returned} ({days} days late)", loan.id, loan.member_id))
        .collect()
}

/// Lost loans, ordered by the date they were reported lost.
pub fn lost(loans: &[Loan]) -> Vec<String> {
    let mut lost: Vec<(&Loan, NaiveDate)> =
        loans.iter().filter_map(|loan| loan.lost.map(|lost| (loan, lost))).collect();
    lost.sort_by(|a, b| (a.1, &a.0.id).cmp(&(b.1, &b.0.id)));
    lost.iter().map(|(loan, lost)| format!("{} {} {} lost {lost}", loan.id, loan.member_id, loan.isbn)).collect()
}

/// Number of loans borrowed per calendar month, in chronological order.
pub fn monthly(borrowed: &[NaiveDate]) -> Vec<String> {
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    for date in borrowed {
        *counts.entry(date.format("%Y-%m").to_string()).or_default() += 1;
    }
    counts.iter().map(|(month, count)| format!("{month}: {count}")).collect()
}
