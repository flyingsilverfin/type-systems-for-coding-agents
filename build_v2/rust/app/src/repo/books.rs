//! Queries behind the book commands: books, book, stats, decades, genre, never-borrowed, available, search.
use typedb_driver::concept::Concept;

use crate::{
    db::{quote, Db},
    error::{AppError, Result},
    model::{Book, Loan},
};

/// The columns of a book listing line.
#[derive(Debug, Clone, PartialEq)]
pub struct BookSummary {
    pub isbn: String,
    pub title: String,
    pub year: i64,
    pub genre: Option<String>,
}

/// A book's title and number of copies, together with all of its loans.
#[derive(Debug, Clone, PartialEq)]
pub struct Availability {
    pub isbn: String,
    pub title: String,
    pub copies: i64,
    pub loans: Vec<Loan>,
}

pub fn books(db: &Db) -> Result<Vec<BookSummary>> {
    let rows = db.read_rows(
        "
        match
          $b isa book, has isbn $isbn, has title $title, has year $year;
          try { $b has genre $genre; };
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(BookSummary {
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                title: row.get("title")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("title", "string"))?
                    .to_owned(),
                year: row.get("year")?
                    .and_then(Concept::try_get_integer)
                    .ok_or_else(|| AppError::column("year", "integer"))?,
                genre: row.get("genre")?
                    .map(|concept| {
                        concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("genre", "string"))
                    })
                    .transpose()?,
            })
        })
        .collect()
}

/// The book with the given ISBN and all of its loans, or `None` if there is no such book.
pub fn book(db: &Db, isbn: &str) -> Result<Option<(Book, Vec<Loan>)>> {
    let book_rows = db.read_rows(&format!(
        "
        match
          $b isa book, has isbn {}, has title $title, has year $year, has copies $copies;
          try {{ $b has subtitle $subtitle; }};
          try {{ $b has genre $genre; }};
        ",
        quote(isbn)
    ))?;
    let Some(row) = book_rows.first() else { return Ok(None) };
    let book = Book {
        isbn: isbn.to_owned(),
        title: row.get("title")?
            .and_then(Concept::try_get_string)
            .ok_or_else(|| AppError::column("title", "string"))?
            .to_owned(),
        subtitle: row.get("subtitle")?
            .map(|concept| {
                concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("subtitle", "string"))
            })
            .transpose()?,
        year: row.get("year")?.and_then(Concept::try_get_integer).ok_or_else(|| AppError::column("year", "integer"))?,
        genre: row.get("genre")?
            .map(|concept| {
                concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("genre", "string"))
            })
            .transpose()?,
        copies: row.get("copies")?
            .and_then(Concept::try_get_integer)
            .ok_or_else(|| AppError::column("copies", "integer"))?,
    };

    let loan_rows = db.read_rows(&format!(
        "
        match
          $b isa book, has isbn {};
          $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due;
          $m has member-id $member_id;
          try {{ $l has returned $returned; }};
          try {{ $l has lost $lost; }};
          try {{ $l has fee-cents $fee; }};
        ",
        quote(isbn)
    ))?;
    let loans = loan_rows
        .iter()
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
                isbn: isbn.to_owned(),
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
    Ok(Some((book, loans)))
}

/// The genre of every book (`None` for books without one).
pub fn stats(db: &Db) -> Result<Vec<Option<String>>> {
    let rows = db.read_rows(
        "
        match
          $b isa book;
          try { $b has genre $genre; };
        ",
    )?;
    rows.iter()
        .map(|row| {
            row.get("genre")?
                .map(|concept| {
                    concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("genre", "string"))
                })
                .transpose()
        })
        .collect()
}

/// The publication year of every book.
pub fn decades(db: &Db) -> Result<Vec<i64>> {
    let rows = db.read_rows(
        "
        match
          $b isa book, has year $year;
        ",
    )?;
    rows.iter()
        .map(|row| {
            row.get("year")?.and_then(Concept::try_get_integer).ok_or_else(|| AppError::column("year", "integer"))
        })
        .collect()
}

