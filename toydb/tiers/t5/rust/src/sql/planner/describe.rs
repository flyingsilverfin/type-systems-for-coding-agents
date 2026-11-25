//! Result description for `DESCRIBE`: the result columns of a statement, or
//! the columns of a table or view, with their data types and nullability.
//!
//! A statement is described from its plan before optimization, so no
//! expression is evaluated.
//!
//! * The type of an expression is the data type of the values it returns when
//!   its evaluation succeeds. It is unknown if that depends on the values of
//!   its operands, not only on their types, or if the expression can't return
//!   a non-NULL value. The NULL constant only returns NULL; an operand of
//!   unknown type may return values of any type.
//! * An expression is nullable if it can return NULL: a nullable column, a
//!   column of the inner side of an outer join, the NULL constant, an operator
//!   or function with a nullable operand (except `IS NULL`), and every
//!   aggregate function except `count`.

use super::plan::{Aggregate, Node, Plan};
use super::planner::Planner;
use crate::errinput;
use crate::error::Result;
use crate::sql::engine::Catalog;
use crate::sql::execution::{StatementResult, system};
use crate::sql::types::{DataType, Expression, Label, Table, Value};

/// A described column.
#[derive(Clone, Debug, PartialEq)]
pub struct ColumnDescription {
    /// The column name, if any.
    pub name: Option<String>,
    /// The column's data type, if known.
    pub datatype: Option<DataType>,
    /// Whether the column can be NULL.
    pub nullable: bool,
}

/// Describes the result columns of a plan: one entry per column of a query,
/// none for other statements.
pub fn describe(plan: &Plan) -> Vec<ColumnDescription> {
    match plan {
        Plan::Select(root) => (0..root.columns())
            .map(|index| ColumnDescription {
                name: match root.column_label(index) {
                    Label::None => None,
                    label => Some(label.to_string()),
                },
                datatype: column_datatype(root, index),
                nullable: column_nullable(root, index),
            })
            .collect(),
        Plan::CreateTable { .. }
        | Plan::DropTable { .. }
        | Plan::Delete { .. }
        | Plan::Insert { .. }
        | Plan::Update { .. } => Vec::new(),
    }
}

/// Describes the columns of a table, view or materialized view.
pub fn describe_table(catalog: &impl Catalog, name: &str) -> Result<Vec<ColumnDescription>> {
    if let Some(table) = catalog.get_table(name)? {
        return Ok(table_columns(table));
    }
    let planner = Planner::new(catalog);
    let (names, node) = match catalog.get_view(name)? {
        Some(view) => (view.columns.clone(), planner.build_view(view, None)?),
        None => match catalog.get_materialized_view(name)? {
            Some(view) => (view.columns.clone(), planner.build_materialized_view(view, None)?),
            None => return errinput!("table {name} does not exist"),
        },
    };
    Ok(names
        .into_iter()
        .enumerate()
        .map(|(index, name)| ColumnDescription {
            name: Some(name),
            datatype: column_datatype(&node, index),
            nullable: column_nullable(&node, index),
        })
        .collect())
}

/// Describes the columns of a table, view, materialized view or system table.
pub fn describe_name(catalog: &impl Catalog, name: &str) -> Result<Vec<ColumnDescription>> {
    match system::schema(name) {
        Some(table) if system::is_system_table(catalog, name)? => Ok(table_columns(table)),
        _ => describe_table(catalog, name),
    }
}

/// Describes the columns of a table schema.
fn table_columns(table: Table) -> Vec<ColumnDescription> {
    table
        .columns
        .into_iter()
        .map(|column| ColumnDescription {
            name: Some(column.name),
            datatype: Some(column.datatype),
            nullable: column.nullable,
        })
        .collect()
}

