//! Query profiles for `EXPLAIN (ANALYZE)`: executes a planned statement, with
//! its effects, and returns its plan with the estimated and the actual number
//! of rows of every line of the plan display.
//!
//! The statement is executed as when it is issued, except that every plan node
//! is run to completion before its parent reads its rows, and the rows each node
//! produces are counted. A write's statement line has the count of its result; a
//! statement line without one (also of a plan added later) has 0; every node,
//! also one added later, has the rows it produced.

use itertools::izip;

use super::StatementResult;
use super::executor::{ExecutionResult, Executor};
use super::system::SessionState;
use crate::error::Result;
use crate::sql::engine::Transaction;
use crate::sql::planner::Plan;
use crate::sql::types::{Label, Value};

/// Executes a plan, running every node to completion, and returns the number
/// of rows of every line of its plan display, in display order.
pub fn profile(
    plan: Plan,
    txn: &impl Transaction,
    user: &str,
    session: Option<&SessionState>,
) -> Result<(ExecutionResult, Vec<u64>)> {
    let mut executor = Executor::profiling(txn, user, session);
    let result = executor.execute(plan)?;
    let statement = match &result {
        // A query's plan display has no statement line.
        ExecutionResult::Select { .. } => None,
        ExecutionResult::Delete { count }
        | ExecutionResult::Insert { count }
        | ExecutionResult::Update { count } => Some(*count),
        _ => Some(0),
    };
    let lines = statement.into_iter().chain(executor.into_profile()).collect();
    Ok((result, lines))
}

