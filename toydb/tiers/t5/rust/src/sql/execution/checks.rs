//! CHECK constraints: created with their table, enforced on every written row.
//!
//! * Constraints are table constraints (`[CONSTRAINT name] CHECK (condition)`)
//!   and column constraints (`CHECK (condition)` in a column definition), in
//!   the order written. A constraint without a name is named
//!   `<table>_check<i>`, its position among the table's constraints counting
//!   from 1. Names are unique in the table. The condition is planned like the
//!   WHERE condition of `SELECT * FROM <table>`. These checks come after the
//!   other checks of CREATE TABLE and before anything is written. A CREATE
//!   TABLE that doesn't create a table neither checks nor creates constraints,
//!   and the existing table's constraints stay as they are.
//! * A statement that adds a column, also one added later, adds the column's
//!   constraints as the table's next ones (named by their position among all
//!   the table's constraints, and checked as in CREATE TABLE, with the new
//!   column in the table) and checks every existing row against them, in
//!   primary key order, with its value of the new column, after all of its own
//!   checks and before anything is changed. The table's other constraints are
//!   not checked again.
//! * Every row that a statement inserts or updates — INSERT, UPDATE, IMPORT,
//!   and every statement added later that inserts or updates rows — is checked
//!   after it is complete and before any of the statement's rows is written
//!   (so before the checks of data types, NULLs, keys, unique values and
//!   references): FALSE violates a constraint, TRUE and NULL satisfy it,
//!   another value is an error.
//! * No statement may drop or rename a column that a constraint of its table
//!   uses (that its condition references): every statement that drops or
//!   renames columns, also one added later, fails with
//!   `column <c> is used by check constraint <name>`, naming the first such
//!   constraint, after all of its own checks and before anything is changed.
//! * DROP TABLE drops the table's constraints.

use crate::errinput;
use crate::error::Result;
use crate::sql::engine::Catalog;
use crate::sql::parser::Parser;
use crate::sql::planner::Planner;
use crate::sql::types::{Check, Expression, Row, Table, Value};

/// Names, checks and stores the CHECK constraints of a new table.
pub fn create_table<C: Catalog>(
    catalog: &C,
    table: Table,
    constraints: Vec<(Option<String>, String)>,
) -> Result<()> {
    if constraints.is_empty() {
        return catalog.create_table(table);
    }
    // The table is created once it and its constraints are checked.
    table.validate_create(catalog)?;
    let mut checks = Vec::with_capacity(constraints.len());
    for (name, condition) in constraints {
        add::<C>(&table, &mut checks, name, condition)?;
    }
    let name = table.name.clone();
    catalog.create_table(table)?;
    catalog.set_checks(&name, checks)
}

/// Adds a named constraint to a table as its next one, after checking it.
pub fn add_check<C: Catalog>(catalog: &C, table: &str, check: Check) -> Result<()> {
    let table = catalog.must_get_table(table)?;
    let mut checks = catalog.get_checks(&table.name)?;
    add::<C>(&table, &mut checks, Some(check.name), check.condition)?;
    catalog.set_checks(&table.name, checks)
}

/// Adds a constraint to the given constraints of a table, after naming it and
/// checking its name and condition.
fn add<C: Catalog>(
    table: &Table,
    checks: &mut Vec<Check>,
    name: Option<String>,
    condition: String,
) -> Result<()> {
    let name = name.unwrap_or_else(|| format!("{}_check{}", table.name, checks.len() + 1));
    if name.is_empty() {
        return errinput!("check constraint name can't be empty");
    }
    if checks.iter().any(|check| check.name == name) {
        return errinput!("duplicate check constraint {name} in table {}", table.name);
    }
    plan::<C>(table, &condition)?;
    checks.push(Check { name, condition });
    Ok(())
}

/// Plans the CHECK constraints of a table, in order.
pub fn plan_checks<C: Catalog>(catalog: &C, table: &Table) -> Result<Vec<(String, Expression)>> {
    catalog
        .get_checks(&table.name)?
        .into_iter()
        .map(|check| Ok((check.name, plan::<C>(table, &check.condition)?)))
        .collect()
}

