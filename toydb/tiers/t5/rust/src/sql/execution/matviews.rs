//! CREATE, REFRESH and DROP MATERIALIZED VIEW, and the dependency checks of
//! materialized views.
//!
//! * CREATE makes the checks of CREATE VIEW, executes the query and stores its
//!   rows; REFRESH executes the stored query again and replaces them. A
//!   statement that reads a materialized view reads the stored rows.
//! * A table, view or materialized view that a materialized view depends on
//!   can't be dropped, after the checks of views, and no statement may change
//!   the definition of a table that a materialized view depends on: every
//!   statement that changes a table's definition, also one added later, fails
//!   with `table <t> is used by materialized view <m>` (the first such
//!   materialized view by name) after the views check and before the check
//!   constraints' check.

use itertools::Itertools as _;

use super::system::SessionState;
use super::{ExecutionResult, views};
use crate::error::{Error, Result};
use crate::sql::engine::{Catalog, Transaction};
use crate::sql::parser::{Parser, ast};
use crate::sql::planner::Plan;
use crate::sql::types::{MaterializedView, Row, View};
use crate::{errdata, errinput};

/// Creates a materialized view: checks it like a view, executes its query and
/// stores the rows.
pub fn create_materialized_view(
    txn: &impl Transaction,
    name: &str,
    columns: Option<Vec<String>>,
    query: ast::Statement,
    user: &str,
    session: Option<&SessionState>,
) -> Result<u64> {
    let View { name, columns, query, dependencies } =
        views::define_view(txn, name, columns, query)?;
    let view = MaterializedView { name, columns, query, dependencies };
    let rows = execute_query(txn, &view, user, session)?;
    let count = rows.len() as u64;
    txn.create_materialized_view(view, rows)?;
    Ok(count)
}

/// Executes a materialized view's query again and replaces its rows.
pub fn refresh_materialized_view(
    txn: &impl Transaction,
    name: &str,
    user: &str,
    session: Option<&SessionState>,
) -> Result<u64> {
    let Some(view) = txn.get_materialized_view(name)? else {
        return errinput!("materialized view {name} does not exist");
    };
    let rows = execute_query(txn, &view, user, session)?;
    let count = rows.len() as u64;
    // Replacing no rows with no rows writes nothing, so check explicitly.
    if txn.state().read_only {
        return Err(Error::ReadOnly);
    }
    txn.refresh_materialized_view(name, rows)?;
    Ok(count)
}

/// Drops a materialized view. Returns true if it existed.
pub fn drop_materialized_view(catalog: &impl Catalog, name: &str, if_exists: bool) -> Result<bool> {
    if catalog.get_materialized_view(name)?.is_none() {
        if if_exists {
            return Ok(false);
        }
        return errinput!("materialized view {name} does not exist");
    }
    if let Some(view) = views::first_dependent(catalog, name)? {
        return errinput!("materialized view {name} is used by view {view}");
    }
    if let Some(view) = first_dependent(catalog, name)? {
        return errinput!("materialized view {name} is used by materialized view {view}");
    }
    catalog.drop_materialized_view(name)
}

/// Errors if a materialized view depends on the given table.
pub fn check_table_dependencies(catalog: &impl Catalog, table: &str) -> Result<()> {
    match first_dependent(catalog, table)? {
        Some(view) => errinput!("table {table} is used by materialized view {view}"),
        None => Ok(()),
    }
}

/// Errors if a materialized view depends on the given view.
pub fn check_view_dependencies(catalog: &impl Catalog, view: &str) -> Result<()> {
    match first_dependent(catalog, view)? {
        Some(dependent) => errinput!("view {view} is used by materialized view {dependent}"),
        None => Ok(()),
    }
}

/// Finds a materialized view that depends on the given name.
fn first_dependent(catalog: &impl Catalog, name: &str) -> Result<Option<String>> {
    let views = catalog.list_materialized_views()?;
    Ok(views.into_iter().find(|view| view.dependencies.iter().any(|d| d == name)).map(|v| v.name))
}

