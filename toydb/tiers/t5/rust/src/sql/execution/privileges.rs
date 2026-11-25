//! Statement accesses and privilege checks.
//!
//! The accesses of a statement are the tables, views and materialized views it
//! uses, each with a kind: `read` (reads rows or statistics), `insert`,
//! `update`, `delete` (writes rows), `create`, `drop`, `alter` (changes the
//! definition of a table: adds, drops or renames columns or otherwise changes
//! its schema display), `policy` (creates or drops a policy of a table),
//! `analyze` (stores statistics), `refresh` (recomputes a materialized view),
//! `grant` (grants or revokes privileges on it).
//!
//! * SELECT reads every table, view and materialized view of its FROM clause;
//!   INSERT, UPDATE, DELETE and IMPORT write their table; CREATE and DROP
//!   create and drop theirs; CREATE VIEW creates the view and reads the tables
//!   and views of its query; ANALYZE analyzes its table, or every table; SHOW
//!   STATISTICS and EXPORT read theirs, EXPORT AS SQL every table and view;
//!   EXPLAIN, DESCRIBE and ESTIMATE of a statement have that statement's
//!   accesses, EXECUTE those of the statement it executes; GRANT and REVOKE on
//!   a table grant it; CREATE and DROP POLICY have the `policy` access of their
//!   table; CREATE MATERIALIZED VIEW creates it and reads the tables and views
//!   of its query, REFRESH has the `refresh` access, DROP the `drop` access.
//!   Other statements have none.
//! * A statement added later has the accesses that follow from what it does:
//!   `read` of every table and view named in a FROM clause of a query it
//!   contains and executes, `insert`, `update` or `delete` of a table whose
//!   rows it writes, `create` or `drop` of what it creates or drops, `alter` of
//!   a table whose definition it changes. A part of an existing statement added
//!   later (a clause, an option, a query in place of a `VALUES` list) adds the
//!   accesses that follow from what it does in the same way.
//! * Accesses are in the order of the statement's canonical text, each name and
//!   kind once.
//!
//! Every statement is checked against the session's current user before it is
//! executed, in the statement's transaction: `read` requires the `SELECT`
//! privilege, `insert`, `update` and `delete` the privilege of that name,
//! `drop`, `alter`, `policy`, `analyze`, `refresh` and `grant` acting for the
//! owner, `create` nothing. A user has a privilege if it is a superuser, acts
//! for the owner, or acts for a user it was granted to. Accesses to names that
//! are neither tables, views nor materialized views are not checked. CREATE and
//! DROP USER, GRANT and REVOKE of memberships, SHOW CHANGES, SHOW AUDIT and
//! backups require a superuser.

use std::collections::BTreeSet;
use std::fmt::Display;

use itertools::Itertools as _;

use super::{system, users};
use crate::error::Result;
use crate::errperm;
use crate::sql::engine::Catalog;
use crate::sql::parser::ast::{self, Statement};
use crate::sql::types::{ADMIN, Privilege};

/// The kind of an access: what a statement does with a table or view.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum AccessKind {
    Read,
    Insert,
    Update,
    Delete,
    Create,
    Drop,
    Alter,
    Policy,
    Analyze,
    Refresh,
    Grant,
}

impl Display for AccessKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Read => "read",
            Self::Insert => "insert",
            Self::Update => "update",
            Self::Delete => "delete",
            Self::Create => "create",
            Self::Drop => "drop",
            Self::Alter => "alter",
            Self::Policy => "policy",
            Self::Analyze => "analyze",
            Self::Refresh => "refresh",
            Self::Grant => "grant",
        })
    }
}

/// An access of a statement: a table or view, by name, and its kind.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Access {
    pub name: String,
    pub kind: AccessKind,
}

impl Access {
    fn new(name: &str, kind: AccessKind) -> Self {
        Self { name: name.to_string(), kind }
    }
}

/// What a statement requires of the current user, besides the privileges of
/// its accesses.
#[derive(Clone, Debug, PartialEq)]
pub enum Requirement {
    /// Nothing.
    Nothing,
    /// The current user is a superuser.
    Superuser,
    /// The login user is a superuser or acts for the given user (SET ROLE).
    SetRole(String),
}

