# lending

A command-line tool for a lending library, backed by a TypeDB database.
Members borrow books; each borrowing is a `loan` relation between a `member` (borrower) and a `book` (item).
A loan is *open* until it is returned or lost; a loan with a lost date is lost (even if it also has a return date).

## Commands and output formats

The output of every command is a contract (scripts parse it), so it must match exactly. `-` means "no value".

| Command | Output |
|---|---|
| `init` | recreates the database and defines the schema; prints `initialised <db>` |
| `load <dir>` | loads `members.csv`, `books.csv`, `loans.csv`; prints `loaded <m> members, <b> books, <l> loans` |
| `books` | one line per book `<isbn>  <title> (<year>) [<genre or ->]`, sorted by title then isbn |
| `book <isbn>` | detail: `book <isbn>: <title>`, `subtitle:`, `year:`, `genre:`, `copies:`, `loans:` (all-time), `on loan:` (open), `borrowers:` (member ids with an open loan, sorted, comma separated, or `-`) |
| `members` | `<id> <name> <email or -> <phone or ->`, sorted by id |
| `member <id>` | `member <id>: <name>`, `email:`, `phone:`, `joined:`, `open loans: <n>`, then `  <loan-id> <isbn> due <date>` per open loan (by due date, loan id), then `lost loans: <n>` |
| `history <id>` | `history <id>: <n> loans`, then `  <loan-id> <isbn> borrowed <date> due <date> <status>` with status `open`, `returned <date>` or `lost <date>`, by borrowed date then loan id |
| `overdue <date>` | open loans due before `<date>`: `<loan-id> <member-id> <isbn> due <due> (<n> days)`, by due date then loan id |
| `due-soon <date> <days>` | open loans due between `<date>` and `<date>+<days>` inclusive: `<loan-id> <member-id> <isbn> due <due>` |
| `returned-late` | returned (not lost) loans returned after their due date: `<loan-id> <member-id> returned <date> (<n> days late)`, most days late first, then loan id |
| `lost` | `<loan-id> <member-id> <isbn> lost <date>`, by lost date then loan id |
| `fees` | members with total fees > 0: `<member-id> <name> <pounds>.<pence>`, highest total first, then member id |
| `member-fees <id>` | `fees <id>: <total>`, then `  <loan-id> <amount>` per loan with a fee, by loan id |
| `stats` | `<genre>: <count>` per genre (`(none)` for books without one), by count descending then genre |
| `decades` | `<decade>s: <count>` by publication decade, ascending |
| `genre <genre>` | books in that genre (`(none)` = books without a genre), in `books` format |
| `never-borrowed` | books with no loans at all, in `books` format |
| `available <isbn>` | `<isbn> <title>: <available> of <copies> available` (copies minus open loans, at least 0) |
| `top <n>` | `<rank>. <member-id> <name> (<count> loans)`, most loans first, then member id |
| `search <text>` | books whose title or subtitle contains the text (case-insensitive), in `books` format |
| `monthly` | `<YYYY-MM>: <count>` loans borrowed per month, ascending |
| `inactive <date>` | members with no loan borrowed on or after `<date>`: `<member-id> <name>`, by id |
| `contact <id>` | `<name> <email>`, else `<name> <phone>`, else `<name> (no contact)` |

Unknown member/book ids print `error: no such member: <id>` / `error: no such book: <isbn>` on stderr and exit 1;
other errors print `error: <message>` and exit 1.

## Data

CSV files with a header row. Fields may be double-quoted (a quoted field may contain commas; `""` inside a
quoted field is a literal `"`). An empty field means "no value". Columns:

- `members.csv`: `member_id,name,email,phone,joined,deleted`
- `books.csv`: `isbn,title,subtitle,year,genre,copies`
- `loans.csv`: `loan_id,member_id,isbn,borrowed,due,returned,fee_cents,lost`

Not every column is used by the app yet.

## Configuration

Environment variables: `LENDING_ADDRESS` (default `127.0.0.1:1729`), `LENDING_DB` (default `lending`),
`LENDING_USER`, `LENDING_PASSWORD`. `check.sh` sets these for you.

## Code layout
