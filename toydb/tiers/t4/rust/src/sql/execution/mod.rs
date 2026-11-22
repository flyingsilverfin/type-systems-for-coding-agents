//! Executes statements and plans.

mod aggregator;
mod analyze;
mod digests;
mod executor;
mod export;
mod import;
mod join;
mod lint;
mod prepared;
mod session;
pub mod views;

pub use executor::{ExecutionResult, Executor};
pub use export::json_line;
pub use session::{Session, StatementResult};
