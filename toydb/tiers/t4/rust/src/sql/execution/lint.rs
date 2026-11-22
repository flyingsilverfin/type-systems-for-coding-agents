//! Statement lint for `LINT`: warnings about likely mistakes in a statement and
//! the statements it contains, without planning or executing them. Each warning
//! has a code and a message.
//!
//! * `unfiltered_write`: UPDATE or DELETE without WHERE.
//! * `implicit_columns`: INSERT without a column list.
//! * `null_comparison`: a comparison with the NULL constant.
//! * `type_mismatch`: a column compared with a literal or parameter of a type it
//!   can't be compared with.
//! * `like_without_wildcard`: LIKE with a pattern without `%` or `_`.
//! * `division_by_zero`: division or remainder by the integer literal 0.
//! * `constant_condition`: a WHERE, HAVING or ON condition without columns,
//!   aggregates or parameters.
//! * `order_by_constant`: an ORDER BY expression without columns, aggregates or
//!   parameters.
//!
//! Column references are resolved against the tables and views the statement
//! reads; references that don't resolve are not checked. Warnings are listed in
//! the order of the statement's canonical text.

use std::fmt::Display;

use super::StatementResult;
use crate::error::{Error, Result};
use crate::sql::engine::Catalog;
use crate::sql::parser::{ast, format};
use crate::sql::planner::Planner;
use crate::sql::planner::describe::{self, ColumnDescription};
use crate::sql::types::{DataType, Label, Value};

/// A lint warning: its code and message.
#[derive(Clone, Debug, PartialEq)]
pub struct Warning {
    pub code: Code,
    pub message: String,
}

/// A warning code.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Code {
    UnfilteredWrite,
    ImplicitColumns,
    NullComparison,
    TypeMismatch,
    LikeWithoutWildcard,
    DivisionByZero,
    ConstantCondition,
    OrderByConstant,
}

impl Display for Code {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str(match self {
            Self::UnfilteredWrite => "unfiltered_write",
            Self::ImplicitColumns => "implicit_columns",
            Self::NullComparison => "null_comparison",
            Self::TypeMismatch => "type_mismatch",
            Self::LikeWithoutWildcard => "like_without_wildcard",
            Self::DivisionByZero => "division_by_zero",
            Self::ConstantCondition => "constant_condition",
            Self::OrderByConstant => "order_by_constant",
        })
    }
}

/// Returns the warnings for a statement and the statements it contains, in
/// canonical text order.
pub fn lint(statement: &ast::Statement, catalog: &impl Catalog) -> Result<Vec<Warning>> {
    let mut linter = Linter { catalog, warnings: Vec::new() };
    linter.statement(statement, &[])?;
    Ok(linter.warnings)
}

/// Builds the LINT result from the warnings.
pub fn result(warnings: Vec<Warning>) -> StatementResult {
    let columns = ["code", "message"]
        .into_iter()
        .map(|name| Label::Unqualified(name.to_string()))
        .collect();
    let rows = warnings
        .into_iter()
        .map(|warning| {
            vec![Value::String(warning.code.to_string()), Value::String(warning.message)]
        })
        .collect();
    StatementResult::Select { columns, rows }
}

/// A clause whose expression is also checked as a whole.
#[derive(Clone, Copy)]
enum Clause {
    /// A WHERE, HAVING or JOIN ... ON condition.
    Condition,
    /// An ORDER BY expression.
    OrderBy,
}

/// A table or view that a statement reads.
struct Source {
    /// The alias, or the name of the table or view.
    name: String,
    /// The columns, as DESCRIBE gives them.
    columns: Vec<ColumnDescription>,
}

impl Source {
    /// Returns the column of the given name, if any.
    fn column(&self, name: &str) -> Option<&ColumnDescription> {
        self.columns.iter().find(|column| column.name.as_deref() == Some(name))
    }
}

/// The scope of an expression: the tables and views its column references
/// resolve against, and the parameter types of the enclosing PREPARE.
struct Scope<'a> {
    sources: Vec<Source>,
    types: &'a [DataType],
}

impl<'a> Scope<'a> {
    /// Creates a scope without tables or views.
    fn new(types: &'a [DataType]) -> Self {
        Self { sources: Vec::new(), types }
    }

