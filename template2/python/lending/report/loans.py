"""Output for history, overdue, due-soon, returned-late, lost, monthly."""
from __future__ import annotations

from collections import Counter
from datetime import date

from lending.model import DueLoan, HistoryLoan, LateReturn, LostLoan


def _by_due(loans: list[DueLoan]) -> list[DueLoan]:
    return sorted(loans, key=lambda loan: (loan.due, loan.id))


def _status(loan: HistoryLoan) -> str:
    if loan.lost is not None:
        return f"lost {loan.lost.isoformat()}"
    if loan.returned is not None:
        return f"returned {loan.returned.isoformat()}"
    return "open"


def history(member_id: str, loans: list[HistoryLoan]) -> list[str]:
    ordered = sorted(loans, key=lambda loan: (loan.borrowed, loan.id))
    lines = [f"history {member_id}: {len(ordered)} loans"]
    lines += [
        f"  {loan.id} {loan.isbn} borrowed {loan.borrowed.isoformat()} due {loan.due.isoformat()} {_status(loan)}"
        for loan in ordered
    ]
    return lines


def overdue(loans: list[DueLoan], as_of: date) -> list[str]:
    return [
        f"{loan.id} {loan.member_id} {loan.isbn} due {loan.due.isoformat()} ({(as_of - loan.due).days} days)"
        for loan in _by_due(loans)
    ]


def due_soon(loans: list[DueLoan]) -> list[str]:
    return [f"{loan.id} {loan.member_id} {loan.isbn} due {loan.due.isoformat()}" for loan in _by_due(loans)]


def returned_late(loans: list[LateReturn]) -> list[str]:
    ranked = sorted(loans, key=lambda loan: (-(loan.returned - loan.due).days, loan.id))
    return [
        f"{loan.id} {loan.member_id} returned {loan.returned.isoformat()} ({(loan.returned - loan.due).days} days late)"
        for loan in ranked
    ]


def lost(loans: list[LostLoan]) -> list[str]:
    ordered = sorted(loans, key=lambda loan: (loan.lost, loan.id))
    return [f"{loan.id} {loan.member_id} {loan.isbn} lost {loan.lost.isoformat()}" for loan in ordered]


def monthly(borrowed: list[date]) -> list[str]:
    counts = Counter(f"{day.year:04d}-{day.month:02d}" for day in borrowed)
    return [f"{month}: {count}" for month, count in sorted(counts.items())]
