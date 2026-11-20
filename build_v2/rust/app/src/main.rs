mod config;
mod csv;
mod db;
mod error;
mod ingest;
mod model;
mod repo;
mod report;
mod schema;

use std::{path::Path, process::ExitCode, str::FromStr};

use chrono::{Days, NaiveDate};

use crate::{
    config::Config,
    db::Db,
    error::{AppError, Result},
};

const USAGE: &str = "usage: lending <command> [args]
commands:
  init                      recreate the database and define the schema
  load <dir>                load members.csv, books.csv, loans.csv from <dir>
  books                     list all books
  book <isbn>               show a book, its loans and current borrowers
  members                   list all members with their contact details
  member <member-id>        show a member, their open loans and lost loans
  history <member-id>       list all loans of a member with their status
  overdue <date>            list open loans due before <date> (YYYY-MM-DD)
  due-soon <date> <days>    list open loans due within <days> days from <date>
  returned-late             list returned loans that came back after their due date
  lost                      list lost loans
  fees                      total fees per member
  member-fees <member-id>   list the fees charged to a member
  stats                     count books per genre
  decades                   count books per decade of publication
  genre <genre>             list books of a genre ('(none)' for books without one)
  never-borrowed            list books that have never been lent out
  available <isbn>          show how many copies of a book are available
  top <n>                   list the <n> members with the most loans
  search <text>             list books whose title or subtitle contains <text>
  monthly                   count loans per month borrowed
  inactive <date>           list members who have not borrowed since <date>
  contact <member-id>       show how to contact a member";

fn arg<'a>(args: &'a [String], index: usize, usage: &str) -> Result<&'a str> {
    args.get(index).map(String::as_str).ok_or_else(|| AppError::Usage(format!("usage: lending {usage}")))
}

fn parse_date(text: &str) -> Result<NaiveDate> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .map_err(|_| AppError::Usage(format!("invalid date '{text}', expected YYYY-MM-DD")))
}

fn parse_count<T: FromStr>(text: &str) -> Result<T> {
    text.parse::<T>().map_err(|_| AppError::Usage(format!("invalid number '{text}', expected a non-negative integer")))
}

fn no_such_member(member_id: &str) -> AppError {
    AppError::NotFound(format!("no such member: {member_id}"))
}

fn no_such_book(isbn: &str) -> AppError {
    AppError::NotFound(format!("no such book: {isbn}"))
}

fn emit(lines: Vec<String>) {
    for line in lines {
        println!("{line}");
    }
}

fn run(args: &[String]) -> Result<()> {
    let command = args.first().map(String::as_str).unwrap_or("help");
    if matches!(command, "help" | "-h" | "--help") {
        println!("{USAGE}");
        return Ok(());
    }
    let config = Config::from_env();
    let db = Db::connect(&config)?;
    match command {
        "init" => {
            db.recreate()?;
            db.define(schema::SCHEMA)?;
            println!("initialised {}", config.database);
        }
        "load" => {
            let stats = ingest::load_dir(&db, Path::new(arg(args, 1, "load <dir>")?))?;
            println!("loaded {} members, {} books, {} loans", stats.members, stats.books, stats.loans);
        }
        "books" => emit(report::books::books(&repo::books::books(&db)?)),
        "book" => {
            let isbn = arg(args, 1, "book <isbn>")?;
            let (book, loans) = repo::books::book(&db, isbn)?.ok_or_else(|| no_such_book(isbn))?;
            emit(report::books::book(&book, &loans));
        }
        "members" => emit(report::members::members(&repo::members::members(&db)?)),
        "member" => {
            let member_id = arg(args, 1, "member <member-id>")?;
            let (member, loans) = repo::members::member(&db, member_id)?.ok_or_else(|| no_such_member(member_id))?;
            emit(report::members::member(&member, &loans));
        }
        "history" => {
            let member_id = arg(args, 1, "history <member-id>")?;
            let loans = repo::loans::history(&db, member_id)?.ok_or_else(|| no_such_member(member_id))?;
            emit(report::loans::history(member_id, &loans));
        }
        "overdue" => {
            let as_of = parse_date(arg(args, 1, "overdue <date>")?)?;
            emit(report::loans::overdue(&repo::loans::overdue(&db, as_of)?, as_of));
        }
        "due-soon" => {
            let start = parse_date(arg(args, 1, "due-soon <date> <days>")?)?;
            let days = parse_count(arg(args, 2, "due-soon <date> <days>")?)?;
            let end = start
                .checked_add_days(Days::new(days))
                .ok_or_else(|| AppError::Usage(format!("{days} days after {start} is out of range")))?;
            emit(report::loans::due_soon(&repo::loans::due_soon(&db, start, end)?));
        }
        "returned-late" => emit(report::loans::returned_late(&repo::loans::returned_late(&db)?)),
        "lost" => emit(report::loans::lost(&repo::loans::lost(&db)?)),
        "fees" => emit(report::members::fees(&repo::members::fees(&db)?)),
        "member-fees" => {
            let member_id = arg(args, 1, "member-fees <member-id>")?;
            let loans = repo::members::member_fees(&db, member_id)?.ok_or_else(|| no_such_member(member_id))?;
            emit(report::members::member_fees(member_id, &loans));
        }
        "stats" => emit(report::books::stats(&repo::books::stats(&db)?)),
        "decades" => emit(report::books::decades(&repo::books::decades(&db)?)),
        "genre" => {
            let name = arg(args, 1, "genre <genre>")?;
            let genre = (name != report::NO_GENRE).then_some(name);
            emit(report::books::genre(&repo::books::genre(&db, genre)?));
        }
        "never-borrowed" => emit(report::books::never_borrowed(&repo::books::never_borrowed(&db)?)),
        "available" => {
            let isbn = arg(args, 1, "available <isbn>")?;
            let availability = repo::books::available(&db, isbn)?.ok_or_else(|| no_such_book(isbn))?;
            emit(report::books::available(&availability));
        }
        "top" => {
            let limit = parse_count(arg(args, 1, "top <n>")?)?;
            emit(report::members::top(&repo::members::top(&db)?, limit));
        }
        "search" => {
            let query = arg(args, 1, "search <text>")?;
            emit(report::books::search(&repo::books::search(&db)?, query));
        }
        "monthly" => emit(report::loans::monthly(&repo::loans::monthly(&db)?)),
        "inactive" => {
            let since = parse_date(arg(args, 1, "inactive <date>")?)?;
            emit(report::members::inactive(&repo::members::inactive(&db, since)?));
        }
        "contact" => {
            let member_id = arg(args, 1, "contact <member-id>")?;
            let member = repo::members::contact(&db, member_id)?.ok_or_else(|| no_such_member(member_id))?;
            emit(report::members::contact(&member));
        }
        other => return Err(AppError::Usage(format!("unknown command '{other}'\n{USAGE}"))),
    }
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::from(1)
        }
    }
}
