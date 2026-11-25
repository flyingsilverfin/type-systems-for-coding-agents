//! Row estimates for `ESTIMATE`: the estimated number of rows of every node of
//! an optimized plan, computed from the column statistics of `ANALYZE` as the
//! transaction sees them.
//!
//! * A table has the row count of its statistics, or 1000 without statistics.
//! * Conditions have a selectivity: from the statistics of the columns they
//!   compare (distinct values and NULLs for equality, interpolation between
//!   minimum and maximum for ranges of numbers), combined for AND, OR and NOT,
//!   and fixed values otherwise.
//! * Each node estimates its rows from its inputs; a column keeps the
//!   statistics of the table column it passes on unchanged.
//!
//! Estimates are computed in floating point and rounded to the nearest integer
//! (halves up) only for display.

use std::collections::HashMap;

use super::plan::{Node, Plan};
use crate::error::Result;
use crate::sql::engine::Catalog;
use crate::sql::execution::StatementResult;
use crate::sql::types::{Expression, Label, Table, TableStatistics, Value};

const TABLE_ROWS: f64 = 1000.0;
const EQUAL_SELECTIVITY: f64 = 0.1;
const RANGE_SELECTIVITY: f64 = 1.0 / 3.0;
const OTHER_SELECTIVITY: f64 = 0.25;
const GROUPS: f64 = 10.0;

/// Returns the estimated rows of every line of the plan display, in display
/// order.
pub fn estimate(plan: &Plan, catalog: &impl Catalog) -> Result<Vec<f64>> {
    let mut estimator = Estimator::new(catalog);
    let mut lines = Vec::new();
    match plan {
        Plan::CreateTable { .. } | Plan::DropTable { .. } => lines.push(0.0),
        Plan::Delete { source, .. } | Plan::Insert { source, .. } | Plan::Update { source, .. } => {
            lines.push(0.0);
            lines[0] = estimator.node(source, &mut lines)?;
        }
        Plan::Select(root) => {
            estimator.node(root, &mut lines)?;
        }
    }
    Ok(lines)
}

/// Builds the ESTIMATE result from a plan and its estimates.
pub fn result(plan: &Plan, estimates: Vec<f64>) -> StatementResult {
    let labels = ["node", "rows"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    let rows = plan
        .to_string()
        .lines()
        .zip(estimates)
        .map(|(line, rows)| {
            vec![Value::String(line.to_string()), Value::Integer(rows.round() as i64)]
        })
        .collect();
    StatementResult::Select { columns: labels, rows }
}

/// The statistics of a table column, for estimates.
#[derive(Clone, Copy, Debug, PartialEq)]
struct ColumnStats {
    /// The number of table rows.
    rows: f64,
    /// The number of NULL values.
    nulls: f64,
    /// The number of distinct non-NULL values.
    distinct: f64,
    /// The minimum and maximum values, if they are numbers.
    range: Option<(f64, f64)>,
}

/// Estimates plan nodes, using the table statistics of a catalog.
struct Estimator<'a, C: Catalog> {
    catalog: &'a C,
    /// Table statistics by table name, as fetched from the catalog.
    statistics: HashMap<String, Option<TableStatistics>>,
}

impl<'a, C: Catalog> Estimator<'a, C> {
    /// Creates a new estimator.
    fn new(catalog: &'a C) -> Self {
        Self { catalog, statistics: HashMap::new() }
    }