    /// Returns the data type of a column reference, if it resolves and is known.
    fn column_datatype(&self, table: Option<&str>, name: &str) -> Option<DataType> {
        let mut sources = self.sources.iter().filter(|source| match table {
            Some(table) => source.name == table,
            None => source.column(name).is_some(),
        });
        match (sources.next(), sources.next()) {
            (Some(source), None) => source.column(name)?.datatype,
            _ => None,
        }
    }

    /// Returns the type of a parameter, if known.
    fn parameter_datatype(&self, n: u64) -> Option<DataType> {
        let index = usize::try_from(n.checked_sub(1)?).ok()?;
        self.types.get(index).copied()
    }
}

/// Lints statements, collecting the warnings.
struct Linter<'a, C: Catalog> {
    catalog: &'a C,
    warnings: Vec<Warning>,
}

impl<C: Catalog> Linter<'_, C> {
    /// Lints a statement, given the parameter types of the enclosing PREPARE.
    fn statement(&mut self, statement: &ast::Statement, types: &[DataType]) -> Result<()> {
        use ast::Statement::*;

        match statement {
            Explain(statement)
            | Format(statement)
            | Describe(ast::Describe::Statement(statement))
            | Estimate(statement)
            | Lint(statement) => self.statement(statement, types)?,
            CreateView { query, .. } => self.statement(query, types)?,
            Prepare { types, statement, .. } => self.statement(statement, types)?,

            CreateTable { columns, .. } => {
                let scope = Scope::new(types);
                for default in columns.iter().filter_map(|column| column.default.as_ref()) {
                    self.expression(default, &scope, None);
                }
            }
            Delete { table, r#where } => {
                if r#where.is_none() {
                    let table = format::ident(table);
                    let message = format!("DELETE without WHERE deletes every row of {table}");
                    self.warn(Code::UnfilteredWrite, message);
                }
                let scope =
                    Scope { sources: self.source(table, None)?.into_iter().collect(), types };
                if let Some(r#where) = r#where {
                    self.expression(r#where, &scope, Some(Clause::Condition));
                }
            }
            Insert { table, columns, values } => {
                if columns.is_none() {
                    let table = format::ident(table);
                    let message = format!(
                        "INSERT without a column list depends on the column order of {table}"
                    );
                    self.warn(Code::ImplicitColumns, message);
                }
                let scope = Scope::new(types);
                for expr in values.iter().flatten() {
                    self.expression(expr, &scope, None);
                }
            }
            Update { table, set, r#where } => {
                if r#where.is_none() {
                    let table = format::ident(table);
                    let message = format!("UPDATE without WHERE changes every row of {table}");
                    self.warn(Code::UnfilteredWrite, message);
                }
                let scope =
                    Scope { sources: self.source(table, None)?.into_iter().collect(), types };
                for expr in set.values().flatten() {
                    self.expression(expr, &scope, None);
                }
                if let Some(r#where) = r#where {
                    self.expression(r#where, &scope, Some(Clause::Condition));
                }
            }
            Select { select, from, r#where, group_by, having, order_by, offset, limit } => {
                let mut sources = Vec::new();
                for item in from {
                    self.from_sources(item, &mut sources)?;
                }
                let scope = Scope { sources, types };
                for (expr, _) in select {
                    self.expression(expr, &scope, None);
                }
                for item in from {
                    self.from_predicates(item, &scope);
                }
                if let Some(r#where) = r#where {
                    self.expression(r#where, &scope, Some(Clause::Condition));
                }
                for expr in group_by {
                    self.expression(expr, &scope, None);
                }
                if let Some(having) = having {
                    self.expression(having, &scope, Some(Clause::Condition));
                }
                for (expr, _) in order_by {
                    self.expression(expr, &scope, Some(Clause::OrderBy));
                }
                if let Some(limit) = limit {
                    self.expression(limit, &scope, None);
                }
                if let Some(offset) = offset {
                    self.expression(offset, &scope, None);
                }
            }
            Execute { arguments, .. } => {
                let scope = Scope::new(types);
                for argument in arguments {
                    self.expression(argument, &scope, None);
                }
            }

            Begin { .. }
            | Commit
            | Rollback
            | DropTable { .. }
            | ShowStatements
            | Analyze { .. }
            | ShowStatistics { .. }
            | Export { .. }
            | Import { .. }
            | DropView { .. }
            | ShowViews
            | Describe(ast::Describe::Name(_))
            | Deallocate { .. }
            | ShowPrepared
            | ShowChanges { .. } => {}
        }
        Ok(())
    }

    /// Adds the tables and views of a FROM item to the sources.
    fn from_sources(&self, item: &ast::From, sources: &mut Vec<Source>) -> Result<()> {
        match item {
            ast::From::Table { name, alias } => {
                sources.extend(self.source(name, alias.as_deref())?)
            }
            ast::From::Join { left, right, .. } => {
                self.from_sources(left, sources)?;
                self.from_sources(right, sources)?;
            }
        }
        Ok(())
    }

    /// Lints the join conditions of a FROM item.
    fn from_predicates(&mut self, item: &ast::From, scope: &Scope) {
        match item {
            ast::From::Table { .. } => {}
            ast::From::Join { left, right, predicate, .. } => {
                self.from_predicates(left, scope);
                self.from_predicates(right, scope);
                if let Some(predicate) = predicate {
                    self.expression(predicate, scope, Some(Clause::Condition));
                }
            }
        }
    }

    /// Returns the table or view of the given name, if it resolves, with the
    /// column types that DESCRIBE gives it.
    fn source(&self, name: &str, alias: Option<&str>) -> Result<Option<Source>> {
        let columns = match describe::describe_table(self.catalog, name) {
            Ok(columns) => columns,
            // Tables and views that don't resolve are not checked.
            Err(Error::InvalidInput(_)) => return Ok(None),
            Err(error) => return Err(error),
        };
        Ok(Some(Source { name: alias.unwrap_or(name).to_string(), columns }))
    }

    /// Lints an expression and the expressions it contains. If the expression
    /// is the whole of a clause, it is also checked as such.
    fn expression(&mut self, expr: &ast::Expression, scope: &Scope, clause: Option<Clause>) {
        self.operator(expr, scope);
        if let Some(clause) = clause
            && Self::is_constant(expr)
        {
            let text = format::expression(expr);
            match clause {
                Clause::Condition => {
                    self.warn(Code::ConstantCondition, format!("condition is constant: {text}"))
                }
                Clause::OrderBy => self.warn(
                    Code::OrderByConstant,
                    format!("ORDER BY a constant has no effect: {text}"),
                ),
            }
        }
        for operand in operands(expr) {
            self.expression(operand, scope, None);
        }
    }

    /// Checks an operator by the rules for comparisons, LIKE and division.
    fn operator(&mut self, expr: &ast::Expression, scope: &Scope) {
        use ast::Operator::*;

        let ast::Expression::Operator(op) = expr else {
            return;
        };
        match op {
            Equal(lhs, rhs)
            | GreaterThan(lhs, rhs)
            | GreaterThanOrEqual(lhs, rhs)
            | LessThan(lhs, rhs)
            | LessThanOrEqual(lhs, rhs)
            | NotEqual(lhs, rhs) => self.comparison(expr, lhs, rhs, scope),

            Like(_, pattern) => {
                if let ast::Expression::Literal(ast::Literal::String(pattern)) = pattern.as_ref()
                    && !pattern.contains(['%', '_'])
                {
                    let text = format::expression(expr);
                    let message = format!("LIKE without wildcards matches like =: {text}");
                    self.warn(Code::LikeWithoutWildcard, message);
                }
            }

            Divide(_, rhs) | Remainder(_, rhs) => {
                if let ast::Expression::Literal(ast::Literal::Integer(0)) = rhs.as_ref() {
                    let message = format!("division by zero: {}", format::expression(expr));
                    self.warn(Code::DivisionByZero, message);
                }
            }

            _ => {}
        }
    }

    /// Checks a comparison of two operands.
    fn comparison(
        &mut self,
        expr: &ast::Expression,
        lhs: &ast::Expression,
        rhs: &ast::Expression,
        scope: &Scope,
    ) {
        let null = ast::Expression::Literal(ast::Literal::Null);
        if *lhs == null || *rhs == null {
            let message =
                format!("comparison with NULL is never true: {}", format::expression(expr));
            self.warn(Code::NullComparison, message);
        }
        let mismatch = type_mismatch(lhs, rhs, scope).or_else(|| type_mismatch(rhs, lhs, scope));
        if let Some(message) = mismatch {
            self.warn(Code::TypeMismatch, message);
        }
    }

    /// Returns true if an expression contains no column reference, aggregate
    /// function or parameter.
    fn is_constant(expr: &ast::Expression) -> bool {
        match expr {
            ast::Expression::Column(..) | ast::Expression::Parameter(_) => false,
            expr if Planner::<C>::is_aggregate_function(expr) => false,
            expr => operands(expr).into_iter().all(Self::is_constant),
        }
    }

    /// Adds a warning.
    fn warn(&mut self, code: Code, message: String) {
        self.warnings.push(Warning { code, message });
    }
}

