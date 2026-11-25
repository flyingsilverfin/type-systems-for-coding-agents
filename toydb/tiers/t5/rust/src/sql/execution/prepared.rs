//! Prepared statements: `PREPARE`, `EXECUTE`, `DEALLOCATE` and `SHOW PREPARED`.
//!
//! A prepared statement is a planned statement with typed parameters `$1`,
//! `$2`, ..., kept by the session under a name. EXECUTE checks the arguments
//! (their number, and that each is a constant of the parameter's type or NULL)
//! and runs the statement with every parameter replaced by the value of its
//! argument.
//!
//! Each prepared statement caches the plan of its last successful planning,
//! with the argument values and the definitions of the tables and views that
//! planning used. The cached plan is used again for identical arguments while
//! those definitions are unchanged; otherwise the statement is planned anew.

use std::cell::RefCell;
use std::collections::{BTreeMap, BTreeSet};

use itertools::Itertools as _;

use super::StatementResult;
use super::session::unplanned_keyword;
use crate::errinput;
use crate::error::Result;
use crate::sql::engine::Catalog;
use crate::sql::parser::{ast, format};
use crate::sql::planner::{Plan, Planner};
use crate::sql::types::{
    Check, DataType, Label, MaterializedView, Policy, Privilege, Row, Table, TableStatistics, User,
    Value, View,
};

/// A prepared statement: the parsed statement, its parameter types, its cached
/// plan and its execution counts.
pub struct PreparedStatement {
    /// The statement.
    statement: ast::Statement,
    /// The parameter types, for $1, $2, ...
    types: Vec<DataType>,
    /// The cached plan, if any.
    cached: Option<CachedPlan>,
    /// The number of executions.
    executions: u64,
    /// The number of plans.
    plans: u64,
}

/// A cached plan, with the arguments and the definitions it was planned for.
struct CachedPlan {
    /// The optimized plan.
    plan: Plan,
    /// The argument values.
    arguments: Vec<Value>,
    /// The definitions that planning looked up.
    definitions: Definitions,
}

/// The table, view and materialized view definitions that planning looked up,
/// by name.
#[derive(Default)]
struct Definitions {
    tables: BTreeMap<String, Option<Table>>,
    views: BTreeMap<String, Option<View>>,
    materialized_views: BTreeMap<String, Option<MaterializedView>>,
}

