//! Executes statements and plans.

mod aggregator;
mod analyze;
mod digests;
mod executor;
mod export;
mod import;
mod join;
mod session;

pub use executor::{ExecutionResult, Executor};
pub use session::{Session, StatementResult};
