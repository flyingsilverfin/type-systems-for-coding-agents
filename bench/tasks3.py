"""v3 tasks: changes whose correct implementation differs per command (no single sweeping fix)."""
from __future__ import annotations

import json
from pathlib import Path

from tasks2 import V2_HIDDEN, V2_VISIBLE

EXTRA = json.loads((Path(__file__).parent / "hidden3_extra.json").read_text())
TASKS3: dict[str, dict] = {}


def task3(tid, family, title, spec, visible=(), structural=()):
    TASKS3[tid] = dict(id=tid, family=family, title=title, spec=spec.strip() + "\n", oracle=tid,
                       visible=V2_VISIBLE + list(visible), hidden=V2_HIDDEN + EXTRA[tid], structural=list(structural),
                       driver=None)


task3("A", "optionality", "Books without a publication year", """
New stock includes books whose publication year is unknown: in `books.csv` the `year` field can now be empty.
`year` becomes an optional attribute (the app must load such books; today it rejects them).

Required behaviour:
- `books`, `genre`, `never-borrowed`, `search`: a book without a year prints `(n.d.)` where the year would be,
  e.g. `9785538829718  The Orchard Winter (n.d.) [biography]`. Sorting does not change.
- `book <isbn>`: prints `year: -`.
- `decades`: books without a year are not in any decade; after the decade lines print one more line
  `unknown: <n>` with the number of such books (omit the line when there are none).
- Books without a year must still appear and be counted **everywhere** they did before (every listing, `stats`,
  `available`, `book`, loans of those books in every loan/member command, ...). Nothing else changes.
""", visible=[["book", "9785538829718"]])

task3("B", "optionality", "Open-ended loans", """
Staff can now take open-ended loans that have no due date: in `loans.csv` the `due` field can be empty, for open
and for returned loans. `due` becomes optional (today the app rejects such loans).

Required behaviour:
- `member <id>`: open loans without a due date are counted as open and listed **after** the dated ones (which
  keep their order), sorted by loan id, as `  <loan-id> <isbn> due -`.
- `history <id>`: prints `due -` for such loans.
- `overdue`, `due-soon`: loans without a due date are never overdue or due soon.
- `returned-late`: loans without a due date are never late.
- Everywhere else these loans count exactly like any other loan (open/returned/lost status, `book`,
  `available`, `top`, `monthly`, `fees`, `inactive`, `never-borrowed`, ...). Nothing else changes.
""", visible=[["history", "M001"]])

task3("C", "cardinality", "Books with several genres", """
A book can now have several genres: in `books.csv` the `genre` field may hold several genres separated by `;`
(e.g. `fiction;poetry`). `genre` becomes a multi-valued attribute: store one `genre` attribute per value
(the schema must allow several per book).

Required behaviour (genres of a book are always sorted alphabetically and listed without duplicates):
- `books`, `genre`, `never-borrowed`, `search`: the bracket shows all genres joined by `/`, e.g.
  `[fiction/poetry]`, or `[-]` when there are none. Each book is still listed **once**.
- `book <isbn>`: `genre: fiction, poetry` (joined by `, `), or `genre: -`.
- `stats`: a book counts once under **each** of its genres; books with no genre count under `(none)`.
- `genre <g>`: lists the books that have `<g>` among their genres; `genre (none)` lists books with no genre.
- `decades`: each book is still counted exactly once.
- Nothing else changes; in particular no command may list a book or loan more than once.
""", visible=[["book", "9784069265501"]])

task3("D", "cardinality", "Members with several email addresses", """
A member can now have several email addresses: in `members.csv` the `email` field may hold several addresses
separated by `;`. `email` becomes a multi-valued attribute: store one `email` attribute per address.

Required behaviour (a member's emails are always sorted alphabetically, without duplicates):
- `members`: the email column shows all addresses joined by `,` (no spaces), or `-`.
- `member <id>`: `email: a@x.org, b@x.org` (joined by `, `), or `email: -`.
- `contact <id>`: uses the alphabetically first email; members without email fall back to phone as before.
- Nothing else changes; in particular no command may list a member or loan more than once.
""", visible=[["contact", "M001"]])

