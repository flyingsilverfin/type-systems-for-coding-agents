use chrono::NaiveDate;

#[derive(Debug, Clone, PartialEq)]
pub struct Member {
    pub id: String,
    pub name: String,
    pub email: Option<String>,
    pub phone: Option<String>,
    pub joined: NaiveDate,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Book {
    pub isbn: String,
    pub title: String,
    pub subtitle: Option<String>,
    pub year: i64,
    pub genre: Option<String>,
    pub copies: i64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Loan {
    pub id: String,
    pub member_id: String,
    pub isbn: String,
    pub borrowed: NaiveDate,
    pub due: NaiveDate,
    pub returned: Option<NaiveDate>,
    pub lost: Option<NaiveDate>,
    pub fee_cents: Option<i64>,
}

impl Loan {
    /// A lost loan counts as lost even if it also carries a return date.
    pub fn is_lost(&self) -> bool {
        self.lost.is_some()
    }

    pub fn is_returned(&self) -> bool {
        self.returned.is_some() && !self.is_lost()
    }

    pub fn is_open(&self) -> bool {
        !self.is_returned() && !self.is_lost()
    }
}