    /// Returns the estimated number of rows of a node.
    fn node(&mut self, node: &Node, lines: &mut Vec<f64>) -> Result<f64> {
        // Appends the estimates of the node's display lines to lines: the
        // node's own line, then those of its inputs.
        let line = lines.len();
        lines.push(0.0);

        let rows = match node {
            // Source nodes read the table's rows.
            Node::Scan { table, filter, .. } => {
                let rows = self.table_rows(table)?;
                match filter {
                    Some(filter) => rows * self.selectivity(filter, node)?,
                    None => rows,
                }
            }
            Node::KeyLookup { table, keys, .. } => (keys.len() as f64).min(self.table_rows(table)?),
            Node::IndexLookup { table, column, values, .. } => {
                let rows = self.table_rows(table)?;
                let statistics = self.table_column_statistics(table, *column)?;
                values
                    .iter()
                    .map(|value| match value {
                        Value::Null => rows * null_selectivity(statistics),
                        _ => rows * equal_selectivity(statistics),
                    })
                    .sum()
            }

            // Filters keep the rows for which the predicate is true.
            Node::Filter { source, predicate } => {
                self.node(source, lines)? * self.selectivity(predicate, source)?
            }

            // These nodes emit a row for every source row.
            Node::Order { source, .. }
            | Node::Projection { source, .. }
            | Node::Remap { source, .. }
            | Node::View { source, .. } => self.node(source, lines)?,

            // Limit and Offset count rows.
            Node::Limit { source, limit } => self.node(source, lines)?.min(*limit as f64),
            Node::Offset { source, offset } => {
                (self.node(source, lines)? - *offset as f64).max(0.0)
            }

            // Aggregates emit one row per group.
            Node::Aggregate { source, group_by, .. } => {
                let rows = self.node(source, lines)?;
                if group_by.is_empty() {
                    1.0
                } else {
                    let mut groups = 1.0;
                    for expr in group_by {
                        groups *= self.groups(expr, source)?;
                    }
                    rows.min(groups)
                }
            }

            // Joins combine the rows of both sides.
            Node::HashJoin { left, left_column, right, right_column, outer } => {
                let left_rows = self.node(left, lines)?;
                let right_rows = self.node(right, lines)?;
                let selectivity = columns_selectivity(
                    self.column_statistics(left, *left_column)?,
                    self.column_statistics(right, *right_column)?,
                );
                join_rows(left_rows, right_rows, selectivity, *outer)
            }
            Node::NestedLoopJoin { left, right, predicate, outer } => {
                let left_rows = self.node(left, lines)?;
                let right_rows = self.node(right, lines)?;
                let selectivity = match predicate {
                    Some(predicate) => self.selectivity(predicate, node)?,
                    None => 1.0,
                };
                join_rows(left_rows, right_rows, selectivity, *outer)
            }

            // Constant nodes have a known number of rows.
            Node::Values { rows } => rows.len() as f64,
            Node::Nothing { .. } => 0.0,

            // System tables have no statistics.
            Node::SystemTable { .. } => 0.0,

            // Materialized views have the number of rows they store.
            Node::MaterializedView { view, .. } => {
                self.catalog.get_materialized_view_rows(view)?.len() as f64
            }
        };

        lines[line] = rows;
        Ok(rows)
    }

    /// Returns the estimated fraction of rows for which a condition is true.
    fn selectivity(&mut self, expr: &Expression, node: &Node) -> Result<f64> {
        use Expression::*;

        Ok(match expr {
            Constant(Value::Boolean(true)) => 1.0,
            Constant(Value::Boolean(false) | Value::Null) => 0.0,

            // Logical operators combine the selectivities of their operands.
            And(lhs, rhs) => self.selectivity(lhs, node)? * self.selectivity(rhs, node)?,
            Or(lhs, rhs) => {
                let lhs = self.selectivity(lhs, node)?;
                let rhs = self.selectivity(rhs, node)?;
                lhs + rhs - lhs * rhs
            }
            Not(expr) => 1.0 - self.selectivity(expr, node)?,

            // Comparisons with the NULL constant.
            Equal(lhs, rhs) | GreaterThan(lhs, rhs) | LessThan(lhs, rhs)
                if is_null(lhs) || is_null(rhs) =>
            {
                0.0
            }

            // Comparisons of columns with constants or other columns.
            Equal(lhs, rhs) => match (lhs.as_ref(), rhs.as_ref()) {
                (Column(lhs), Column(rhs)) => columns_selectivity(
                    self.column_statistics(node, *lhs)?,
                    self.column_statistics(node, *rhs)?,
                ),
                (Column(index), Constant(_)) | (Constant(_), Column(index)) => {
                    equal_selectivity(self.column_statistics(node, *index)?)
                }
                _ => OTHER_SELECTIVITY,
            },
            LessThan(lhs, rhs) => match (lhs.as_ref(), rhs.as_ref()) {
                (Column(index), Constant(value)) => {
                    range_selectivity(self.column_statistics(node, *index)?, value, true)
                }
                (Constant(value), Column(index)) => {
                    range_selectivity(self.column_statistics(node, *index)?, value, false)
                }
                _ => OTHER_SELECTIVITY,
            },
            GreaterThan(lhs, rhs) => match (lhs.as_ref(), rhs.as_ref()) {
                (Column(index), Constant(value)) => {
                    range_selectivity(self.column_statistics(node, *index)?, value, false)
                }
                (Constant(value), Column(index)) => {
                    range_selectivity(self.column_statistics(node, *index)?, value, true)
                }
                _ => OTHER_SELECTIVITY,
            },
            Is(expr, Value::Null) => match expr.as_ref() {
                Column(index) => null_selectivity(self.column_statistics(node, *index)?),
                _ => OTHER_SELECTIVITY,
            },

            _ => OTHER_SELECTIVITY,
        })
    }