/// Plans and executes a materialized view's stored query, returning its rows.
fn execute_query(
    txn: &impl Transaction,
    view: &MaterializedView,
    user: &str,
    session: Option<&SessionState>,
) -> Result<Vec<Row>> {
    let plan = Plan::build(Parser::parse(&view.query)?, txn)?.optimize()?;
    let ExecutionResult::Select { rows, .. } = plan.execute(txn, user, session)? else {
        return errdata!("materialized view {} has no SELECT query", view.name);
    };
    rows.try_collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sql::engine::{Engine, Local};
    use crate::sql::planner::{describe, estimate};
    use crate::sql::types::{ADMIN, DataType, Value};
    use crate::storage::Memory;

    /// Creates a local engine and executes the given statements.
    fn setup(statements: &[&str]) -> Local<Memory> {
        let engine = Local::new(Memory::new());
        let mut session = engine.session();
        for statement in statements {
            session.execute(statement).expect("statement failed");
        }
        drop(session);
        engine
    }

    /// Creates a materialized view from a CREATE MATERIALIZED VIEW statement
    /// as admin, in a new transaction. Returns the number of rows stored.
    fn create_admin(engine: &Local<Memory>, statement: &str) -> Result<u64> {
        let ast::Statement::CreateMaterializedView { name, columns, query } =
            Parser::parse(statement)?
        else {
            panic!("not a CREATE MATERIALIZED VIEW statement");
        };
        let txn = engine.begin()?;
        let result = create_materialized_view(&txn, &name, columns, *query, ADMIN, None);
        match result {
            Ok(_) => txn.commit()?,
            Err(_) => txn.rollback()?,
        }
        result
    }

    /// Returns the stored rows of a materialized view, in a new read-only
    /// transaction.
    fn rows(engine: &Local<Memory>, name: &str) -> Vec<Row> {
        engine.begin_read_only().unwrap().get_materialized_view_rows(name).unwrap()
    }

    #[test]
    fn create_definition() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, v INT)",
            "INSERT INTO t VALUES (1, 30), (2, 10), (3, 20)",
            "CREATE VIEW tv AS SELECT id FROM t",
        ]);
        let count = create_admin(
            &engine,
            "create materialized view m (k, \"V\") as select t.id, v from t join tv \
             on t.id = tv.id where v > 10 order by v desc",
        )?;
        assert_eq!(count, 2);
        let txn = engine.begin_read_only()?;
        assert_eq!(
            txn.get_materialized_view("m")?,
            Some(MaterializedView {
                name: "m".into(),
                columns: vec!["k".into(), "V".into()],
                query: "SELECT t.id, v FROM t JOIN tv ON t.id = tv.id WHERE v > 10 ORDER BY v DESC"
                    .into(),
                dependencies: vec!["t".into(), "tv".into()],
            })
        );
        // The rows are stored in the order of the query.
        assert_eq!(
            rows(&engine, "m"),
            vec![vec![Value::Integer(1), Value::Integer(30)], vec![3.into(), 20.into()]]
        );

        // A materialized view of a materialized view depends on it, and its
        // column names come from the query.
        assert_eq!(create_admin(&engine, "CREATE MATERIALIZED VIEW n AS SELECT k FROM m")?, 2);
        let n = engine.begin_read_only()?.get_materialized_view("n")?.unwrap();
        assert_eq!(n.columns, vec!["k"]);
        assert_eq!(n.dependencies, vec!["m"]);
        Ok(())
    }

    #[test]
    fn create_errors() {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, d INT)",
            "INSERT INTO t VALUES (1, 0)",
            "CREATE VIEW v AS SELECT id FROM t",
        ]);
        create_admin(&engine, "CREATE MATERIALIZED VIEW m AS SELECT id FROM t").unwrap();

        let error = |statement| create_admin(&engine, statement).unwrap_err().to_string();
        assert_eq!(
            error(r#"CREATE MATERIALIZED VIEW "" AS SELECT 1 AS a"#),
            "invalid input: view name can't be empty"
        );
        assert_eq!(
            error("CREATE MATERIALIZED VIEW t AS SELECT 1 AS a"),
            "invalid input: table t already exists"
        );
        assert_eq!(
            error("CREATE MATERIALIZED VIEW v AS SELECT 1 AS a"),
            "invalid input: view v already exists"
        );
        assert_eq!(
            error("CREATE MATERIALIZED VIEW m AS SELECT 1 AS a"),
            "invalid input: materialized view m already exists"
        );
        assert_eq!(
            error("CREATE MATERIALIZED VIEW x (a, b) AS SELECT id FROM t"),
            "invalid input: wrong number of column names for view x: expected 1, found 2"
        );
        assert_eq!(
            error("CREATE MATERIALIZED VIEW x AS SELECT id, id + 1 FROM t"),
            "invalid input: column 2 of view x has no name"
        );
        // The query is executed.
        assert_eq!(
            error("CREATE MATERIALIZED VIEW x AS SELECT id / d AS q FROM t"),
            "invalid input: can't divide by zero"
        );
        assert_eq!(engine.begin_read_only().unwrap().get_materialized_view("x").unwrap(), None);
    }

    #[test]
    fn refresh_rows() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, d INT)",
            "INSERT INTO t VALUES (1, 1), (2, 2), (3, 3)",
        ]);
        create_admin(&engine, "CREATE MATERIALIZED VIEW m AS SELECT id, 6 / d AS q FROM t")?;
        engine.session().execute("DELETE FROM t WHERE id > 1")?;

        // Refreshing replaces all stored rows, also when there are fewer.
        let txn = engine.begin()?;
        assert_eq!(refresh_materialized_view(&txn, "m", ADMIN, None)?, 1);
        txn.commit()?;
        assert_eq!(rows(&engine, "m"), vec![vec![Value::Integer(1), Value::Integer(6)]]);

        // A failing query leaves the stored rows unchanged.
        engine.session().execute("INSERT INTO t VALUES (4, 0)")?;
        let txn = engine.begin()?;
        let error = refresh_materialized_view(&txn, "m", ADMIN, None).unwrap_err();
        assert_eq!(error.to_string(), "invalid input: can't divide by zero");
        let error = refresh_materialized_view(&txn, "t", ADMIN, None).unwrap_err();
        assert_eq!(error.to_string(), "invalid input: materialized view t does not exist");
        txn.rollback()?;
        assert_eq!(rows(&engine, "m").len(), 1);

        // A refresh in a read-only transaction fails, also without rows.
        create_admin(&engine, "CREATE MATERIALIZED VIEW e AS SELECT id FROM t WHERE FALSE")?;
        let txn = engine.begin_read_only()?;
        let error = refresh_materialized_view(&txn, "e", ADMIN, None).unwrap_err();
        assert_eq!(error, Error::ReadOnly);
        Ok(())
    }

    #[test]
    fn read() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, v STRING)",
            "INSERT INTO t VALUES (1, 'a'), (2, NULL)",
        ]);
        create_admin(&engine, "CREATE MATERIALIZED VIEW m (k, s) AS SELECT id, v FROM t")?;
        engine.session().execute("DELETE FROM t")?;

        // Queries read the stored rows through a MaterializedView node.
        let txn = engine.begin_read_only()?;
        let query = Parser::parse("SELECT x.s FROM m AS x WHERE x.k > 1")?;
        let plan = Plan::build(query, &txn)?.optimize()?;
        assert_eq!(
            plan.to_string(),
            "Projection: x.s\n└─ Filter: x.k > 1\n   └─ MaterializedView: m as x"
        );
        let ExecutionResult::Select { rows, .. } = plan.clone().execute(&txn, ADMIN, None)? else {
            panic!("expected select result");
        };
        assert_eq!(rows.try_collect::<_, Vec<Row>, _>()?, vec![vec![Value::Null]]);

        // ESTIMATE counts the stored rows, DESCRIBE describes the query.
        assert_eq!(estimate::estimate(&plan, &txn)?.last(), Some(&2.0));
        let columns = describe::describe_table(&txn, "m")?
            .into_iter()
            .map(|column| (column.name, column.datatype, column.nullable))
            .collect_vec();
        assert_eq!(
            columns,
            vec![
                (Some("k".into()), Some(DataType::Integer), false),
                (Some("s".into()), Some(DataType::String), true),
            ]
        );
        Ok(())
    }

    #[test]
    fn drop_dependencies() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY)",
            "CREATE TABLE u (id INT PRIMARY KEY)",
            "CREATE VIEW v AS SELECT id FROM t",
        ]);
        create_admin(&engine, "CREATE MATERIALIZED VIEW b AS SELECT t.id FROM t, u")?;
        create_admin(&engine, "CREATE MATERIALIZED VIEW a AS SELECT id FROM v")?;
        create_admin(&engine, "CREATE MATERIALIZED VIEW c AS SELECT id FROM b")?;
        engine.session().execute("CREATE VIEW w AS SELECT id FROM c")?;

        let txn = engine.begin()?;
        let error = |result: Result<bool>| result.unwrap_err().to_string();
        assert_eq!(
            error(txn.drop_table("u", false)),
            "invalid input: table u is used by materialized view b"
        );
        assert_eq!(
            error(views::drop_view(&txn, "v", false)),
            "invalid input: view v is used by materialized view a"
        );
        assert_eq!(
            error(drop_materialized_view(&txn, "b", false)),
            "invalid input: materialized view b is used by materialized view c"
        );
        assert_eq!(
            error(drop_materialized_view(&txn, "c", false)),
            "invalid input: materialized view c is used by view w"
        );
        assert_eq!(
            error(drop_materialized_view(&txn, "v", false)),
            "invalid input: materialized view v does not exist"
        );
        check_table_dependencies(&txn, "nosuch")?;
        check_view_dependencies(&txn, "w")?;

        // Dropping removes the definition and the rows.
        assert!(!drop_materialized_view(&txn, "nosuch", true)?);
        assert!(views::drop_view(&txn, "w", false)?);
        assert!(drop_materialized_view(&txn, "c", false)?);
        assert_eq!(txn.get_materialized_view("c")?, None);
        assert_eq!(txn.get_materialized_view_rows("c")?, Vec::<Row>::new());
        assert_eq!(
            txn.list_materialized_views()?.into_iter().map(|view| view.name).collect_vec(),
            vec!["a", "b"]
        );
        txn.rollback()
    }
}
