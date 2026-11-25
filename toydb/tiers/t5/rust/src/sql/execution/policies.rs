//! Row-level security policies: CREATE POLICY and DROP POLICY, and the rows a
//! user reads.
//!
//! * A table with policies is protected. A user that is neither a superuser
//!   nor acts for the table's owner reads only the rows of a protected table
//!   for which the USING condition of one of its applicable policies — those
//!   for a user it acts for, and those without a user — is TRUE, evaluated in
//!   order of name before the statement's own conditions. This holds for every
//!   read of the table's rows: scans, key and index lookups, in queries, views,
//!   UPDATE, DELETE, EXPORT and every statement added later that reads rows.
//!   Plans don't change.
//! * Every row such a user inserts or updates (INSERT, UPDATE, IMPORT and every
//!   statement added later that inserts or updates rows) must satisfy the check
//!   condition (WITH CHECK, or else USING) of one applicable policy, checked
//!   after the row's CHECK constraints and before any row is written.
//! * No statement may drop or rename a column that a policy of its table uses
//!   (that its USING or WITH CHECK condition references): every statement that
//!   drops or renames columns, also one added later, fails with
//!   `column <c> is used by policy <name>`, naming the first such policy by
//!   name, after the check-constraint check and before anything is changed.
//! * DROP TABLE drops the table's policies.

use super::{privileges, users};
use crate::errinput;
use crate::error::Result;
use crate::sql::engine::{Catalog, Transaction};
use crate::sql::parser::{Parser, ast, format};
use crate::sql::planner::Planner;
use crate::sql::types::{Expression, Policy, Row, Rows, Table, Value};

/// Planned policy conditions, with their policy names.
pub type Conditions = Vec<(String, Expression)>;

/// Creates a policy of a table, after checking it.
pub fn create_policy<C: Catalog>(
    catalog: &C,
    name: &str,
    table: &str,
    user: Option<&str>,
    using: &ast::Expression,
    with_check: Option<&ast::Expression>,
) -> Result<()> {
    let table = catalog.must_get_table(table)?;
    if name.is_empty() {
        return errinput!("policy name can't be empty");
    }
    if catalog.list_policies(&table.name)?.iter().any(|policy| policy.name == name) {
        return errinput!("policy {name} already exists on table {}", table.name);
    }
    if let Some(user) = user {
        users::must_get_user(catalog, user)?;
    }
    let using = format::expression(using);
    plan::<C>(&table, &using)?;
    let with_check = with_check.map(format::expression);
    if let Some(with_check) = &with_check {
        plan::<C>(&table, with_check)?;
    }
    catalog.create_policy(Policy {
        table: table.name,
        name: name.to_string(),
        user: user.map(str::to_string),
        using,
        with_check,
    })
}

/// Drops a policy of a table. Returns true if it existed.
pub fn drop_policy(
    catalog: &impl Catalog,
    name: &str,
    table: &str,
    if_exists: bool,
) -> Result<bool> {
    let table = catalog.must_get_table(table)?;
    let existed = catalog.drop_policy(&table.name, name)?;
    if !existed && !if_exists {
        return errinput!("policy {name} does not exist on table {}", table.name);
    }
    Ok(existed)
}

/// Returns the policies of a table that apply to a user, by name, or none if
/// the user reads every row.
pub fn applicable(catalog: &impl Catalog, table: &str, user: &str) -> Result<Option<Vec<Policy>>> {
    if privileges::is_superuser(catalog, user)? {
        return Ok(None);
    }
    let policies = catalog.list_policies(table)?;
    if policies.is_empty() {
        return Ok(None);
    }
    let acted_for = privileges::acts_for(catalog, user)?;
    if acted_for.contains(&catalog.get_owner(table)?) {
        return Ok(None);
    }
    Ok(Some(
        policies
            .into_iter()
            .filter(|policy| policy.user.as_ref().is_none_or(|user| acted_for.contains(user)))
            .collect(),
    ))
}

/// Plans the USING conditions of the policies of a table that apply to a
/// user, or returns none if the user reads every row.
pub fn plan_using<C: Catalog>(
    catalog: &C,
    table: &Table,
    user: &str,
) -> Result<Option<Conditions>> {
    plan_conditions(catalog, table, user, |policy| &policy.using)
}