/// Returns the type mismatch message for a column compared with a literal or
/// parameter of a type it can't be compared with, if any.
fn type_mismatch(
    column: &ast::Expression,
    value: &ast::Expression,
    scope: &Scope,
) -> Option<String> {
    let ast::Expression::Column(table, name) = column else {
        return None;
    };
    let column_type = scope.column_datatype(table.as_deref(), name)?;
    let (value_type, kind) = match value {
        ast::Expression::Parameter(n) => (scope.parameter_datatype(*n)?, "parameter"),
        value => (literal_datatype(value)?, "value"),
    };
    let numeric = |datatype| matches!(datatype, DataType::Integer | DataType::Float);
    if column_type == value_type || (numeric(column_type) && numeric(value_type)) {
        return None;
    }
    Some(format!(
        "{column_type} column {} compared with {value_type} {kind} {}",
        format::expression(column),
        format::expression(value)
    ))
}

/// Returns the data type of a literal operand, if known.
fn literal_datatype(expr: &ast::Expression) -> Option<DataType> {
    let literal = match expr {
        ast::Expression::Literal(literal) => literal,
        ast::Expression::Operator(ast::Operator::Negate(expr) | ast::Operator::Identity(expr)) => {
            let ast::Expression::Literal(literal) = expr.as_ref() else {
                return None;
            };
            literal
        }
        _ => return None,
    };
    match literal {
        ast::Literal::Null => None,
        ast::Literal::Boolean(_) => Some(DataType::Boolean),
        ast::Literal::Integer(_) => Some(DataType::Integer),
        ast::Literal::Float(_) => Some(DataType::Float),
        ast::Literal::String(_) => Some(DataType::String),
    }
}