/// Returns the accesses of a statement, in the order of its canonical text.
pub fn accesses(catalog: &impl Catalog, statement: &Statement) -> Result<Vec<Access>> {
    let mut accesses = Vec::new();
    collect_accesses(catalog, statement, &mut accesses)?;
    Ok(accesses.into_iter().unique().collect())
}

/// Adds the accesses of a statement to the given vector.
fn collect_accesses(
    catalog: &impl Catalog,
    statement: &Statement,
    accesses: &mut Vec<Access>,
) -> Result<()> {
    match statement {
        Statement::Explain { statement, .. }
        | Statement::Describe(ast::Describe::Statement(statement))
        | Statement::Estimate(statement) => collect_accesses(catalog, statement, accesses)?,

        Statement::CreateTable { name, .. } => accesses.push(Access::new(name, AccessKind::Create)),
        Statement::DropTable { name, .. } | Statement::DropView { name, .. } => {
            accesses.push(Access::new(name, AccessKind::Drop))
        }
        Statement::Delete { table, .. } => accesses.push(Access::new(table, AccessKind::Delete)),
        Statement::Insert { table, .. } | Statement::Import { table, .. } => {
            accesses.push(Access::new(table, AccessKind::Insert))
        }
        Statement::Update { table, .. } => accesses.push(Access::new(table, AccessKind::Update)),
        Statement::Select { from, .. } => {
            for item in from {
                collect_reads(catalog, item, accesses)?;
            }
        }
        Statement::CreateView { name, query, .. } => {
            accesses.push(Access::new(name, AccessKind::Create));
            collect_accesses(catalog, query, accesses)?;
        }

        Statement::Analyze { table: Some(table) } => {
            accesses.push(Access::new(table, AccessKind::Analyze))
        }
        Statement::Analyze { table: None } => {
            for table in catalog.list_tables()? {
                accesses.push(Access::new(&table.name, AccessKind::Analyze));
            }
        }
        Statement::ShowStatistics { table } | Statement::Export { table: Some(table), .. } => {
            accesses.push(Access::new(table, AccessKind::Read))
        }
        Statement::Export { table: None, .. } => {
            for name in users::objects(catalog)? {
                accesses.push(Access::new(&name, AccessKind::Read));
            }
        }

        Statement::Grant(ast::Grant::Privileges { object, .. })
        | Statement::Revoke(ast::Grant::Privileges { object, .. }) => {
            accesses.push(Access::new(object, AccessKind::Grant))
        }
        Statement::CreatePolicy { table, .. } | Statement::DropPolicy { table, .. } => {
            accesses.push(Access::new(table, AccessKind::Policy))
        }
        Statement::CreateMaterializedView { name, query, .. } => {
            accesses.push(Access::new(name, AccessKind::Create));
            collect_accesses(catalog, query, accesses)?;
        }
        Statement::RefreshMaterializedView { name } => {
            accesses.push(Access::new(name, AccessKind::Refresh))
        }
        Statement::DropMaterializedView { name, .. } => {
            accesses.push(Access::new(name, AccessKind::Drop))
        }

        // EXECUTE has the accesses of the prepared statement with its
        // arguments bound, which the session checks.
        Statement::Execute { .. } => {}

        Statement::Begin { .. }
        | Statement::Commit
        | Statement::Rollback
        | Statement::ShowStatements
        | Statement::Format(_)
        | Statement::ShowViews
        | Statement::Describe(ast::Describe::Name(_))
        | Statement::Prepare { .. }
        | Statement::Deallocate { .. }
        | Statement::ShowPrepared
        | Statement::Lint(_)
        | Statement::ShowChanges { .. }
        | Statement::CreateUser { .. }
        | Statement::DropUser { .. }
        | Statement::Grant(ast::Grant::Membership { .. })
        | Statement::Revoke(ast::Grant::Membership { .. })
        | Statement::SetRole { .. }
        | Statement::ResetRole
        | Statement::ShowUsers
        | Statement::ShowGrants
        | Statement::ShowAudit { .. }
        | Statement::ShowSessions => {}
    }
    Ok(())
}

/// Adds the reads of the tables and views of a FROM item.
fn collect_reads(
    catalog: &impl Catalog,
    item: &ast::From,
    accesses: &mut Vec<Access>,
) -> Result<()> {
    match item {
        ast::From::Table { name, .. } => {
            if !system::is_system_table(catalog, name)? {
                accesses.push(Access::new(name, AccessKind::Read))
            }
        }
        ast::From::Join { left, right, .. } => {
            collect_reads(catalog, left, accesses)?;
            collect_reads(catalog, right, accesses)?;
        }
    }
    Ok(())
}

