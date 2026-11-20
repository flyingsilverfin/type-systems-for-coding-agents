"""Book-centred queries: books, book, stats, decades, genre, never-borrowed, available, search."""
from __future__ import annotations

from datetime import date
from typing import Optional

from lending.db import Db, quote
from lending.errors import DataError
from lending.model import Book, BookAvailability, BookListing, BookLoan


def list_books(db: Db) -> list[BookListing]:
    rows = db.read_rows("""
        match
          $b isa book, has isbn $isbn, has title $t, has year $y;
          try { $b has genre $g; };
    """)
    books: list[BookListing] = []
    for row in rows:
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("t")) is None or (title := concept.try_get_string()) is None:
            raise DataError("$t: expected a string")
        if (concept := row.get("y")) is None or (year := concept.try_get_integer()) is None:
            raise DataError("$y: expected an integer")
        genre: Optional[str] = None
        if (concept := row.get("g")) is not None and (genre := concept.try_get_string()) is None:
            raise DataError("$g: expected a string")
        books.append(BookListing(isbn=isbn, title=title, year=year, genre=genre))
    return books


def find_book(db: Db, isbn: str) -> Optional[Book]:
    rows = db.read_rows(f"""
        match
          $b isa book, has isbn {quote(isbn)}, has title $t, has year $y, has copies $c;
          try {{ $b has subtitle $s; }};
          try {{ $b has genre $g; }};
    """)
    if not rows:
        return None
    row = rows[0]
    if (concept := row.get("t")) is None or (title := concept.try_get_string()) is None:
        raise DataError("$t: expected a string")
    if (concept := row.get("y")) is None or (year := concept.try_get_integer()) is None:
        raise DataError("$y: expected an integer")
    if (concept := row.get("c")) is None or (copies := concept.try_get_integer()) is None:
        raise DataError("$c: expected an integer")
    subtitle: Optional[str] = None
    if (concept := row.get("s")) is not None and (subtitle := concept.try_get_string()) is None:
        raise DataError("$s: expected a string")
    genre: Optional[str] = None
    if (concept := row.get("g")) is not None and (genre := concept.try_get_string()) is None:
        raise DataError("$g: expected a string")
    return Book(isbn=isbn, title=title, subtitle=subtitle, year=year, genre=genre, copies=copies)


def book_loans(db: Db, isbn: str) -> list[BookLoan]:
    rows = db.read_rows(f"""
        match
          $b isa book, has isbn {quote(isbn)};
          $l isa loan, links (borrower: $m, item: $b);
          $m has member-id $mid;
          try {{ $l has returned $re; }};
          try {{ $l has lost $lo; }};
    """)
    loans: list[BookLoan] = []
    for row in rows:
        if (concept := row.get("mid")) is None or (member_id := concept.try_get_string()) is None:
            raise DataError("$mid: expected a string")
        returned: Optional[date] = None
        if (concept := row.get("re")) is not None and (returned := concept.try_get_date()) is None:
            raise DataError("$re: expected a date")
        lost: Optional[date] = None
        if (concept := row.get("lo")) is not None and (lost := concept.try_get_date()) is None:
            raise DataError("$lo: expected a date")
        loans.append(BookLoan(member_id=member_id, returned=returned, lost=lost))
    return loans


def book_genres(db: Db) -> list[Optional[str]]:
    """The genre of every book (None for books without one): one entry per book."""
    rows = db.read_rows("""
        match
          $b isa book;
          try { $b has genre $g; };
    """)
    genres: list[Optional[str]] = []
    for row in rows:
        genre: Optional[str] = None
        if (concept := row.get("g")) is not None and (genre := concept.try_get_string()) is None:
            raise DataError("$g: expected a string")
        genres.append(genre)
    return genres


def book_years(db: Db) -> list[int]:
    """The publication year of every book: one entry per book."""
    rows = db.read_rows("""
        match
          $b isa book, has year $y;
    """)
    years: list[int] = []
    for row in rows:
        if (concept := row.get("y")) is None or (year := concept.try_get_integer()) is None:
            raise DataError("$y: expected an integer")
        years.append(year)
    return years


