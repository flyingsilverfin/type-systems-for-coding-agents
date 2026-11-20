"""Load members.csv, books.csv and loans.csv into the database."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from lending.csvio import Record, read_csv
from lending.db import Db, quote
from lending.errors import CsvError


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


def member_query(record: Record, line: int) -> str:
    f = "members.csv"
    member_id = _field(record, "member_id", f, line)
    name = _field(record, "name", f, line)
    joined = _date(_field(record, "joined", f, line), f, line)
    parts = [f"$m isa member, has member-id {quote(member_id)}, has name {quote(name)}, has joined {joined.isoformat()}"]
    email = record.get("email", "")
    if email != "":
        parts.append(f"has email {quote(email)}")
    return "insert " + ", ".join(parts) + ";"


def book_query(record: Record, line: int) -> str:
    f = "books.csv"
    isbn = _field(record, "isbn", f, line)
    title = _field(record, "title", f, line)
    year = _int(_field(record, "year", f, line), f, line)
    copies = _int(_field(record, "copies", f, line), f, line)
    parts = [f"$b isa book, has isbn {quote(isbn)}, has title {quote(title)}, has year {year}, has copies {copies}"]
    genre = record.get("genre", "")
    if genre != "":
        parts.append(f"has genre {quote(genre)}")
    return "insert " + ", ".join(parts) + ";"


def loan_query(record: Record, line: int) -> str:
    f = "loans.csv"
    loan_id = _field(record, "loan_id", f, line)
    member_id = _field(record, "member_id", f, line)
    isbn = _field(record, "isbn", f, line)
    borrowed = _date(_field(record, "borrowed", f, line), f, line)
    due = _date(_field(record, "due", f, line), f, line)
    attrs = [f"has loan-id {quote(loan_id)}", f"has borrowed {borrowed.isoformat()}", f"has due {due.isoformat()}"]
    returned = record.get("returned", "")
    if returned != "":
        attrs.append(f"has returned {_date(returned, f, line).isoformat()}")
    return (
        f"match $m isa member, has member-id {quote(member_id)}; $b isa book, has isbn {quote(isbn)};\n"
        f"insert $l isa loan, links (borrower: $m, item: $b), " + ", ".join(attrs) + ";"
    )


def load_dir(db: Db, directory: Path) -> LoadStats:
    stats = LoadStats()
    members = [member_query(rec, line) for line, rec in read_csv(directory / "members.csv")]
    books = [book_query(rec, line) for line, rec in read_csv(directory / "books.csv")]
    db.write(members + books)
    stats.members = len(members)
    stats.books = len(books)
    loans = [loan_query(rec, line) for line, rec in read_csv(directory / "loans.csv")]
    db.write(loans)
    stats.loans = len(loans)
    return stats