/// Checks rows against the CHECK constraints of their table, in order.
pub fn check_rows<'a>(
    table: &Table,
    checks: &[(String, Expression)],
    rows: impl IntoIterator<Item = &'a Row>,
) -> Result<()> {
    for row in rows {
        for (name, condition) in checks {
            match condition.evaluate(Some(row))? {
                Value::Boolean(true) | Value::Null => {}
                Value::Boolean(false) => {
                    let key = &row[table.primary_key];
                    return errinput!("row {key} violates check constraint {name}");
                }
                value => {
                    return errinput!("check constraint {name} returned {value}, expected boolean");
                }
            }
        }
    }
    Ok(())
}

/// Plans a constraint's condition on the rows of its table.
fn plan<C: Catalog>(table: &Table, condition: &str) -> Result<Expression> {
    Planner::<C>::build_table_condition(Parser::parse_expr(condition)?, table)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sql::engine::{Engine, Local, Transaction as _};
    use crate::sql::planner::Plan;
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

    /// Plans a CREATE TABLE statement and creates the table with its
    /// constraints, in a new transaction.
    fn create(engine: &Local<Memory>, statement: &str) -> Result<()> {
        let txn = engine.begin()?;
        let Plan::CreateTable { schema, checks } = Plan::build(Parser::parse(statement)?, &txn)?
        else {
            panic!("not a CREATE TABLE statement");
        };
        create_table(&txn, schema, checks)?;
        txn.commit()
    }

    /// Fetches the constraints of a table, in a new read-only transaction.
    fn get(engine: &Local<Memory>, table: &str) -> Vec<String> {
        let checks = engine.begin_read_only().unwrap().get_checks(table).unwrap();
        checks.into_iter().map(|check| check.to_string()).collect()
    }

    /// Checks rows against a table's constraints, in a new read-only
    /// transaction.
    fn check(engine: &Local<Memory>, table: &str, rows: Vec<Row>) -> Result<()> {
        let txn = engine.begin_read_only()?;
        let table = txn.must_get_table(table)?;
        check_rows(&table, &plan_checks(&txn, &table)?, &rows)
    }

    #[test]
    fn create_names() -> Result<()> {
        let engine = setup(&[]);
        create(
            &engine,
            "CREATE TABLE t (id INT PRIMARY KEY CHECK (id != 5), CHECK (id > 0), \
             n INT CHECK (n IS NOT NULL) CHECK (n < id), CONSTRAINT \"Max\" CHECK (id < 9), \
             CHECK (t.n >= -1e3))",
        )?;
        assert_eq!(
            get(&engine, "t"),
            vec![
                "CONSTRAINT t_check1 CHECK (id != 5)",
                "CONSTRAINT t_check2 CHECK (id > 0)",
                "CONSTRAINT t_check3 CHECK (n IS NOT NULL)",
                "CONSTRAINT t_check4 CHECK (n < id)",
                "CONSTRAINT Max CHECK (id < 9)",
                "CONSTRAINT t_check6 CHECK (t.n >= -1000.0)",
            ]
        );

        // A table without constraints stores none.
        create(&engine, "CREATE TABLE u (id INT PRIMARY KEY)")?;
        assert!(get(&engine, "u").is_empty());
        Ok(())
    }

    #[test]
    fn create_errors() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);
        let error = |statement| create(&engine, statement).unwrap_err().to_string();
        assert_eq!(
            error("CREATE TABLE t (id INT PRIMARY KEY, CHECK (nosuch))"),
            "invalid input: table t already exists"
        );
        assert_eq!(
            error("CREATE TABLE u (id INT PRIMARY KEY, n INT DEFAULT 'a', CHECK (nosuch))"),
            "invalid input: invalid default type STRING for INTEGER column n"
        );
        assert_eq!(
            error(
                "CREATE TABLE u (id INT PRIMARY KEY, CONSTRAINT u_check2 CHECK (id > 0), \
                 CHECK (TRUE))"
            ),
            "invalid input: duplicate check constraint u_check2 in table u"
        );
        assert_eq!(
            error("CREATE TABLE u (id INT PRIMARY KEY, CONSTRAINT \"\" CHECK (TRUE))"),
            "invalid input: check constraint name can't be empty"
        );
        assert_eq!(
            error("CREATE TABLE u (id INT PRIMARY KEY, CHECK (t.id > 0))"),
            "invalid input: unknown table t"
        );
        assert_eq!(
            error("CREATE TABLE u (id INT PRIMARY KEY, CHECK (count(id) > 0))"),
            "invalid input: unknown function count with 1 arguments"
        );
        // Each constraint is checked in order, its name before its condition.
        assert_eq!(
            error(
                "CREATE TABLE u (id INT PRIMARY KEY, CHECK (x), CONSTRAINT c CHECK (y), \
                 CONSTRAINT c CHECK (TRUE))"
            ),
            "invalid input: unknown column x"
        );
        assert_eq!(
            error(
                "CREATE TABLE u (id INT PRIMARY KEY, CONSTRAINT c CHECK (TRUE), \
                 CONSTRAINT c CHECK (y))"
            ),
            "invalid input: duplicate check constraint c in table u"
        );
        // Nothing was created.
        assert!(engine.begin_read_only()?.get_table("u")?.is_none());
        assert!(get(&engine, "u").is_empty());
        Ok(())
    }

    #[test]
    fn rows() -> Result<()> {
        use Value::{Integer, Null, String};
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, n INT, s STRING, CHECK (n > 0), \
             CONSTRAINT small CHECK (n < 10), CHECK (n + 1), CHECK (s > 0))",
            "CREATE TABLE u (k STRING PRIMARY KEY, CHECK (k LIKE 'k%'))",
        ]);
        let check = |table, rows| check(&engine, table, rows).map_err(|e| e.to_string());

        // NULL satisfies a constraint.
        let rows = vec![vec![Integer(1), Null, Null], vec![Integer(2), Null, Null]];
        assert_eq!(check("t", rows), Ok(()));
        // FALSE violates it: the first violation is reported, rows in order
        // and for each row the constraints in order.
        assert_eq!(
            check(
                "t",
                vec![
                    vec![Integer(1), Null, Null],
                    vec![Integer(2), Integer(0), Null],
                    vec![Integer(3), Integer(10), Null],
                ]
            ),
            Err("invalid input: row 2 violates check constraint t_check1".into())
        );
        assert_eq!(
            check("t", vec![vec![Integer(3), Integer(10), Null]]),
            Err("invalid input: row 3 violates check constraint small".into())
        );
        assert_eq!(
            check("u", vec![vec![String("x".into())]]),
            Err("invalid input: row 'x' violates check constraint u_check1".into())
        );
        // Another value than a boolean or NULL is an error, and so is an
        // evaluation error.
        assert_eq!(
            check("t", vec![vec![Integer(1), Integer(5), Null]]),
            Err("invalid input: check constraint t_check3 returned 6, expected boolean".into())
        );
        assert_eq!(
            check("t", vec![vec![Integer(1), Null, String("x".into())]]),
            Err("invalid input: can't compare 'x' and 0".into())
        );
        Ok(())
    }

    #[test]
    fn drop_table() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY, CHECK (id > 0))"]);
        assert_eq!(get(&engine, "t"), vec!["CONSTRAINT t_check1 CHECK (id > 0)"]);
        let txn = engine.begin()?;
        txn.drop_table("t", false)?;
        txn.commit()?;
        assert!(get(&engine, "t").is_empty());
        Ok(())
    }

    #[test]
    fn add_check() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY, CHECK (id > 0))"]);
        let add = |name: &str, condition: &str| {
            let txn = engine.begin()?;
            let check = Check { name: name.into(), condition: condition.into() };
            let result = super::add_check(&txn, "t", check);
            txn.commit()?;
            result
        };
        add("small", "id < 10")?;
        assert_eq!(
            get(&engine, "t"),
            vec!["CONSTRAINT t_check1 CHECK (id > 0)", "CONSTRAINT small CHECK (id < 10)"]
        );
        let error = |name, condition| add(name, condition).unwrap_err().to_string();
        assert_eq!(
            error("small", "TRUE"),
            "invalid input: duplicate check constraint small in table t"
        );
        assert_eq!(error("c", "nosuch > 0"), "invalid input: unknown column nosuch");
        let txn = engine.begin()?;
        let check = Check { name: "c".into(), condition: "TRUE".into() };
        let error = super::add_check(&txn, "u", check).unwrap_err().to_string();
        assert_eq!(error, "invalid input: table u does not exist");
        Ok(())
    }
}