/// Books with the given genre, or books without a genre when `genre` is `None`.
pub fn genre(db: &Db, genre: Option<&str>) -> Result<Vec<BookSummary>> {
    let genre_constraint = match genre {
        Some(name) => format!("$b has genre {};", quote(name)),
        None => "not { $b has genre $genre; };".to_owned(),
    };
    let rows = db.read_rows(&format!(
        "
        match
          $b isa book, has isbn $isbn, has title $title, has year $year;
          {genre_constraint}
        "
    ))?;
    rows.iter()
        .map(|row| {
            Ok(BookSummary {
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                title: row.get("title")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("title", "string"))?
                    .to_owned(),
                year: row.get("year")?
                    .and_then(Concept::try_get_integer)
                    .ok_or_else(|| AppError::column("year", "integer"))?,
                genre: genre.map(str::to_owned),
            })
        })
        .collect()
}

/// Books that have never been lent out.
pub fn never_borrowed(db: &Db) -> Result<Vec<BookSummary>> {
    let rows = db.read_rows(
        "
        match
          $b isa book, has isbn $isbn, has title $title, has year $year;
          try { $b has genre $genre; };
          not { $l isa loan, links (item: $b); };
        ",
    )?;
    rows.iter()
        .map(|row| {
            Ok(BookSummary {
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                title: row.get("title")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("title", "string"))?
                    .to_owned(),
                year: row.get("year")?
                    .and_then(Concept::try_get_integer)
                    .ok_or_else(|| AppError::column("year", "integer"))?,
                genre: row.get("genre")?
                    .map(|concept| {
                        concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("genre", "string"))
                    })
                    .transpose()?,
            })
        })
        .collect()
}

/// The book's title, copies and loans, or `None` if there is no such book.
///
/// `$member_id` sits in its own nested `try` only to work around a TypeDB 3.13.6 server panic
/// (`immediate_executor.rs:611`) that this query otherwise triggers when one member borrowed the same book
/// twice. Every member has an id, so the column is still treated as required below.
pub fn available(db: &Db, isbn: &str) -> Result<Option<Availability>> {
    let rows = db.read_rows(&format!(
        "
        match
          $b isa book, has isbn {}, has title $title, has copies $copies;
          try {{
            $l isa loan, links (borrower: $m, item: $b), has loan-id $loan_id, has borrowed $borrowed, has due $due;
            try {{ $m has member-id $member_id; }};
            try {{ $l has returned $returned; }};
            try {{ $l has lost $lost; }};
            try {{ $l has fee-cents $fee; }};
          }};
        ",
        quote(isbn)
    ))?;
    let Some(first) = rows.first() else { return Ok(None) };
    let title = first
        .get("title")?
        .and_then(Concept::try_get_string)
        .ok_or_else(|| AppError::column("title", "string"))?
        .to_owned();
    let copies =
        first.get("copies")?.and_then(Concept::try_get_integer).ok_or_else(|| AppError::column("copies", "integer"))?;
    let mut loans = Vec::new();
    for row in &rows {
        // A row without a loan id is the single row of a book that has never been lent out.
        let Some(loan_id) = row.get("loan_id")? else { continue };
        loans.push(Loan {
            id: loan_id.try_get_string().ok_or_else(|| AppError::column("loan_id", "string"))?.to_owned(),
            member_id: row.get("member_id")?
                .and_then(Concept::try_get_string)
                .ok_or_else(|| AppError::column("member_id", "string"))?
                .to_owned(),
            isbn: isbn.to_owned(),
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
    Ok(Some(Availability { isbn: isbn.to_owned(), title, copies, loans }))
}

/// Every book's listing columns together with its subtitle; matching happens in the report.
pub fn search(db: &Db) -> Result<Vec<(BookSummary, Option<String>)>> {
    let rows = db.read_rows(
        "
        match
          $b isa book, has isbn $isbn, has title $title, has year $year;
          try { $b has subtitle $subtitle; };
          try { $b has genre $genre; };
        ",
    )?;
    rows.iter()
        .map(|row| {
            let summary = BookSummary {
                isbn: row.get("isbn")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("isbn", "string"))?
                    .to_owned(),
                title: row.get("title")?
                    .and_then(Concept::try_get_string)
                    .ok_or_else(|| AppError::column("title", "string"))?
                    .to_owned(),
                year: row.get("year")?
                    .and_then(Concept::try_get_integer)
                    .ok_or_else(|| AppError::column("year", "integer"))?,
                genre: row.get("genre")?
                    .map(|concept| {
                        concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("genre", "string"))
                    })
                    .transpose()?,
            };
            let subtitle = row.get("subtitle")?
                .map(|concept| {
                    concept.try_get_string().map(str::to_owned).ok_or_else(|| AppError::column("subtitle", "string"))
                })
                .transpose()?;
            Ok((summary, subtitle))
        })
        .collect()
}
