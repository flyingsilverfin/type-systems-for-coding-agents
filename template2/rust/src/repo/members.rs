//! Queries behind the member commands: members, member, contact, inactive, top, fees, member-fees.
use chrono::NaiveDate;
use typedb_driver::concept::Concept;

use crate::{
    db::{quote, Db},
    error::{AppError, Result},
    model::{Loan, Member},
};

/// A member's id, name and contact details.
#[derive(Debug, Clone, PartialEq)]
pub struct MemberSummary {
    pub id: String,
    pub name: String,
    pub email: Option<String>,
    pub phone: Option<String>,
}

/// A member's id and name.
#[derive(Debug, Clone, PartialEq)]
pub struct MemberName {
    pub id: String,
    pub name: String,
}

/// The fee charged on one loan, with the borrower's id and name.
#[derive(Debug, Clone, PartialEq)]
pub struct MemberFee {
    pub member_id: String,
    pub name: String,
    pub fee_cents: i64,
}

pub fn members(db: &Db) -> Result<Vec<MemberSummary>> {
    let rows = db.read_rows(
        "
        match
          $m isa member, has member-id $member_id, has name $name;
          try { $m has email $email; };
          try { $m has phone $phone; };
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(MemberSummary {
                id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                name: row.get("name")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("name", "string"))?
                    .to_owned(),
                email: row.get("email")?
                    .map(|concept| {
                        concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("email", "string"))
                    })
                    .transpose()?,
                phone: row.get("phone")?
                    .map(|concept| {
                        concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("phone", "string"))
                    })
                    .transpose()?,
            })
        })
        .collect()
}

/// The member with the given id and all of their loans, or `None` if there is no such member.
pub fn member(db: &Db, member_id: &str) -> Result<Option<(Member, Vec<Loan>)>> {
    let member_rows = db.read_rows(&format!(
        "
        match
          $m isa member, has member-id {}, has name $name, has joined $joined;
          try {{ $m has email $email; }};
          try {{ $m has phone $phone; }};
        ",
        quote(member_id)
    ))?;
    let Some(row) = member_rows.first() else { return Ok(None) };
    let member = Member {
        id: member_id.to_owned(),
        name: row.get("name")?
            .and_then(Concept::try_get_string)
            .ok_or_else(|| AppError::column("name", "string"))?
            .to_owned(),
        email: row.get("email")?
            .map(|concept| {
                concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("email", "string"))
            })
            .transpose()?,
        phone: row.get("phone")?
            .map(|concept| {
                concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("phone", "string"))
            })
            .transpose()?,
        joined: row.get("joined")?.and_then(Concept::try_get_date).ok_or_else(|| AppError::column("joined", "date"))?,
    };

    let loan_rows = db.read_rows(&format!(
        "
        match
          $m isa member, has member-id {};
          $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due;
          $b has isbn $isbn;
          try {{ $l has returned $returned; }};
          try {{ $l has lost $lost; }};
          try {{ $l has fee-cents $fee; }};
        ",
        quote(member_id)
    ))?;
    let loans = loan_rows
        .iter()
        .map(|row| {
            Ok(Loan {
                id: row.get("loan_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("loan_id", "string"))?
                    .to_owned(),
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
            })
        })
        .collect::<Result<Vec<_>>>()?;
    Ok(Some((member, loans)))
}

/// The member's name and contact details, or `None` if there is no such member.
pub fn contact(db: &Db, member_id: &str) -> Result<Option<MemberSummary>> {
    let rows = db.read_rows(&format!(
        "
        match
          $m isa member, has member-id {}, has name $name;
          try {{ $m has email $email; }};
          try {{ $m has phone $phone; }};
        ",
        quote(member_id)
    ))?;
    let Some(row) = rows.first() else { return Ok(None) };
    Ok(Some(MemberSummary {
        id: member_id.to_owned(),
        name: row.get("name")?
            .and_then(Concept::try_get_string)
            .ok_or_else(|| AppError::column("name", "string"))?
            .to_owned(),
        email: row.get("email")?
            .map(|concept| {
                concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("email", "string"))
            })
            .transpose()?,
        phone: row.get("phone")?
            .map(|concept| {
                concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("phone", "string"))
            })
            .transpose()?,
    }))
}

/// Members who have not borrowed anything on or after `since`.
pub fn inactive(db: &Db, since: NaiveDate) -> Result<Vec<MemberName>> {
    let rows = db.read_rows(&format!(
        "
        match
          $m isa member, has member-id $member_id, has name $name;
          not {{ $l isa loan, links (borrower: $m), has borrowed $borrowed; $borrowed >= {since}; }};
        "
    ))?;
    rows.iter()
        .map(|row| {
            Ok(MemberName {
                id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                name: row.get("name")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("name", "string"))?
                    .to_owned(),
            })
        })
        .collect()
}

/// The borrower of every loan: one entry per loan.
pub fn top(db: &Db) -> Result<Vec<MemberName>> {
    let rows = db.read_rows(
        "
        match
          $l isa loan, links (borrower: $m);
          $m has member-id $member_id, has name $name;
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(MemberName {
                id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                name: row.get("name")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("name", "string"))?
                    .to_owned(),
            })
        })
        .collect()
}

/// The fee of every loan that carries one, with its borrower.
pub fn fees(db: &Db) -> Result<Vec<MemberFee>> {
    let rows = db.read_rows(
        "
        match
          $l isa loan, links (borrower: $m), has fee-cents $fee;
          $m has member-id $member_id, has name $name;
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(MemberFee {
                member_id: row.get("member_id")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("member_id", "string"))?
                    .to_owned(),
                name: row.get("name")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("name", "string"))?
                    .to_owned(),
                fee_cents: row.get("fee")?
                    .and_then(Concept::try_get_integer)
                    .ok_or_else(|| AppError::column("fee", "integer"))?,
            })
        })
        .collect()
}

/// All loans of the given member, or `None` if there is no such member.
pub fn member_fees(db: &Db, member_id: &str) -> Result<Option<Vec<Loan>>> {
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