    /// Returns the estimated number of distinct values of a GROUP BY
    /// expression.
    fn groups(&mut self, expr: &Expression, node: &Node) -> Result<f64> {
        let statistics = match expr {
            Expression::Column(index) => self.column_statistics(node, *index)?,
            _ => None,
        };
        Ok(match statistics {
            Some(statistics) if statistics.nulls > 0.0 => statistics.distinct + 1.0,
            Some(statistics) => statistics.distinct,
            None => GROUPS,
        })
    }

    /// Returns the statistics of the table column that a column reference
    /// passes on unchanged, if any.
    fn column_statistics(&mut self, node: &Node, index: usize) -> Result<Option<ColumnStats>> {
        match node {
            // Source nodes use the table's statistics.
            Node::IndexLookup { table, .. }
            | Node::KeyLookup { table, .. }
            | Node::Scan { table, .. } => self.table_column_statistics(table, index),

            // These nodes pass on plain column references.
            Node::Aggregate { source, group_by, .. } => match group_by.get(index) {
                Some(Expression::Column(index)) => self.column_statistics(source, *index),
                Some(_) | None => Ok(None),
            },
            Node::Projection { source, expressions, .. } => match &expressions[index] {
                Expression::Column(index) => self.column_statistics(source, *index),
                _ => Ok(None),
            },
            Node::Remap { source, targets } => {
                match targets.iter().position(|target| *target == Some(index)) {
                    Some(index) => self.column_statistics(source, index),
                    None => Ok(None),
                }
            }

            // Joins dispatch to the appropriate source.
            Node::HashJoin { left, right, .. } | Node::NestedLoopJoin { left, right, .. } => {
                if index < left.columns() {
                    self.column_statistics(left, index)
                } else {
                    self.column_statistics(right, index - left.columns())
                }
            }

            // Views and simple nodes pass through the source columns.
            Node::Filter { source, .. }
            | Node::Limit { source, .. }
            | Node::Offset { source, .. }
            | Node::Order { source, .. }
            | Node::View { source, .. } => self.column_statistics(source, index),

            // Constant nodes, materialized views and system tables have no
            // statistics.
            Node::MaterializedView { .. }
            | Node::Nothing { .. }
            | Node::SystemTable { .. }
            | Node::Values { .. } => Ok(None),
        }
    }

    /// Returns the statistics of a table's column, if the table was analyzed.
    fn table_column_statistics(
        &mut self,
        table: &Table,
        column: usize,
    ) -> Result<Option<ColumnStats>> {
        let Some(statistics) = self.table_statistics(&table.name)? else {
            return Ok(None);
        };
        let rows = statistics.rows as f64;
        Ok(statistics.columns.get(&table.columns[column].name).map(|column| ColumnStats {
            rows,
            nulls: column.nulls as f64,
            distinct: column.distinct as f64,
            range: column.min.as_ref().and_then(number).zip(column.max.as_ref().and_then(number)),
        }))
    }

    /// Returns the number of rows of a table.
    fn table_rows(&mut self, table: &Table) -> Result<f64> {
        Ok(self.table_statistics(&table.name)?.map_or(TABLE_ROWS, |stats| stats.rows as f64))
    }

