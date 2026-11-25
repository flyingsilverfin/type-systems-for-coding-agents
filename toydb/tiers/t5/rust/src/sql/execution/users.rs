//! CREATE USER, DROP USER, GRANT, REVOKE, SET ROLE, RESET ROLE, SHOW USERS and
//! SHOW GRANTS, and login.
//!
//! * Users are created and dropped by superusers. Dropping a user removes its
//!   memberships and the privileges granted to it; a user that owns a table or
//!   view, or that a policy names, can't be dropped, nor can `admin` or the
//!   session's current or login user.
//! * GRANT and REVOKE of privileges on a table or view require acting for its
//!   owner; on a view only SELECT can be granted. GRANT and REVOKE of a user
//!   make a user a member of another one or end that; they require a superuser,
//!   and memberships can't form a cycle.
//! * SET ROLE makes another user the session's current user, if the login user
//!   is a superuser or acts for it; RESET ROLE returns to the login user.
//!   Neither is transactional.
//! * SHOW GRANTS lists, for every table and view, its owner as the privilege
//!   `OWNER` and the granted privileges.

use std::collections::BTreeSet;

use itertools::Itertools as _;

use super::StatementResult;
use super::privileges::acts_for;
use crate::encoding::md5;
use crate::error::Result;
use crate::sql::engine::Catalog;
use crate::sql::types::{ADMIN, Label, Privilege, User, Value};
use crate::{errinput, errperm};

/// Creates a user, after checking its name.
pub fn create_user(
    catalog: &impl Catalog,
    name: &str,
    password: Option<&str>,
    superuser: bool,
) -> Result<()> {
    let password = password.map(|password| hash_password(name, password));
    create_stored_user(catalog, User { name: name.to_string(), superuser, password })
}

/// Creates a user with a stored password hash, after checking its name.
pub fn create_stored_user(catalog: &impl Catalog, user: User) -> Result<()> {
    if user.name.is_empty() {
        return errinput!("user name can't be empty");
    }
    if get_user(catalog, &user.name)?.is_some() {
        return errinput!("user {} already exists", user.name);
    }
    catalog.create_user(user)
}

/// Drops a user, its memberships and its privileges. Returns true if the user
/// existed.
pub fn drop_user(
    catalog: &impl Catalog,
    name: &str,
    if_exists: bool,
    login: &str,
    current: &str,
) -> Result<bool> {
    if get_user(catalog, name)?.is_none() {
        if if_exists {
            return Ok(false);
        }
        return errinput!("user {name} does not exist");
    }
    if name == ADMIN {
        return errinput!("user {name} can't be dropped");
    }
    if name == current || name == login {
        return errinput!("can't drop the current user {name}");
    }
    for object in objects(catalog)? {
        if catalog.get_owner(&object)? == name {
            return errinput!("user {name} owns {object}");
        }
    }
    for table in catalog.list_tables()? {
        let policies = catalog.list_policies(&table.name)?;
        if let Some(policy) = policies.iter().find(|policy| policy.user.as_deref() == Some(name)) {
            return errinput!(
                "user {name} is used by policy {} on table {}",
                policy.name,
                table.name
            );
        }
    }
    catalog.drop_user(name)?;
    Ok(true)
}

/// Grants privileges on a table or view to a user.
pub fn grant(
    catalog: &impl Catalog,
    privileges: &[Privilege],
    object: &str,
    user: &str,
) -> Result<()> {
    let granted = check_grant(catalog, privileges, object, user)?;
    let privileges = granted.union(&privileges.iter().copied().collect()).copied().collect();
    if privileges != granted {
        catalog.set_privileges(object, user, privileges)?;
    }
    Ok(())
}

/// Revokes privileges on a table or view from a user.
pub fn revoke(
    catalog: &impl Catalog,
    privileges: &[Privilege],
    object: &str,
    user: &str,
) -> Result<()> {
    let granted = check_grant(catalog, privileges, object, user)?;
    let privileges = granted.difference(&privileges.iter().copied().collect()).copied().collect();
    if privileges != granted {
        catalog.set_privileges(object, user, privileges)?;
    }
    Ok(())
}

