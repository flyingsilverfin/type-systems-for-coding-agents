//! Row-level security policies, as stored in the catalog: a policy of a table
//! has a name, optionally the user it applies to, and the canonical one-line
//! text of its USING condition and, if it has one, of its WITH CHECK
//! condition.

use std::fmt::Display;

use serde::{Deserialize, Serialize};

use crate::encoding;
use crate::sql::parser::format;

/// A row-level security policy: its table, name, user (none for all users),
/// USING condition and WITH CHECK condition (if any).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Policy {
    /// The table name.
    pub table: String,
    /// The policy name.
    pub name: String,
    /// The user the policy applies to, or None for all users.
    pub user: Option<String>,
    /// The canonical one-line text of the USING condition.
    pub using: String,
    /// The canonical one-line text of the WITH CHECK condition, if any.
    pub with_check: Option<String>,
}

impl encoding::Value for Policy {}

impl Policy {
    /// Returns the check condition: WITH CHECK, or else USING.
    pub fn check_condition(&self) -> &str {
        self.with_check.as_deref().unwrap_or(&self.using)
    }
}

/// Formats the policy as a CREATE POLICY statement.
impl Display for Policy {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "CREATE POLICY {} ON {}", format::ident(&self.name), format::ident(&self.table))?;
        if let Some(user) = &self.user {
            write!(f, " TO {}", format::ident(user))?;
        }
        write!(f, " USING ({})", self.using)?;
        if let Some(with_check) = &self.with_check {
            write!(f, " WITH CHECK ({with_check})")?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display() {
        let policy = Policy {
            table: "doc".into(),
            name: "own".into(),
            user: Some("Alice".into()),
            using: "owner = 'it''s'".into(),
            with_check: None,
        };
        assert_eq!(
            policy.to_string(),
            r#"CREATE POLICY own ON doc TO "Alice" USING (owner = 'it''s')"#
        );
        let policy = Policy {
            table: "t".into(),
            name: "select".into(),
            user: None,
            using: "FALSE".into(),
            with_check: Some("id > 0".into()),
        };
        assert_eq!(
            policy.to_string(),
            r#"CREATE POLICY "select" ON t USING (FALSE) WITH CHECK (id > 0)"#
        );
    }

    #[test]
    fn check_condition() {
        let mut policy = Policy {
            table: "t".into(),
            name: "p".into(),
            user: None,
            using: "id > 1".into(),
            with_check: None,
        };
        assert_eq!(policy.check_condition(), "id > 1");
        policy.with_check = Some("id > 2".into());
        assert_eq!(policy.check_condition(), "id > 2");
    }

    #[test]
    fn encoding() {
        use encoding::Value as _;
        let policy = Policy {
            table: "t".into(),
            name: "p".into(),
            user: Some("u".into()),
            using: "TRUE".into(),
            with_check: Some("id > 2".into()),
        };
        assert_eq!(Policy::decode(&policy.encode()).unwrap(), policy);
    }
}