    /// Returns a table's statistics, if it was analyzed.
    fn table_statistics(&mut self, table: &str) -> Result<Option<&TableStatistics>> {
        if !self.statistics.contains_key(table) {
            let statistics = self.catalog.get_table_statistics(table)?;
            self.statistics.insert(table.to_string(), statistics);
        }
        Ok(self.statistics[table].as_ref())
    }
}

/// Returns the selectivity of a column's equality with a constant.
fn equal_selectivity(statistics: Option<ColumnStats>) -> f64 {
    match statistics {
        Some(s) if s.rows == 0.0 || s.distinct == 0.0 => 0.0,
        Some(s) => (1.0 - s.nulls / s.rows) / s.distinct,
        None => EQUAL_SELECTIVITY,
    }
}

/// Returns the selectivity of the equality of two columns.
fn columns_selectivity(lhs: Option<ColumnStats>, rhs: Option<ColumnStats>) -> f64 {
    match (lhs, rhs) {
        (Some(lhs), Some(rhs)) if lhs.distinct == 0.0 && rhs.distinct == 0.0 => 0.0,
        (Some(lhs), Some(rhs)) => 1.0 / lhs.distinct.max(rhs.distinct),
        _ => EQUAL_SELECTIVITY,
    }
}

/// Returns the selectivity of a column IS NULL.
fn null_selectivity(statistics: Option<ColumnStats>) -> f64 {
    match statistics {
        Some(s) if s.rows == 0.0 => 0.0,
        Some(s) => s.nulls / s.rows,
        None => EQUAL_SELECTIVITY,
    }
}

/// Returns the selectivity of a column below (c < v) or above (c > v) a
/// constant.
fn range_selectivity(statistics: Option<ColumnStats>, value: &Value, below: bool) -> f64 {
    let (Some(s), Some(value)) = (statistics, number(value)) else {
        return RANGE_SELECTIVITY;
    };
    let Some((min, max)) = s.range.filter(|(min, max)| (max - min).is_finite()) else {
        return RANGE_SELECTIVITY;
    };
    let fraction = match (below, max == min) {
        (true, true) => f64::from(value > min),
        (false, true) => f64::from(value < max),
        (true, false) => ((value - min) / (max - min)).clamp(0.0, 1.0),
        (false, false) => ((max - value) / (max - min)).clamp(0.0, 1.0),
    };
    fraction * (1.0 - s.nulls / s.rows)
}

/// Returns the estimated rows of a join with the given condition selectivity.
fn join_rows(left: f64, right: f64, selectivity: f64, outer: bool) -> f64 {
    let rows = left * right * selectivity;
    if outer { rows.max(left) } else { rows }
}