/// Returns what a statement requires of the current user, besides the
/// privileges of its accesses.
pub fn requirement(statement: &Statement) -> Requirement {
    match statement {
        Statement::CreateUser { .. }
        | Statement::DropUser { .. }
        | Statement::Grant(ast::Grant::Membership { .. })
        | Statement::Revoke(ast::Grant::Membership { .. })
        | Statement::ShowChanges { .. }
        | Statement::ShowAudit { .. } => Requirement::Superuser,
        Statement::SetRole { user } => Requirement::SetRole(user.clone()),

        Statement::Begin { .. }
        | Statement::Commit
        | Statement::Rollback
        | Statement::Explain { .. }
        | Statement::CreateTable { .. }
        | Statement::DropTable { .. }
        | Statement::Delete { .. }
        | Statement::Insert { .. }
        | Statement::Update { .. }
        | Statement::Select { .. }
        | Statement::ShowStatements
        | Statement::Analyze { .. }
        | Statement::ShowStatistics { .. }
        | Statement::Export { .. }
        | Statement::Import { .. }
        | Statement::Format(_)
        | Statement::CreateView { .. }
        | Statement::DropView { .. }
        | Statement::ShowViews
        | Statement::Describe(_)
        | Statement::Estimate(_)
        | Statement::Prepare { .. }
        | Statement::Execute { .. }
        | Statement::Deallocate { .. }
        | Statement::ShowPrepared
        | Statement::Lint(_)
        | Statement::Grant(ast::Grant::Privileges { .. })
        | Statement::Revoke(ast::Grant::Privileges { .. })
        | Statement::ResetRole
        | Statement::ShowUsers
        | Statement::ShowGrants
        | Statement::ShowSessions
        | Statement::CreatePolicy { .. }
        | Statement::DropPolicy { .. }
        | Statement::CreateMaterializedView { .. }
        | Statement::RefreshMaterializedView { .. }
        | Statement::DropMaterializedView { .. } => Requirement::Nothing,
    }
}

/// Checks that the current user may execute a statement: the privileges its
/// accesses require, then its own requirement.
pub fn check(catalog: &impl Catalog, login: &str, user: &str, statement: &Statement) -> Result<()> {
    // A superuser has every privilege.
    let superuser = is_superuser(catalog, user)?;
    if !superuser {
        for access in accesses(catalog, statement)? {
            check_access(catalog, user, &access)?;
        }
    }
    match requirement(statement) {
        Requirement::Nothing => Ok(()),
        Requirement::Superuser if superuser => Ok(()),
        Requirement::Superuser => errperm!("user {user} is not a superuser"),
        Requirement::SetRole(role) => {
            if is_superuser(catalog, login)? || acts_for(catalog, login)?.contains(&role) {
                return Ok(());
            }
            errperm!("user {login} can't set role {role}")
        }
    }
}

/// Checks that a user that isn't a superuser may make an access.
fn check_access(catalog: &impl Catalog, user: &str, access: &Access) -> Result<()> {
    let name = &access.name;
    let privilege = match access.kind {
        AccessKind::Create => return Ok(()),
        AccessKind::Read => Privilege::Select,
        AccessKind::Insert => Privilege::Insert,
        AccessKind::Update => Privilege::Update,
        AccessKind::Delete => Privilege::Delete,
        AccessKind::Drop
        | AccessKind::Alter
        | AccessKind::Policy
        | AccessKind::Analyze
        | AccessKind::Refresh
        | AccessKind::Grant => {
            if is_object(catalog, name)? && !acts_for_owner(catalog, user, name)? {
                return errperm!("user {user} is not the owner of {name}");
            }
            return Ok(());
        }
    };
    if is_object(catalog, name)? && !has_privilege(catalog, user, privilege, name)? {
        return errperm!("user {user} has no {privilege} privilege on {name}");
    }
    Ok(())
}

