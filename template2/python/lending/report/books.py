"""Output for books, book, stats, decades, genre, never-borrowed, available, search."""
from __future__ import annotations

from collections import Counter
from typing import Optional

from lending.model import Book, BookAvailability, BookListing, BookLoan
from lending.report import NONE, or_none

NO_GENRE = "(none)"


def _listing(items: list[BookListing]) -> list[str]:
    ordered = sorted(items, key=lambda b: (b.title, b.isbn))
    return [f"{b.isbn}  {b.title} ({b.year}) [{or_none(b.genre)}]" for b in ordered]


def books(items: list[BookListing]) -> list[str]:
    return _listing(items)


def book(b: Book, loans: list[BookLoan]) -> list[str]:
    open_loans = [loan for loan in loans if loan.is_open()]
    borrowers = sorted({loan.member_id for loan in open_loans})
    return [
        f"book {b.isbn}: {b.title}",
        f"subtitle: {or_none(b.subtitle)}",
        f"year: {b.year}",
        f"genre: {or_none(b.genre)}",
        f"copies: {b.copies}",
        f"loans: {len(loans)}",
        f"on loan: {len(open_loans)}",
        f"borrowers: {', '.join(borrowers) if borrowers else NONE}",
    ]


def stats(genres: list[Optional[str]]) -> list[str]:
    counts = Counter(genre if genre is not None else NO_GENRE for genre in genres)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{genre}: {count}" for genre, count in ordered]


def decades(years: list[int]) -> list[str]:
    counts = Counter(year // 10 * 10 for year in years)
    return [f"{decade}s: {count}" for decade, count in sorted(counts.items())]


def genre(items: list[BookListing]) -> list[str]:
    return _listing(items)


def never_borrowed(items: list[BookListing]) -> list[str]:
    return _listing(items)


def available(a: BookAvailability) -> list[str]:
    free = max(0, a.copies - len(a.open_loan_ids))
    return [f"{a.isbn} {a.title}: {free} of {a.copies} available"]


def search(items: list[BookListing]) -> list[str]:
    return _listing(items)
