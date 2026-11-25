//! Users, memberships and privileges, as stored in the catalog.
//!
//! A user has a name, a superuser flag and optionally a password, stored as
//! PostgreSQL stores MD5 passwords: `md5` and the hex MD5 digest of the
//! password followed by the user name. The superuser `admin` always exists and
//! is not stored. A user can be a member of other users; it acts for itself,
//! for the users it is a member of, and for the users those act for. A table
//! or view has an owner, the user that created it (objects of `admin` are
//! owned by `admin`, which is not stored), and privileges granted to users:
//! `SELECT`, `INSERT`, `UPDATE`, `DELETE`. A statement that creates a table or
//! view, also one added later, makes the current user its owner. A statement
//! that changes a table's definition doesn't change its owner or its
//! privileges, and a CREATE TABLE that doesn't create a table doesn't change
//! the existing table's owner.

use std::fmt::Display;

use serde::{Deserialize, Serialize};

use crate::encoding;

/// The name of the superuser admin.
pub const ADMIN: &str = "admin";

/// A user, as stored in the catalog.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct User {
    /// The user name.
    pub name: String,
    /// Whether the user is a superuser.
    pub superuser: bool,
    /// The user's password as an MD5 hash, if it has one.
    pub password: Option<String>,
}

impl encoding::Value for User {}

impl User {
    /// Returns the superuser admin.
    pub fn admin() -> Self {
        Self { name: ADMIN.to_string(), superuser: true, password: None }
    }
}

/// Formats the user for the catalog key display.
impl Display for User {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let password = self.password.as_deref().unwrap_or("None");
        write!(f, "{} superuser={} password={password}", self.name, self.superuser)
    }
}

/// A privilege on a table or view: SELECT, INSERT, UPDATE or DELETE.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum Privilege {
    Select,
    Insert,
    Update,
    Delete,
}

impl encoding::Value for Privilege {}

impl Display for Privilege {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Select => "SELECT",
            Self::Insert => "INSERT",
            Self::Update => "UPDATE",
            Self::Delete => "DELETE",
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use encoding::Value as _;

    use super::*;

    #[test]
    fn display() {
        let user = User { name: "alice".into(), superuser: false, password: Some("md5ab".into()) };
        assert_eq!(user.to_string(), "alice superuser=false password=md5ab");
        assert_eq!(User::admin().to_string(), "admin superuser=true password=None");
        assert_eq!(Privilege::Select.to_string(), "SELECT");
        assert_eq!(Privilege::Delete.to_string(), "DELETE");
    }

    #[test]
    fn privilege_order() {
        let privileges: BTreeSet<_> =
            [Privilege::Delete, Privilege::Select, Privilege::Update, Privilege::Insert].into();
        assert_eq!(
            privileges.into_iter().collect::<Vec<_>>(),
            vec![Privilege::Select, Privilege::Insert, Privilege::Update, Privilege::Delete]
        );
    }

    #[test]
    fn encoding() {
        let user = User { name: "bob".into(), superuser: true, password: None };
        assert_eq!(User::decode(&user.encode()).unwrap(), user);
        let privileges: BTreeSet<_> = [Privilege::Insert, Privilege::Select].into();
        assert_eq!(BTreeSet::<Privilege>::decode(&privileges.encode()).unwrap(), privileges);
    }
}
