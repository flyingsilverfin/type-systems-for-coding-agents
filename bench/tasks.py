"""Task definitions. Specs are language-neutral; `inject` holds per-language bug injections for control tasks."""
from __future__ import annotations

BASE_HIDDEN = [
    ["books"],
    ["member", "M001"],
    ["member", "M002"],
    ["member", "M017"],
    ["member", "M900"],
    ["member", "NOPE"],
    ["overdue", "2024-05-01"],
    ["overdue", "2024-03-15"],
    ["stats"],
]

BASE_VISIBLE = [
    ["books"],
    ["member", "M001"],
    ["overdue", "2024-05-01"],
    ["stats"],
]

TASKS: dict[str, dict] = {}


def task(tid, family, title, spec, visible=(), hidden=(), inject=None, base_visible=True):
    TASKS[tid] = dict(
        id=tid, family=family, title=title, spec=spec.strip() + "\n",
        visible=(BASE_VISIBLE if base_visible else []) + list(visible),
        hidden=BASE_HIDDEN + list(hidden),
        inject=inject or {},
    )


task("T01", "optional-field", "Show member phone numbers", """
`members.csv` has a `phone` column that the app currently ignores. Phone numbers are optional: the column
may be empty for some members.

1. Store it: add a `phone` attribute (string) to the schema, owned by `member`, and load it from `members.csv`
   (skip it when the field is empty).
2. Show it: the `member` command prints a new line `phone: <phone>` directly after the `email:` line.
   Members without a phone number print `phone: -`.

Everything else about the `member` output, and every other command, stays exactly the same.
""", visible=[["member", "M003"]], hidden=[["member", "M005"], ["member", "M009"], ["member", "M011"], ["member", "M023"]])

task("T02", "optional-field", "Show book subtitles", """
`books.csv` has a `subtitle` column that the app currently ignores. Subtitles are optional.

1. Store it: add a `subtitle` attribute (string) owned by `book`, loaded from `books.csv` (skip when empty).
2. Show it: in the `books` command, when a book has a subtitle, print the title as `<title>: <subtitle>`.
   Books without a subtitle print exactly as today.
   The sort order does not change: by title (without the subtitle), then ISBN.

Example line: `9784069265501  The Stone Northern: A atlas story (1955) [travel]`.
Every other command stays exactly the same.
""", visible=[], hidden=[])

task("T03", "optional-field", "Loan history command", """
Add a new command `history <member-id>` that lists *all* loans of a member (open and returned).

Output: a header line `history <member-id>: <n> loans` followed by one line per loan, sorted by borrowed
date, then loan id. Example (illustrative data):

    history M042: 2 loans
      L0103 9787717592285 borrowed 2024-01-16 due 2024-01-30 returned open
      L0110 9787717592285 borrowed 2024-04-12 due 2024-04-26 returned 2024-04-26

The `returned` value is the return date, or the word `open` if the loan has not been returned.
A member with no loans prints just the header with `0 loans`.
An unknown member id prints `error: no such member: <id>` to stderr and exits with status 1 (like `member`).
Also add the command to the usage text. Every other command stays exactly the same.
""", visible=[["history", "M001"]], hidden=[["history", "M001"], ["history", "M007"], ["history", "M900"], ["history", "NOPE"], ["history", "M031"]])

task("T04", "new-feature", "Outstanding fees report", """
`loans.csv` has a `fee_cents` column (an integer number of pence) that the app currently ignores.
The field is optional: empty means no fee.

1. Store it: add a `fee-cents` attribute (integer) owned by `loan`, loaded from `loans.csv` (skip when empty).
2. Add a command `fees` that prints, for every member whose total fees across all their loans is greater
   than zero, one line `<member-id> <member name> <pounds>.<pence>` where pence is always two digits
   (e.g. 1234 cents prints `12.34`, 5 cents prints `0.05`, 300 cents prints `3.00`).
   Sort by total descending, then member id ascending. Members with no fees are not printed.

Also add the command to the usage text. Every other command stays exactly the same.
""", visible=[["fees"]], hidden=[["fees"]])

task("T05", "new-feature", "Book availability command", """
Add a command `available <isbn>` that prints one line:

    <isbn> <title>: <available> of <copies> available

where `available` is the number of copies minus the number of *open* (not returned) loans of that book,
never less than 0. An unknown ISBN prints `error: no such book: <isbn>` to stderr and exits with status 1.

Also add the command to the usage text. Every other command stays exactly the same.
""", visible=[["available", "9787717592285"]], hidden=[["available", "9781000000001"], ["available", "9781000000003"], ["available", "0000"]])

task("T06", "new-feature", "Top borrowers command", """
Add a command `top <n>` that ranks members by their total number of loans (open and returned), and prints
the first `n`. Example (illustrative data):

    1. M104 Alan Hopper (5 loans)
    2. M101 Hedy Liskov (4 loans)
    3. M107 Tony Turing (4 loans)

Sort by number of loans descending, then member id ascending. Ranks are 1, 2, 3, ... in printed order. Members with no loans are never printed.
If fewer than `n` members have loans, print all of them. A non-integer or negative `n` prints
`error: <anything>` to stderr and exits with status 1.

Also add the command to the usage text. Every other command stays exactly the same.
""", visible=[["top", "3"]], hidden=[["top", "5"], ["top", "100"], ["top", "x"]])

