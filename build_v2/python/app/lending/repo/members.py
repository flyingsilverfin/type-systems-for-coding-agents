"""Member-centred queries: members, member, contact, inactive, top, fees, member-fees."""
from __future__ import annotations

from datetime import date
from typing import Optional

from lending.db import Db, quote
from lending.errors import DataError
from lending.model import LoanFee, Member, MemberContact, MemberFee, MemberLoan, MemberName


def list_members(db: Db) -> list[MemberContact]:
    rows = db.read_rows("""
        match
          $m isa member, has member-id $mid, has name $n;
          try { $m has email $e; };
          try { $m has phone $p; };
    """)
    members: list[MemberContact] = []
    for row in rows:
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("n")) is None or (name := concept.try_get_string()) is None:
            raise DataError("$n: expected a string")
        email: Optional[str] = None
        if (concept := row.get("e")) is not None and (email := concept.try_get_string()) is None:
            raise DataError("$e: expected a string")
        phone: Optional[str] = None
        if (concept := row.get("p")) is not None and (phone := concept.try_get_string()) is None:
            raise DataError("$p: expected a string")
        members.append(MemberContact(id=member_id, name=name, email=email, phone=phone))
    return members


def find_member(db: Db, member_id: str) -> Optional[Member]:
    rows = db.read_rows(f"""
        match
          $m isa member, has member-id {quote(member_id)}, has name $n, has joined $j;
          try {{ $m has email $e; }};
          try {{ $m has phone $p; }};
    """)
    if not rows:
        return None
    row = rows[0]
    if (concept := row.get("n")) is None or (name := concept.try_get_string()) is None:
        raise DataError("$n: expected a string")
    if (concept := row.get("j")) is None or (joined := concept.try_get_date()) is None:
        raise DataError("$j: expected a date")
    email: Optional[str] = None
    if (concept := row.get("e")) is not None and (email := concept.try_get_string()) is None:
        raise DataError("$e: expected a string")
    phone: Optional[str] = None
    if (concept := row.get("p")) is not None and (phone := concept.try_get_string()) is None:
        raise DataError("$p: expected a string")
    return Member(id=member_id, name=name, email=email, phone=phone, joined=joined)


def member_loans(db: Db, member_id: str) -> list[MemberLoan]:
    rows = db.read_rows(f"""
        match
          $m isa member, has member-id {quote(member_id)};
          $l isa loan, links (borrower: $m, item: $b), has loan-id $lid, has due $du;
          $b has isbn $isbn;
          try {{ $l has returned $re; }};
          try {{ $l has lost $lo; }};
    """)
    loans: list[MemberLoan] = []
    for row in rows:
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("du")) is None or (due := concept.try_get_date()) is None:
            raise DataError("$du: expected a date")
        returned: Optional[date] = None
        if (concept := row.get("re")) is not None and (returned := concept.try_get_date()) is None:
            raise DataError("$re: expected a date")
        lost: Optional[date] = None
        if (concept := row.get("lo")) is not None and (lost := concept.try_get_date()) is None:
            raise DataError("$lo: expected a date")
        loans.append(MemberLoan(id=loan_id, isbn=isbn, due=due, returned=returned, lost=lost))
    return loans


def find_contact(db: Db, member_id: str) -> Optional[MemberContact]:
    rows = db.read_rows(f"""
        match
          $m isa member, has member-id {quote(member_id)}, has name $n;
          try {{ $m has email $e; }};
          try {{ $m has phone $p; }};
    """)
    if not rows:
        return None
    row = rows[0]
    if (concept := row.get("n")) is None or (name := concept.try_get_string()) is None:
        raise DataError("$n: expected a string")
    email: Optional[str] = None
    if (concept := row.get("e")) is not None and (email := concept.try_get_string()) is None:
        raise DataError("$e: expected a string")
    phone: Optional[str] = None
    if (concept := row.get("p")) is not None and (phone := concept.try_get_string()) is None:
        raise DataError("$p: expected a string")
    return MemberContact(id=member_id, name=name, email=email, phone=phone)


def inactive_members(db: Db, since: date) -> list[MemberName]:
    """Members with no loan borrowed on or after `since`."""
    rows = db.read_rows(f"""
        match
          $m isa member, has member-id $mid, has name $n;
          not {{
            $l isa loan, links (borrower: $m), has borrowed $bo;
            $bo >= {since.isoformat()};
          }};
    """)
    members: list[MemberName] = []
    for row in rows:
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("n")) is None or (name := concept.try_get_string()) is None:
            raise DataError("$n: expected a string")
        members.append(MemberName(id=member_id, name=name))
    return members


def loan_borrowers(db: Db) -> list[MemberName]:
    """The borrower of every loan: one row per loan, for ranking members by loan count."""
    rows = db.read_rows("""
        match
          $l isa loan, links (borrower: $m);
          $m has member-id $mid, has name $n;
    """)
    borrowers: list[MemberName] = []
    for row in rows:
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("n")) is None or (name := concept.try_get_string()) is None:
            raise DataError("$n: expected a string")
        borrowers.append(MemberName(id=member_id, name=name))
    return borrowers


def loan_fees(db: Db) -> list[MemberFee]:
    """Every loan that carries a fee, with its borrower."""
    rows = db.read_rows("""
        match
          $l isa loan, links (borrower: $m), has fee-cents $f;
          $m has member-id $mid, has name $n;
    """)
    fees: list[MemberFee] = []
    for row in rows:
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        if (concept := row.get("n")) is None or (name := concept.try_get_string()) is None:
            raise DataError("$n: expected a string")
        if (concept := row.get("f")) is None or (fee_cents := concept.try_get_integer()) is None:
            raise DataError("$f: expected an integer")
        fees.append(MemberFee(member_id=member_id, name=name, fee_cents=fee_cents))
    return fees


def member_fees(db: Db, member_id: str) -> Optional[list[LoanFee]]:
    """The member's loans that carry a fee, or None if there is no such member."""
    rows = db.read_rows(f"""
        match
          $m isa member, has member-id {quote(member_id)};
          try {{ $l isa loan, links (borrower: $m), has loan-id $lid, has fee-cents $f; }};
    """)
    if not rows:
        return None
    fees: list[LoanFee] = []
    for row in rows:
        if row.get("l") is None:
            continue  # the member exists but has no loan with a fee
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        if (concept := row.get("f")) is None or (fee_cents := concept.try_get_integer()) is None:
            raise DataError("$f: expected an integer")
        fees.append(LoanFee(loan_id=loan_id, fee_cents=fee_cents))
    return fees