/// Builds the EXPLAIN (ANALYZE) result from a plan display, its estimates and
/// its profile.
pub fn result(display: &str, estimates: Vec<f64>, rows: Vec<u64>) -> StatementResult {
    let labels = ["node", "estimate", "rows"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    let rows = izip!(display.lines(), estimates, rows)
        .map(|(line, estimate, rows)| {
            vec![
                Value::String(line.to_string()),
                Value::Integer(estimate.round() as i64),
                Value::Integer(rows as i64),
            ]
        })
        .collect();
    StatementResult::Select { columns: labels, rows }
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::engine::{Engine as _, Local};
    use crate::sql::parser::Parser;
    use crate::storage::Memory;

    /// Creates a local engine with the tables t (5 rows) and u (2 rows).
    fn setup() -> Local<Memory> {
        let engine = Local::new(Memory::new());
        let mut session = engine.session();
        for statement in [
            "CREATE TABLE t (id INT PRIMARY KEY, i INT, s STRING INDEX)",
            "INSERT INTO t VALUES (1, 0, 'a'), (2, 1, 'a'), (3, 2, 'b'), (4, NULL, 'b'), \
             (5, 4, NULL)",
            "CREATE TABLE u (id INT PRIMARY KEY, t_id INT)",
            "INSERT INTO u VALUES (1, 1), (2, 3)",
        ] {
            session.execute(statement).expect("statement failed");
        }
        drop(session);
        engine
    }

    /// Plans, optimizes and profiles a statement in a transaction of its own,
    /// which is committed if the statement succeeds.
    fn profile_sql(engine: &Local<Memory>, statement: &str) -> Result<Vec<u64>> {
        let txn = engine.begin()?;
        let plan = Plan::build(Parser::parse(statement)?, &txn)?.optimize()?;
        let display = plan.to_string();
        let (_, rows) = match profile(plan, &txn, "admin", None) {
            Ok(profile) => profile,
            Err(error) => {
                txn.rollback()?;
                return Err(error);
            }
        };
        txn.commit()?;
        assert_eq!(rows.len(), display.lines().count(), "{display}");
        Ok(rows)
    }

    /// The rows of every plan line of a statement over the tables t and u.
    #[test_case("SELECT * FROM t" => vec![5]; "scan")]
    #[test_case("SELECT * FROM t WHERE i > 0" => vec![3]; "scan filter")]
    #[test_case("SELECT * FROM t WHERE id = 1 OR id = 9" => vec![1]; "key lookup")]
    #[test_case("SELECT * FROM t WHERE s = 'b' OR s IS NULL" => vec![3]; "index lookup")]
    #[test_case("SELECT * FROM t WHERE id = 1 AND i = 1" => vec![0, 1]; "filter")]
    #[test_case("SELECT s FROM t ORDER BY i" => vec![5, 5, 5, 5]; "projection order remap")]
    #[test_case("SELECT id FROM t LIMIT 2" => vec![2, 5, 5]; "limit")]
    #[test_case("SELECT id FROM t LIMIT 2 OFFSET 4" => vec![1, 1, 5, 5]; "limit offset")]
    #[test_case("SELECT 1, 2" => vec![1, 1]; "values")]
    #[test_case("SELECT * FROM t WHERE FALSE" => vec![0]; "nothing")]
    #[test_case("SELECT COUNT(*) FROM t" => vec![1, 5]; "aggregate")]
    #[test_case("SELECT s, COUNT(*) FROM t GROUP BY s" => vec![3, 5]; "group by")]
    #[test_case("SELECT * FROM t JOIN u ON u.t_id = t.id" => vec![2, 5, 2]; "hash join")]
    #[test_case("SELECT * FROM t LEFT JOIN u ON u.t_id = t.id" => vec![5, 5, 2]; "hash join outer")]
    #[test_case("SELECT * FROM t, u" => vec![10, 5, 2]; "nested loop join")]
    #[test_case("SELECT * FROM t JOIN u ON t.id < u.t_id" => vec![2, 5, 2]; "nested loop join condition")]
    #[test_case("INSERT INTO u VALUES (3, 1), (4, 2)" => vec![2, 2]; "insert")]
    #[test_case("UPDATE t SET i = 1 WHERE i IS NULL" => vec![1, 1]; "update")]
    #[test_case("DELETE FROM t WHERE id = 1 OR id = 9" => vec![1, 1]; "delete")]
    #[test_case("DELETE FROM t WHERE i > 9" => vec![0, 0]; "delete nothing")]
    #[test_case("CREATE TABLE x (id INT PRIMARY KEY)" => vec![0]; "create table")]
    #[test_case("DROP TABLE u" => vec![0]; "drop table")]
    fn statement(statement: &str) -> Vec<u64> {
        let engine = setup();
        profile_sql(&engine, statement).unwrap()
    }

    #[test]
    fn views() -> Result<()> {
        let engine = setup();
        let mut session = engine.session();
        session.execute("CREATE VIEW v AS SELECT id, i FROM t WHERE i < 2")?;
        drop(session);
        assert_eq!(profile_sql(&engine, "SELECT * FROM v WHERE i = 1")?, vec![1, 2, 2, 2]);
        Ok(())
    }

    /// Every node runs to completion, also below a LIMIT that the statement
    /// reaches before the failing row.
    #[test]
    fn run_to_completion() -> Result<()> {
        let engine = setup();
        let mut session = engine.session();
        let value: i64 = session.execute("SELECT 1 / (id - 2) FROM t LIMIT 1")?.try_into()?;
        assert_eq!(value, -1);
        drop(session);
        assert_eq!(
            profile_sql(&engine, "SELECT 1 / (id - 2) FROM t LIMIT 1").map_err(|e| e.to_string()),
            Err("invalid input: can't divide by zero".to_string())
        );
        Ok(())
    }

    /// The statement's writes are made.
    #[test]
    fn writes() -> Result<()> {
        let engine = setup();
        assert_eq!(profile_sql(&engine, "DELETE FROM t WHERE i >= 1")?, vec![3, 3]);
        assert_eq!(profile_sql(&engine, "UPDATE u SET t_id = 9")?, vec![2, 2]);
        let mut session = engine.session();
        let count: i64 = session.execute("SELECT COUNT(*) FROM t")?.try_into()?;
        assert_eq!(count, 2);
        let count: i64 = session.execute("SELECT COUNT(*) FROM u WHERE t_id = 9")?.try_into()?;
        assert_eq!(count, 2);
        Ok(())
    }

    #[test]
    fn result_rows() {
        let display = "Limit: 1\n└─ Scan: t";
        assert_eq!(
            result(display, vec![1.0, 2.5], vec![1, 5]),
            StatementResult::Select {
                columns: vec![
                    Label::Unqualified("node".into()),
                    Label::Unqualified("estimate".into()),
                    Label::Unqualified("rows".into()),
                ],
                rows: vec![
                    vec!["Limit: 1".into(), 1.into(), 1.into()],
                    vec!["└─ Scan: t".into(), 3.into(), 5.into()],
                ],
            }
        );
    }
}
