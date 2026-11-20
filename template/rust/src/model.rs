use chrono::NaiveDate;

#[derive(Debug, Clone, PartialEq)]
pub struct Member {
    pub id: String,
    pub name: String,
    pub email: Option<String>,
    pub joined: NaiveDate,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Book {
    pub isbn: String,
    pub title: String,
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
}

impl Loan {
    pub fn is_open(&self) -> bool {
        self.returned.is_none()
    }
}
