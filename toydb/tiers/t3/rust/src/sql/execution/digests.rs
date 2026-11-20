use std::collections::HashMap;

use super::StatementResult;
use crate::sql::types::{Label, Value};

/// Statement statistics of a session, grouped by statement digest (see
/// `sql::parser::digest`). Shown by SHOW STATEMENTS. They're kept in memory
/// and end with the session.
#[derive(Default)]
pub struct Digests {
    /// The digest statistics, in order of first execution.
    digests: Vec<DigestStatistics>,
    /// Index of digests into the digests vector.
    index: HashMap<String, usize>,
}

/// Statistics for a single statement digest.
struct DigestStatistics {
    /// The statement digest.
    digest: String,
    /// The number of executed statements, successful or not.
    calls: u64,
    /// The number of failed statements.
    errors: u64,
    /// The number of rows produced by successful statements: returned rows,
    /// or the rows written, imported or exported.
    rows: u64,
}

impl Digests {
    /// Creates an empty set of statement statistics.
    pub fn new() -> Self {
        Self::default()
    }

    /// Records an executed statement, with the number of rows it produced if
    /// it succeeded, or None if it failed.
    pub fn record(&mut self, digest: String, rows: Option<u64>) {
        let index = *self.index.entry(digest.clone()).or_insert_with(|| {
            self.digests.push(DigestStatistics { digest, calls: 0, errors: 0, rows: 0 });
            self.digests.len() - 1
        });
        let statistics = &mut self.digests[index];
        statistics.calls += 1;
        match rows {
            Some(rows) => statistics.rows += rows,
            None => statistics.errors += 1,
        }
    }

    /// Returns the statistics as a SELECT result for SHOW STATEMENTS.
    pub fn to_result(&self) -> StatementResult {
        let columns = ["digest", "calls", "errors", "rows"]
            .into_iter()
            .map(|name| Label::Unqualified(name.to_string()))
            .collect();
        let rows = self
            .digests
            .iter()
            .map(|d| {
                vec![
                    Value::String(d.digest.clone()),
                    Value::Integer(d.calls as i64),
                    Value::Integer(d.errors as i64),
                    Value::Integer(d.rows as i64),
                ]
            })
            .collect();
        StatementResult::Select { columns, rows }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record() {
        let mut digests = Digests::new();
        digests.record("SELECT ?".into(), Some(1));
        digests.record("INSERT INTO t VALUES (?)".into(), Some(2));
        digests.record("SELECT ?".into(), None);
        digests.record("SELECT ?".into(), Some(1));

        let StatementResult::Select { columns, rows } = digests.to_result() else {
            panic!("expected select result");
        };
        let columns = columns.iter().map(|c| c.to_string()).collect::<Vec<_>>();
        assert_eq!(columns, ["digest", "calls", "errors", "rows"]);
        assert_eq!(
            rows,
            vec![
                vec!["SELECT ?".into(), 3i64.into(), 1i64.into(), 2i64.into()],
                vec!["INSERT INTO t VALUES (?)".into(), 1i64.into(), 0i64.into(), 2i64.into()],
            ]
        );
    }
}