/// Checks a GRANT or REVOKE of privileges on a table or view, and returns the
/// privileges the user holds on it.
fn check_grant(
    catalog: &impl Catalog,
    privileges: &[Privilege],
    object: &str,
    user: &str,
) -> Result<BTreeSet<Privilege>> {
    let view = catalog.get_view(object)?.is_some()
        || catalog.get_materialized_view(object)?.is_some();
    if !view && catalog.get_table(object)?.is_none() {
        return errinput!("table {object} does not exist");
    }
    if view && privileges.iter().any(|privilege| *privilege != Privilege::Select) {
        return errinput!("only SELECT can be granted on view {object}");
    }
    must_get_user(catalog, user)?;
    Ok(catalog
        .list_grants()?
        .into_iter()
        .find(|(grant_object, grantee, _)| grant_object == object && grantee == user)
        .map(|(_, _, privileges)| privileges)
        .unwrap_or_default())
}

/// Makes a user a member of another user, unless that would create a cycle.
pub fn grant_membership(catalog: &impl Catalog, group: &str, user: &str) -> Result<()> {
    must_get_user(catalog, group)?;
    must_get_user(catalog, user)?;
    if group == user {
        return errinput!("user {user} can't be a member of itself");
    }
    if acts_for(catalog, group)?.contains(user) {
        return errinput!("granting {group} to {user} would create a cycle");
    }
    catalog.set_membership(user, group, true)
}

/// Ends a user's membership of another user, if it is a member.
pub fn revoke_membership(catalog: &impl Catalog, group: &str, user: &str) -> Result<()> {
    must_get_user(catalog, group)?;
    must_get_user(catalog, user)?;
    catalog.set_membership(user, group, false)
}

/// Makes a user the owner of a table or view that it created.
pub fn set_creator(catalog: &impl Catalog, object: &str, user: &str) -> Result<()> {
    // Ownership by admin isn't stored.
    if user != ADMIN {
        catalog.set_owner(object, user)?;
    }
    Ok(())
}

/// Checks a login: the user must exist, and the password must be the user's
/// password (none for a user without one).
pub fn check_login(catalog: &impl Catalog, name: &str, password: Option<&str>) -> Result<()> {
    let user = must_get_user(catalog, name)?;
    if password.map(|password| hash_password(name, password)) != user.password {
        return errperm!("password authentication failed for user {name}");
    }
    Ok(())
}

/// Returns the stored form of a user's password: md5 and the hex MD5 of the
/// password followed by the user name.
pub fn hash_password(user: &str, password: &str) -> String {
    format!("md5{}", md5::hex(format!("{password}{user}").as_bytes()))
}

/// Fetches a user, admin included, or None if it doesn't exist.
pub fn get_user(catalog: &impl Catalog, name: &str) -> Result<Option<User>> {
    if name == ADMIN {
        return Ok(Some(User::admin()));
    }
    catalog.get_user(name)
}

/// Fetches a user, admin included, or errors if it doesn't exist.
pub fn must_get_user(catalog: &impl Catalog, name: &str) -> Result<User> {
    get_user(catalog, name)?.ok_or_else(|| errinput!("user {name} does not exist"))
}