/// Returns the operands of an expression.
fn operands(expr: &ast::Expression) -> Vec<&ast::Expression> {
    use ast::Operator::*;

    match expr {
        ast::Expression::Operator(op) => match op {
            Add(lhs, rhs)
            | And(lhs, rhs)
            | Divide(lhs, rhs)
            | Equal(lhs, rhs)
            | Exponentiate(lhs, rhs)
            | GreaterThan(lhs, rhs)
            | GreaterThanOrEqual(lhs, rhs)
            | LessThan(lhs, rhs)
            | LessThanOrEqual(lhs, rhs)
            | Like(lhs, rhs)
            | Multiply(lhs, rhs)
            | NotEqual(lhs, rhs)
            | Or(lhs, rhs)
            | Remainder(lhs, rhs)
            | Subtract(lhs, rhs) => vec![lhs, rhs],

            Factorial(expr)
            | Identity(expr)
            | Is(expr, _)
            | IsNot(expr, _)
            | Negate(expr)
            | Not(expr) => vec![expr],
        },
        ast::Expression::Function(_, args) => args.iter().collect(),
        ast::Expression::All
        | ast::Expression::Column(..)
        | ast::Expression::Literal(_)
        | ast::Expression::Parameter(_) => Vec::new(),
    }
}

#[cfg(test)]
mod tests {
    use itertools::Itertools as _;
    use test_case::test_case;

    use super::*;
    use crate::sql::engine::{Engine as _, Local, Transaction as _};
    use crate::sql::parser::Parser;
    use crate::sql::types::View;
    use crate::storage::Memory;

    /// Creates a local engine with a table t and a view v.
    fn setup() -> Local<Memory> {
        let engine = Local::new(Memory::new());
        let mut session = engine.session();
        for statement in [
            "CREATE TABLE t (id INT PRIMARY KEY, s STRING, n INT, f FLOAT, b BOOLEAN)",
            "CREATE TABLE u (id INT PRIMARY KEY, s STRING)",
            "CREATE VIEW v AS SELECT id, s AS vs, n * 2 AS twice, NULL AS z FROM t",
        ] {
            session.execute(statement).expect("statement failed");
        }
        drop(session);
        engine
    }

    /// Lints a statement, returning the warnings as "code: message".
    fn lint_sql(statement: &str) -> Vec<String> {
        let engine = setup();
        let txn = engine.begin_read_only().unwrap();
        let statement = Parser::parse(statement).unwrap();
        let warnings = lint(&statement, &txn).unwrap();
        warnings.into_iter().map(|w| format!("{}: {}", w.code, w.message)).collect()
    }

