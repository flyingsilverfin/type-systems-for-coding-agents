from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

from lending import ingest, repo, report
from lending.config import Config
from lending.db import Db
from lending.errors import AppError, NotFoundError, UsageError
from lending.schema import SCHEMA

USAGE = """usage: lending <command> [args]
commands:
  init                 recreate the database and define the schema
  load <dir>           load members.csv, books.csv, loans.csv from <dir>
  books                list all books
  member <member-id>   show a member and their open loans
  overdue <date>       list open loans due before <date> (YYYY-MM-DD)
  stats                count books per genre"""


def arg(args: list[str], index: int, usage: str) -> str:
    if index >= len(args):
        raise UsageError(f"usage: lending {usage}")
    return args[index]


def parse_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise UsageError(f"invalid date '{text}', expected YYYY-MM-DD") from None


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
        if command == "init":
            db.recreate()
            db.define(SCHEMA)
            print(f"initialised {db.name}")
        elif command == "load":
            stats = ingest.load_dir(db, Path(arg(args, 1, "load <dir>")))
            print(f"loaded {stats.members} members, {stats.books} books, {stats.loans} loans")
        elif command == "books":
            emit(report.books(repo.list_books(db)))
        elif command == "member":
            member_id = arg(args, 1, "member <member-id>")
            m = repo.find_member(db, member_id)
            if m is None:
                raise NotFoundError(f"no such member: {member_id}")
            emit(report.member(m, repo.loans_for_member(db, member_id)))
        elif command == "overdue":
            as_of = parse_date(arg(args, 1, "overdue <date>"))
            emit(report.overdue(repo.all_loans(db), as_of))
        elif command == "stats":
            emit(report.genre_stats(repo.list_books(db)))
        else:
            raise UsageError(f"unknown command '{command}'\n{USAGE}")
    finally:
        db.close()


def main() -> int:
    try:
        run(sys.argv[1:])
        return 0
    except AppError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