def books_in_genre(db: Db, genre: Optional[str]) -> list[BookListing]:
    """Books with the given genre, or books with no genre at all when `genre` is None."""
    genre_pattern = f"$b has genre {quote(genre)};" if genre is not None else "not { $b has genre $any; };"
    rows = db.read_rows(f"""
        match
          $b isa book, has isbn $isbn, has title $t, has year $y;
          {genre_pattern}
    """)
    books: list[BookListing] = []
    for row in rows:
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("t")) is None or (title := concept.try_get_string()) is None:
            raise DataError("$t: expected a string")
        if (concept := row.get("y")) is None or (year := concept.try_get_integer()) is None:
            raise DataError("$y: expected an integer")
        books.append(BookListing(isbn=isbn, title=title, year=year, genre=genre))
    return books


def never_borrowed_books(db: Db) -> list[BookListing]:
    rows = db.read_rows("""
        match
          $b isa book, has isbn $isbn, has title $t, has year $y;
          try { $b has genre $g; };
          not { $l isa loan, links (item: $b); };
    """)
    books: list[BookListing] = []
    for row in rows:
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("t")) is None or (title := concept.try_get_string()) is None:
            raise DataError("$t: expected a string")
        if (concept := row.get("y")) is None or (year := concept.try_get_integer()) is None:
            raise DataError("$y: expected an integer")
        genre: Optional[str] = None
        if (concept := row.get("g")) is not None and (genre := concept.try_get_string()) is None:
            raise DataError("$g: expected a string")
        books.append(BookListing(isbn=isbn, title=title, year=year, genre=genre))
    return books


def book_availability(db: Db, isbn: str) -> Optional[BookAvailability]:
    """The book's copies and its open (neither returned nor lost) loans, or None if there is no such book."""
    rows = db.read_rows(f"""
        match
          $b isa book, has isbn {quote(isbn)}, has title $t, has copies $c;
          try {{
            $l isa loan, links (item: $b), has loan-id $lid;
            not {{ $l has returned $re; }};
            not {{ $l has lost $lo; }};
          }};
    """)
    if not rows:
        return None
    first = rows[0]
    if (concept := first.get("t")) is None or (title := concept.try_get_string()) is None:
        raise DataError("$t: expected a string")
    if (concept := first.get("c")) is None or (copies := concept.try_get_integer()) is None:
        raise DataError("$c: expected an integer")
    open_loan_ids: list[str] = []
    for row in rows:
        if row.get("l") is None:
            continue  # the book exists but has no open loans
        if (concept := row.get("lid")) is None or (loan_id := concept.try_get_string()) is None:
            raise DataError("$lid: expected a string")
        open_loan_ids.append(loan_id)
    return BookAvailability(isbn=isbn, title=title, copies=copies, open_loan_ids=tuple(open_loan_ids))


def search_books(db: Db, text: str) -> list[BookListing]:
    """Books whose title or subtitle contains `text`, ignoring case."""
    rows = db.read_rows("""
        match
          $b isa book, has isbn $isbn, has title $t, has year $y;
          try { $b has subtitle $s; };
          try { $b has genre $g; };
    """)
    needle = text.lower()
    books: list[BookListing] = []
    for row in rows:
        if (concept := row.get("isbn")) is None or (isbn := concept.try_get_string()) is None:
            raise DataError("$isbn: expected a string")
        if (concept := row.get("t")) is None or (title := concept.try_get_string()) is None:
            raise DataError("$t: expected a string")
        if (concept := row.get("y")) is None or (year := concept.try_get_integer()) is None:
            raise DataError("$y: expected an integer")
        subtitle: Optional[str] = None
        if (concept := row.get("s")) is not None and (subtitle := concept.try_get_string()) is None:
            raise DataError("$s: expected a string")
        genre: Optional[str] = None
        if (concept := row.get("g")) is not None and (genre := concept.try_get_string()) is None:
            raise DataError("$g: expected a string")
        if needle in title.lower() or (subtitle is not None and needle in subtitle.lower()):
            books.append(BookListing(isbn=isbn, title=title, year=year, genre=genre))
    return books