    #[test_case("SELECT * FROM t WHERE id = 1" => Vec::<String>::new(); "none")]
    #[test_case("UPDATE t SET n = 1"
        => vec!["unfiltered_write: UPDATE without WHERE changes every row of t"]; "update")]
    #[test_case("DELETE FROM \"T\""
        => vec!["unfiltered_write: DELETE without WHERE deletes every row of \"T\""]; "delete")]
    #[test_case("INSERT INTO t VALUES (1)"
        => vec!["implicit_columns: INSERT without a column list depends on the column order of t"]; "insert")]
    #[test_case("INSERT INTO t (id) VALUES (1)" => Vec::<String>::new(); "insert with columns")]
    #[test_case("SELECT * FROM t WHERE NULL >= n OR n IS NULL"
        => vec!["null_comparison: comparison with NULL is never true: NULL >= n"]; "null comparison")]
    #[test_case("SELECT * FROM t WHERE s = 1 OR n < 'a' OR -1 = b OR f > +1 OR n = 1.5"
        => vec![
            "type_mismatch: STRING column s compared with INTEGER value 1",
            "type_mismatch: INTEGER column n compared with STRING value 'a'",
            "type_mismatch: BOOLEAN column b compared with INTEGER value -1",
        ]; "type mismatch")]
    #[test_case("SELECT * FROM t WHERE s LIKE 'a' OR s LIKE 'a%' OR s LIKE '_'"
        => vec!["like_without_wildcard: LIKE without wildcards matches like =: s LIKE 'a'"]; "like")]
    #[test_case("SELECT n / 0, n % 00, n / 0.0, n / -0 FROM t"
        => vec!["division_by_zero: division by zero: n / 0", "division_by_zero: division by zero: n % 0"]; "division")]
    #[test_case("SELECT * FROM t JOIN u ON 1 = 1 WHERE NOT TRUE GROUP BY id HAVING count(*) > 0"
        => vec!["constant_condition: condition is constant: 1 = 1", "constant_condition: condition is constant: NOT TRUE"]; "constant conditions")]
    #[test_case("SELECT * FROM t ORDER BY 1, -n, sqrt(2) DESC, max(n)"
        => vec!["order_by_constant: ORDER BY a constant has no effect: 1", "order_by_constant: ORDER BY a constant has no effect: sqrt(2)"]; "order by constant")]
    fn rules(statement: &str) -> Vec<String> {
        lint_sql(statement)
    }

    #[test_case("SELECT * FROM t WHERE s = NULL AND 1 / 0 = n" => vec![
        "null_comparison: comparison with NULL is never true: s = NULL",
        "division_by_zero: division by zero: 1 / 0",
    ]; "clauses left to right")]
    #[test_case("SELECT * FROM t WHERE NULL = 1 / 0" => vec![
        "null_comparison: comparison with NULL is never true: NULL = 1 / 0",
        "constant_condition: condition is constant: NULL = 1 / 0",
        "division_by_zero: division by zero: 1 / 0",
    ]; "same part by rule")]
    #[test_case("UPDATE t SET s = 1 / 0, b = n = NULL" => vec![
        "unfiltered_write: UPDATE without WHERE changes every row of t",
        "null_comparison: comparison with NULL is never true: n = NULL",
        "division_by_zero: division by zero: 1 / 0",
    ]; "statement first")]
    #[test_case("SELECT 1 / 0 FROM t JOIN u ON FALSE JOIN t AS w ON w.id = NULL ORDER BY 'a'" => vec![
        "division_by_zero: division by zero: 1 / 0",
        "constant_condition: condition is constant: FALSE",
        "null_comparison: comparison with NULL is never true: w.id = NULL",
        "order_by_constant: ORDER BY a constant has no effect: 'a'",
    ]; "select clauses")]
    #[test_case("EXPLAIN DELETE FROM t" => vec![
        "unfiltered_write: DELETE without WHERE deletes every row of t",
    ]; "explain")]
    #[test_case("CREATE VIEW w AS SELECT * FROM t WHERE TRUE" => vec![
        "constant_condition: condition is constant: TRUE",
    ]; "create view")]
    #[test_case("LINT FORMAT DESCRIBE ESTIMATE INSERT INTO t VALUES (1)" => vec![
        "implicit_columns: INSERT without a column list depends on the column order of t",
    ]; "nested")]
    #[test_case("CREATE TABLE x (id INT PRIMARY KEY DEFAULT 1 % 0)" => vec![
        "division_by_zero: division by zero: 1 % 0",
    ]; "create table default")]
    #[test_case("EXECUTE p (1 / 0, NULL = NULL)" => vec![
        "division_by_zero: division by zero: 1 / 0",
        "null_comparison: comparison with NULL is never true: NULL = NULL",
    ]; "execute arguments")]
    fn order(statement: &str) -> Vec<String> {
        lint_sql(statement)
    }

    #[test_case("SELECT * FROM t, u WHERE s = 1" => Vec::<String>::new(); "ambiguous")]
    #[test_case("SELECT * FROM t, u WHERE u.s = 1 AND n = 'x'" => vec![
        "type_mismatch: STRING column u.s compared with INTEGER value 1",
        "type_mismatch: INTEGER column n compared with STRING value 'x'",
    ]; "qualified and unique")]
    #[test_case("SELECT * FROM t AS a WHERE a.s = 1 AND t.s = 1" => vec![
        "type_mismatch: STRING column a.s compared with INTEGER value 1",
    ]; "alias")]
    #[test_case("SELECT n AS x FROM t WHERE x = 'a' AND nosuch = 1 AND n = s" => Vec::<String>::new(); "unresolved")]
    #[test_case("SELECT * FROM nosuch, t WHERE s = 1 AND nosuch.s = 1" => vec![
        "type_mismatch: STRING column s compared with INTEGER value 1",
    ]; "unknown table")]
    #[test_case("SELECT * FROM t x, u x WHERE x.n = 'a' AND x.id = 'a'" => Vec::<String>::new(); "duplicate alias")]
    #[test_case("SELECT * FROM nosuch x, t x WHERE x.n = 'a'" => vec![
        "type_mismatch: INTEGER column x.n compared with STRING value 'a'",
    ]; "duplicate alias of unknown table")]
    #[test_case("SELECT * FROM v WHERE vs = 1 AND twice = 'a' AND z = 1 AND id = 1.5" => vec![
        "type_mismatch: STRING column vs compared with INTEGER value 1",
        "type_mismatch: INTEGER column twice compared with STRING value 'a'",
    ]; "view")]
    #[test_case("DELETE FROM t WHERE b = 'a'" => vec![
        "type_mismatch: BOOLEAN column b compared with STRING value 'a'",
    ]; "delete table")]
    #[test_case("INSERT INTO t (id) VALUES (s = 1)" => Vec::<String>::new(); "insert")]
    #[test_case("PREPARE p (STRING, FLOAT) AS SELECT * FROM t WHERE n = $1 AND n = $2 AND s = $3" => vec![
        "type_mismatch: INTEGER column n compared with STRING parameter $1",
    ]; "parameters")]
    #[test_case("PREPARE p (INT) AS EXPLAIN DELETE FROM t WHERE $1 = s" => vec![
        "type_mismatch: STRING column s compared with INTEGER parameter $1",
    ]; "parameters in contained statement")]
    #[test_case("SELECT * FROM t WHERE s = $1" => Vec::<String>::new(); "parameter outside prepare")]
    fn types(statement: &str) -> Vec<String> {
        lint_sql(statement)
    }

    /// A view whose query can't be planned doesn't resolve.
    #[test]
    fn unplannable_view() -> Result<()> {
        let engine = setup();
        let txn = engine.begin()?;
        txn.create_view(View {
            name: "w".to_string(),
            columns: vec!["s".to_string()],
            query: "SELECT nosuch AS s FROM t".to_string(),
            dependencies: vec!["t".to_string()],
        })?;
        let statement = Parser::parse("SELECT * FROM w, u WHERE s = 1 AND w.s = 1")?;
        let messages = lint(&statement, &txn)?.into_iter().map(|w| w.message).collect_vec();
        assert_eq!(messages, vec!["STRING column s compared with INTEGER value 1"]);
        txn.rollback()
    }

    #[test]
    fn lint_result() {
        let warnings = vec![Warning { code: Code::TypeMismatch, message: "m".to_string() }];
        let StatementResult::Select { columns, rows } = result(warnings) else {
            panic!("expected select result");
        };
        assert_eq!(columns.iter().join(", "), "code, message");
        assert_eq!(
            rows,
            vec![vec![Value::String("type_mismatch".to_string()), Value::String("m".to_string())]]
        );
    }
}