/// Returns true if a user has a privilege on a table or view: it is a
/// superuser, acts for the owner, or acts for a user the privilege was granted
/// to.
pub fn has_privilege(
    catalog: &impl Catalog,
    user: &str,
    privilege: Privilege,
    object: &str,
) -> Result<bool> {
    if is_superuser(catalog, user)? {
        return Ok(true);
    }
    let acted_for = acts_for(catalog, user)?;
    if acted_for.contains(&catalog.get_owner(object)?) {
        return Ok(true);
    }
    Ok(catalog.list_grants()?.into_iter().any(|(grant_object, grantee, privileges)| {
        grant_object == object && acted_for.contains(&grantee) && privileges.contains(&privilege)
    }))
}

/// Returns true if a user is a superuser or acts for the owner of an object.
pub fn acts_for_owner(catalog: &impl Catalog, user: &str, object: &str) -> Result<bool> {
    Ok(is_superuser(catalog, user)?
        || acts_for(catalog, user)?.contains(&catalog.get_owner(object)?))
}

/// Returns true if a user exists and is a superuser.
pub fn is_superuser(catalog: &impl Catalog, user: &str) -> Result<bool> {
    Ok(user == ADMIN || users::get_user(catalog, user)?.is_some_and(|user| user.superuser))
}

/// Returns the users that a user acts for: itself, the users it is a member
/// of, and the users those act for.
pub fn acts_for(catalog: &impl Catalog, user: &str) -> Result<BTreeSet<String>> {
    let memberships = catalog.list_memberships()?;
    let mut acted_for = BTreeSet::from([user.to_string()]);
    let mut pending = vec![user.to_string()];
    while let Some(member) = pending.pop() {
        for (_, group) in memberships.iter().filter(|(m, _)| *m == member) {
            if acted_for.insert(group.clone()) {
                pending.push(group.clone());
            }
        }
    }
    Ok(acted_for)
}

