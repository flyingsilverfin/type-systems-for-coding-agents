"""Plain-text output. Output formats are part of the CLI contract: see README.md."""
from __future__ import annotations

from collections import Counter
from datetime import date

from lending.model import Book, Loan, Member

NONE = "-"


def by_due(loans: list[Loan]) -> list[Loan]:
    return sorted(loans, key=lambda loan: (loan.due, loan.id))


def books(items: list[Book]) -> list[str]:
    ordered = sorted(items, key=lambda b: (b.title, b.isbn))
    return [f"{b.isbn}  {b.title} ({b.year}) [{b.genre if b.genre is not None else NONE}]" for b in ordered]


def member(m: Member, loans: list[Loan]) -> list[str]:
    open_loans = by_due([loan for loan in loans if loan.is_open()])
    lines = [
        f"member {m.id}: {m.name}",
        f"email: {m.email if m.email is not None else NONE}",
        f"joined: {m.joined.isoformat()}",
        f"open loans: {len(open_loans)}",
    ]
    lines += [f"  {loan.id} {loan.isbn} due {loan.due.isoformat()}" for loan in open_loans]
    return lines


def overdue(loans: list[Loan], as_of: date) -> list[str]:
    late = by_due([loan for loan in loans if loan.is_open() and loan.due < as_of])
    return [
        f"{loan.id} {loan.member_id} {loan.isbn} due {loan.due.isoformat()} ({(as_of - loan.due).days} days)"
        for loan in late
    ]


def genre_stats(items: list[Book]) -> list[str]:
    counts = Counter(b.genre if b.genre is not None else "(none)" for b in items)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{genre}: {count}" for genre, count in ordered]