impl Definitions {
    /// Returns true if the catalog has the same definitions.
    fn unchanged(&self, catalog: &impl Catalog) -> Result<bool> {
        for (name, table) in &self.tables {
            if catalog.get_table(name)? != *table {
                return Ok(false);
            }
        }
        for (name, view) in &self.views {
            if catalog.get_view(name)? != *view {
                return Ok(false);
            }
        }
        for (name, view) in &self.materialized_views {
            if catalog.get_materialized_view(name)? != *view {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

/// The prepared statements of a session, by name.
#[derive(Default)]
pub struct PreparedStatements(BTreeMap<String, PreparedStatement>);

impl PreparedStatements {
    /// Creates an empty set of prepared statements.
    pub fn new() -> Self {
        Self::default()
    }

    /// Prepares a statement under the given name.
    pub fn prepare(
        &mut self,
        name: String,
        types: Vec<DataType>,
        statement: ast::Statement,
    ) -> Result<()> {
        if self.0.contains_key(&name) {
            return errinput!("prepared statement {name} already exists");
        }
        if let Some(keyword) = unplanned_keyword(&statement) {
            return errinput!("PREPARE is not supported for {keyword}");
        }
        if let Some(n) = statement.parameters().into_iter().find(|n| *n > types.len() as u64) {
            return errinput!("no type given for parameter ${n}");
        }
        let prepared =
            PreparedStatement { statement, types, cached: None, executions: 0, plans: 0 };
        self.0.insert(name, prepared);
        Ok(())
    }

    /// Removes a prepared statement.
    pub fn deallocate(&mut self, name: &str) -> Result<()> {
        match self.0.remove(name) {
            Some(_) => Ok(()),
            None => errinput!("prepared statement {name} does not exist"),
        }
    }

    /// Fetches a prepared statement, or errors if it does not exist.
    fn get(&self, name: &str) -> Result<&PreparedStatement> {
        self.0.get(name).ok_or_else(|| errinput!("prepared statement {name} does not exist"))
    }

    /// Checks the arguments of an EXECUTE and returns the statement with every
    /// parameter replaced by its value.
    pub fn bind<C: Catalog>(
        &self,
        name: &str,
        arguments: Vec<ast::Expression>,
    ) -> Result<(ast::Statement, Vec<Value>)> {
        let prepared = self.get(name)?;
        if arguments.len() != prepared.types.len() {
            return errinput!(
                "wrong number of arguments for prepared statement {name}: expected {}, found {}",
                prepared.types.len(),
                arguments.len()
            );
        }
        let mut values = Vec::with_capacity(arguments.len());
        for (i, (argument, datatype)) in arguments.into_iter().zip(&prepared.types).enumerate() {
            // The planner is generic over the catalog, which constants don't use.
            let value = Planner::<C>::build_constant_value(argument)?;
            match value.datatype() {
                None => {}
                Some(found) if found == *datatype => {}
                Some(found) => {
                    let n = i + 1;
                    return errinput!("invalid datatype {found} for {datatype} parameter ${n}");
                }
            }
            values.push(value);
        }

        Ok((bind_parameters(prepared.statement.clone(), &values), values))
    }

    /// Returns the statement of a prepared statement, if it exists.
    pub fn statement(&self, name: &str) -> Option<&ast::Statement> {
        self.0.get(name).map(|prepared| &prepared.statement)
    }

    /// Binds the arguments of an EXECUTE, as bind does, and counts the
    /// execution.
    pub fn execute<C: Catalog>(
        &mut self,
        name: &str,
        arguments: Vec<ast::Expression>,
    ) -> Result<(ast::Statement, Vec<Value>)> {
        let bound = self.bind::<C>(name, arguments)?;
        if let Some(prepared) = self.0.get_mut(name) {
            prepared.executions += 1;
        }
        Ok(bound)
    }

    /// Returns the cached plan if it is valid for the arguments and the current
    /// definitions, otherwise plans the statement and caches the plan if
    /// planning succeeds.
    pub fn plan(
        &mut self,
        name: &str,
        statement: ast::Statement,
        arguments: Vec<Value>,
        catalog: &impl Catalog,
    ) -> Result<Plan> {
        let Some(prepared) = self.0.get_mut(name) else {
            return errinput!("prepared statement {name} does not exist");
        };
        if let Some(cached) = &prepared.cached
            && identical(&cached.arguments, &arguments)
            && cached.definitions.unchanged(catalog)?
        {
            return Ok(cached.plan.clone());
        }

        prepared.plans += 1;
        let recorder = RecordingCatalog::new(catalog);
        let plan = Plan::build(statement, &recorder)?.optimize()?;
        let definitions = recorder.definitions.into_inner();
        prepared.cached = Some(CachedPlan { plan: plan.clone(), arguments, definitions });
        Ok(plan)
    }

    /// Returns the SHOW PREPARED result: one row per prepared statement,
    /// ordered by name.
    pub fn show(&self) -> StatementResult {
        let columns = ["name", "parameters", "statement", "executions", "plans"]
            .into_iter()
            .map(|name| Label::Unqualified(name.to_string()))
            .collect();
        let rows = self
            .0
            .iter()
            .map(|(name, prepared)| {
                vec![
                    Value::String(name.clone()),
                    Value::String(prepared.types.iter().join(", ")),
                    Value::String(format::one_line(&prepared.statement)),
                    Value::Integer(prepared.executions as i64),
                    Value::Integer(prepared.plans as i64),
                ]
            })
            .collect();
        StatementResult::Select { columns, rows }
    }
}

/// Returns true if the argument values are identical.
fn identical(a: &[Value], b: &[Value]) -> bool {
    a.len() == b.len()
        && a.iter().zip(b).all(|(a, b)| match (a, b) {
            // 0.0 and -0.0 are equal, but not identical.
            (Value::Float(a), Value::Float(b)) => {
                a.is_nan() && b.is_nan() || a == b && a.is_sign_negative() == b.is_sign_negative()
            }
            (a, b) => a.datatype() == b.datatype() && a == b,
        })
}

/// Returns the statement with every parameter $n replaced by the n-th value.
pub fn bind_parameters(statement: ast::Statement, values: &[Value]) -> ast::Statement {
    statement.transform_expressions(&|expr| {
        expr.transform(&|expr| match expr {
            ast::Expression::Parameter(n) => literal(values[n as usize - 1].clone()).into(),
            expr => expr,
        })
    })
}

/// Returns the literal of a value.
fn literal(value: Value) -> ast::Literal {
    match value {
        Value::Null => ast::Literal::Null,
        Value::Boolean(b) => ast::Literal::Boolean(b),
        Value::Integer(i) => ast::Literal::Integer(i),
        Value::Float(f) => ast::Literal::Float(f),
        Value::String(s) => ast::Literal::String(s),
    }
}

/// A catalog that records the table, view and materialized view definitions
/// looked up in it.
struct RecordingCatalog<'a, C: Catalog> {
    catalog: &'a C,
    definitions: RefCell<Definitions>,
}

impl<'a, C: Catalog> RecordingCatalog<'a, C> {
    fn new(catalog: &'a C) -> Self {
        Self { catalog, definitions: RefCell::default() }
    }
}

impl<C: Catalog> Catalog for RecordingCatalog<'_, C> {
    fn create_table(&self, table: Table) -> Result<()> {
        self.catalog.create_table(table)
    }

    fn drop_table(&self, table: &str, if_exists: bool) -> Result<bool> {
        self.catalog.drop_table(table, if_exists)
    }

    fn get_table(&self, table: &str) -> Result<Option<Table>> {
        let definition = self.catalog.get_table(table)?;
        self.definitions.borrow_mut().tables.insert(table.to_string(), definition.clone());
        Ok(definition)
    }

    fn list_tables(&self) -> Result<Vec<Table>> {
        self.catalog.list_tables()
    }

    fn set_table_statistics(&self, table: &str, statistics: TableStatistics) -> Result<()> {
        self.catalog.set_table_statistics(table, statistics)
    }

    fn get_table_statistics(&self, table: &str) -> Result<Option<TableStatistics>> {
        self.catalog.get_table_statistics(table)
    }

    fn create_view(&self, view: View) -> Result<()> {
        self.catalog.create_view(view)
    }

    fn drop_view(&self, view: &str, if_exists: bool) -> Result<bool> {
        self.catalog.drop_view(view, if_exists)
    }

    fn get_view(&self, view: &str) -> Result<Option<View>> {
        let definition = self.catalog.get_view(view)?;
        self.definitions.borrow_mut().views.insert(view.to_string(), definition.clone());
        Ok(definition)
    }

    fn list_views(&self) -> Result<Vec<View>> {
        self.catalog.list_views()
    }

    fn create_user(&self, user: User) -> Result<()> {
        self.catalog.create_user(user)
    }

    fn drop_user(&self, user: &str) -> Result<()> {
        self.catalog.drop_user(user)
    }

    fn get_user(&self, user: &str) -> Result<Option<User>> {
        self.catalog.get_user(user)
    }

    fn list_users(&self) -> Result<Vec<User>> {
        self.catalog.list_users()
    }

    fn set_membership(&self, member: &str, user: &str, is_member: bool) -> Result<()> {
        self.catalog.set_membership(member, user, is_member)
    }

    fn list_memberships(&self) -> Result<Vec<(String, String)>> {
        self.catalog.list_memberships()
    }

    fn set_privileges(
        &self,
        object: &str,
        grantee: &str,
        privileges: BTreeSet<Privilege>,
    ) -> Result<()> {
        self.catalog.set_privileges(object, grantee, privileges)
    }

    fn list_grants(&self) -> Result<Vec<(String, String, BTreeSet<Privilege>)>> {
        self.catalog.list_grants()
    }

    fn set_owner(&self, object: &str, owner: &str) -> Result<()> {
        self.catalog.set_owner(object, owner)
    }

    fn get_owner(&self, object: &str) -> Result<String> {
        self.catalog.get_owner(object)
    }

    fn set_checks(&self, table: &str, checks: Vec<Check>) -> Result<()> {
        self.catalog.set_checks(table, checks)
    }

    fn get_checks(&self, table: &str) -> Result<Vec<Check>> {
        self.catalog.get_checks(table)
    }

    fn create_policy(&self, policy: Policy) -> Result<()> {
        self.catalog.create_policy(policy)
    }

    fn drop_policy(&self, table: &str, policy: &str) -> Result<bool> {
        self.catalog.drop_policy(table, policy)
    }

    fn list_policies(&self, table: &str) -> Result<Vec<Policy>> {
        self.catalog.list_policies(table)
    }

    fn create_materialized_view(&self, view: MaterializedView, rows: Vec<Row>) -> Result<()> {
        self.catalog.create_materialized_view(view, rows)
    }

    fn refresh_materialized_view(&self, view: &str, rows: Vec<Row>) -> Result<()> {
        self.catalog.refresh_materialized_view(view, rows)
    }

    fn drop_materialized_view(&self, view: &str) -> Result<bool> {
        self.catalog.drop_materialized_view(view)
    }

    fn get_materialized_view(&self, view: &str) -> Result<Option<MaterializedView>> {
        let definition = self.catalog.get_materialized_view(view)?;
        let mut definitions = self.definitions.borrow_mut();
        definitions.materialized_views.insert(view.to_string(), definition.clone());
        Ok(definition)
    }

    fn get_materialized_view_rows(&self, view: &str) -> Result<Vec<Row>> {
        self.catalog.get_materialized_view_rows(view)
    }

    fn list_materialized_views(&self) -> Result<Vec<MaterializedView>> {
        self.catalog.list_materialized_views()
    }
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::engine::{Engine, Local, Transaction as _};
    use crate::sql::parser::Parser;
    use crate::storage::Memory;

    type Txn = <Local<Memory> as Engine<'static>>::Transaction;

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

    /// Prepares the statement of a PREPARE statement.
    fn prepare(prepared: &mut PreparedStatements, statement: &str) -> Result<()> {
        let ast::Statement::Prepare { name, types, statement } = Parser::parse(statement)? else {
            panic!("not a PREPARE statement");
        };
        prepared.prepare(name, types, *statement)
    }

    /// Binds the arguments of the EXECUTE statement.
    fn bind(
        prepared: &PreparedStatements,
        statement: &str,
    ) -> Result<(ast::Statement, Vec<Value>)> {
        let ast::Statement::Execute { name, arguments } = Parser::parse(statement)? else {
            panic!("not an EXECUTE statement");
        };
        prepared.bind::<Txn>(&name, arguments)
    }

    /// Binds and plans the EXECUTE statement in a new read-only transaction,
    /// and returns the plan's display.
    fn plan(
        engine: &Local<Memory>,
        prepared: &mut PreparedStatements,
        statement: &str,
    ) -> Result<String> {
        let ast::Statement::Execute { name, arguments } = Parser::parse(statement)? else {
            panic!("not an EXECUTE statement");
        };
        let (statement, arguments) = prepared.execute::<Txn>(&name, arguments)?;
        let txn = engine.begin_read_only()?;
        let result = prepared.plan(&name, statement, arguments, &txn);
        txn.commit()?;
        Ok(result?.to_string())
    }

    /// Returns the SHOW PREPARED rows, as text.
    fn show(prepared: &PreparedStatements) -> Vec<String> {
        let StatementResult::Select { columns, rows } = prepared.show() else {
            panic!("expected select result");
        };
        assert_eq!(columns.iter().join(", "), "name, parameters, statement, executions, plans");
        rows.into_iter().map(|row| row.into_iter().join(", ")).collect()
    }

    #[test]
    fn prepare_errors() {
        let mut prepared = PreparedStatements::new();
        prepare(&mut prepared, "PREPARE p (INT) AS SELECT $1").unwrap();

        let mut error = |statement| prepare(&mut prepared, statement).unwrap_err().to_string();
        assert_eq!(
            error("PREPARE p AS BEGIN"),
            "invalid input: prepared statement p already exists"
        );
        assert_eq!(
            error("PREPARE q AS COMMIT"),
            "invalid input: PREPARE is not supported for COMMIT"
        );
        assert_eq!(
            error("PREPARE q AS DROP VIEW v"),
            "invalid input: PREPARE is not supported for DROP VIEW"
        );
        assert_eq!(
            error("PREPARE q AS FORMAT SELECT $1"),
            "invalid input: PREPARE is not supported for FORMAT"
        );
        assert_eq!(
            error("PREPARE q (INT) AS SELECT $4, $1, $2, $3"),
            "invalid input: no type given for parameter $2"
        );
        assert_eq!(show(&prepared), vec!["'p', 'INTEGER', 'SELECT $1', 0, 0"]);
    }

    #[test]
    fn deallocate() -> Result<()> {
        let mut prepared = PreparedStatements::new();
        prepare(&mut prepared, "PREPARE p AS SELECT 1")?;
        prepared.deallocate("p")?;
        assert_eq!(
            prepared.deallocate("p").unwrap_err().to_string(),
            "invalid input: prepared statement p does not exist"
        );
        assert_eq!(show(&prepared), Vec::<String>::new());
        Ok(())
    }

    /// Binding replaces every parameter by its argument's value.
    #[test_case("PREPARE p (INT, STRING) AS SELECT $1, -$1 FROM t WHERE v = $2 LIMIT $1", "EXECUTE p (2, 'it''s')"
        => "SELECT 2, -2 FROM t WHERE v = 'it''s' LIMIT 2"; "select")]
    #[test_case("PREPARE p (FLOAT, BOOLEAN) AS INSERT INTO t VALUES ($1, $2), (1, NOT $2)", "EXECUTE p (NAN, NULL)"
        => "INSERT INTO t VALUES (NAN, NULL), (1, NOT NULL)"; "insert")]
    #[test_case("PREPARE p (FLOAT) AS UPDATE t SET v = $1 * 2 WHERE id = $1", "EXECUTE p (INFINITY)"
        => "UPDATE t SET v = INFINITY * 2 WHERE id = INFINITY"; "update")]
    #[test_case("PREPARE p (INT) AS CREATE TABLE x (id INT PRIMARY KEY, n INT DEFAULT $1)", "EXECUTE p (1 + 2)"
        => "CREATE TABLE x (id INTEGER PRIMARY KEY, n INTEGER DEFAULT 3)"; "create table")]
    #[test_case("PREPARE p (INT) AS SELECT * FROM t a JOIN t b ON a.id = b.id + $1", "EXECUTE p (1)"
        => "SELECT * FROM t AS a JOIN t AS b ON a.id = b.id + 1"; "join")]
    #[test_case("PREPARE p AS SELECT 1", "EXECUTE p" => "SELECT 1"; "no parameters")]
    fn bind_statement(prepare_statement: &str, execute: &str) -> String {
        let mut prepared = PreparedStatements::new();
        prepare(&mut prepared, prepare_statement).unwrap();
        let (statement, _) = bind(&prepared, execute).unwrap();
        format::one_line(&statement)
    }

    /// Arguments are checked in order: their number, then each one's value.
    #[test_case("EXECUTE nosuch" => "invalid input: prepared statement nosuch does not exist"; "unknown")]
    #[test_case("EXECUTE p (1)" => "invalid input: wrong number of arguments for prepared statement p: expected 2, found 1"; "too few")]
    #[test_case("EXECUTE p (1, 2.0, 3)" => "invalid input: wrong number of arguments for prepared statement p: expected 2, found 3"; "too many")]
    #[test_case("EXECUTE p (1.0, 'x')" => "invalid input: invalid datatype FLOAT for INTEGER parameter $1"; "float for integer")]
    #[test_case("EXECUTE p (1, 2)" => "invalid input: invalid datatype INTEGER for FLOAT parameter $2"; "integer for float")]
    #[test_case("EXECUTE p (id, 1 / 0)" => "invalid input: expression must be constant, found column id"; "column")]
    #[test_case("EXECUTE p (1, 1 / 0)" => "invalid input: can't divide by zero"; "division by zero")]
    #[test_case("EXECUTE p ('a', 1 / 0)" => "invalid input: invalid datatype STRING for INTEGER parameter $1"; "first error")]
    fn bind_errors(execute: &str) -> String {
        let mut prepared = PreparedStatements::new();
        prepare(&mut prepared, "PREPARE p (INT, FLOAT) AS SELECT $1, $2").unwrap();
        bind(&prepared, execute).unwrap_err().to_string()
    }

    #[test]
    fn bind_values() -> Result<()> {
        let mut prepared = PreparedStatements::new();
        prepare(&mut prepared, "PREPARE p (INT, FLOAT, STRING, BOOLEAN) AS SELECT $1, $2, $3, $4")?;
        let (_, values) = bind(&prepared, "EXECUTE p (NULL, -1.5, 'a', NOT TRUE)")?;
        assert_eq!(
            values,
            vec![Value::Null, Value::Float(-1.5), Value::String("a".into()), Value::Boolean(false)]
        );
        Ok(())
    }

    #[test]
    fn identical_arguments() {
        use Value::*;
        let values = || vec![Null, Float(f64::NAN), String("a".into())];
        assert!(identical(&values(), &values()));
        assert!(identical(&[], &[]));
        assert!(!identical(&[Integer(1)], &[Float(1.0)]));
        assert!(!identical(&[Integer(1)], &[Integer(2)]));
        assert!(!identical(&[Null], &[Integer(0)]));
        assert!(identical(&[Float(-f64::NAN)], &[Float(f64::NAN)]));
        assert!(!identical(&[Float(0.0)], &[Float(-0.0)]));
    }

    #[test]
    fn plan_cache() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, v STRING)",
            "CREATE VIEW tv AS SELECT v FROM t",
        ]);
        let execute = |statement| engine.session().execute(statement).map(|_| ());
        let mut prepared = PreparedStatements::new();
        prepare(&mut prepared, "PREPARE get (INT) AS SELECT v FROM t WHERE id = $1")?;
        prepare(&mut prepared, "PREPARE pv AS SELECT * FROM tv")?;

        // The same arguments use the cached plan, other arguments plan anew.
        let lookup = "Projection: t.v\n└─ KeyLookup: t (1)";
        assert_eq!(plan(&engine, &mut prepared, "EXECUTE get (1)")?, lookup);
        assert_eq!(plan(&engine, &mut prepared, "EXECUTE get (1)")?, lookup);
        assert_eq!(
            plan(&engine, &mut prepared, "EXECUTE get (2)")?,
            "Projection: t.v\n└─ KeyLookup: t (2)"
        );
        assert_eq!(
            plan(&engine, &mut prepared, "EXECUTE pv")?,
            "View: tv\n└─ Projection: t.v\n   └─ Scan: t"
        );
        assert_eq!(
            show(&prepared),
            vec![
                "'get', 'INTEGER', 'SELECT v FROM t WHERE id = $1', 3, 2",
                "'pv', '', 'SELECT * FROM tv', 1, 1",
            ]
        );

        // Writes don't affect the cached plan, nor does a table dropped and
        // created again with the same definition.
        execute("INSERT INTO t VALUES (1, 'a')")?;
        execute("DROP VIEW tv")?;
        execute("DROP TABLE t")?;
        execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING)")?;
        execute("CREATE VIEW tv AS SELECT v FROM t")?;
        plan(&engine, &mut prepared, "EXECUTE get (2)")?;
        plan(&engine, &mut prepared, "EXECUTE pv")?;
        assert_eq!(
            show(&prepared),
            vec![
                "'get', 'INTEGER', 'SELECT v FROM t WHERE id = $1', 4, 2",
                "'pv', '', 'SELECT * FROM tv', 2, 1",
            ]
        );

        // Other definitions plan anew. A failed planning counts as a plan, but
        // keeps the cached plan.
        execute("DROP VIEW tv")?;
        execute("CREATE VIEW tv AS SELECT v FROM t WHERE id = 1")?;
        plan(&engine, &mut prepared, "EXECUTE pv")?;
        execute("DROP VIEW tv")?;
        execute("DROP TABLE t")?;
        assert_eq!(
            plan(&engine, &mut prepared, "EXECUTE get (2)").unwrap_err().to_string(),
            "invalid input: table t does not exist"
        );
        execute("CREATE TABLE t (id INT PRIMARY KEY, v STRING)")?;
        plan(&engine, &mut prepared, "EXECUTE get (2)")?;
        assert_eq!(
            show(&prepared),
            vec![
                "'get', 'INTEGER', 'SELECT v FROM t WHERE id = $1', 6, 3",
                "'pv', '', 'SELECT * FROM tv', 3, 2",
            ]
        );
        Ok(())
    }
}