task3("E", "semantics", "Loan renewals", """
Loans can now be renewed: `loans.csv` has a new `renewed` column holding the new due date after a renewal
(empty if the loan was never renewed). Store it as an optional `renewed` date attribute on `loan`.
The *effective due date* of a loan is its `renewed` date if it has one, otherwise its `due` date.

Required behaviour:
- `overdue`, `due-soon`: use the effective due date for filtering, ordering, the printed `due <date>` and the
  number of days.
- `returned-late`: a loan is late if it was returned after its effective due date; days late are counted from it.
- `member <id>`: open loans are ordered by effective due date (then loan id) and print `due <effective date>`;
  renewed loans get ` (renewed)` appended to their line.
- `history <id>`: still prints the original `due <date>`; renewed loans additionally print
  ` renewed <date>` right after it (before the status).
- Nothing else changes.
""", visible=[["history", "M001"]])

task3("F", "release-notes", "Release notes 2.3 (six small changes)", """
Implement all six items of the 2.3 release notes. Each is small; they are independent.

1. `top <n>`: ties (same number of loans) are broken by member **name** ascending, then member id.
2. `fees`: only members whose total is **at least 1.00** are listed.
3. `history <id>`: the header becomes `history <id>: <n> loans (<k> open)` where `k` is the number of open loans.
4. `inactive <date>`: members who joined **after** `<date>` are not listed (they are too new to be inactive).
5. `due-soon <date> [<days>]`: `<days>` becomes optional and defaults to 7.
6. `search <text>`: also matches a book whose ISBN is exactly `<text>`.

Update the usage text where a command's arguments changed. Nothing else changes.
""", visible=[["top", "3"]])

task3("G", "release-notes", "Release notes 2.4 (six small changes)", """
Implement all six items of the 2.4 release notes. Each is small; they are independent.

1. `members`: sorted by name, then member id.
2. `lost`: each line gets ` (<n> days after due)` appended: the number of days between the due date and the
   lost date.
3. `book <isbn>`: the `loans:` line becomes `loans: <total> (<r> returned, <l> lost)`.
4. `monthly`: months with no loans between the first and the last month are printed with a count of 0.
5. `available <isbn>`: when 0 copies are available, append ` (next due <date>)` with the earliest due date
   among the book's open loans.
6. `stats`: after the genre lines print `total: <n>`, the number of books.

Nothing else changes.
""", visible=[["lost"]])


INTERACTIONS = {
    "BE": "- A loan's effective due date is its `renewed` date if it has one, otherwise its `due` date. A loan with "
          "neither has no effective due date and follows the open-ended-loan rules.",
    "EG": "- `lost` (days after due) and `available` (next due) use the effective due date.",
    "BG": "- `available` (next due) ignores loans without a due date.",
}


def combo(tid, parts):
    sections = []
    for i, p in enumerate(parts, start=1):
        sections.append(f"## Change {i}: {TASKS3[p]['title']}\n\n{TASKS3[p]['spec']}")
    inter = [txt for key, txt in INTERACTIONS.items() if all(k in parts for k in key)]
    spec = ("Implement **all** of the following changes. They are written as separate sections, but they must all "
            "hold at the same time, including in commands touched by more than one of them.\n\n" + "\n".join(sections))
    if inter:
        spec += "\n## How the changes interact\n\n" + "\n".join(inter) + "\n"
    visible = []
    for p in parts:
        for c in TASKS3[p]["visible"][len(V2_VISIBLE):]:
            if c not in visible:
                visible.append(c)
    TASKS3[tid] = dict(id=tid, family="combined", title=" + ".join(TASKS3[p]["title"] for p in parts) if len(parts) < 7
                       else "Library 3.0 (seven changes)", spec=spec, oracle=tid, visible=V2_VISIBLE + visible,
                       hidden=V2_HIDDEN + EXTRA[tid], structural=[], driver=None)


for _tid in ["AC", "BE", "DF", "CG", "ABCDEFG"]:
    combo(_tid, list(_tid))
