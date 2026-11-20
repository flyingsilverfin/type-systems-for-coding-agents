mod config;
mod csv;
mod db;
mod error;
mod ingest;
mod model;
mod repo;
mod report;
mod schema;

use std::{path::Path, process::ExitCode};

use chrono::NaiveDate;

use crate::{
    config::Config,
    db::Db,
    error::{AppError, Result},
};

const USAGE: &str = "usage: lending <command> [args]
commands:
  init                 recreate the database and define the schema
  load <dir>           load members.csv, books.csv, loans.csv from <dir>
  books                list all books
  member <member-id>   show a member and their open loans
  overdue <date>       list open loans due before <date> (YYYY-MM-DD)
  stats                count books per genre";

fn arg<'a>(args: &'a [String], index: usize, usage: &str) -> Result<&'a str> {
    args.get(index).map(String::as_str).ok_or_else(|| AppError::Usage(format!("usage: lending {usage}")))
}

fn parse_date(text: &str) -> Result<NaiveDate> {
    NaiveDate::parse_from_str(text, "%Y-%m-%d")
        .map_err(|_| AppError::Usage(format!("invalid date '{text}', expected YYYY-MM-DD")))
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
            println!("initialised {}", db.name());
        }
        "load" => {
            let stats = ingest::load_dir(&db, Path::new(arg(args, 1, "load <dir>")?))?;
            println!("loaded {} members, {} books, {} loans", stats.members, stats.books, stats.loans);
        }
        "books" => emit(report::books(&repo::list_books(&db)?)),
        "member" => {
            let member_id = arg(args, 1, "member <member-id>")?;
            let Some(m) = repo::find_member(&db, member_id)? else {
                return Err(AppError::NotFound(format!("no such member: {member_id}")));
            };
            emit(report::member(&m, &repo::loans_for_member(&db, member_id)?));
        }
        "overdue" => {
            let as_of = parse_date(arg(args, 1, "overdue <date>")?)?;
            emit(report::overdue(&repo::all_loans(&db)?, as_of));
        }
        "stats" => emit(report::genre_stats(&repo::list_books(&db)?)),
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