/// Plans the check conditions of the policies of a table that apply to a
/// user, or returns none if the user writes every row.
pub fn plan_checks<C: Catalog>(
    catalog: &C,
    table: &Table,
    user: &str,
) -> Result<Option<Conditions>> {
    plan_conditions(catalog, table, user, Policy::check_condition)
}

/// Plans a condition of each of the policies of a table that apply to a user.
fn plan_conditions<C: Catalog>(
    catalog: &C,
    table: &Table,
    user: &str,
    condition: impl Fn(&Policy) -> &str,
) -> Result<Option<Conditions>> {
    let Some(policies) = applicable(catalog, &table.name, user)? else {
        return Ok(None);
    };
    let conditions = policies
        .iter()
        .map(|policy| Ok((policy.name.clone(), plan::<C>(table, condition(policy))?)))
        .collect::<Result<_>>()?;
    Ok(Some(conditions))
}

/// Returns true if a policy shows the row: one of the policies is TRUE for it.
pub fn shows(policies: &[(String, Expression)], row: &Row) -> Result<bool> {
    for (name, condition) in policies {
        match condition.evaluate(Some(row))? {
            Value::Boolean(true) => return Ok(true),
            Value::Boolean(false) | Value::Null => {}
            value => return errinput!("policy {name} returned {value}, expected boolean"),
        }
    }
    Ok(false)
}

/// Returns the rows that the policies show.
pub fn filter(rows: Rows, policies: Conditions) -> Rows {
    Box::new(rows.filter_map(move |result| {
        result.and_then(|row| Ok(shows(&policies, &row)?.then_some(row))).transpose()
    }))
}

/// Scans the rows of a table that a user reads.
pub fn scan(txn: &impl Transaction, table: &Table, user: &str) -> Result<Rows> {
    let rows = txn.scan(&table.name, None)?;
    Ok(match plan_using(txn, table, user)? {
        Some(policies) => filter(rows, policies),
        None => rows,
    })
}

/// Checks that each written row satisfies the check condition of one
/// applicable policy.
pub fn check_rows<'a>(
    table: &Table,
    policies: &[(String, Expression)],
    rows: impl IntoIterator<Item = &'a Row>,
) -> Result<()> {
    for row in rows {
        if !shows(policies, row)? {
            return errinput!("new row violates row-level security policy for table {}", table.name);
        }
    }
    Ok(())
}

/// Plans a policy condition on the rows of its table.
fn plan<C: Catalog>(table: &Table, condition: &str) -> Result<Expression> {
    Planner::<C>::build_table_condition(Parser::parse_expr(condition)?, table)
}

#[cfg(test)]
mod tests {
    use itertools::Itertools as _;

    use super::*;
    use crate::sql::engine::{Engine, Local};
    use crate::storage::Memory;

    /// Creates a local engine and executes the given statements in a session.
    fn setup(statements: &[&str]) -> Local<Memory> {
        let engine = Local::new(Memory::new());
        setup_more(&engine, statements);
        engine
    }

    /// Executes further statements in a new session.
    fn setup_more(engine: &Local<Memory>, statements: &[&str]) {
        let mut session = engine.session();
        for statement in statements {
            session.execute(statement).expect("statement failed");
        }
    }

    /// Creates a policy from a CREATE POLICY statement, in a new transaction.
    fn create(engine: &Local<Memory>, statement: &str) -> Result<()> {
        let ast::Statement::CreatePolicy { name, table, user, using, with_check } =
            Parser::parse(statement)?
        else {
            panic!("not a CREATE POLICY statement");
        };
        let txn = engine.begin()?;
        create_policy(&txn, &name, &table, user.as_deref(), &using, with_check.as_ref())?;
        txn.commit()
    }

    /// Returns the policies of a table, displayed, in a new read-only
    /// transaction.
    fn list(engine: &Local<Memory>, table: &str) -> Vec<String> {
        let policies = engine.begin_read_only().unwrap().list_policies(table).unwrap();
        policies.into_iter().map(|policy| policy.to_string()).collect()
    }

    /// Returns the names of a user's applicable policies of a table, or None.
    fn applicable(engine: &Local<Memory>, table: &str, user: &str) -> Option<Vec<String>> {
        let txn = engine.begin_read_only().unwrap();
        let policies = super::applicable(&txn, table, user).unwrap()?;
        Some(policies.into_iter().map(|policy| policy.name).collect())
    }

