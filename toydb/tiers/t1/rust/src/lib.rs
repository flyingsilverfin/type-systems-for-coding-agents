#![warn(clippy::all)]
#![allow(clippy::large_enum_variant)]
#![allow(clippy::module_inception)]
#![allow(clippy::type_complexity)]

pub mod encoding;
pub mod error;
pub mod sql;
pub mod storage;

pub use sql::execution::StatementResult;
