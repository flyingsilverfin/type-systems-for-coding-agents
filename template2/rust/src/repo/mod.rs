//! Read-side queries: one function per CLI command, each with its own TypeQL query.
//!
//! Commands whose output depends on loan status return `model::Loan`, so the status rules live in one
//! place; the others return small purpose-built rows holding only the columns the command needs.
//! Column values are extracted inline with `ConceptRow::get` and the `Concept::try_get_*` accessors.
pub mod books;
pub mod loans;
pub mod members;
