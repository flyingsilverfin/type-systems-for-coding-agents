# lending

A small command-line tool for a lending library, backed by a TypeDB database.
Members borrow books; each borrowing is a `loan` relation between a `member` (borrower) and a `book` (item).

## Commands and output formats

The output of every command is a contract (scripts parse it), so it must match exactly.

| Command | Output |
|---|---|
| `init` | recreates the database and defines the schema; prints `initialised <db>` |
| `load <dir>` | loads `members.csv`, `books.csv`, `loans.csv`; prints `loaded <m> members, <b> books, <l> loans` |
| `books` | one line per book `<isbn>  <title> (<year>) [<genre or ->]`, sorted by title then isbn |
| `member <id>` | `member <id>: <name>`, `email: <email or ->`, `joined: <date>`, `open loans: <n>`, then `  <loan-id> <isbn> due <date>` per open loan sorted by due date then loan id. Unknown id: `error: no such member: <id>` on stderr, exit 1 |
| `overdue <date>` | open loans due before `<date>`: `<loan-id> <member-id> <isbn> due <due> (<n> days)`, sorted by due date then loan id |
| `stats` | `<genre>: <count>` per genre (books without a genre count as `(none)`), sorted by count descending then genre |

Errors are printed as `error: <message>` on stderr with exit status 1.

## Data

CSV files with a header row. Fields may be double-quoted (a quoted field may contain commas; `""` inside a
quoted field is a literal `"`). An empty field means "no value". Columns:

- `members.csv`: `member_id,name,email,phone,joined`
- `books.csv`: `isbn,title,subtitle,year,genre,copies`
- `loans.csv`: `loan_id,member_id,isbn,borrowed,due,returned,fee_cents,lost`

Not every column is used by the app yet.

## Configuration

Environment variables: `LENDING_ADDRESS` (default `127.0.0.1:1729`), `LENDING_DB` (default `lending`),
`LENDING_USER`, `LENDING_PASSWORD`. `check.sh` sets these for you.

## Code layout