/// Returns true if the name is a table, view or materialized view.
pub fn is_object(catalog: &impl Catalog, name: &str) -> Result<bool> {
    Ok(catalog.get_table(name)?.is_some()
        || catalog.get_view(name)?.is_some()
        || catalog.get_materialized_view(name)?.is_some())
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::engine::{Engine, Local};
    use crate::sql::parser::Parser;
    use crate::storage::Memory;

    /// Creates a local engine and executes the given statements in a session.
    fn setup(statements: &[&str]) -> Local<Memory> {
        let engine = Local::new(Memory::new());
        let mut session = engine.session();
        for statement in statements {
            session.execute(statement).expect("statement failed");
        }
        drop(session);
        engine
    }

    /// An engine with tables a and b, the view v, the users alice, bob (a
    /// member of alice) and the superuser root. alice owns b and has the
    /// SELECT privilege on a.
    fn engine() -> Local<Memory> {
        setup(&[
            "CREATE TABLE a (id INT PRIMARY KEY)",
            "CREATE VIEW v AS SELECT id FROM a",
            "CREATE USER alice",
            "CREATE USER bob",
            "CREATE USER root SUPERUSER",
            "GRANT alice TO bob",
            "GRANT SELECT ON a TO alice",
            "SET ROLE alice",
            "CREATE TABLE b (id INT PRIMARY KEY)",
        ])
    }

    #[test_case("SELECT * FROM a JOIN b ON a.id = b.id, a AS x" => "a (read), b (read)"; "select")]
    #[test_case("SELECT * FROM nosuch" => "nosuch (read)"; "select missing")]
    #[test_case("SELECT 1" => ""; "select constant")]
    #[test_case("INSERT INTO a VALUES (1)" => "a (insert)"; "insert")]
    #[test_case("UPDATE a SET id = 2" => "a (update)"; "update")]
    #[test_case("DELETE FROM a" => "a (delete)"; "delete")]
    #[test_case("IMPORT INTO a FROM CSV 'id'" => "a (insert)"; "import")]
    #[test_case("CREATE TABLE c (id INT PRIMARY KEY, a_id INT REFERENCES a)" => "c (create)"; "create table")]
    #[test_case("DROP TABLE IF EXISTS nosuch" => "nosuch (drop)"; "drop table")]
    #[test_case("CREATE VIEW w AS SELECT * FROM v JOIN a ON TRUE" => "w (create), v (read), a (read)"; "create view")]
    #[test_case("DROP VIEW v" => "v (drop)"; "drop view")]
    #[test_case("ANALYZE a" => "a (analyze)"; "analyze table")]
    #[test_case("ANALYZE" => "a (analyze), b (analyze)"; "analyze")]
    #[test_case("SHOW STATISTICS b" => "b (read)"; "show statistics")]
    #[test_case("EXPORT a AS CSV" => "a (read)"; "export table")]
    #[test_case("EXPORT AS SQL" => "a (read), b (read), v (read)"; "export sql")]
    #[test_case("EXPLAIN SELECT * FROM b" => "b (read)"; "explain")]
    #[test_case("EXPLAIN (ANALYZE) DELETE FROM a" => "a (delete)"; "explain analyze")]
    #[test_case("DESCRIBE UPDATE a SET id = 1" => "a (update)"; "describe statement")]
    #[test_case("DESCRIBE a" => ""; "describe name")]
    #[test_case("ESTIMATE DELETE FROM b" => "b (delete)"; "estimate")]
    #[test_case("LINT DELETE FROM b" => ""; "lint")]
    #[test_case("FORMAT DELETE FROM b" => ""; "format")]
    #[test_case("PREPARE p AS DELETE FROM b" => ""; "prepare")]
    #[test_case("EXECUTE p" => ""; "execute")]
    #[test_case("GRANT SELECT, INSERT ON v TO bob" => "v (grant)"; "grant privileges")]
    #[test_case("REVOKE DELETE ON nosuch FROM bob" => "nosuch (grant)"; "revoke privileges")]
    #[test_case("GRANT alice TO root" => ""; "grant membership")]
    #[test_case("CREATE USER carol" => ""; "create user")]
    #[test_case("SET ROLE bob" => ""; "set role")]
    #[test_case("SHOW GRANTS" => ""; "show grants")]
    #[test_case("CREATE POLICY p ON a TO bob USING (id > 0)" => "a (policy)"; "create policy")]
    #[test_case("DROP POLICY IF EXISTS p ON nosuch" => "nosuch (policy)"; "drop policy")]
    fn accesses(statement: &str) -> String {
        let engine = engine();
        let txn = engine.begin_read_only().unwrap();
        let statement = Parser::parse(statement).unwrap();
        super::accesses(&txn, &statement)
            .unwrap()
            .into_iter()
            .map(|access| format!("{} ({})", access.name, access.kind))
            .join(", ")
    }

    #[test_case("CREATE USER carol" => Requirement::Superuser; "create user")]
    #[test_case("DROP USER IF EXISTS carol" => Requirement::Superuser; "drop user")]
    #[test_case("GRANT alice TO bob" => Requirement::Superuser; "grant membership")]
    #[test_case("REVOKE alice FROM bob" => Requirement::Superuser; "revoke membership")]
    #[test_case("SHOW CHANGES SINCE 1" => Requirement::Superuser; "show changes")]
    #[test_case("SHOW AUDIT" => Requirement::Superuser; "show audit")]
    #[test_case("SHOW SESSIONS" => Requirement::Nothing; "show sessions")]
    #[test_case("SET ROLE bob" => Requirement::SetRole("bob".into()); "set role")]
    #[test_case("GRANT SELECT ON a TO bob" => Requirement::Nothing; "grant privileges")]
    #[test_case("RESET ROLE" => Requirement::Nothing; "reset role")]
    #[test_case("SHOW USERS" => Requirement::Nothing; "show users")]
    #[test_case("EXPLAIN SELECT 1" => Requirement::Nothing; "explain")]
    #[test_case("CREATE POLICY p ON a USING (TRUE)" => Requirement::Nothing; "create policy")]
    #[test_case("DROP POLICY p ON a" => Requirement::Nothing; "drop policy")]
    fn requirement(statement: &str) -> Requirement {
        super::requirement(&Parser::parse(statement).unwrap())
    }

    /// Checks a statement for the given login and current user.
    fn check(engine: &Local<Memory>, login: &str, user: &str, statement: &str) -> String {
        let txn = engine.begin_read_only().unwrap();
        let statement = Parser::parse(statement).unwrap();
        match super::check(&txn, login, user, &statement) {
            Ok(()) => "ok".to_string(),
            Err(error) => error.to_string(),
        }
    }

    #[test]
    fn check_accesses() {
        let engine = engine();
        let check = |user, statement| check(&engine, user, user, statement);

        // Granted privileges, also through membership.
        assert_eq!(check("alice", "SELECT * FROM a"), "ok");
        assert_eq!(check("bob", "SELECT * FROM a"), "ok");
        assert_eq!(
            check("alice", "INSERT INTO a VALUES (1)"),
            "permission denied: user alice has no INSERT privilege on a"
        );
        assert_eq!(
            check("alice", "SELECT * FROM v"),
            "permission denied: user alice has no SELECT privilege on v"
        );

        // Ownership, also through membership.
        assert_eq!(check("alice", "DELETE FROM b"), "ok");
        assert_eq!(check("bob", "DROP TABLE b"), "ok");
        assert_eq!(check("bob", "GRANT SELECT ON b TO root"), "ok");
        assert_eq!(
            check("alice", "DROP TABLE a"),
            "permission denied: user alice is not the owner of a"
        );
        assert_eq!(check("bob", "ANALYZE"), "permission denied: user bob is not the owner of a");

        // The first failing access is reported; creating and unknown names
        // aren't checked.
        assert_eq!(
            check("alice", "SELECT * FROM a, b, v"),
            "permission denied: user alice has no SELECT privilege on v"
        );
        assert_eq!(check("alice", "CREATE VIEW w AS SELECT * FROM nosuch, a"), "ok");
        assert_eq!(check("alice", "DROP TABLE IF EXISTS nosuch"), "ok");

        // Superusers have every privilege, and other users that don't exist
        // have none.
        assert_eq!(check("root", "DROP TABLE b"), "ok");
        assert_eq!(check("admin", "INSERT INTO a VALUES (1)"), "ok");
        assert_eq!(
            check("nosuch", "SELECT * FROM a"),
            "permission denied: user nosuch has no SELECT privilege on a"
        );
    }

    #[test]
    fn check_requirements() {
        let engine = setup(&[
            "CREATE USER alice",
            "CREATE USER bob",
            "CREATE USER root SUPERUSER",
            "GRANT alice TO bob",
            "GRANT root TO alice",
        ]);

        // Superuser status isn't passed on through membership.
        assert_eq!(check(&engine, "admin", "root", "CREATE USER x"), "ok");
        assert_eq!(
            check(&engine, "admin", "alice", "SHOW CHANGES"),
            "permission denied: user alice is not a superuser"
        );
        assert_eq!(
            check(&engine, "admin", "bob", "REVOKE alice FROM bob"),
            "permission denied: user bob is not a superuser"
        );

        // SET ROLE depends on the login user, not the current one.
        assert_eq!(check(&engine, "bob", "bob", "SET ROLE alice"), "ok");
        assert_eq!(check(&engine, "bob", "bob", "SET ROLE root"), "ok");
        assert_eq!(check(&engine, "bob", "admin", "SET ROLE bob"), "ok");
        assert_eq!(
            check(&engine, "alice", "alice", "SET ROLE bob"),
            "permission denied: user alice can't set role bob"
        );
        assert_eq!(
            check(&engine, "alice", "admin", "SET ROLE bob"),
            "permission denied: user alice can't set role bob"
        );
        assert_eq!(check(&engine, "root", "alice", "SET ROLE bob"), "ok");
    }

    #[test]
    fn acts_for() {
        let engine = setup(&[
            "CREATE USER a",
            "CREATE USER b",
            "CREATE USER c",
            "CREATE USER d",
            "GRANT b TO a",
            "GRANT c TO b",
            "GRANT d TO c",
            "GRANT d TO a",
        ]);
        let txn = engine.begin_read_only().unwrap();
        let acts_for = |user| super::acts_for(&txn, user).unwrap().into_iter().join(",");
        assert_eq!(acts_for("a"), "a,b,c,d");
        assert_eq!(acts_for("b"), "b,c,d");
        assert_eq!(acts_for("d"), "d");
        assert_eq!(acts_for("nosuch"), "nosuch");
    }

    #[test]
    fn has_privilege() {
        let engine = engine();
        let txn = engine.begin_read_only().unwrap();
        let has = |user, privilege, object| super::has_privilege(&txn, user, privilege, object);
        assert!(has("alice", Privilege::Select, "a").unwrap());
        assert!(!has("alice", Privilege::Delete, "a").unwrap());
        assert!(has("bob", Privilege::Delete, "b").unwrap());
        assert!(has("root", Privilege::Update, "v").unwrap());
        assert!(!has("bob", Privilege::Select, "v").unwrap());
    }
}
