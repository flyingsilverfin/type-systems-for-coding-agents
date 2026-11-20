//! Queries behind the loan commands: history, overdue, due-soon, returned-late, lost, monthly.
use chrono::NaiveDate;
use typedb_driver::concept::Concept;

use crate::{
    db::{quote, Db},
    error::{AppError, Result},
    model::Loan,
};

/// All loans of the given member, or `None` if there is no such member.
pub fn history(db: &Db, member_id: &str) -> Result<Option<Vec<Loan>>> {
    let rows = db.read_rows(&format!(
        "
        match
          $m isa member, has member-id {};
          try {{
            $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due;
            $b has isbn $isbn;
            try {{ $l has returned $returned; }};
            try {{ $l has lost $lost; }};
            try {{ $l has fee-cents $fee; }};
          }};
        ",
        quote(member_id)
    ))?;
    if rows.is_empty() {
        return Ok(None);
    }
    let mut loans = Vec::new();
    for row in &rows {
        // A row without a loan id is the single row of a member who has never borrowed anything.
        let Some(loan_id) = row.get("loan_id")? else { continue };
        loans.push(Loan {
            id: loan_id.try_get_string().ok_or_else(|| AppError::column("loan_id", "string"))?.to_owned(),
            member_id: member_id.to_owned(),
            isbn: row.get("isbn")?
                .and_then(Concept::try_get_string)
                .ok_or_else(|| AppError::column("isbn", "string"))?
                .to_owned(),
            borrowed: row.get("borrowed")?
                .and_then(Concept::try_get_date)
                .ok_or_else(|| AppError::column("borrowed", "date"))?,
            due: row.get("due")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("due", "date"))?,
            returned: row.get("returned")?
                .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("returned", "date")))
                .transpose()?,
            lost: row.get("lost")?
                .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("lost", "date")))
                .transpose()?,
            fee_cents: row.get("fee")?
                .map(|concept| concept.try_get_integer().ok_or_else(|| AppError::column("fee", "integer")))
                .transpose()?,
        });
    }
    Ok(Some(loans))
}

/// Loans (of any status) that were due before `as_of`.
pub fn overdue(db: &Db, as_of: NaiveDate) -> Result<Vec<Loan>> {
    let rows = db.read_rows(&format!(
        "
        match
          $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due;
          $due < {as_of};
          $m has member-id $member_id;
          $b has isbn $isbn;
          try {{ $l has returned $returned; }};
          try {{ $l has lost $lost; }};
          try {{ $l has fee-cents $fee; }};
        "
    ))?;
    rows.iter()
        .map(|row| {
            Ok(Loan {
                id: row.get("loan_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("loan_id", "string"))?
                    .to_owned(),
                member_id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                borrowed: row.get("borrowed")?
                    .and_then(Concept::try_get_date)
                    .ok_or_else(|| AppError::column("borrowed", "date"))?,
                due: row.get("due")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("due", "date"))?,
                returned: row.get("returned")?
                    .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("returned", "date")))
                    .transpose()?,
                lost: row.get("lost")?
                    .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("lost", "date")))
                    .transpose()?,
                fee_cents: row.get("fee")?
                    .map(|concept| concept.try_get_integer().ok_or_else(|| AppError::column("fee", "integer")))
                    .transpose()?,
            })
        })
        .collect()
}

/// Loans (of any status) due between `start` and `end`, both inclusive.
pub fn due_soon(db: &Db, start: NaiveDate, end: NaiveDate) -> Result<Vec<Loan>> {
    let rows = db.read_rows(&format!(
        "
        match
          $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due;
          $due >= {start};
          $due <= {end};
          $m has member-id $member_id;
          $b has isbn $isbn;
          try {{ $l has returned $returned; }};
          try {{ $l has lost $lost; }};
          try {{ $l has fee-cents $fee; }};
        "
    ))?;
    rows.iter()
        .map(|row| {
            Ok(Loan {
                id: row.get("loan_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("loan_id", "string"))?
                    .to_owned(),
                member_id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                borrowed: row.get("borrowed")?
                    .and_then(Concept::try_get_date)
                    .ok_or_else(|| AppError::column("borrowed", "date"))?,
                due: row.get("due")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("due", "date"))?,
                returned: row.get("returned")?
                    .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("returned", "date")))
                    .transpose()?,
                lost: row.get("lost")?
                    .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("lost", "date")))
                    .transpose()?,
                fee_cents: row.get("fee")?
                    .map(|concept| concept.try_get_integer().ok_or_else(|| AppError::column("fee", "integer")))
                    .transpose()?,
            })
        })
        .collect()
}

/// Loans that carry a return date (including any that were later marked lost).
pub fn returned_late(db: &Db) -> Result<Vec<Loan>> {
    let rows = db.read_rows(
        "
        match
          $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due,
            has returned $returned;
          $m has member-id $member_id;
          $b has isbn $isbn;
          try { $l has lost $lost; };
          try { $l has fee-cents $fee; };
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(Loan {
                id: row.get("loan_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("loan_id", "string"))?
                    .to_owned(),
                member_id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                borrowed: row.get("borrowed")?
                    .and_then(Concept::try_get_date)
                    .ok_or_else(|| AppError::column("borrowed", "date"))?,
                due: row.get("due")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("due", "date"))?,
                returned: Some(
                    row.get("returned")?
                        .and_then(Concept::try_get_date)
                        .ok_or_else(|| AppError::column("returned", "date"))?,
                ),
                lost: row.get("lost")?
                    .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("lost", "date")))
                    .transpose()?,
                fee_cents: row.get("fee")?
                    .map(|concept| concept.try_get_integer().ok_or_else(|| AppError::column("fee", "integer")))
                    .transpose()?,
            })
        })
        .collect()
}

/// Loans that have been marked lost.
pub fn lost(db: &Db) -> Result<Vec<Loan>> {
    let rows = db.read_rows(
        "
        match
          $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due,
            has lost $lost;
          $m has member-id $member_id;
          $b has isbn $isbn;
          try { $l has returned $returned; };
          try { $l has fee-cents $fee; };
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(Loan {
                id: row.get("loan_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("loan_id", "string"))?
                    .to_owned(),
                member_id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                borrowed: row.get("borrowed")?
                    .and_then(Concept::try_get_date)
                    .ok_or_else(|| AppError::column("borrowed", "date"))?,
                due: row.get("due")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("due", "date"))?,
                returned: row.get("returned")?
                    .map(|concept| concept.try_get_date().ok_or_else(|| AppError::column("returned", "date")))
                    .transpose()?,
                lost: Some(
                    row.get("lost")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("lost", "date"))?,
                ),
                fee_cents: row.get("fee")?
                    .map(|concept| concept.try_get_integer().ok_or_else(|| AppError::column("fee", "integer")))
                    .transpose()?,
            })
        })
        .collect()
}

/// The borrow date of every loan.
pub fn monthly(db: &Db) -> Result<Vec<NaiveDate>> {
    let rows = db.read_rows(
        "
        match
          $l isa loan, has borrowed $borrowed;
        ",
    )?;
    rows.iter()
        .map(|row| {
            row.get("borrowed")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("borrowed", "date"))
        })
        .collect()
}
