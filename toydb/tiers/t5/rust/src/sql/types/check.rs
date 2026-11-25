//! CHECK constraints, as stored in the catalog: a table's constraints, each
//! with its name and the canonical one-line text of its condition.

use std::fmt::Display;

use serde::{Deserialize, Serialize};

use crate::encoding;
use crate::sql::parser::format_ident;

/// A CHECK constraint: its name and its condition, as canonical text.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Check {
    /// The constraint name.
    pub name: String,
    /// The canonical one-line text of the condition.
    pub condition: String,
}

impl encoding::Value for Check {}

/// Formats the constraint as a table constraint of CREATE TABLE.
impl Display for Check {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "CONSTRAINT {} CHECK ({})", format_ident(&self.name), self.condition)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display() {
        let check = Check { name: "t_check1".into(), condition: "n > 0".into() };
        assert_eq!(check.to_string(), "CONSTRAINT t_check1 CHECK (n > 0)");
        let check = Check { name: "check".into(), condition: "s != 'it''s'".into() };
        assert_eq!(check.to_string(), r#"CONSTRAINT "check" CHECK (s != 'it''s')"#);
    }

    #[test]
    fn encoding() {
        use encoding::Value as _;
        let checks = vec![
            Check { name: "a".into(), condition: "id > 0".into() },
            Check { name: "Big".into(), condition: "id < 1000.0".into() },
        ];
        assert_eq!(Vec::<Check>::decode(&checks.encode()).unwrap(), checks);
    }
}
