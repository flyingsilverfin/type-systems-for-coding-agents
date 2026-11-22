//! CREATE VIEW, DROP VIEW and SHOW VIEWS, and the dependency checks of views.
//!
//! * CREATE VIEW checks the name, plans the query (without optimizing or
//!   executing it), names the columns from the column list or the query's
//!   result columns, and stores the definition.
//! * A table or view that a view depends on can't be dropped, and no statement
//!   may change the definition of a table that a view depends on.
//! * SHOW VIEWS lists the views by name, with their columns and query.

use itertools::Itertools as _;

use super::StatementResult;
use crate::errinput;
use crate::error::Result;
use crate::sql::engine::Catalog;
use crate::sql::parser::{ast, format};
use crate::sql::planner::Plan;
use crate::sql::types::{Label, Value, View};

/// Creates a view, after checking its name, its query and its column names.
pub fn create_view(
    catalog: &impl Catalog,
    name: &str,
    columns: Option<Vec<String>>,
    query: ast::Statement,
) -> Result<()> {
    if name.is_empty() {
        return errinput!("view name can't be empty");
    }
    if catalog.get_table(name)?.is_some() {
        return errinput!("table {name} already exists");
    }
    if catalog.get_view(name)?.is_some() {
        return errinput!("view {name} already exists");
    }

    let ast::Statement::Select { from, .. } = &query else {
        panic!("view query must be a SELECT, found {query:?}");
    };
    let dependencies = dependencies(from);
    let text = format::one_line(&query);

    // Plan the query, to check it and to find its result columns.
    let Plan::Select(root) = Plan::build(query, catalog)? else {
        panic!("SELECT must have a SELECT plan");
    };
    let names = match columns {
        Some(columns) => {
            if columns.len() != root.columns() {
                return errinput!(
                    "wrong number of column names for view {name}: expected {}, found {}",
                    root.columns(),
                    columns.len()
                );
            }
            columns.into_iter().map(Some).collect_vec()
        }
        None => (0..root.columns())
            .map(|i| match root.column_label(i) {
                Label::Qualified(_, column) | Label::Unqualified(column) => Some(column),
                Label::None => None,
            })
            .collect(),
    };

    let mut columns = Vec::with_capacity(names.len());
    for (i, column) in names.into_iter().enumerate() {
        let Some(column) = column else {
            return errinput!("column {} of view {name} has no name", i + 1);
        };
        if column.is_empty() {
            return errinput!("column name can't be empty");
        }
        if columns.contains(&column) {
            return errinput!("duplicate column {column} in view {name}");
        }
        columns.push(column);
    }

    catalog.create_view(View { name: name.to_string(), columns, query: text, dependencies })
}

/// Drops a view, unless another view depends on it. Returns true if the view
/// existed.
pub fn drop_view(catalog: &impl Catalog, name: &str, if_exists: bool) -> Result<bool> {
    if catalog.get_view(name)?.is_some()
        && let Some(dependent) = first_dependent(catalog, name)?
    {
        return errinput!("view {name} is used by view {dependent}");
    }
    catalog.drop_view(name, if_exists)
}

/// Errors if a view depends on the given table.
pub fn check_table_dependencies(catalog: &impl Catalog, table: &str) -> Result<()> {
    match first_dependent(catalog, table)? {
        Some(view) => errinput!("table {table} is used by view {view}"),
        None => Ok(()),
    }
}

