//! Materialized view definitions, as stored in the catalog: a name, the column
//! names, the canonical one-line text of the query and its dependencies, as for
//! a view. The rows are stored separately.

use std::fmt::Display;

use itertools::Itertools as _;
use serde::{Deserialize, Serialize};

use crate::encoding;
use crate::sql::parser::format;

/// A materialized view definition, as stored in the catalog.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct MaterializedView {
    /// The materialized view name.
    pub name: String,
    /// The column names, in query column order.
    pub columns: Vec<String>,
    /// The canonical one-line text of the query.
    pub query: String,
    /// The names in the query's FROM clause.
    pub dependencies: Vec<String>,
}

impl encoding::Value for MaterializedView {}

/// Formats the materialized view as a CREATE MATERIALIZED VIEW statement.
impl Display for MaterializedView {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let columns = self.columns.iter().map(|c| format::ident(c)).join(", ");
        let name = format::ident(&self.name);
        write!(f, "CREATE MATERIALIZED VIEW {name} ({columns}) AS {}", self.query)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display() {
        let view = MaterializedView {
            name: "Totals".into(),
            columns: vec!["s".into(), "sum".into(), "table".into()],
            query: "SELECT s, sum(v), 1 FROM t GROUP BY s".into(),
            dependencies: vec!["t".into()],
        };
        assert_eq!(
            view.to_string(),
            r#"CREATE MATERIALIZED VIEW "Totals" (s, sum, "table") AS SELECT s, sum(v), 1 FROM t GROUP BY s"#
        );
    }

    #[test]
    fn encoding() {
        use encoding::Value as _;
        let view = MaterializedView {
            name: "m".into(),
            columns: vec!["a".into()],
            query: "SELECT a FROM v".into(),
            dependencies: vec!["v".into()],
        };
        assert_eq!(MaterializedView::decode(&view.encode()).unwrap(), view);
    }
}
