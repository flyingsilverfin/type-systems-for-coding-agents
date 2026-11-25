//! View definitions. A view is a named query, stored in the catalog as the
//! canonical one-line text of its query together with its column names and
//! the names of the tables and views that its query reads (its dependencies).
//! The query is parsed and planned again every time a statement uses the view.

use std::fmt::Display;

use itertools::Itertools as _;
use serde::{Deserialize, Serialize};

use crate::encoding;
use crate::sql::parser::format;

/// A view definition, as stored in the catalog.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct View {
    /// The view name. Views and tables share one namespace.
    pub name: String,
    /// The column names, in query column order.
    pub columns: Vec<String>,
    /// The canonical one-line text of the view's query.
    pub query: String,
    /// The tables and views that the query's FROM clause reads, by name.
    pub dependencies: Vec<String>,
}

impl encoding::Value for View {}

/// Formats the view as a CREATE VIEW statement.
impl Display for View {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let columns = self.columns.iter().map(|c| format::ident(c)).join(", ");
        write!(f, "CREATE VIEW {} ({columns}) AS {}", format::ident(&self.name), self.query)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display() {
        let view = View {
            name: "Mixed".into(),
            columns: vec!["id".into(), "a b".into(), "view".into()],
            query: "SELECT id, b, c FROM t".into(),
            dependencies: vec!["t".into()],
        };
        assert_eq!(
            view.to_string(),
            r#"CREATE VIEW "Mixed" (id, "a b", "view") AS SELECT id, b, c FROM t"#
        );
    }

    #[test]
    fn encoding() {
        use encoding::Value as _;
        let view = View {
            name: "v".into(),
            columns: vec!["a".into()],
            query: "SELECT 1 AS a".into(),
            dependencies: Vec::new(),
        };
        assert_eq!(View::decode(&view.encode()).unwrap(), view);
    }
}
