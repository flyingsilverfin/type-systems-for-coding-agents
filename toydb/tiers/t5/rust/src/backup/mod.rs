//! Backups of a whole database: created in one transaction (a consistent
//! snapshot), restored in one transaction, both by a superuser.
//!
//! A backup holds the users and memberships, the tables with their owners,
//! check constraints and rows, the views, the grants and the policies — not
//! statistics, prepared statements, digests or the logs. A backup can be
//! created as of an earlier version. A restore first checks the whole file,
//! then applies its records in order with the effects and errors of the
//! corresponding statements; the first error ends it and nothing is restored.

pub mod format;

pub use format::Record;

use itertools::Itertools as _;

use crate::error::Result;
use crate::sql::engine::Transaction;
use crate::sql::execution::export::ViewDefinition;
use crate::sql::execution::system::SessionState;
use crate::sql::execution::{checks, export, import, matviews, policies, privileges, users, views};
use crate::sql::parser::{Parser, ast};
use crate::sql::types::{ADMIN, MaterializedView, View};
use crate::{errinput, errperm};

/// Creates a backup of the database the transaction sees.
pub fn create(txn: &impl Transaction, user: &str) -> Result<Vec<u8>> {
    check_superuser(txn, user)?;
    Ok(format::write(&records(txn)?))
}

/// Restores a backup into the database, in the transaction.
pub fn restore(
    txn: &impl Transaction,
    user: &str,
    data: &[u8],
    session: Option<&SessionState>,
) -> Result<()> {
    check_superuser(txn, user)?;
    for record in format::read(data)? {
        apply(txn, record, session)?;
    }
    Ok(())
}

/// Errors if the user isn't a superuser.
fn check_superuser(txn: &impl Transaction, user: &str) -> Result<()> {
    if !privileges::is_superuser(txn, user)? {
        return errperm!("user {user} is not a superuser");
    }
    Ok(())
}

/// Returns the records of the database that the transaction sees, in backup
/// order.
fn records(txn: &impl Transaction) -> Result<Vec<Record>> {
    let mut records = Vec::new();
    let users = txn.list_users()?.into_iter().sorted_by(|a, b| a.name.cmp(&b.name));
    records.extend(users.map(Record::User));
    let memberships = txn.list_memberships()?.into_iter().sorted();
    records.extend(memberships.map(|(member, user)| Record::Member { member, user }));

    let tables = txn.list_tables()?; // in name order
    for table in export::table_order(tables.clone()) {
        let name = table.name.clone();
        let owner = txn.get_owner(&name)?;
        records.push(Record::Table { table, owner });
        for check in txn.get_checks(&name)? {
            records.push(Record::Check { table: name.clone(), check });
        }
        for row in txn.scan(&name, None)? {
            records.push(Record::Row { table: name.clone(), values: row? });
        }
    }
    for view in export::view_order(txn.list_views()?, txn.list_materialized_views()?) {
        let owner = txn.get_owner(view.name())?;
        records.push(match view {
            ViewDefinition::View(View { name, columns, query, .. }) => {
                Record::View { name, owner, columns, query }
            }
            ViewDefinition::Materialized(MaterializedView { name, columns, query, .. }) => {
                Record::MaterializedView { name, owner, columns, query }
            }
        });
    }

    let grants = txn.list_grants()?.into_iter();
    for (object, grantee, privileges) in grants.sorted_by(|a, b| (&a.0, &a.1).cmp(&(&b.0, &b.1))) {
        records.push(Record::Grant { object, grantee, privileges });
    }
    for table in &tables {
        records.extend(txn.list_policies(&table.name)?.into_iter().map(Record::Policy));
    }
    Ok(records)
}

