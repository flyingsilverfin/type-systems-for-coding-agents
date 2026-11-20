pub const SCHEMA: &str = r#"
define
  attribute member-id, value string;
  attribute name, value string;
  attribute email, value string;
  attribute joined, value date;

  attribute isbn, value string;
  attribute title, value string;
  attribute year, value integer;
  attribute genre, value string;
  attribute copies, value integer;

  attribute loan-id, value string;
  attribute borrowed, value date;
  attribute due, value date;
  attribute returned, value date;

  entity member,
    owns member-id @key,
    owns name,
    owns email,
    owns joined,
    plays loan:borrower;

  entity book,
    owns isbn @key,
    owns title,
    owns year,
    owns genre,
    owns copies,
    plays loan:item;

  relation loan,
    relates borrower,
    relates item,
    owns loan-id @key,
    owns borrowed,
    owns due,
    owns returned;
"#;
