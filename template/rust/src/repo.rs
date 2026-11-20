//! Read-side queries. Every function returns domain objects from crate::model.
use crate::{
    db::{date, quote, req_date, req_integer, req_string, string, Db},
    error::Result,
    model::{Book, Loan, Member},
};

const LOAN_PATTERN: &str = "
  $l isa loan, links (borrower: $m, item: $b),
    has loan-id $lid, has borrowed $bo, has due $du;
  $m has member-id $mid;
  $b has isbn $isbn;
  try { $l has returned $re; };
";

pub fn list_books(db: &Db) -> Result<Vec<Book>> {
    let rows = db.read_rows(
        "
        match
          $b isa book, has isbn $isbn, has title $t, has year $y, has copies $c;
          try { $b has genre $g; };
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(Book {
                isbn: req_string(row, "isbn")?,
                title: req_string(row, "t")?,
                year: req_integer(row, "y")?,
                genre: string(row, "g")?,
                copies: req_integer(row, "c")?,
            })
        })
        .collect()
}

pub fn find_member(db: &Db, member_id: &str) -> Result<Option<Member>> {
    let rows = db.read_rows(&format!(
        "
        match
          $m isa member, has member-id {}, has name $n, has joined $j;
          try {{ $m has email $e; }};
        ",
        quote(member_id)
    ))?;
    let Some(row) = rows.first() else { return Ok(None) };
    Ok(Some(Member {
        id: member_id.to_owned(),
        name: req_string(row, "n")?,
        email: string(row, "e")?,
        joined: req_date(row, "j")?,
    }))
}

fn loans(db: &Db, extra: &str) -> Result<Vec<Loan>> {
    let rows = db.read_rows(&format!("match{LOAN_PATTERN}{extra}"))?;
    rows.iter()
        .map(|row| {
            Ok(Loan {
                id: req_string(row, "lid")?,
                member_id: req_string(row, "mid")?,
                isbn: req_string(row, "isbn")?,
                borrowed: req_date(row, "bo")?,
                due: req_date(row, "du")?,
                returned: date(row, "re")?,
            })
        })
        .collect()
}

pub fn loans_for_member(db: &Db, member_id: &str) -> Result<Vec<Loan>> {
    loans(db, &format!("  $m has member-id {};\n", quote(member_id)))
}

pub fn all_loans(db: &Db) -> Result<Vec<Loan>> {
    loans(db, "")
}