/// Returns a value as a finite number, if it is one.
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Integer(integer) => Some(*integer as f64),
        Value::Float(float) if float.is_finite() => Some(*float),
        _ => None,
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

    /// Creates the analyzed tables t (10 rows) and u (2 rows).
    fn setup_analyzed() -> Local<Memory> {
        setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, i INT, f FLOAT, s STRING INDEX, k INT)",
            "INSERT INTO t VALUES (1, 0, 0.0, 'a', 7), (2, 0, 1.0, 'a', 7), (3, 1, 2.0, 'a', 7), \
             (4, 1, 3.0, 'a', 7), (5, 2, 4.0, 'a', 7), (6, 2, 5.0, 'b', 7), (7, 3, 6.0, 'b', 7), \
             (8, 4, 7.0, 'b', 7), (9, NULL, 8.0, 'c', 7), (10, NULL, NULL, NULL, 7)",
            "CREATE TABLE u (id INT PRIMARY KEY, t_id INT)",
            "INSERT INTO u VALUES (1, 1), (2, NULL)",
            "ANALYZE",
        ])
    }

    /// Plans and optimizes a statement, and estimates the plan.
    fn estimate_sql(engine: &Local<Memory>, statement: &str) -> Result<Vec<f64>> {
        let txn = engine.begin_read_only()?;
        let plan = Plan::build(Parser::parse(statement)?, &txn)?.optimize()?;
        let estimates = estimate(&plan, &txn)?;
        assert_eq!(estimates.len(), plan.to_string().lines().count(), "{plan}");
        // Round off floating point errors.
        Ok(estimates.into_iter().map(|rows| (rows * 1e6).round() / 1e6).collect())
    }

    /// The estimated rows of a scan of the analyzed table t with a condition.
    /// t has 10 rows; i has 2 NULLs and 5 distinct values from 0 to 4, f 1 NULL
    /// and 9 distinct values from 0.0 to 8.0, s 1 NULL and 3 distinct values,
    /// and k is always 7.
    #[test_case("TRUE" => 10.0; "constant true")]
    #[test_case("FALSE" => 0.0; "constant false")]
    #[test_case("i = 2" => 1.6; "equal")]
    #[test_case("2 = i" => 1.6; "equal reversed")]
    #[test_case("s = 'a'" => 3.0; "equal string")]
    #[test_case("i = NULL" => 0.0; "equal null")]
    #[test_case("i > NULL" => 0.0; "greater null")]
    #[test_case("i = f" => 1.111111; "equal columns")]
    #[test_case("i IS NULL" => 2.0; "is null")]
    #[test_case("i IS NOT NULL" => 8.0; "is not null")]
    #[test_case("i < 3" => 6.0; "less")]
    #[test_case("3 > i" => 6.0; "less reversed")]
    #[test_case("i > 3" => 2.0; "greater")]
    #[test_case("3 < i" => 2.0; "greater reversed")]
    #[test_case("i < 10" => 8.0; "less above max")]
    #[test_case("i > -1" => 8.0; "greater below min")]
    #[test_case("f < 2.0" => 2.25; "less float")]
    #[test_case("f > 2" => 6.75; "greater float with integer")]
    #[test_case("f < NAN" => 3.333333; "less nan")]
    #[test_case("f < INFINITY" => 3.333333; "less infinity")]
    #[test_case("k < 8" => 10.0; "less single value")]
    #[test_case("k < 7" => 0.0; "less single value equal")]
    #[test_case("k > 6" => 10.0; "greater single value")]
    #[test_case("k > 7" => 0.0; "greater single value equal")]
    #[test_case("s < 'b'" => 3.333333; "less string")]
    #[test_case("i >= 3" => 3.28; "greater or equal")]
    #[test_case("i != 2" => 8.4; "not equal")]
    #[test_case("i = 2 AND s = 'a'" => 0.48; "and")]
    #[test_case("i = 2 OR s = 'a'" => 4.12; "or")]
    #[test_case("NOT (i = 2 OR s = 'a')" => 5.88; "not")]
    #[test_case("s LIKE 'a%'" => 2.5; "like")]
    #[test_case("f IS NAN" => 2.5; "is nan")]
    #[test_case("i * 2 = 4" => 2.5; "equal expression")]
    #[test_case("i < f" => 2.5; "less columns")]
    fn condition(condition: &str) -> f64 {
        let engine = setup_analyzed();
        let estimates =
            estimate_sql(&engine, &format!("SELECT * FROM t WHERE {condition}")).unwrap();
        estimates[0]
    }

    /// The estimated rows of a scan of a table without statistics.
    #[test_case("" => 1000.0; "no condition")]
    #[test_case("WHERE i = 2" => 100.0; "equal")]
    #[test_case("WHERE i = f" => 100.0; "equal columns")]
    #[test_case("WHERE i IS NULL" => 100.0; "is null")]
    #[test_case("WHERE i < 3" => 333.333333; "less")]
    #[test_case("WHERE 3 < i" => 333.333333; "greater reversed")]
    #[test_case("WHERE i = NULL" => 0.0; "equal null")]
    #[test_case("WHERE s LIKE 'a%'" => 250.0; "like")]
    fn condition_without_statistics(condition: &str) -> f64 {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY, i INT, f FLOAT, s STRING)"]);
        let estimates = estimate_sql(&engine, &format!("SELECT * FROM t {condition}")).unwrap();
        estimates[0]
    }

    /// The estimated rows of every plan line of a statement over the analyzed
    /// tables t and u.
    #[test_case("SELECT * FROM t" => vec![10.0]; "scan")]
    #[test_case("SELECT * FROM t WHERE id = 1 OR id = 2 OR id = 99" => vec![3.0]; "key lookup")]
    #[test_case("SELECT * FROM u WHERE id = 1 OR id = 2 OR id = 3" => vec![2.0]; "key lookup above rows")]
    #[test_case("SELECT * FROM t WHERE s = 'b' OR s IS NULL" => vec![4.0]; "index lookup")]
    #[test_case("SELECT * FROM t WHERE id = 1 AND i > 3" => vec![0.2, 1.0]; "filter")]
    #[test_case("SELECT s FROM t ORDER BY f" => vec![10.0, 10.0, 10.0, 10.0]; "projection order remap")]
    #[test_case("SELECT id FROM t LIMIT 3 OFFSET 8" => vec![2.0, 2.0, 10.0, 10.0]; "limit offset")]
    #[test_case("SELECT id FROM t LIMIT 3" => vec![3.0, 10.0, 10.0]; "limit")]
    #[test_case("SELECT id FROM t OFFSET 20" => vec![0.0, 10.0, 10.0]; "offset")]
    #[test_case("SELECT 1, 2" => vec![1.0, 1.0]; "values")]
    #[test_case("SELECT * FROM t WHERE FALSE" => vec![0.0]; "nothing")]
    #[test_case("SELECT COUNT(*) FROM t" => vec![1.0, 10.0]; "aggregate")]
    #[test_case("SELECT s, COUNT(*) FROM t GROUP BY s" => vec![4.0, 10.0]; "group by nullable")]
    #[test_case("SELECT k, COUNT(*) FROM t GROUP BY k" => vec![1.0, 10.0]; "group by not null")]
    #[test_case("SELECT i, s, COUNT(*) FROM t GROUP BY i, s" => vec![10.0, 10.0]; "group by many")]
    #[test_case("SELECT i % 2, COUNT(*) FROM t WHERE id = 1 GROUP BY i % 2" => vec![1.0, 1.0]; "group by expression")]
    #[test_case("SELECT * FROM t a JOIN t b ON a.i = b.i" => vec![20.0, 10.0, 10.0]; "hash join")]
    #[test_case("SELECT * FROM t JOIN u ON u.t_id = t.id" => vec![2.0, 10.0, 2.0]; "hash join inner")]
    #[test_case("SELECT * FROM t LEFT JOIN u ON u.t_id = t.id" => vec![10.0, 10.0, 2.0]; "hash join outer")]
    #[test_case("SELECT * FROM t, u" => vec![20.0, 10.0, 2.0]; "nested loop join")]
    #[test_case("SELECT * FROM t JOIN u ON t.f > u.id" => vec![5.0, 10.0, 2.0]; "nested loop join condition")]
    #[test_case("SELECT * FROM u LEFT JOIN t ON t.f > u.id" => vec![5.0, 2.0, 10.0]; "nested loop join outer")]
    #[test_case("SELECT * FROM u LEFT JOIN t ON t.k > u.id AND t.f < u.id" => vec![2.0, 2.0, 10.0]; "nested loop join outer minimum")]
    #[test_case("SELECT t.s, COUNT(*) FROM t JOIN u ON u.t_id = t.id GROUP BY t.s" => vec![2.0, 2.0, 10.0, 2.0]; "group by join column")]
    #[test_case("INSERT INTO u VALUES (3, 1), (4, 2)" => vec![2.0, 2.0]; "insert")]
    #[test_case("UPDATE t SET k = 1 WHERE i IS NULL" => vec![2.0, 2.0]; "update")]
    #[test_case("DELETE FROM t WHERE id = 1" => vec![1.0, 1.0]; "delete")]
    #[test_case("CREATE TABLE x (id INT PRIMARY KEY)" => vec![0.0]; "create table")]
    #[test_case("DROP TABLE u" => vec![0.0]; "drop table")]
    fn statement(statement: &str) -> Vec<f64> {
        let engine = setup_analyzed();
        estimate_sql(&engine, statement).unwrap()
    }

    #[test]
    fn views() -> Result<()> {
        let engine = setup_analyzed();
        let mut session = engine.session();
        session.execute("CREATE VIEW v AS SELECT i, i + 0 AS j FROM t")?;
        drop(session);

        // Columns keep their statistics through the view, computed columns
        // have none.
        let estimates = estimate_sql(&engine, "SELECT * FROM v WHERE i = 2")?;
        assert_eq!(estimates, vec![1.6, 10.0, 10.0, 10.0]);
        let estimates = estimate_sql(&engine, "SELECT * FROM v WHERE j = 2")?;
        assert_eq!(estimates, vec![1.0, 10.0, 10.0, 10.0]);
        Ok(())
    }

    #[test]
    fn statistics_as_of_analyze() -> Result<()> {
        let engine = setup_analyzed();
        let mut session = engine.session();
        session.execute("DELETE FROM t WHERE id > 2")?;
        drop(session);
        assert_eq!(estimate_sql(&engine, "SELECT * FROM t")?, vec![10.0]);

        let mut session = engine.session();
        session.execute("ANALYZE t")?;
        drop(session);
        assert_eq!(estimate_sql(&engine, "SELECT * FROM t")?, vec![2.0]);
        assert_eq!(estimate_sql(&engine, "SELECT * FROM t WHERE i IS NULL")?, vec![0.0]);
        Ok(())
    }

    #[test]
    fn empty_table() -> Result<()> {
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY, i INT)", "ANALYZE t"]);
        for statement in [
            "SELECT * FROM t",
            "SELECT * FROM t WHERE i = 1",
            "SELECT * FROM t WHERE i IS NULL",
            "SELECT * FROM t WHERE i > 1",
            "SELECT * FROM t WHERE id = 1",
        ] {
            assert_eq!(estimate_sql(&engine, statement)?, vec![0.0], "{statement}");
        }
        Ok(())
    }

    /// A range whose width max - min overflows has no interpolation.
    #[test]
    fn range_overflow() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE t (id INT PRIMARY KEY, f FLOAT)",
            "INSERT INTO t VALUES (1, -1e308), (2, 0.0), (3, 1e308)",
            "CREATE TABLE u (id INT PRIMARY KEY, f FLOAT)",
            "INSERT INTO u VALUES (1, 1e308), (2, 1.5e308)",
            "ANALYZE",
        ]);
        assert_eq!(estimate_sql(&engine, "SELECT * FROM t WHERE f < 0")?, vec![1.0]);
        assert_eq!(estimate_sql(&engine, "SELECT * FROM t WHERE f < 1e308")?, vec![1.0]);
        assert_eq!(estimate_sql(&engine, "SELECT * FROM t WHERE f > -1e308")?, vec![1.0]);
        // A finite width interpolates, also where v - min overflows.
        assert_eq!(estimate_sql(&engine, "SELECT * FROM u WHERE f > -1e308")?, vec![2.0]);
        assert_eq!(estimate_sql(&engine, "SELECT * FROM u WHERE f < -1e308")?, vec![0.0]);
        Ok(())
    }

    #[test]
    fn result_rows() -> Result<()> {
        let engine = setup_analyzed();
        let txn = engine.begin_read_only()?;
        let plan = Plan::build(Parser::parse("SELECT s FROM t WHERE i > 3")?, &txn)?.optimize()?;
        let estimates = estimate(&plan, &txn)?;
        assert_eq!(
            result(&plan, estimates),
            StatementResult::Select {
                columns: vec![Label::Unqualified("node".into()), Label::Unqualified("rows".into())],
                rows: vec![
                    vec!["Projection: t.s".into(), 2.into()],
                    vec!["└─ Scan: t (t.i > 3)".into(), 2.into()],
                ],
            }
        );

        // Halves are rounded up.
        let nothing = Plan::Select(Node::Nothing { columns: Vec::new() });
        let rows = |estimates| match result(&nothing, estimates) {
            StatementResult::Select { rows, .. } => rows,
            result => panic!("unexpected result {result:?}"),
        };
        assert_eq!(rows(vec![2.5]), vec![vec!["Nothing".into(), 3.into()]]);
        assert_eq!(rows(vec![0.49]), vec![vec!["Nothing".into(), 0.into()]]);
        Ok(())
    }
}