/// Applies a record with the effects and errors of its statement.
fn apply(txn: &impl Transaction, record: Record, session: Option<&SessionState>) -> Result<()> {
    match record {
        Record::User(user) => users::create_stored_user(txn, user),
        Record::Member { member, user } => users::grant_membership(txn, &user, &member),
        Record::Table { table, owner } => {
            users::must_get_user(txn, &owner)?;
            let name = table.name.clone();
            txn.create_table(table)?;
            users::set_creator(txn, &name, &owner)
        }
        Record::Check { table, check } => checks::add_check(txn, &table, check),
        Record::Row { table, values } => {
            let table = txn.must_get_table(&table)?;
            let columns = (0..values.len()).collect();
            import::insert(txn, table, vec![(columns, values)], ADMIN)?;
            Ok(())
        }
        Record::View { name, owner, columns, query } => {
            users::must_get_user(txn, &owner)?;
            let statement = parse_view_query(&query)?;
            views::create_view(txn, &name, Some(columns), statement)?;
            users::set_creator(txn, &name, &owner)
        }
        Record::MaterializedView { name, owner, columns, query } => {
            users::must_get_user(txn, &owner)?;
            let statement = parse_view_query(&query)?;
            let columns = Some(columns);
            matviews::create_materialized_view(txn, &name, columns, statement, ADMIN, session)?;
            users::set_creator(txn, &name, &owner)
        }
        Record::Grant { object, grantee, privileges } => {
            users::grant(txn, &privileges.into_iter().collect_vec(), &object, &grantee)
        }
        Record::Policy(policy) => {
            let using = Parser::parse_expr(&policy.using)?;
            let with_check = policy.with_check.as_deref().map(Parser::parse_expr).transpose()?;
            let user = policy.user.as_deref();
            policies::create_policy(
                txn,
                &policy.name,
                &policy.table,
                user,
                &using,
                with_check.as_ref(),
            )
        }
    }
}

