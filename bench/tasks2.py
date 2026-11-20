"""v2 task definitions and case lists."""
from __future__ import annotations

V2_VISIBLE = [
    ["books"], ["book", "9787717592285"], ["members"], ["member", "M001"], ["history", "M003"],
    ["overdue", "2024-05-01"], ["fees"], ["stats"],
]

V2_HIDDEN = [
    ["books"], ["book", "9781000000001"], ["book", "9781000000002"], ["book", "0000"],
    ["members"], ["member", "M001"], ["member", "M005"], ["member", "M900"], ["member", "M003"], ["member", "NOPE"],
    ["history", "M003"], ["history", "M016"], ["history", "M900"],
    ["overdue", "2024-05-01"], ["overdue", "2024-07-15"],
    ["due-soon", "2024-04-20", "14"], ["due-soon", "2024-06-01", "0"],
    ["returned-late"], ["lost"], ["fees"], ["member-fees", "M004"], ["member-fees", "M900"],
    ["stats"], ["decades"], ["genre", "fiction"], ["genre", "(none)"], ["never-borrowed"],
    ["available", "9781000000001"], ["available", "9781000000003"], ["top", "5"], ["top", "x"],
    ["search", "ATLAS"], ["search", "with, commas"], ["monthly"], ["inactive", "2024-05-01"],
    ["contact", "M001"], ["contact", "M005"], ["contact", "M900"], ["contact", "M009"], ["contact", "M002"], ["contact", "XX"],
]


UPGRADE_HOW = """
The driver dependency has already been bumped for you: {where}. Its release notes are in `DRIVER_CHANGELOG.md`
(the full driver source is in `{src}` if you need it). Nothing else about the app's behaviour changes: every
command must produce exactly the same output as before the upgrade.
"""

TASKS2: dict[str, dict] = {}


def task2(tid, family, title, spec, oracle="BASE", visible=(), hidden=(), driver=None, structural=None):
    TASKS2[tid] = dict(id=tid, family=family, title=title, spec=spec.strip() + "\n", oracle=oracle,
                       visible=V2_VISIBLE + list(visible), hidden=V2_HIDDEN + list(hidden), driver=driver,
                       structural=structural or [])


task2("U1", "driver-upgrade", "Upgrade to TypeDB driver 3.13", """
Upgrade the app to TypeDB driver 3.13.0, which has breaking API changes.
{UPGRADE}
""", driver="full")

task2("U2", "driver-upgrade", "Driver 3.13: ConceptRow.get change", """
Upgrade the app to TypeDB driver 3.13.0-rc1, which changes how `ConceptRow.get` treats empty columns.
{UPGRADE}
""", driver="get")

task2("U3", "driver-upgrade", "Driver 3.13: Date values", """
Upgrade the app to TypeDB driver 3.13.0-rc2, which changes the type of date values.
{UPGRADE}
""", driver="date")

task2("R1", "refactor", "Loan status type", """
Loan state is currently modelled with two optional date fields, `returned` and `lost`, on every loan-shaped type
in `model` (plus helper methods). That allows impossible states, and every caller has to remember that "lost
wins over returned".

Replace the two optional fields with a single `status` field whose type has exactly three cases: open; returned
(with the return date); lost (with the lost date). Use the idiomatic representation in this language for
"exactly one of these cases" (an enum with data in Rust; in Python e.g. a union of small frozen dataclasses).
Do this for **every** type that currently has optional `returned`/`lost` fields, and update every place that
built or read them (repository, ingest, reports, helpers). Behaviour of every command must stay exactly the same.
""", structural=["no_loan_returned_field"])

task2("R2", "refactor", "Integer member ids", """
Member ids are moving to a numeric key. In the database, `member-id` must become an **integer** attribute:
`M001` in the CSV is stored as `1`, `M042` as `42` (the CSV files do not change: ids there are always `M`
followed by digits).

Everything user-facing stays exactly the same: commands still take ids like `M001` as arguments, and every
output still prints ids in the `M` + three digits form (e.g. `M007`). An argument that is not `M` followed by
digits is treated as an unknown member (`error: no such member: <arg>`, exit 1).
Update the schema, the loader, every query that uses member ids, and every place that prints them.
""", structural=["member_id_integer"])

task2("C1", "control", "Rename the name attribute", """
The data team is standardising attribute names: the `name` attribute (owned by `member`) must be renamed
`full-name` in the schema, and every query must use the new name. The CSV column stays `name`.
No command's output changes.
""", structural=["name_renamed"])

task2("C2", "control", "Soft-deleted members", """
`members.csv` has a `deleted` column (`true` or empty) that the app currently ignores. Deleted members must
still be **stored** (add a `deleted` boolean attribute owned by `member`, set to `true` for deleted members and
absent otherwise), but every command must now behave **as if deleted members, and all of their loans, did not
exist**: they are not listed, `member`/`history`/`contact`/`member-fees` treat their ids as unknown, their loans
are not counted or listed anywhere (overdue, lost, fees, availability, borrowers, never-borrowed, top, monthly, ...).
""", oracle="V2_SOFTDEL", visible=[["members"], ["member", "M002"]], hidden=[["member", "M004"]], structural=["deleted_stored"])

task2("F1", "new-feature", "Borrowers of a book", """
Add a command `borrowers <isbn>` that lists every member who has ever borrowed the book (open, returned or lost
loans), with their number of loans of that book. Example (illustrative data):

    borrowers 9780000000042: 2 members
      M104 Alan Hopper (3 loans)
      M101 Hedy Liskov (1 loans)

Sort by number of loans descending, then member id ascending. A book nobody borrowed prints just the header with
`0 members`. An unknown ISBN prints `error: no such book: <isbn>` to stderr and exits 1. Add it to the usage text.
Every other command stays exactly the same.
""", oracle="V2F1", visible=[["borrowers", "9787717592285"]],
      hidden=[["borrowers", "9781000000001"], ["borrowers", "9789809117487"], ["borrowers", "0000"], ["borrowers", "9784092316010"]])