/// Builds the DESCRIBE result from the described columns.
pub fn result(columns: Vec<ColumnDescription>) -> StatementResult {
    let labels = ["column", "type", "nullable"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    let rows = columns
        .into_iter()
        .map(|column| {
            vec![
                column.name.map(Value::String).unwrap_or(Value::Null),
                column.datatype.map(|t| Value::String(t.to_string())).unwrap_or(Value::Null),
                Value::Boolean(column.nullable),
            ]
        })
        .collect();
    StatementResult::Select { columns: labels, rows }
}

/// Returns the data type of an expression, or None if it is unknown.
pub fn datatype(expr: &Expression, node: &Node) -> Option<DataType> {
    use DataType::*;
    use Expression::*;

    match expr {
        Constant(value) => value.datatype(),
        Column(index) => column_datatype(node, *index),

        // a >= b and a <= b are planned as a > b OR a = b (resp. <), a != b as
        // NOT a = b, and a IS NOT x as NOT a IS x, so these have the type of the
        // comparison of a and b, or of a IS x.
        Or(lhs, rhs) => match (lhs.as_ref(), rhs.as_ref()) {
            (GreaterThan(a, b) | LessThan(a, b), Equal(c, d)) if a == c && b == d => {
                comparison_datatype(a, b, node)
            }
            _ => logical_datatype(&[lhs.as_ref(), rhs.as_ref()], node),
        },
        Not(expr) => match expr.as_ref() {
            Equal(a, b) => comparison_datatype(a, b, node),
            Is(..) => datatype(expr, node),
            expr => logical_datatype(&[expr], node),
        },
        And(lhs, rhs) => logical_datatype(&[lhs.as_ref(), rhs.as_ref()], node),

        Equal(lhs, rhs) | GreaterThan(lhs, rhs) | LessThan(lhs, rhs) => {
            comparison_datatype(lhs, rhs, node)
        }
        Like(lhs, rhs) => {
            (may_return(lhs, &[String], node) && may_return(rhs, &[String], node))
                .then_some(Boolean)
        }
        Is(_, Value::Null) => Some(Boolean),
        Is(expr, _) => may_return(expr, &[Float], node).then_some(Boolean),

        Add(lhs, rhs)
        | Divide(lhs, rhs)
        | Multiply(lhs, rhs)
        | Remainder(lhs, rhs)
        | Subtract(lhs, rhs) => match (datatype(lhs, node), datatype(rhs, node)) {
            (Some(Integer), Some(Integer)) => Some(Integer),
            _ => float_datatype(lhs, rhs, node),
        },
        Exponentiate(lhs, rhs) => float_datatype(lhs, rhs, node),
        Negate(expr) | Identity(expr) => match datatype(expr, node) {
            Some(t @ (Integer | Float)) => Some(t),
            _ => None,
        },
        Factorial(expr) => may_return(expr, &[Integer], node).then_some(Integer),
        SquareRoot(expr) => may_return(expr, &[Integer, Float], node).then_some(Float),
    }
}

/// Returns true if an expression can return NULL.
pub fn nullable(expr: &Expression, node: &Node) -> bool {
    use Expression::*;

    match expr {
        Constant(value) => matches!(value, Value::Null),
        Column(index) => column_nullable(node, *index),

        Is(_, Value::Null) => false,

        Add(lhs, rhs)
        | And(lhs, rhs)
        | Divide(lhs, rhs)
        | Equal(lhs, rhs)
        | Exponentiate(lhs, rhs)
        | GreaterThan(lhs, rhs)
        | LessThan(lhs, rhs)
        | Like(lhs, rhs)
        | Multiply(lhs, rhs)
        | Or(lhs, rhs)
        | Remainder(lhs, rhs)
        | Subtract(lhs, rhs) => nullable(lhs, node) || nullable(rhs, node),

        Factorial(expr)
        | Identity(expr)
        | Is(expr, _)
        | Negate(expr)
        | Not(expr)
        | SquareRoot(expr) => nullable(expr, node),
    }
}

/// Returns the data type of a node's column, or None if it is unknown.
fn column_datatype(node: &Node, index: usize) -> Option<DataType> {
    match node {
        // Source nodes use the table's column schema.
        Node::IndexLookup { table, .. }
        | Node::KeyLookup { table, .. }
        | Node::Scan { table, .. }
        | Node::SystemTable { table, .. } => Some(table.columns[index].datatype),

        // These nodes compute their columns from the source columns.
        Node::Aggregate { source, group_by, aggregates } => match group_by.get(index) {
            Some(expr) => datatype(expr, source),
            None => aggregate_datatype(&aggregates[index - group_by.len()], source),
        },
        Node::Projection { source, expressions, .. } => datatype(&expressions[index], source),
        Node::Remap { source, targets } => targets
            .iter()
            .position(|target| *target == Some(index))
            .and_then(|index| column_datatype(source, index)),

        // Joins dispatch to the appropriate source.
        Node::HashJoin { left, right, .. } | Node::NestedLoopJoin { left, right, .. } => {
            if index < left.columns() {
                column_datatype(left, index)
            } else {
                column_datatype(right, index - left.columns())
            }
        }

        // Views and simple nodes pass through the source columns.
        Node::Filter { source, .. }
        | Node::Limit { source, .. }
        | Node::Offset { source, .. }
        | Node::Order { source, .. }
        | Node::View { source, .. } => column_datatype(source, index),

        // Materialized views have the types described from their query.
        Node::MaterializedView { types, .. } => types[index].0,

        // Nothing nodes don't retain the types of the replaced nodes.
        Node::Nothing { .. } => None,

        // A Values column returns the value of one of its rows.
        Node::Values { rows } => {
            let mut datatypes = rows
                .iter()
                .map(|row| &row[index])
                .filter(|expr| !is_null(expr))
                .map(|expr| datatype(expr, node));
            let first = datatypes.next().flatten()?;
            datatypes.all(|t| t == Some(first)).then_some(first)
        }
    }
}

/// Returns true if a node's column can be NULL.
fn column_nullable(node: &Node, index: usize) -> bool {
    match node {
        Node::IndexLookup { table, .. }
        | Node::KeyLookup { table, .. }
        | Node::Scan { table, .. }
        | Node::SystemTable { table, .. } => table.columns[index].nullable,

        Node::Aggregate { source, group_by, aggregates } => match group_by.get(index) {
            Some(expr) => nullable(expr, source),
            None => aggregate_nullable(&aggregates[index - group_by.len()]),
        },
        Node::Projection { source, expressions, .. } => nullable(&expressions[index], source),
        Node::Remap { source, targets } => targets
            .iter()
            .position(|target| *target == Some(index))
            .is_none_or(|index| column_nullable(source, index)),

        // The right side of an outer join is NULL for unmatched left rows.
        Node::HashJoin { left, right, outer, .. }
        | Node::NestedLoopJoin { left, right, outer, .. } => {
            if index < left.columns() {
                column_nullable(left, index)
            } else {
                *outer || column_nullable(right, index - left.columns())
            }
        }

        Node::Filter { source, .. }
        | Node::Limit { source, .. }
        | Node::Offset { source, .. }
        | Node::Order { source, .. }
        | Node::View { source, .. } => column_nullable(source, index),

        Node::MaterializedView { types, .. } => types[index].1,

        Node::Nothing { .. } => true,

        Node::Values { rows } => rows.iter().any(|row| nullable(&row[index], node)),
    }
}

/// Returns the data type of an aggregate function's result, or None if it is
/// unknown.
fn aggregate_datatype(aggregate: &Aggregate, node: &Node) -> Option<DataType> {
    match aggregate {
        Aggregate::Count(_) => Some(DataType::Integer),
        Aggregate::Average(expr) | Aggregate::Sum(expr) => match datatype(expr, node) {
            Some(t @ (DataType::Integer | DataType::Float)) => Some(t),
            _ => None,
        },
        Aggregate::Max(expr) | Aggregate::Min(expr) => datatype(expr, node),
    }
}

/// Returns true if an aggregate function's result can be NULL.
fn aggregate_nullable(aggregate: &Aggregate) -> bool {
    match aggregate {
        Aggregate::Count(_) => false,
        Aggregate::Average(_) | Aggregate::Max(_) | Aggregate::Min(_) | Aggregate::Sum(_) => true,
    }
}

/// Returns the data type of AND, OR or NOT of the given operands.
fn logical_datatype(operands: &[&Expression], node: &Node) -> Option<DataType> {
    let other = operands
        .iter()
        .any(|expr| matches!(datatype(expr, node), Some(t) if t != DataType::Boolean));
    let null = operands.iter().all(|expr| is_null(expr));
    (!other && !null).then_some(DataType::Boolean)
}

/// Returns the data type of a comparison of two operands.
fn comparison_datatype(lhs: &Expression, rhs: &Expression, node: &Node) -> Option<DataType> {
    use DataType::*;
    if is_null(lhs) || is_null(rhs) {
        return None;
    }
    match (datatype(lhs, node), datatype(rhs, node)) {
        (Some(Integer | Float), Some(Integer | Float)) => Some(Boolean),
        (Some(lhs), Some(rhs)) if lhs != rhs => None,
        _ => Some(Boolean),
    }
}

/// Returns the data type of arithmetic with a FLOAT operand.
fn float_datatype(lhs: &Expression, rhs: &Expression, node: &Node) -> Option<DataType> {
    use DataType::*;
    let float = |expr| datatype(expr, node) == Some(Float);
    let number = |expr| may_return(expr, &[Integer, Float], node);
    ((float(lhs) && number(rhs)) || (float(rhs) && number(lhs))).then_some(Float)
}

/// Returns true if an expression can return a value of one of the given data
/// types.
fn may_return(expr: &Expression, datatypes: &[DataType], node: &Node) -> bool {
    match datatype(expr, node) {
        Some(t) => datatypes.contains(&t),
        None => !is_null(expr),
    }
}

/// Returns true if an expression is the NULL constant.
fn is_null(expr: &Expression) -> bool {
    matches!(expr, Expression::Constant(Value::Null))
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::engine::{Engine as _, Local};
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

    /// Plans a statement without optimizing it, and describes the plan.
    fn describe_sql(engine: &Local<Memory>, statement: &str) -> Result<Vec<ColumnDescription>> {
        let txn = engine.begin_read_only()?;
        let plan = Plan::build(Parser::parse(statement)?, &txn)?;
        Ok(describe(&plan))
    }

    /// Shorthand for a column description.
    fn column(name: Option<&str>, datatype: Option<DataType>, nullable: bool) -> ColumnDescription {
        ColumnDescription { name: name.map(str::to_string), datatype, nullable }
    }

    /// The data type and nullability of an expression over a table t.
    #[test_case("1" => (Some(DataType::Integer), false); "integer")]
    #[test_case("1.5" => (Some(DataType::Float), false); "float")]
    #[test_case("'a'" => (Some(DataType::String), false); "string")]
    #[test_case("NULL" => (None, true); "null")]
    #[test_case("id" => (Some(DataType::Integer), false); "primary key")]
    #[test_case("i" => (Some(DataType::Integer), true); "nullable column")]
    #[test_case("s" => (Some(DataType::String), false); "not null column")]
    #[test_case("id + 1" => (Some(DataType::Integer), false); "integer arithmetic")]
    #[test_case("i * f" => (Some(DataType::Float), true); "mixed arithmetic")]
    #[test_case("s + 1" => (None, false); "string arithmetic")]
    #[test_case("i + NULL" => (None, true); "arithmetic with null")]
    #[test_case("i ^ i" => (None, true); "integer exponent")]
    #[test_case("f ^ i" => (Some(DataType::Float), true); "float exponent")]
    #[test_case("-f" => (Some(DataType::Float), true); "negate")]
    #[test_case("-s" => (None, false); "negate string")]
    #[test_case("id!" => (Some(DataType::Integer), false); "factorial")]
    #[test_case("f!" => (None, true); "factorial of float")]
    #[test_case("sqrt(id)" => (Some(DataType::Float), false); "square root")]
    #[test_case("sqrt(NULL)" => (None, true); "square root of null")]
    #[test_case("i = f" => (Some(DataType::Boolean), true); "numeric comparison")]
    #[test_case("s < 'a'" => (Some(DataType::Boolean), false); "string comparison")]
    #[test_case("s = 1" => (None, false); "incomparable")]
    #[test_case("s != 1" => (None, false); "incomparable not equal")]
    #[test_case("s >= 1" => (None, false); "incomparable greater or equal")]
    #[test_case("i <= f" => (Some(DataType::Boolean), true); "less or equal")]
    #[test_case("NULL = 1" => (None, true); "compare null")]
    #[test_case("NULL != 1" => (None, true); "not equal null")]
    #[test_case("1 >= NULL" => (None, true); "greater or equal null")]
    #[test_case("s LIKE 'a%'" => (Some(DataType::Boolean), false); "like")]
    #[test_case("i LIKE 'a'" => (None, true); "like integer")]
    #[test_case("i IS NULL" => (Some(DataType::Boolean), false); "is null")]
    #[test_case("NULL IS NOT NULL" => (Some(DataType::Boolean), false); "is not null")]
    #[test_case("f IS NAN" => (Some(DataType::Boolean), true); "is nan")]
    #[test_case("s IS NAN" => (None, false); "is nan string")]
    #[test_case("f IS NOT NAN" => (Some(DataType::Boolean), true); "is not nan")]
    #[test_case("s IS NOT NAN" => (None, false); "is not nan string")]
    #[test_case("NULL IS NOT NAN" => (None, true); "is not nan null")]
    #[test_case("i IS NOT NULL" => (Some(DataType::Boolean), false); "is not null column")]
    #[test_case("NOT (s = 1)" => (None, false); "written out not equal")]
    #[test_case("s > 1 OR s = 1" => (None, false); "written out greater or equal")]
    #[test_case("s = 1 OR s > 1" => (Some(DataType::Boolean), false); "or of comparisons")]
    #[test_case("(NULL + 1) = 1" => (Some(DataType::Boolean), true); "compare null expression")]
    #[test_case("NOT -NULL" => (Some(DataType::Boolean), true); "not null expression")]
    #[test_case("(s + 1) * 2.0" => (Some(DataType::Float), false); "float with unknown")]
    #[test_case("NOT b" => (Some(DataType::Boolean), true); "not")]
    #[test_case("NOT i" => (None, true); "not integer")]
    #[test_case("NOT NULL" => (None, true); "not null constant")]
    #[test_case("b AND NULL" => (Some(DataType::Boolean), true); "and null")]
    #[test_case("NULL OR NULL" => (None, true); "or nulls")]
    #[test_case("s = 'a' OR id > 1" => (Some(DataType::Boolean), false); "or")]
    #[test_case("b OR s" => (None, true); "or string")]
    fn expression(expr: &str) -> (Option<DataType>, bool) {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, i INT, f FLOAT, s STRING NOT NULL, b BOOLEAN)",
        ]);
        let columns = describe_sql(&engine, &format!("SELECT {expr} FROM t")).unwrap();
        assert_eq!(columns.len(), 1);
        (columns[0].datatype, columns[0].nullable)
    }

    /// The data type and nullability of an aggregate function over a table t.
    #[test_case("COUNT(*)" => (Some(DataType::Integer), false); "count all")]
    #[test_case("COUNT(i)" => (Some(DataType::Integer), false); "count")]
    #[test_case("SUM(id)" => (Some(DataType::Integer), true); "sum")]
    #[test_case("AVG(f)" => (Some(DataType::Float), true); "average")]
    #[test_case("SUM(s)" => (None, true); "sum of strings")]
    #[test_case("MIN(s)" => (Some(DataType::String), true); "min")]
    #[test_case("MAX(NULL)" => (None, true); "max of null")]
    #[test_case("MIN(NULL) = 1" => (Some(DataType::Boolean), true); "compare min of null")]
    #[test_case("COUNT(*) * 2.0" => (Some(DataType::Float), false); "expression of count")]
    fn aggregate(expr: &str) -> (Option<DataType>, bool) {
        let engine =
            setup(&["CREATE TABLE t (id INT PRIMARY KEY, i INT, f FLOAT, s STRING NOT NULL)"]);
        let columns = describe_sql(&engine, &format!("SELECT {expr} FROM t")).unwrap();
        assert_eq!(columns.len(), 1);
        (columns[0].datatype, columns[0].nullable)
    }

    #[test]
    fn query_columns() -> Result<()> {
        use DataType::*;
        let engine = setup(&[
            "CREATE TABLE dept (id INT PRIMARY KEY, name STRING NOT NULL)",
            "CREATE TABLE emp (id INT PRIMARY KEY, name STRING NOT NULL, dept_id INT, pay FLOAT)",
            "CREATE VIEW ev AS SELECT e.id, d.name dept FROM emp e LEFT JOIN dept d ON d.id = e.id",
        ]);

        // Columns are named like result headers. The inner side of an outer
        // join is nullable.
        assert_eq!(
            describe_sql(
                &engine,
                "SELECT e.id, d.name, pay * 2 AS p, NULL FROM emp e LEFT JOIN dept d ON d.id = e.id"
            )?,
            vec![
                column(Some("e.id"), Some(Integer), false),
                column(Some("d.name"), Some(String), true),
                column(Some("p"), Some(Float), true),
                column(None, None, true),
            ]
        );
        assert_eq!(
            describe_sql(&engine, "SELECT * FROM emp e RIGHT JOIN dept d ON e.dept_id = d.id")?,
            vec![
                column(Some("e.id"), Some(Integer), true),
                column(Some("e.name"), Some(String), true),
                column(Some("e.dept_id"), Some(Integer), true),
                column(Some("e.pay"), Some(Float), true),
                column(Some("d.id"), Some(Integer), false),
                column(Some("d.name"), Some(String), false),
            ]
        );
        assert_eq!(
            describe_sql(&engine, "SELECT e.name, d.name FROM emp e, dept d")?,
            vec![
                column(Some("e.name"), Some(String), false),
                column(Some("d.name"), Some(String), false),
            ]
        );

        // GROUP BY expressions and aggregates, and hidden ORDER BY columns.
        assert_eq!(
            describe_sql(
                &engine,
                "SELECT dept_id % 2 AS m, COUNT(*) FROM emp GROUP BY dept_id % 2 ORDER BY MAX(pay)"
            )?,
            vec![column(Some("m"), Some(Integer), true), column(None, Some(Integer), false)]
        );

        // Views, also as the inner side of an outer join.
        assert_eq!(
            describe_sql(&engine, "SELECT * FROM ev")?,
            vec![
                column(Some("ev.id"), Some(Integer), false),
                column(Some("ev.dept"), Some(String), true),
            ]
        );
        assert_eq!(
            describe_sql(&engine, "SELECT x.id, d.id FROM dept d LEFT JOIN ev x ON x.id = d.id")?,
            vec![
                column(Some("x.id"), Some(Integer), true),
                column(Some("d.id"), Some(Integer), false),
            ]
        );

        // Expressions aren't evaluated.
        let columns = describe_sql(&engine, "SELECT 1 / 0")?;
        assert_eq!(columns, vec![column(None, Some(Integer), false)]);
        Ok(())
    }

    #[test]
    fn statements_without_columns() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY, v STRING)"]);
        for statement in [
            "INSERT INTO t VALUES (1, 'a')",
            "UPDATE t SET v = 'b'",
            "DELETE FROM t",
            "CREATE TABLE u (id INT PRIMARY KEY)",
            "DROP TABLE t",
        ] {
            assert_eq!(describe_sql(&engine, statement)?, Vec::new(), "{statement}");
        }
        Ok(())
    }

    #[test]
    fn tables_and_views() -> Result<()> {
        use DataType::*;
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, name STRING NOT NULL, v FLOAT)",
            "CREATE VIEW w (a, b, c) AS SELECT id, v * 2, NULL FROM t",
        ]);
        let txn = engine.begin_read_only()?;
        assert_eq!(
            describe_table(&txn, "t")?,
            vec![
                column(Some("id"), Some(Integer), false),
                column(Some("name"), Some(String), false),
                column(Some("v"), Some(Float), true),
            ]
        );
        assert_eq!(
            describe_table(&txn, "w")?,
            vec![
                column(Some("a"), Some(Integer), false),
                column(Some("b"), Some(Float), true),
                column(Some("c"), None, true),
            ]
        );
        assert_eq!(
            describe_table(&txn, "nosuch").unwrap_err().to_string(),
            "invalid input: table nosuch does not exist"
        );
        Ok(())
    }

    #[test]
    fn result_rows() {
        let result = result(vec![
            column(Some("t.id"), Some(DataType::Integer), false),
            column(None, None, true),
        ]);
        assert_eq!(
            result,
            StatementResult::Select {
                columns: vec![
                    Label::Unqualified("column".into()),
                    Label::Unqualified("type".into()),
                    Label::Unqualified("nullable".into()),
                ],
                rows: vec![
                    vec!["t.id".into(), "INTEGER".into(), false.into()],
                    vec![Value::Null, Value::Null, true.into()],
                ],
            }
        );
    }
}
