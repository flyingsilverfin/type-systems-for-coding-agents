"""Loan-centred queries: history, overdue, due-soon, returned-late, lost, monthly."""
from __future__ import annotations

from datetime import date
from typing import Optional

from lending.db import Db, quote
from lending.errors import DataError
from lending.model import DueLoan, HistoryLoan, LateReturn, LostLoan


def member_history(db: Db, member_id: str) -> Optional[list[HistoryLoan]]:
    """Every loan the member has made, or None if there is no such member."""
    rows = db.read_rows(f"""
        match
          $m isa member, has member-id {quote(member_id)};
          try {{
            $l isa loan, links (borrower: $m, item: $b), has loan-id $lid, has borrowed $bo, has due $du;
            $b has isbn $isbn;
            try {{ $l has returned $re; }};
            try {{ $l has lost $lo; }};
          }};
    """)
    if not rows:
        return None
    loans: list[HistoryLoan] = []
    for row in rows:
        if row.get("l") is None:
            continue  # the member exists but has no loans
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("bo")) is None or (borrowed := concept.try_get_date()) is None:
            raise DataError("$bo: expected a date")
        if (concept := row.get("du")) is None or (due := concept.try_get_date()) is None:
            raise DataError("$du: expected a date")
        returned: Optional[date] = None
        if (concept := row.get("re")) is not None and (returned := concept.try_get_date()) is None:
            raise DataError("$re: expected a date")
        lost: Optional[date] = None
        if (concept := row.get("lo")) is not None and (lost := concept.try_get_date()) is None:
            raise DataError("$lo: expected a date")
        loans.append(
            HistoryLoan(id=loan_id, isbn=isbn, borrowed=borrowed, due=due, returned=returned, lost=lost)
        )
    return loans


def overdue_loans(db: Db, as_of: date) -> list[DueLoan]:
    """Open loans due strictly before `as_of`."""
    rows = db.read_rows(f"""
        match
          $l isa loan, links (borrower: $m, item: $b), has loan-id $lid, has due $du;
          $m has member-id $mid;
          $b has isbn $isbn;
          $du < {as_of.isoformat()};
          not {{ $l has returned $re; }};
          not {{ $l has lost $lo; }};
    """)
    loans: list[DueLoan] = []
    for row in rows:
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("du")) is None or (due := concept.try_get_date()) is None:
            raise DataError("$du: expected a date")
        loans.append(DueLoan(id=loan_id, member_id=member_id, isbn=isbn, due=due))
    return loans


def loans_due_between(db: Db, start: date, end: date) -> list[DueLoan]:
    """Open loans due on or after `start` and on or before `end`."""
    rows = db.read_rows(f"""
        match
          $l isa loan, links (borrower: $m, item: $b), has loan-id $lid, has due $du;
          $m has member-id $mid;
          $b has isbn $isbn;
          $du >= {start.isoformat()};
          $du <= {end.isoformat()};
          not {{ $l has returned $re; }};
          not {{ $l has lost $lo; }};
    """)
    loans: list[DueLoan] = []
    for row in rows:
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("du")) is None or (due := concept.try_get_date()) is None:
            raise DataError("$du: expected a date")
        loans.append(DueLoan(id=loan_id, member_id=member_id, isbn=isbn, due=due))
    return loans


def late_returns(db: Db) -> list[LateReturn]:
    """Returned (and not lost) loans that came back after their due date."""
    rows = db.read_rows("""
        match
          $l isa loan, links (borrower: $m), has loan-id $lid, has due $du, has returned $re;
          $m has member-id $mid;
          $re > $du;
          not { $l has lost $lo; };
    """)
    loans: list[LateReturn] = []
    for row in rows:
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("du")) is None or (due := concept.try_get_date()) is None:
            raise DataError("$du: expected a date")
        if (concept := row.get("re")) is None or (returned := concept.try_get_date()) is None:
            raise DataError("$re: expected a date")
        loans.append(LateReturn(id=loan_id, member_id=member_id, due=due, returned=returned))
    return loans


def lost_loans(db: Db) -> list[LostLoan]:
    rows = db.read_rows("""
        match
          $l isa loan, links (borrower: $m, item: $b), has loan-id $lid, has lost $lo;
          $m has member-id $mid;
          $b has isbn $isbn;
    """)
    loans: list[LostLoan] = []
    for row in rows:
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("lo")) is None or (lost := concept.try_get_date()) is None:
            raise DataError("$lo: expected a date")
        loans.append(LostLoan(id=loan_id, member_id=member_id, isbn=isbn, lost=lost))
    return loans


def borrow_dates(db: Db) -> list[date]:
    """The borrowed date of every loan: one entry per loan."""
    rows = db.read_rows("""
        match
          $l isa loan, has borrowed $bo;
    """)
    dates: list[date] = []
    for row in rows:
        if (concept := row.get("bo")) is None or (borrowed := concept.try_get_date()) is None:
            raise DataError("$bo: expected a date")
        dates.append(borrowed)
    return dates