task("T07", "refactor", "Lost loans", """
Loans can now be marked as lost. `loans.csv` has a `lost` column (a date, optional) that the app currently
ignores. A loan with a lost date is *lost*: it is neither open nor returned.

1. Store it: add a `lost` attribute (date) owned by `loan`, loaded from `loans.csv` (skip when empty).
2. Replace the loan's `returned` field in the domain model with a single status that is exactly one of:
   open; returned (with its return date); lost (with its lost date). Use whatever representation is
   idiomatic in this language for "exactly one of these cases", and update every use of the old field.
3. Behaviour changes:
   - `member`: lost loans are not counted or listed as open loans. After the open-loan lines, print one more
     line `lost loans: <n>` (always printed, `lost loans: 0` if none).
   - `overdue`: lost loans are never overdue.
   Everything else stays exactly the same.
""", visible=[["member", "M003"]], hidden=[["member", "M003"], ["member", "M035"], ["member", "M016"], ["member", "M024"], ["member", "M012"], ["overdue", "2024-06-30"], ["overdue", "2024-09-01"]])

task("T08", "refactor", "Stream query results", """
`Db.read_rows` loads every row of a query into memory before returning. Replace it with a streaming API:
rows must be handed to the caller one at a time while the transaction is still open, rather than being
collected into a list/Vec first. Pick the design that is idiomatic in this language (an iterator, a
generator, a callback per row, a visitor, ...).

- Remove `read_rows` completely and port every caller in the repository layer to the new API.
- Results of every command must stay exactly the same.
- The read transaction must be closed after the rows are consumed (or the stream is dropped / exhausted).
""", visible=[], hidden=[])

task("T09", "new-feature", "Search books", """
Add a command `search <text>` that lists books whose title *or subtitle* contains `<text>`
(case-insensitive substring match). `books.csv` has a `subtitle` column (optional) that the app currently
ignores; add a `subtitle` attribute (string) owned by `book` and load it (skip when empty).

Matching books are printed in exactly the same format and order as the `books` command (which does not
show subtitles). No matches prints nothing and exits with status 0.
Also add the command to the usage text. Every other command stays exactly the same.
""", visible=[["search", "atlas"]], hidden=[["search", "ATLAS"], ["search", "story"], ["search", "hello"], ["search", "zzz"], ["search", "with, commas"]])

task("T10", "control", "Bug: overdue on the due date", """
Bug report from the front desk:

> `overdue 2024-05-02` lists L0008, which is due on 2024-05-02, as `(0 days)` overdue.
> A loan is only overdue *after* its due date: a loan due on the date given should not be listed.

Fix the bug. Nothing else should change.
""", visible=[["overdue", "2024-05-02"]], hidden=[["overdue", "2024-05-01"]],
     inject={
         "python": [("lending/report.py", "loan.is_open() and loan.due < as_of", "loan.is_open() and loan.due <= as_of")],
         "rust": [("src/report.rs", "loan.is_open() && loan.due < as_of", "loan.is_open() && loan.due <= as_of")],
     })

task("T11", "control", "Bug: wrong days overdue", """
Bug report:

> In `overdue 2024-05-01` the number of days looks wrong: L0003 was due 2024-01-30 and shows
> `(106 days)`, but it is 92 days overdue on 2024-05-01.

Fix the bug. Nothing else should change.
""", visible=[], hidden=[["overdue", "2024-07-01"]],
     inject={
         "python": [("lending/report.py", "({(as_of - loan.due).days} days)", "({(as_of - loan.borrowed).days} days)")],
         "rust": [("src/report.rs", "(as_of - loan.due).num_days()", "(as_of - loan.borrowed).num_days()")],
     })

task("T12", "control", "Bug: quotes in CSV fields", """
Bug report:

> Since we added the book `Say "Hello", World`, the `books` command shows its title as
> `Say Hello, World`: the quotes are lost. In the CSV the title is written as `"Say ""Hello"", World"`
> (a quoted field, with `""` meaning a literal double quote), which is standard CSV.

Fix the CSV reader so that `""` inside a quoted field is read as one `"` character. Nothing else should change.
""", visible=[], hidden=[],
     inject={
         "python": [("lending/csvio.py", """            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    current.append('"')
                    i += 1
                else:
                    in_quotes = False""", """            if ch == '"':
                in_quotes = False""")],
         "rust": [("src/csv.rs", """            if ch == '"' {
                if chars.peek() == Some(&'"') {
                    current.push('"');
                    chars.next();
                } else {
                    in_quotes = false;
                }
            } else {""", """            if ch == '"' {
                in_quotes = false;
            } else {""")],
     })