    /// Returns the ids of the rows of a table that a user reads, or the error.
    fn scan(
        engine: &Local<Memory>,
        table: &str,
        user: &str,
    ) -> std::result::Result<String, String> {
        let txn = engine.begin_read_only().unwrap();
        let table = txn.must_get_table(table).unwrap();
        let rows: Vec<Row> = super::scan(&txn, &table, user)
            .and_then(|rows| rows.collect())
            .map_err(|error| error.to_string())?;
        Ok(rows.iter().map(|row| &row[0]).join(", "))
    }

    /// Checks written rows of a table for a user, in a new read-only
    /// transaction.
    fn check(engine: &Local<Memory>, table: &str, user: &str, rows: Vec<Row>) -> Result<()> {
        let txn = engine.begin_read_only()?;
        let table = txn.must_get_table(table)?;
        match plan_checks(&txn, &table, user)? {
            Some(policies) => check_rows(&table, &policies, &rows),
            None => Ok(()),
        }
    }

    #[test]
    fn create_drop() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, v STRING)",
            "CREATE USER alice",
            "CREATE USER \"Alice\"",
        ]);
        create(&engine, "CREATE POLICY q ON t USING (v IS NOT NULL) WITH CHECK (t.id >= 1e1)")?;
        create(&engine, "CREATE POLICY \"P\" ON t TO \"Alice\" USING (NOT (id = 1))")?;
        create(&engine, "CREATE POLICY p ON t TO alice USING (v != 'it''s')")?;
        assert_eq!(
            list(&engine, "t"),
            vec![
                r#"CREATE POLICY "P" ON t TO "Alice" USING (NOT id = 1)"#,
                "CREATE POLICY p ON t TO alice USING (v != 'it''s')",
                "CREATE POLICY q ON t USING (v IS NOT NULL) WITH CHECK (t.id >= 10.0)",
            ]
        );

        let txn = engine.begin()?;
        assert!(drop_policy(&txn, "P", "t", false)?);
        assert!(!drop_policy(&txn, "P", "t", true)?);
        assert_eq!(
            drop_policy(&txn, "P", "t", false).unwrap_err().to_string(),
            "invalid input: policy P does not exist on table t"
        );
        assert_eq!(
            drop_policy(&txn, "p", "nosuch", true).unwrap_err().to_string(),
            "invalid input: table nosuch does not exist"
        );
        txn.commit()?;
        assert_eq!(list(&engine, "t").len(), 2);

        // DROP TABLE drops the table's policies.
        let txn = engine.begin()?;
        txn.drop_table("t", false)?;
        txn.commit()?;
        assert!(list(&engine, "t").is_empty());
        Ok(())
    }

    #[test]
    fn create_errors() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "CREATE VIEW v AS SELECT * FROM t",
            "CREATE POLICY p ON t USING (TRUE)",
        ]);
        let error = |statement| create(&engine, statement).unwrap_err().to_string();
        assert_eq!(
            error("CREATE POLICY p ON v TO nosuch USING (nosuch)"),
            "invalid input: table v does not exist"
        );
        assert_eq!(
            error("CREATE POLICY \"\" ON t TO nosuch USING (nosuch)"),
            "invalid input: policy name can't be empty"
        );
        assert_eq!(
            error("CREATE POLICY p ON t TO nosuch USING (nosuch)"),
            "invalid input: policy p already exists on table t"
        );
        assert_eq!(
            error("CREATE POLICY q ON t TO nosuch USING (nosuch)"),
            "invalid input: user nosuch does not exist"
        );
        assert_eq!(
            error("CREATE POLICY q ON t USING (x) WITH CHECK (y)"),
            "invalid input: unknown column x"
        );
        assert_eq!(
            error("CREATE POLICY q ON t USING (TRUE) WITH CHECK (y)"),
            "invalid input: unknown column y"
        );
        assert_eq!(
            error("CREATE POLICY q ON t USING (u.id = 1)"),
            "invalid input: unknown table u"
        );
        assert_eq!(
            error("CREATE POLICY q ON t USING (max(id) > 1)"),
            "invalid input: unknown function max with 1 arguments"
        );
        assert_eq!(list(&engine, "t").len(), 1);
        Ok(())
    }

    #[test]
    fn applicable_policies() {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "CREATE TABLE u (id INT PRIMARY KEY)",
            "CREATE USER staff",
            "CREATE USER alice",
            "CREATE USER bob",
            "CREATE USER owner",
            "CREATE USER root SUPERUSER",
            "GRANT staff TO alice",
            "GRANT owner TO bob",
            "CREATE POLICY c ON t USING (TRUE)",
            "CREATE POLICY b ON t TO staff USING (TRUE)",
            "CREATE POLICY a ON t TO alice USING (TRUE)",
            "SET ROLE owner",
            "CREATE TABLE o (id INT PRIMARY KEY)",
            "CREATE POLICY p ON o TO alice USING (TRUE)",
        ]);
        let names = |names: &[&str]| Some(names.iter().map(|name| name.to_string()).collect());
        // Policies for the users the user acts for and for all users, by name.
        assert_eq!(applicable(&engine, "t", "alice"), names(&["a", "b", "c"]));
        assert_eq!(applicable(&engine, "t", "staff"), names(&["b", "c"]));
        assert_eq!(applicable(&engine, "t", "owner"), names(&["c"]));
        assert_eq!(applicable(&engine, "o", "staff"), names(&[]));
        // Superusers and those that act for the owner read every row, and so
        // does every user of a table without policies.
        assert_eq!(applicable(&engine, "t", "admin"), None);
        assert_eq!(applicable(&engine, "t", "root"), None);
        assert_eq!(applicable(&engine, "o", "owner"), None);
        assert_eq!(applicable(&engine, "o", "bob"), None);
        assert_eq!(applicable(&engine, "u", "alice"), None);
    }

    #[test]
    fn reads() {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, n INT)",
            "INSERT INTO t VALUES (1, 1), (2, 2), (3, NULL), (4, 0)",
            "CREATE USER alice",
            "CREATE USER bob",
            "CREATE POLICY b ON t TO alice USING (n > 1)",
            "CREATE POLICY a ON t TO alice USING (id = 1)",
        ]);
        let scan = |user| scan(&engine, "t", user);
        assert_eq!(scan("admin"), Ok("1, 2, 3, 4".into()));
        // A row is read if one policy is TRUE; NULL and FALSE hide it.
        assert_eq!(scan("alice"), Ok("1, 2".into()));
        // Without an applicable policy, no row is read.
        assert_eq!(scan("bob"), Ok("".into()));

        // The policies are evaluated in order of name, until one is TRUE: c
        // is only evaluated for the rows a and b hide, and NULL hides a row.
        setup_more(&engine, &["CREATE POLICY c ON t TO alice USING (n)"]);
        assert_eq!(
            scan("alice"),
            Err("invalid input: policy c returned 0, expected boolean".into())
        );
        setup_more(&engine, &["DROP POLICY c ON t", "CREATE POLICY c ON t USING (1 / n > 0)"]);
        assert_eq!(scan("alice"), Err("invalid input: can't divide by zero".into()));
        assert_eq!(scan("bob"), Err("invalid input: can't divide by zero".into()));
    }

    #[test]
    fn writes() {
        use Value::{Integer, Null, String};
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, s STRING)",
            "CREATE USER alice",
            "CREATE USER bob",
            "CREATE USER carol",
            "CREATE POLICY a ON t TO alice USING (s = 'a')",
            "CREATE POLICY b ON t TO bob USING (FALSE) WITH CHECK (s LIKE 'b%')",
            "CREATE POLICY c ON t TO bob USING (TRUE) WITH CHECK (s = 'x')",
        ]);
        let check = |user, rows| check(&engine, "t", user, rows).map_err(|e| e.to_string());
        let row = |id, s: &str| vec![Integer(id), String(s.into())];
        let violation =
            Err("invalid input: new row violates row-level security policy for table t".into());

        // Without WITH CHECK, the USING condition is the check condition.
        assert_eq!(check("alice", vec![row(1, "a"), row(2, "a")]), Ok(()));
        assert_eq!(check("alice", vec![row(1, "a"), row(2, "b")]), violation);
        // One applicable policy must be TRUE; NULL doesn't satisfy it.
        assert_eq!(check("bob", vec![row(1, "b1"), row(2, "x")]), Ok(()));
        assert_eq!(check("bob", vec![vec![Integer(1), Null]]), violation);
        // Without an applicable policy, no row can be written.
        assert_eq!(check("carol", vec![row(1, "a")]), violation);
        assert_eq!(check("carol", vec![]), Ok(()));
        // The owner and superusers are not restricted.
        assert_eq!(check("admin", vec![row(1, "z")]), Ok(()));
    }
}