/// Returns the SHOW USERS result.
pub fn show_users(catalog: &impl Catalog) -> Result<StatementResult> {
    let columns = ["name", "is_superuser", "has_password", "member_of"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    let memberships = catalog.list_memberships()?;
    let users = std::iter::once(User::admin()).chain(catalog.list_users()?);
    let rows = users
        .sorted_by(|a, b| a.name.cmp(&b.name))
        .map(|user| {
            let member_of = memberships
                .iter()
                .filter(|(member, _)| *member == user.name)
                .map(|(_, group)| group)
                .sorted()
                .join(", ");
            vec![
                Value::String(user.name),
                Value::Boolean(user.superuser),
                Value::Boolean(user.password.is_some()),
                Value::String(member_of),
            ]
        })
        .collect();
    Ok(StatementResult::Select { columns, rows })
}

/// Returns the SHOW GRANTS result: the owner and the privileges of every table
/// and view, by object, privilege and grantee.
pub fn show_grants(catalog: &impl Catalog) -> Result<StatementResult> {
    let columns = ["object", "grantee", "privilege"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    let grants = catalog.list_grants()?;
    let mut rows = Vec::new();
    for object in objects(catalog)? {
        let owner = catalog.get_owner(&object)?;
        rows.push(row(&object, owner, "OWNER".to_string()));
        let privileges = grants
            .iter()
            .filter(|(grant_object, _, _)| *grant_object == object)
            .flat_map(|(_, grantee, privileges)| privileges.iter().map(move |p| (*p, grantee)))
            .sorted();
        for (privilege, grantee) in privileges {
            rows.push(row(&object, grantee.clone(), privilege.to_string()));
        }
    }
    Ok(StatementResult::Select { columns, rows })
}

/// Returns a row of the SHOW GRANTS result.
fn row(object: &str, grantee: String, privilege: String) -> Vec<Value> {
    vec![Value::String(object.to_string()), Value::String(grantee), Value::String(privilege)]
}

/// Returns the names of all tables, views and materialized views, sorted.
pub fn objects(catalog: &impl Catalog) -> Result<Vec<String>> {
    let tables = catalog.list_tables()?.into_iter().map(|table| table.name);
    let views = catalog.list_views()?.into_iter().map(|view| view.name);
    let matviews = catalog.list_materialized_views()?.into_iter().map(|view| view.name);
    Ok(tables.chain(views).chain(matviews).sorted().collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sql::engine::{Engine, Local, Transaction as _};
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

    /// Runs a closure in a new transaction, committing it if it succeeds.
    fn with_txn<T>(
        engine: &Local<Memory>,
        f: impl FnOnce(&<Local<Memory> as Engine>::Transaction) -> Result<T>,
    ) -> Result<T> {
        let txn = engine.begin()?;
        let result = f(&txn);
        match result {
            Ok(_) => txn.commit()?,
            Err(_) => txn.rollback()?,
        }
        result
    }

    /// Returns the rows of a SHOW result, one string each.
    fn rows(result: StatementResult) -> Vec<String> {
        let StatementResult::Select { rows, .. } = result else {
            panic!("expected select result");
        };
        rows.into_iter().map(|row| row.into_iter().join(", ")).collect()
    }

    #[test]
    fn hash_password() {
        assert_eq!(super::hash_password("alice", "pw"), "md5aa7bb6fc2700e717ae61ec5c1567469c");
    }

    #[test]
    fn create_stored_user() -> Result<()> {
        let engine = setup(&["CREATE USER alice"]);
        let user = User { name: "bob".into(), superuser: false, password: Some("md5ab".into()) };
        with_txn(&engine, |txn| super::create_stored_user(txn, user.clone()))?;
        assert_eq!(get_user(&engine.begin_read_only()?, "bob")?, Some(user));
        let error = |name: &str| {
            let user = User { name: name.into(), superuser: true, password: None };
            with_txn(&engine, |txn| super::create_stored_user(txn, user)).unwrap_err().to_string()
        };
        assert_eq!(error("alice"), "invalid input: user alice already exists");
        assert_eq!(error("admin"), "invalid input: user admin already exists");
        assert_eq!(error(""), "invalid input: user name can't be empty");
        Ok(())
    }

    #[test]
    fn create_drop_user() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);
        with_txn(&engine, |txn| create_user(txn, "alice", Some("pw"), false))?;
        with_txn(&engine, |txn| create_user(txn, "Bob", None, true))?;
        let txn = engine.begin_read_only()?;
        assert_eq!(
            get_user(&txn, "alice")?,
            Some(User {
                name: "alice".into(),
                superuser: false,
                password: Some("md5aa7bb6fc2700e717ae61ec5c1567469c".into())
            })
        );
        assert_eq!(get_user(&txn, "admin")?, Some(User::admin()));
        assert_eq!(get_user(&txn, "bob")?, None);
        drop(txn);

        let error = |f: &dyn Fn(&_) -> Result<bool>| with_txn(&engine, f).unwrap_err().to_string();
        assert_eq!(
            error(&|txn| create_user(txn, "", None, false).map(|_| true)),
            "invalid input: user name can't be empty"
        );
        assert_eq!(
            error(&|txn| create_user(txn, "admin", None, false).map(|_| true)),
            "invalid input: user admin already exists"
        );
        assert_eq!(
            error(&|txn| drop_user(txn, "nosuch", false, "admin", "admin")),
            "invalid input: user nosuch does not exist"
        );
        assert_eq!(
            error(&|txn| drop_user(txn, "admin", true, "admin", "admin")),
            "invalid input: user admin can't be dropped"
        );
        assert_eq!(
            error(&|txn| drop_user(txn, "Bob", false, "Bob", "admin")),
            "invalid input: can't drop the current user Bob"
        );
        assert_eq!(
            error(&|txn| drop_user(txn, "Bob", false, "admin", "Bob")),
            "invalid input: can't drop the current user Bob"
        );
        with_txn(&engine, |txn| txn.set_owner("t", "Bob"))?;
        assert_eq!(
            error(&|txn| drop_user(txn, "Bob", false, "admin", "admin")),
            "invalid input: user Bob owns t"
        );

        assert!(!with_txn(&engine, |txn| drop_user(txn, "nosuch", true, "admin", "admin"))?);
        assert!(with_txn(&engine, |txn| drop_user(txn, "alice", false, "admin", "admin"))?);
        assert_eq!(engine.begin_read_only()?.get_user("alice")?, None);
        Ok(())
    }

    #[test]
    fn drop_user_memberships_and_grants() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "CREATE USER a",
            "CREATE USER b",
            "CREATE USER c",
            "GRANT b TO a",
            "GRANT c TO b",
            "GRANT SELECT ON t TO b",
            "GRANT INSERT ON t TO c",
            "DROP USER b",
        ]);
        let txn = engine.begin_read_only()?;
        assert_eq!(txn.list_memberships()?, Vec::new());
        assert_eq!(txn.list_grants()?, vec![("t".into(), "c".into(), [Privilege::Insert].into())]);
        Ok(())
    }

    #[test]
    fn drop_user_used_by_policy() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE b (id INT PRIMARY KEY)",
            "CREATE TABLE a (id INT PRIMARY KEY)",
            "CREATE USER alice",
            "CREATE POLICY q ON b TO alice USING (TRUE)",
            "CREATE POLICY p ON b TO alice USING (TRUE)",
            "CREATE POLICY z ON a TO alice USING (TRUE)",
        ]);
        let drop = |engine: &Local<Memory>| {
            with_txn(engine, |txn| drop_user(txn, "alice", false, "admin", "admin"))
        };
        // The first policy by table name, then by policy name.
        assert_eq!(
            drop(&engine).unwrap_err().to_string(),
            "invalid input: user alice is used by policy z on table a"
        );
        let mut session = engine.session();
        session.execute("DROP TABLE a")?;
        assert_eq!(
            drop(&engine).unwrap_err().to_string(),
            "invalid input: user alice is used by policy p on table b"
        );
        session.execute("DROP POLICY p ON b")?;
        session.execute("DROP POLICY q ON b")?;
        assert!(drop(&engine)?);
        Ok(())
    }

    #[test]
    fn grant_revoke() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "CREATE VIEW v AS SELECT * FROM t",
            "CREATE USER alice",
        ]);
        let privileges = |engine: &Local<Memory>| -> Result<Vec<BTreeSet<Privilege>>> {
            Ok(engine.begin_read_only()?.list_grants()?.into_iter().map(|(_, _, p)| p).collect())
        };
        use Privilege::*;

        with_txn(&engine, |txn| grant(txn, &[Delete, Select, Delete], "t", "alice"))?;
        assert_eq!(privileges(&engine)?, vec![[Select, Delete].into()]);
        with_txn(&engine, |txn| grant(txn, &[Insert], "t", "alice"))?;
        assert_eq!(privileges(&engine)?, vec![[Select, Insert, Delete].into()]);
        with_txn(&engine, |txn| revoke(txn, &[Select, Update], "t", "alice"))?;
        assert_eq!(privileges(&engine)?, vec![[Insert, Delete].into()]);
        with_txn(&engine, |txn| revoke(txn, &[Insert, Delete], "t", "alice"))?;
        assert_eq!(privileges(&engine)?, Vec::new());

        // Granting held privileges and revoking others doesn't write, also in
        // a read-only transaction.
        with_txn(&engine, |txn| grant(txn, &[Select], "v", "alice"))?;
        let txn = engine.begin_read_only()?;
        grant(&txn, &[Select], "v", "alice")?;
        revoke(&txn, &[Select], "t", "alice")?;
        assert_eq!(grant(&txn, &[Select], "t", "alice"), Err(crate::error::Error::ReadOnly));

        let error = |privileges: &[Privilege], object, user| {
            with_txn(&engine, |txn| grant(txn, privileges, object, user)).unwrap_err().to_string()
        };
        assert_eq!(
            error(&[Select], "nosuch", "nosuch"),
            "invalid input: table nosuch does not exist"
        );
        assert_eq!(
            error(&[Select, Update], "v", "nosuch"),
            "invalid input: only SELECT can be granted on view v"
        );
        assert_eq!(error(&[Select], "v", "nosuch"), "invalid input: user nosuch does not exist");
        Ok(())
    }

    #[test]
    fn memberships() -> Result<()> {
        let engine = setup(&["CREATE USER a", "CREATE USER b", "CREATE USER c"]);
        with_txn(&engine, |txn| grant_membership(txn, "b", "a"))?;
        with_txn(&engine, |txn| grant_membership(txn, "c", "b"))?;
        with_txn(&engine, |txn| grant_membership(txn, "c", "b"))?;
        assert_eq!(
            engine.begin_read_only()?.list_memberships()?,
            vec![("a".into(), "b".into()), ("b".into(), "c".into())]
        );

        let error = |group, user| {
            with_txn(&engine, |txn| grant_membership(txn, group, user)).unwrap_err().to_string()
        };
        assert_eq!(error("nosuch", "nosuch2"), "invalid input: user nosuch does not exist");
        assert_eq!(error("a", "nosuch2"), "invalid input: user nosuch2 does not exist");
        assert_eq!(error("a", "a"), "invalid input: user a can't be a member of itself");
        assert_eq!(error("a", "c"), "invalid input: granting a to c would create a cycle");
        assert_eq!(error("b", "c"), "invalid input: granting b to c would create a cycle");

        with_txn(&engine, |txn| revoke_membership(txn, "b", "a"))?;
        with_txn(&engine, |txn| revoke_membership(txn, "b", "a"))?;
        assert_eq!(engine.begin_read_only()?.list_memberships()?, vec![("b".into(), "c".into())]);
        with_txn(&engine, |txn| grant_membership(txn, "a", "c"))?;
        Ok(())
    }

    #[test]
    fn check_login() -> Result<()> {
        let engine = setup(&["CREATE USER alice PASSWORD 'pw'", "CREATE USER bob"]);
        let txn = engine.begin_read_only()?;
        let login = |user, password| match super::check_login(&txn, user, password) {
            Ok(()) => "ok".to_string(),
            Err(error) => error.to_string(),
        };
        assert_eq!(login("alice", Some("pw")), "ok");
        assert_eq!(login("bob", None), "ok");
        assert_eq!(login("admin", None), "ok");
        assert_eq!(
            login("alice", Some("wrong")),
            "permission denied: password authentication failed for user alice"
        );
        assert_eq!(
            login("alice", None),
            "permission denied: password authentication failed for user alice"
        );
        assert_eq!(
            login("bob", Some("")),
            "permission denied: password authentication failed for user bob"
        );
        assert_eq!(login("nosuch", None), "invalid input: user nosuch does not exist");
        Ok(())
    }

    #[test]
    fn show_users_grants() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "CREATE VIEW b AS SELECT * FROM t",
            r#"CREATE USER "Zed" PASSWORD 'x' SUPERUSER"#,
            "CREATE USER bob",
            "CREATE USER alice",
            "GRANT bob TO alice",
            r#"GRANT "Zed" TO alice"#,
            "GRANT DELETE, SELECT ON t TO bob",
            "GRANT SELECT ON t TO alice",
            "SET ROLE alice",
            "CREATE TABLE a (id INT PRIMARY KEY)",
        ]);
        let txn = engine.begin_read_only()?;
        assert_eq!(
            rows(show_users(&txn)?),
            vec![
                "'Zed', TRUE, TRUE, ''",
                "'admin', TRUE, FALSE, ''",
                "'alice', FALSE, FALSE, 'Zed, bob'",
                "'bob', FALSE, FALSE, ''",
            ]
        );
        assert_eq!(
            rows(show_grants(&txn)?),
            vec![
                "'a', 'alice', 'OWNER'",
                "'b', 'admin', 'OWNER'",
                "'t', 'admin', 'OWNER'",
                "'t', 'alice', 'SELECT'",
                "'t', 'bob', 'SELECT'",
                "'t', 'bob', 'DELETE'",
            ]
        );
        Ok(())
    }
}
