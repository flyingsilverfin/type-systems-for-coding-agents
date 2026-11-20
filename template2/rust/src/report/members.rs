//! Output of the member commands.
use std::collections::BTreeMap;

use super::{by_due, money, NONE};
use crate::{
    model::{Loan, Member},
    repo::members::{MemberFee, MemberName, MemberSummary},
};

/// One line per member, ordered by member id.
pub fn members(items: &[MemberSummary]) -> Vec<String> {
    let mut ordered: Vec<&MemberSummary> = items.iter().collect();
    ordered.sort_by(|a, b| a.id.cmp(&b.id));
    ordered
        .iter()
        .map(|m| {
            format!(
                "{} {} {} {}",
                m.id,
                m.name,
                m.email.as_deref().unwrap_or(NONE),
                m.phone.as_deref().unwrap_or(NONE)
            )
        })
        .collect()
}

pub fn member(member: &Member, loans: &[Loan]) -> Vec<String> {
    let open = by_due(loans.iter().filter(|loan| loan.is_open()).collect());
    let mut lines = vec![
        format!("member {}: {}", member.id, member.name),
        format!("email: {}", member.email.as_deref().unwrap_or(NONE)),
        format!("phone: {}", member.phone.as_deref().unwrap_or(NONE)),
        format!("joined: {}", member.joined),
        format!("open loans: {}", open.len()),
    ];
    lines.extend(open.iter().map(|loan| format!("  {} {} due {}", loan.id, loan.isbn, loan.due)));
    lines.push(format!("lost loans: {}", loans.iter().filter(|loan| loan.is_lost()).count()));
    lines
}

/// The member's name and their preferred contact: email, else phone.
pub fn contact(member: &MemberSummary) -> Vec<String> {
    let via = member.email.as_deref().or(member.phone.as_deref()).unwrap_or("(no contact)");
    vec![format!("{} {via}", member.name)]
}

/// One line per inactive member, ordered by member id.
pub fn inactive(items: &[MemberName]) -> Vec<String> {
    let mut ordered: Vec<&MemberName> = items.iter().collect();
    ordered.sort_by(|a, b| a.id.cmp(&b.id));
    ordered.iter().map(|m| format!("{} {}", m.id, m.name)).collect()
}

/// The `limit` members with the most loans, given one borrower entry per loan.
pub fn top(borrowers: &[MemberName], limit: usize) -> Vec<String> {
    let mut counts: BTreeMap<&str, (&str, usize)> = BTreeMap::new();
    for borrower in borrowers {
        counts.entry(&borrower.id).or_insert((&borrower.name, 0)).1 += 1;
    }
    let mut ranked: Vec<(&str, &str, usize)> =
        counts.into_iter().map(|(id, (name, count))| (id, name, count)).collect();
    ranked.sort_by(|a, b| b.2.cmp(&a.2).then(a.0.cmp(b.0)));
    ranked
        .iter()
        .take(limit)
        .enumerate()
        .map(|(index, (id, name, count))| format!("{}. {id} {name} ({count} loans)", index + 1))
        .collect()
}

/// Total fees per member, highest first; members without fees are left out.
pub fn fees(items: &[MemberFee]) -> Vec<String> {
    let mut totals: BTreeMap<&str, (&str, i64)> = BTreeMap::new();
    for fee in items {
        totals.entry(&fee.member_id).or_insert((&fee.name, 0)).1 += fee.fee_cents;
    }
    let mut ranked: Vec<(&str, &str, i64)> =
        totals.into_iter().filter(|(_, (_, total))| *total > 0).map(|(id, (name, total))| (id, name, total)).collect();
    ranked.sort_by(|a, b| b.2.cmp(&a.2).then(a.0.cmp(b.0)));
    ranked.iter().map(|(id, name, total)| format!("{id} {name} {}", money(*total))).collect()
}

/// The member's total fees, followed by every loan that carries a fee, ordered by loan id.
pub fn member_fees(member_id: &str, loans: &[Loan]) -> Vec<String> {
    let mut charged: Vec<(&str, i64)> = loans
        .iter()
        .filter_map(|loan| loan.fee_cents.filter(|cents| *cents > 0).map(|cents| (loan.id.as_str(), cents)))
        .collect();
    charged.sort_by(|a, b| a.0.cmp(b.0));
    let total: i64 = charged.iter().map(|(_, cents)| cents).sum();
    let mut lines = vec![format!("fees {member_id}: {}", money(total))];
    lines.extend(charged.iter().map(|(loan_id, cents)| format!("  {loan_id} {}", money(*cents))));
    lines
}