/// Parses the query of a VIEW or MATVIEW record, which must be a SELECT.
fn parse_view_query(query: &str) -> Result<ast::Statement> {
    let statement = Parser::parse(query)?;
    if !matches!(statement, ast::Statement::Select { .. }) {
        return errinput!("view query is not a SELECT: {query}");
    }
    Ok(statement)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sql::engine::{Catalog as _, Engine, Local};
    use crate::sql::execution::StatementResult;
    use crate::sql::types::Value;
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

    /// Creates a backup of the engine's database as the given user.
    fn backup(engine: &Local<Memory>, user: &str) -> Result<Vec<u8>> {
        let txn = engine.begin_read_only()?;
        let result = create(&txn, user);
        txn.commit()?;
        result
    }

    /// Restores a backup into the engine's database as admin, committing the
    /// transaction if it succeeds.
    fn restore_admin(engine: &Local<Memory>, data: &[u8]) -> Result<()> {
        let txn = engine.begin()?;
        let result = restore(&txn, ADMIN, data, None);
        match result {
            Ok(_) => txn.commit()?,
            Err(_) => txn.rollback()?,
        }
        result
    }

    /// Returns the rows of a query, one string each.
    fn query(engine: &Local<Memory>, query: &str) -> Vec<String> {
        let StatementResult::Select { rows, .. } = engine.session().execute(query).unwrap() else {
            panic!("expected select result");
        };
        rows.into_iter().map(|row| row.into_iter().join(", ")).collect()
    }

    const DATABASE: &[&str] = &[
        "CREATE USER alice PASSWORD 'pw'",
        "CREATE USER staff",
        "GRANT staff TO alice",
        "CREATE TABLE p (id INT PRIMARY KEY, n INT CHECK (n > 0))",
        "CREATE TABLE c (id INT PRIMARY KEY, p INT REFERENCES p)",
        "INSERT INTO p VALUES (2, 20), (1, 10)",
        "INSERT INTO c VALUES (1, 2)",
        "CREATE VIEW v AS SELECT id FROM p",
        "CREATE VIEW a AS SELECT id FROM v",
        "CREATE MATERIALIZED VIEW m AS SELECT id FROM a",
        "GRANT SELECT, INSERT ON p TO staff",
        "CREATE POLICY own ON p TO staff USING (id = 1)",
    ];

    #[test]
    fn create_records() -> Result<()> {
        let engine = setup(DATABASE);
        let mut session = engine.session();
        session.execute("SET ROLE alice")?;
        session.execute("CREATE TABLE b (id INT PRIMARY KEY)")?;
        drop(session);

        let lines = format::list(&backup(&engine, ADMIN)?);
        assert_eq!(
            lines[1..lines.len() - 1],
            [
                "user alice password",
                "user staff",
                "member alice of staff",
                "table b owner alice: CREATE TABLE b (id INTEGER PRIMARY KEY)",
                "table p owner admin: CREATE TABLE p (id INTEGER PRIMARY KEY, n INTEGER DEFAULT NULL)",
                "check p.p_check1: n > 0",
                "row p: 1, 10",
                "row p: 2, 20",
                "table c owner admin: CREATE TABLE c (id INTEGER PRIMARY KEY, \
                 p INTEGER DEFAULT NULL INDEX REFERENCES p)",
                "row c: 1, 2",
                "view v owner admin (id): SELECT id FROM p",
                "view a owner admin (id): SELECT id FROM v",
                "materialized view m owner admin (id): SELECT id FROM a",
                "grant p to staff: SELECT, INSERT",
                "policy p.own to staff: id = 1",
            ]
        );
        Ok(())
    }

    #[test]
    fn restore_recreates_database() -> Result<()> {
        let engine = setup(DATABASE);
        let data = backup(&engine, ADMIN)?;

        let empty = setup(&[]);
        restore_admin(&empty, &data)?;
        assert_eq!(backup(&empty, ADMIN)?, data);
        assert_eq!(query(&empty, "SELECT * FROM a"), vec!["1", "2"]);
        assert_eq!(query(&empty, "SELECT * FROM m"), vec!["1", "2"]);

        // The password and the policy are restored.
        let mut session = empty.session();
        session.login("alice", Some("pw"))?;
        let StatementResult::Select { rows, .. } = session.execute("SELECT * FROM p")? else {
            panic!("expected select result");
        };
        assert_eq!(rows, vec![vec![1.into(), 10.into()]]);
        let error = session.execute("INSERT INTO p VALUES (3, 0)").unwrap_err();
        assert_eq!(error.to_string(), "invalid input: row 3 violates check constraint p_check1");
        Ok(())
    }

    #[test]
    fn restore_errors() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)", "INSERT INTO t VALUES (1)"]);
        let data = backup(&engine, ADMIN)?;

        // A failing record restores nothing.
        let target = setup(&["CREATE USER alice", "CREATE TABLE u (id INT PRIMARY KEY)"]);
        let mut records = format::read(&data)?;
        records.push(Record::Table {
            table: target.begin_read_only()?.must_get_table("u")?,
            owner: ADMIN.into(),
        });
        let error = restore_admin(&target, &format::write(&records)).unwrap_err();
        assert_eq!(error.to_string(), "invalid input: table u already exists");
        assert!(target.begin_read_only()?.get_table("t")?.is_none());

        // Only superusers create and restore backups.
        let txn = target.begin()?;
        let error = restore(&txn, "alice", &data, None).unwrap_err();
        assert_eq!(error.to_string(), "permission denied: user alice is not a superuser");
        let error = create(&txn, "alice").unwrap_err();
        assert_eq!(error.to_string(), "permission denied: user alice is not a superuser");
        txn.rollback()?;

        // A table or view whose owner doesn't exist, and a view or materialized
        // view whose query isn't a SELECT.
        let table = target.begin_read_only()?.must_get_table("u")?;
        let view = |owner: &str, query: &str| Record::View {
            name: "x".into(),
            owner: owner.into(),
            columns: vec!["id".into()],
            query: query.into(),
        };
        for (record, expect) in [
            (Record::Table { table, owner: "bob".into() }, "user bob does not exist"),
            (view("bob", "SELECT id FROM u"), "user bob does not exist"),
            (view(ADMIN, "DELETE FROM u"), "view query is not a SELECT: DELETE FROM u"),
            (
                Record::MaterializedView {
                    name: "x".into(),
                    owner: ADMIN.into(),
                    columns: vec!["id".into()],
                    query: "DELETE FROM u".into(),
                },
                "view query is not a SELECT: DELETE FROM u",
            ),
        ] {
            let error = restore_admin(&target, &format::write(&[record])).unwrap_err();
            assert_eq!(error.to_string(), format!("invalid input: {expect}"));
        }

        // A row of a missing table, and a row that violates the table's
        // definition.
        let row = |values: Vec<Value>| Record::Row { table: "t".into(), values };
        let error = restore_admin(&target, &format::write(&[row(vec![1.into()])])).unwrap_err();
        assert_eq!(error.to_string(), "invalid input: table t does not exist");
        records = format::read(&data)?;
        records.push(row(vec![1.into()]));
        let error = restore_admin(&target, &format::write(&records)).unwrap_err();
        assert_eq!(error.to_string(), "invalid input: primary key 1 already exists");
        Ok(())
    }
}
