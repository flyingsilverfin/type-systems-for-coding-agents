from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Member:
    id: str
    name: str
    email: Optional[str]
    joined: date


@dataclass(frozen=True)
class Book:
    isbn: str
    title: str
    year: int
    genre: Optional[str]
    copies: int


@dataclass(frozen=True)
class Loan:
    id: str
    member_id: str
    isbn: str
    borrowed: date
    due: date
    returned: Optional[date]

    def is_open(self) -> bool:
        return self.returned is None
