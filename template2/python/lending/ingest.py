"""Load members.csv, books.csv and loans.csv into the database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from lending.csvio import Record, read_csv
from lending.db import Db, quote
from lending.errors import CsvError
from lending.model import Book, Loan, Member


@dataclass
class LoadStats:
    members: int = 0
    books: int = 0
    loans: int = 0


def _field(record: Record, key: str, file: str, line: int) -> str:
    value = record.get(key, "")
    if value == "":
        raise CsvError(file, line, f"missing required field '{key}'")
    return value


def _optional(record: Record, key: str) -> Optional[str]:
    value = record.get(key, "")
    return value if value != "" else None


def _date(text: str, file: str, line: int) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise CsvError(file, line, f"invalid date '{text}'") from None


def _int(text: str, file: str, line: int) -> int:
    try:
        return int(text)
    except ValueError:
        raise CsvError(file, line, f"invalid integer '{text}'") from None


def parse_member(record: Record, line: int) -> Member:
    """Parse a members.csv record. The `deleted` column is deliberately ignored."""
    f = "members.csv"
    return Member(
        id=_field(record, "member_id", f, line),
        name=_field(record, "name", f, line),
        email=_optional(record, "email"),
        phone=_optional(record, "phone"),
        joined=_date(_field(record, "joined", f, line), f, line),
    )


def parse_book(record: Record, line: int) -> Book:
    f = "books.csv"
    return Book(
        isbn=_field(record, "isbn", f, line),
        title=_field(record, "title", f, line),
        subtitle=_optional(record, "subtitle"),
        year=_int(_field(record, "year", f, line), f, line),
        genre=_optional(record, "genre"),
        copies=_int(_field(record, "copies", f, line), f, line),
    )


def parse_loan(record: Record, line: int) -> Loan:
    f = "loans.csv"
    returned = _optional(record, "returned")
    lost = _optional(record, "lost")
    fee_cents = _optional(record, "fee_cents")
    return Loan(
        id=_field(record, "loan_id", f, line),
        member_id=_field(record, "member_id", f, line),
        isbn=_field(record, "isbn", f, line),
        borrowed=_date(_field(record, "borrowed", f, line), f, line),
        due=_date(_field(record, "due", f, line), f, line),
        returned=_date(returned, f, line) if returned is not None else None,
        lost=_date(lost, f, line) if lost is not None else None,
        fee_cents=_int(fee_cents, f, line) if fee_cents is not None else None,
    )


def member_query(member: Member) -> str:
    attrs = [
        f"has member-id {quote(member.id)}",
        f"has name {quote(member.name)}",
        f"has joined {member.joined.isoformat()}",
    ]
    if member.email is not None:
        attrs.append(f"has email {quote(member.email)}")
    if member.phone is not None:
        attrs.append(f"has phone {quote(member.phone)}")
    return "insert $m isa member, " + ", ".join(attrs) + ";"


def book_query(book: Book) -> str:
    attrs = [
        f"has isbn {quote(book.isbn)}",
        f"has title {quote(book.title)}",
        f"has year {book.year}",
        f"has copies {book.copies}",
    ]
    if book.subtitle is not None:
        attrs.append(f"has subtitle {quote(book.subtitle)}")
    if book.genre is not None:
        attrs.append(f"has genre {quote(book.genre)}")
    return "insert $b isa book, " + ", ".join(attrs) + ";"


def loan_query(loan: Loan) -> str:
    """A loan whose member or book does not exist matches nothing and so inserts nothing."""
    attrs = [
        f"has loan-id {quote(loan.id)}",
        f"has borrowed {loan.borrowed.isoformat()}",
        f"has due {loan.due.isoformat()}",
    ]
    if loan.returned is not None:
        attrs.append(f"has returned {loan.returned.isoformat()}")
    if loan.lost is not None:
        attrs.append(f"has lost {loan.lost.isoformat()}")
    if loan.fee_cents is not None:
        attrs.append(f"has fee-cents {loan.fee_cents}")
    return (
        f"match $m isa member, has member-id {quote(loan.member_id)}; $b isa book, has isbn {quote(loan.isbn)};\n"
        "insert $l isa loan, links (borrower: $m, item: $b), " + ", ".join(attrs) + ";"
    )


def load_dir(db: Db, directory: Path) -> LoadStats:
    stats = LoadStats()
    members = [member_query(parse_member(rec, line)) for line, rec in read_csv(directory / "members.csv")]
    books = [book_query(parse_book(rec, line)) for line, rec in read_csv(directory / "books.csv")]
    db.write(members + books)
    stats.members = len(members)
    stats.books = len(books)
    loans = [loan_query(parse_loan(rec, line)) for line, rec in read_csv(directory / "loans.csv")]
    db.write(loans)
    stats.loans = len(loans)
    return stats
