"""Domain types, plus the small purpose-built row types returned by the repository functions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol


class _HasLoanEnd(Protocol):
    @property
    def returned(self) -> Optional[date]: ...

    @property
    def lost(self) -> Optional[date]: ...


class LoanStatus:
    """Status predicates for any loan-shaped type with `returned` and `lost` dates. Lost wins over returned."""

    def is_lost(self: _HasLoanEnd) -> bool:
        return self.lost is not None

    def is_returned(self: _HasLoanEnd) -> bool:
        return self.returned is not None and self.lost is None

    def is_open(self: _HasLoanEnd) -> bool:
        return self.returned is None and self.lost is None


# Domain types.

@dataclass(frozen=True)
class Member:
    id: str
    name: str
    email: Optional[str]
    phone: Optional[str]
    joined: date


@dataclass(frozen=True)
class Book:
    isbn: str
    title: str
    subtitle: Optional[str]
    year: int
    genre: Optional[str]
    copies: int


@dataclass(frozen=True)
class Loan(LoanStatus):
    id: str
    member_id: str
    isbn: str
    borrowed: date
    due: date
    returned: Optional[date]
    lost: Optional[date]
    fee_cents: Optional[int]


# Row types: each holds just the columns one command needs.

@dataclass(frozen=True)
class MemberContact:
    """members, contact"""
    id: str
    name: str
    email: Optional[str]
    phone: Optional[str]


@dataclass(frozen=True)
class MemberName:
    """inactive; top (one row per loan, naming its borrower)"""
    id: str
    name: str


@dataclass(frozen=True)
class MemberLoan(LoanStatus):
    """member"""
    id: str
    isbn: str
    due: date
    returned: Optional[date]
    lost: Optional[date]


@dataclass(frozen=True)
class MemberFee:
    """fees (one row per loan with a fee)"""
    member_id: str
    name: str
    fee_cents: int


@dataclass(frozen=True)
class LoanFee:
    """member-fees"""
    loan_id: str
    fee_cents: int


@dataclass(frozen=True)
class BookListing:
    """books, genre, never-borrowed, search"""
    isbn: str
    title: str
    year: int
    genre: Optional[str]


@dataclass(frozen=True)
class BookLoan(LoanStatus):
    """book"""
    member_id: str
    returned: Optional[date]
    lost: Optional[date]


@dataclass(frozen=True)
class BookAvailability:
    """available"""
    isbn: str
    title: str
    copies: int
    open_loan_ids: tuple[str, ...]


@dataclass(frozen=True)
class HistoryLoan:
    """history"""
    id: str
    isbn: str
    borrowed: date
    due: date
    returned: Optional[date]
    lost: Optional[date]


@dataclass(frozen=True)
class DueLoan:
    """overdue, due-soon (open loans only)"""
    id: str
    member_id: str
    isbn: str
    due: date


@dataclass(frozen=True)
class LateReturn:
    """returned-late"""
    id: str
    member_id: str
    due: date
    returned: date


@dataclass(frozen=True)
class LostLoan:
    """lost"""
    id: str
    member_id: str
    isbn: str
    lost: date
