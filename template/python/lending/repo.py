"""Read-side queries. Every function returns domain objects from lending.model."""
from __future__ import annotations

from typing import Optional

from lending.db import Db, date_value, quote, req_date, req_integer, req_string, string
from lending.model import Book, Loan, Member

_LOAN_PATTERN = """
  $l isa loan, links (borrower: $m, item: $b),
    has loan-id $lid, has borrowed $bo, has due $du;
  $m has member-id $mid;
  $b has isbn $isbn;
  try { $l has returned $re; };
"""


def list_books(db: Db) -> list[Book]:
    rows = db.read_rows("""
        match
          $b isa book, has isbn $isbn, has title $t, has year $y, has copies $c;
          try { $b has genre $g; };
    """)
    return [
        Book(
            isbn=req_string(row, "isbn"),
            title=req_string(row, "t"),
            year=req_integer(row, "y"),
            genre=string(row, "g"),
            copies=req_integer(row, "c"),
        )
        for row in rows
    ]


def find_member(db: Db, member_id: str) -> Optional[Member]:
    rows = db.read_rows(f"""
        match
          $m isa member, has member-id {quote(member_id)}, has name $n, has joined $j;
          try {{ $m has email $e; }};
    """)
    if not rows:
        return None
    row = rows[0]
    return Member(
        id=member_id,
        name=req_string(row, "n"),
        email=string(row, "e"),
        joined=req_date(row, "j"),
    )


def _loans(db: Db, extra: str = "") -> list[Loan]:
    rows = db.read_rows("match" + _LOAN_PATTERN + extra)
    return [
        Loan(
            id=req_string(row, "lid"),
            member_id=req_string(row, "mid"),
            isbn=req_string(row, "isbn"),
            borrowed=req_date(row, "bo"),
            due=req_date(row, "du"),
            returned=date_value(row, "re"),
        )
        for row in rows
    ]


def loans_for_member(db: Db, member_id: str) -> list[Loan]:
    return _loans(db, f"  $m has member-id {quote(member_id)};\n")


def all_loans(db: Db) -> list[Loan]:
    return _loans(db)
