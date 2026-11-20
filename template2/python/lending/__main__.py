from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from lending import ingest
from lending.config import Config
from lending.db import Db
from lending.errors import AppError, NotFoundError, UsageError
from lending.repo import books as book_repo
from lending.repo import loans as loan_repo
from lending.repo import members as member_repo
from lending.report import books as book_report
from lending.report import loans as loan_report
from lending.report import members as member_report
from lending.schema import SCHEMA

USAGE = """usage: lending <command> [args]
commands:
  init                     recreate the database and define the schema
  load <dir>               load members.csv, books.csv, loans.csv from <dir>
  books                    list all books
  book <isbn>              show a book and who has it on loan
  members                  list all members
  member <member-id>       show a member, their open loans and lost loan count
  contact <member-id>      show the best way to contact a member
  history <member-id>      list every loan a member has made
  overdue <date>           list open loans due before <date> (YYYY-MM-DD)
  due-soon <date> <days>   list open loans due within <days> days from <date>
  returned-late            list loans returned after their due date, latest first
  lost                     list lost loans
  fees                     total fees per member, highest first
  member-fees <member-id>  list a member's loans that carry a fee
  stats                    count books per genre
  decades                  count books per publication decade
  genre <genre>            list books in <genre> ('(none)' for books without one)
  never-borrowed           list books that have never been borrowed
  available <isbn>         show how many copies of a book are available
  top <n>                  rank the <n> members with the most loans
  search <text>            list books whose title or subtitle contains <text>
  monthly                  count loans per month borrowed
  inactive <date>          list members who have borrowed nothing since <date>"""


def arg(args: list[str], index: int, usage: str) -> str:
    if index >= len(args):
        raise UsageError(f"usage: lending {usage}")
    return args[index]


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UsageError(f"invalid date '{text}', expected YYYY-MM-DD") from None


def parse_count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise UsageError(f"invalid number '{text}', expected a non-negative integer") from None
    if value < 0:
        raise UsageError(f"invalid number '{text}', expected a non-negative integer")
    return value


def emit(lines: list[str]) -> None:
    for line in lines:
        print(line)


def run(args: list[str]) -> None:
    command = args[0] if args else "help"
    if command in ("help", "-h", "--help"):
        print(USAGE)
        return
    config = Config.from_env()
    db = Db(config)
    try:
        dispatch(db, command, args)
    finally:
        db.close()


def dispatch(db: Db, command: str, args: list[str]) -> None:
    match command:
        case "init":
            db.recreate()
            db.define(SCHEMA)
            print(f"initialised {db.name}")
        case "load":
            stats = ingest.load_dir(db, Path(arg(args, 1, "load <dir>")))
            print(f"loaded {stats.members} members, {stats.books} books, {stats.loans} loans")

        case "books":
            emit(book_report.books(book_repo.list_books(db)))
        case "book":
            isbn = arg(args, 1, "book <isbn>")
            b = book_repo.find_book(db, isbn)
            if b is None:
                raise NotFoundError(f"no such book: {isbn}")
            emit(book_report.book(b, book_repo.book_loans(db, isbn)))
        case "stats":
            emit(book_report.stats(book_repo.book_genres(db)))
        case "decades":
            emit(book_report.decades(book_repo.book_years(db)))
        case "genre":
            name = arg(args, 1, "genre <genre>")
            genre = None if name == book_report.NO_GENRE else name
            emit(book_report.genre(book_repo.books_in_genre(db, genre)))
        case "never-borrowed":
            emit(book_report.never_borrowed(book_repo.never_borrowed_books(db)))
        case "available":
            isbn = arg(args, 1, "available <isbn>")
            availability = book_repo.book_availability(db, isbn)
            if availability is None:
                raise NotFoundError(f"no such book: {isbn}")
            emit(book_report.available(availability))
        case "search":
            text = arg(args, 1, "search <text>")
            emit(book_report.search(book_repo.search_books(db, text)))

        case "members":
            emit(member_report.members(member_repo.list_members(db)))
        case "member":
            member_id = arg(args, 1, "member <member-id>")
            m = member_repo.find_member(db, member_id)
            if m is None:
                raise NotFoundError(f"no such member: {member_id}")
            emit(member_report.member(m, member_repo.member_loans(db, member_id)))
        case "contact":
            member_id = arg(args, 1, "contact <member-id>")
            contact = member_repo.find_contact(db, member_id)
            if contact is None:
                raise NotFoundError(f"no such member: {member_id}")
            emit(member_report.contact(contact))
        case "inactive":
            since = parse_date(arg(args, 1, "inactive <date>"))
            emit(member_report.inactive(member_repo.inactive_members(db, since)))
        case "top":
            limit = parse_count(arg(args, 1, "top <n>"))
            emit(member_report.top(member_repo.loan_borrowers(db), limit))
        case "fees":
            emit(member_report.fees(member_repo.loan_fees(db)))
        case "member-fees":
            member_id = arg(args, 1, "member-fees <member-id>")
            fees = member_repo.member_fees(db, member_id)
            if fees is None:
                raise NotFoundError(f"no such member: {member_id}")
            emit(member_report.member_fees(member_id, fees))

        case "history":
            member_id = arg(args, 1, "history <member-id>")
            history = loan_repo.member_history(db, member_id)
            if history is None:
                raise NotFoundError(f"no such member: {member_id}")
            emit(loan_report.history(member_id, history))
        case "overdue":
            as_of = parse_date(arg(args, 1, "overdue <date>"))
            emit(loan_report.overdue(loan_repo.overdue_loans(db, as_of), as_of))
        case "due-soon":
            start = parse_date(arg(args, 1, "due-soon <date> <days>"))
            days = parse_count(arg(args, 2, "due-soon <date> <days>"))
            end = start + timedelta(days=days)
            emit(loan_report.due_soon(loan_repo.loans_due_between(db, start, end)))
        case "returned-late":
            emit(loan_report.returned_late(loan_repo.late_returns(db)))
        case "lost":
            emit(loan_report.lost(loan_repo.lost_loans(db)))
        case "monthly":
            emit(loan_report.monthly(loan_repo.borrow_dates(db)))

        case _:
            raise UsageError(f"unknown command '{command}'\n{USAGE}")


def main() -> int:
    try:
        run(sys.argv[1:])
        return 0
    except AppError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