/// Returns the SHOW VIEWS result: one row per view, ordered by name.
pub fn show_views(catalog: &impl Catalog) -> Result<StatementResult> {
    let columns = ["name", "columns", "query"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    // Views are listed in name order.
    let rows = catalog
        .list_views()?
        .into_iter()
        .map(|view| {
            let columns = view.columns.iter().map(|c| format::ident(c)).join(", ");
            vec![Value::String(view.name), Value::String(columns), Value::String(view.query)]
        })
        .collect();
    Ok(StatementResult::Select { columns, rows })
}

/// Finds a view that depends on the given table or view.
fn first_dependent(catalog: &impl Catalog, name: &str) -> Result<Option<String>> {
    // Views are listed in name order.
    let views = catalog.list_views()?;
    Ok(views.into_iter().find(|view| view.dependencies.iter().any(|d| d == name)).map(|v| v.name))
}

/// Returns the names of the tables and views that the FROM items read.
fn dependencies(from: &[ast::From]) -> Vec<String> {
    fn collect(item: &ast::From, names: &mut Vec<String>) {
        match item {
            ast::From::Table { name, .. } => {
                if !names.contains(name) {
                    names.push(name.clone());
                }
            }
            ast::From::Join { left, right, .. } => {
                collect(left, names);
                collect(right, names);
            }
        }
    }
    let mut names = Vec::new();
    for item in from {
        collect(item, &mut names);
    }
    names
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sql::engine::{Engine, Local, Transaction as _};
    use crate::sql::parser::Parser;
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

    /// Creates a view from a CREATE VIEW statement, in a new transaction.
    fn create(engine: &Local<Memory>, statement: &str) -> Result<()> {
        let ast::Statement::CreateView { name, columns, query } = Parser::parse(statement)? else {
            panic!("not a CREATE VIEW statement");
        };
        let txn = engine.begin()?;
        create_view(&txn, &name, columns, *query)?;
        txn.commit()
    }

    /// Fetches a view, in a new read-only transaction.
    fn get(engine: &Local<Memory>, name: &str) -> Option<View> {
        engine.begin_read_only().unwrap().get_view(name).unwrap()
    }

    #[test]
    fn create_definition() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE a (id INT PRIMARY KEY, v STRING)",
            "CREATE TABLE b (id INT PRIMARY KEY, a_id INT REFERENCES a)",
        ]);
        create(
            &engine,
            "create view v as select a.id, B.ID as \"B\", a.v from a join b on b.a_id = a.id, a x",
        )?;
        assert_eq!(
            get(&engine, "v"),
            Some(View {
                name: "v".into(),
                columns: vec!["id".into(), "B".into(), "v".into()],
                query: r#"SELECT a.id, b.id AS "B", a.v FROM a JOIN b ON b.a_id = a.id, a AS x"#
                    .into(),
                dependencies: vec!["a".into(), "b".into()],
            })
        );

        // A view of a view depends on the view, and a constant view on nothing.
        create(&engine, "CREATE VIEW w (x, y, z) AS SELECT * FROM v")?;
        let w = get(&engine, "w").unwrap();
        assert_eq!(w.columns, vec!["x", "y", "z"]);
        assert_eq!(w.dependencies, vec!["v"]);

        create(&engine, "CREATE VIEW k AS SELECT 1 AS one")?;
        assert_eq!(get(&engine, "k").unwrap().dependencies, Vec::<String>::new());
        Ok(())
    }

    #[test]
    fn create_errors() {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY, v STRING)"]);
        create(&engine, "CREATE VIEW w AS SELECT id FROM t").unwrap();

        let error = |statement| create(&engine, statement).unwrap_err().to_string();
        assert_eq!(
            error(r#"CREATE VIEW "" AS SELECT 1 AS a"#),
            "invalid input: view name can't be empty"
        );
        assert_eq!(
            error("CREATE VIEW t AS SELECT 1 AS a"),
            "invalid input: table t already exists"
        );
        assert_eq!(
            error("CREATE VIEW w AS SELECT x FROM nosuch"),
            "invalid input: view w already exists"
        );
        assert_eq!(
            error("CREATE VIEW x AS SELECT * FROM nosuch"),
            "invalid input: table nosuch does not exist"
        );
        assert_eq!(
            error("CREATE VIEW x (a, b) AS SELECT id, v, 1 FROM t"),
            "invalid input: wrong number of column names for view x: expected 3, found 2"
        );
        assert_eq!(
            error("CREATE VIEW x AS SELECT id, id + 1 FROM t"),
            "invalid input: column 2 of view x has no name"
        );
        assert_eq!(
            error(r#"CREATE VIEW x AS SELECT id AS "", 1 FROM t"#),
            "invalid input: column name can't be empty"
        );
        assert_eq!(
            error("CREATE VIEW x AS SELECT t.id, u.id FROM t, t AS u"),
            "invalid input: duplicate column id in view x"
        );
        assert_eq!(get(&engine, "x"), None);
    }

    #[test]
    fn drop_views() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);
        create(&engine, "CREATE VIEW v AS SELECT id FROM t")?;
        create(&engine, "CREATE VIEW x AS SELECT id FROM v")?;
        create(&engine, "CREATE VIEW w AS SELECT id FROM v")?;

        let txn = engine.begin()?;
        assert_eq!(
            drop_view(&txn, "v", false).unwrap_err().to_string(),
            "invalid input: view v is used by view w"
        );
        assert_eq!(
            drop_view(&txn, "t", false).unwrap_err().to_string(),
            "invalid input: view t does not exist"
        );
        assert!(!drop_view(&txn, "t", true)?);
        assert!(drop_view(&txn, "w", false)?);
        assert!(drop_view(&txn, "x", false)?);
        assert!(drop_view(&txn, "v", true)?);
        assert_eq!(txn.list_views()?, Vec::new());
        txn.commit()
    }

    #[test]
    fn table_dependencies() -> Result<()> {
        let engine =
            setup(&["CREATE TABLE t (id INT PRIMARY KEY)", "CREATE TABLE u (id INT PRIMARY KEY)"]);
        create(&engine, "CREATE VIEW vb AS SELECT u.id FROM u, t")?;
        create(&engine, "CREATE VIEW va AS SELECT id FROM t")?;

        let txn = engine.begin()?;
        assert_eq!(
            check_table_dependencies(&txn, "t").unwrap_err().to_string(),
            "invalid input: table t is used by view va"
        );
        assert_eq!(
            txn.drop_table("u", false).unwrap_err().to_string(),
            "invalid input: table u is used by view vb"
        );
        check_table_dependencies(&txn, "nosuch")?;
        txn.rollback()
    }

    #[test]
    fn show_views_rows() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);
        create(
            &engine,
            r#"CREATE VIEW b ("ID", "select") AS SELECT id, id FROM t WHERE id > 1.50"#,
        )?;
        create(&engine, r#"CREATE VIEW "B" AS SELECT 'it''s' AS s"#)?;

        let txn = engine.begin_read_only()?;
        let StatementResult::Select { columns, rows } = show_views(&txn)? else {
            panic!("expected select result");
        };
        assert_eq!(columns.iter().join(", "), "name, columns, query");
        let rows = rows.into_iter().map(|row| row.into_iter().join(", ")).collect_vec();
        assert_eq!(
            rows,
            vec![
                r#"'B', 's', 'SELECT \'it\'\'s\' AS s'"#,
                r#"'b', '\"ID\", \"select\"', 'SELECT id, id FROM t WHERE id > 1.5'"#,
            ]
        );
        Ok(())
    }
}
