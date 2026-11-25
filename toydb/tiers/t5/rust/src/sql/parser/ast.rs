use std::collections::{BTreeMap, BTreeSet};
use std::hash::{Hash, Hasher};

use itertools::Either;
use serde::{Deserialize, Serialize};

use crate::sql::types::{DataType, Privilege};

/// SQL statements are represented as an Abstract Syntax Tree (AST). The
/// statement is the root node of this tree, and describes the syntactic
/// structure of a SQL statement. It is built from a raw SQL string by the
/// parser, and passed on to the planner which validates it and builds an
/// execution plan from it.
#[derive(Clone, Debug)]
pub enum Statement {
    /// BEGIN: begins a new transaction.
    Begin {
        /// READ ONLY: if true, begin a read-only transaction.
        read_only: bool,
        /// AS OF: if given, the MVCC version to read at.
        as_of: Option<u64>,
    },
    /// COMMIT: commits a transaction.
    Commit,
    /// ROLLBACK: rolls back a transaction.
    Rollback,
    /// EXPLAIN: explains a SQL statement's execution plan.
    Explain {
        /// The statement to explain.
        statement: Box<Statement>,
        /// Whether to execute the statement and count the rows of every node
        /// (EXPLAIN (ANALYZE)).
        analyze: bool,
    },
    /// CREATE TABLE: creates a new table.
    CreateTable {
        /// The table name.
        name: String,
        /// Column specifications.
        columns: Vec<Column>,
        /// The table's CHECK constraints, in the order written.
        checks: Vec<Check>,
    },
    /// DROP TABLE: drops a table.
    DropTable {
        /// The table to drop.
        name: String,
        /// IF EXISTS: if true, don't error if the table doesn't exist.
        if_exists: bool,
    },
    /// DELETE: deletes rows from a table.
    Delete {
        /// The table to delete from.
        table: String,
        /// WHERE: optional condition to match rows to delete.
        r#where: Option<Expression>,
    },
    /// INSERT INTO: inserts new rows into a table.
    Insert {
        /// Table to insert into.
        table: String,
        /// Columns to insert values into. If None, all columns are used.
        columns: Option<Vec<String>>,
        /// Row values to insert.
        values: Vec<Vec<Expression>>,
    },
    /// UPDATE: updates rows in a table.
    Update {
        table: String,
        set: BTreeMap<String, Option<Expression>>, // column → value, None for default value
        r#where: Option<Expression>,
    },
    /// SELECT: selects rows, possibly from a table.
    Select {
        /// Expressions to select, with an optional column alias.
        select: Vec<(Expression, Option<String>)>,
        /// FROM: tables to select from.
        from: Vec<From>,
        /// WHERE: optional condition to filter rows.
        r#where: Option<Expression>,
        /// GROUP BY: expressions to group and aggregate by.
        group_by: Vec<Expression>,
        /// HAVING: expression to filter groups by.
        having: Option<Expression>,
        /// ORDER BY: expresisions to sort by, with direction.
        order_by: Vec<(Expression, Direction)>,
        /// OFFSET: row offset to start from.
        offset: Option<Expression>,
        /// LIMIT: maximum number of rows to return.
        limit: Option<Expression>,
    },
    /// SHOW STATEMENTS: shows the session's statement statistics.
    ShowStatements,
    /// ANALYZE: computes and stores column statistics.
    Analyze {
        /// The table to analyze. If None, all tables are analyzed.
        table: Option<String>,
    },
    /// SHOW STATISTICS: shows a table's column statistics.
    ShowStatistics {
        /// The table to show statistics for.
        table: String,
    },
    /// EXPORT: exports a table, or the entire database, as text lines.
    Export {
        /// The table to export. If None, all tables are exported (SQL only).
        table: Option<String>,
        /// The export format.
        format: ExportFormat,
    },
    /// IMPORT INTO: imports rows into a table from text lines.
    Import {
        /// The table to import into.
        table: String,
        /// The import format.
        format: ImportFormat,
        /// The lines to import, one string literal each.
        lines: Vec<String>,
    },
    /// FORMAT: returns the canonical SQL text of a statement.
    Format(Box<Statement>),
    /// CREATE VIEW: creates a view.
    CreateView {
        /// The view name.
        name: String,
        /// The column names, if given.
        columns: Option<Vec<String>>,
        /// The view's query, a SELECT statement.
        query: Box<Statement>,
    },
    /// DROP VIEW: drops a view.
    DropView {
        /// The view to drop.
        name: String,
        /// IF EXISTS: if true, don't error if the view doesn't exist.
        if_exists: bool,
    },
    /// SHOW VIEWS: lists the views.
    ShowViews,
    /// DESCRIBE: describes a table or view, or the result of a statement.
    Describe(Describe),
    /// ESTIMATE: estimates the rows of a statement's plan.
    Estimate(Box<Statement>),
    /// PREPARE: prepares a statement with typed parameters.
    Prepare {
        /// The prepared statement's name.
        name: String,
        /// The parameter types, for $1, $2, ...
        types: Vec<DataType>,
        /// The statement.
        statement: Box<Statement>,
    },
    /// EXECUTE: executes a prepared statement.
    Execute {
        /// The prepared statement's name.
        name: String,
        /// The arguments, one per parameter.
        arguments: Vec<Expression>,
    },
    /// DEALLOCATE: removes a prepared statement.
    Deallocate { name: String },
    /// SHOW PREPARED: lists the session's prepared statements.
    ShowPrepared,
    /// LINT: returns warnings about a statement.
    Lint(Box<Statement>),
    /// SHOW CHANGES: lists the committed changes.
    ShowChanges {
        /// SINCE: only changes of transactions above this version.
        since: Option<u64>,
    },
    /// CREATE USER: creates a user.
    CreateUser {
        /// The user name.
        name: String,
        /// PASSWORD: the user's password, if given.
        password: Option<String>,
        /// SUPERUSER: if true, the user is a superuser.
        superuser: bool,
    },
    /// DROP USER: drops a user.
    DropUser {
        /// The user to drop.
        name: String,
        /// IF EXISTS: if true, don't error if the user doesn't exist.
        if_exists: bool,
    },
    /// GRANT: grants privileges on a table or view, or makes a user a member of another.
    Grant(Grant),
    /// REVOKE: revokes privileges on a table or view, or ends a membership.
    Revoke(Grant),
    /// SET ROLE: sets the session's current user.
    SetRole {
        /// The new current user.
        user: String,
    },
    /// RESET ROLE: resets the session's current user to its login user.
    ResetRole,
    /// SHOW USERS: lists the users.
    ShowUsers,
    /// SHOW GRANTS: lists the owners and privileges of tables and views.
    ShowGrants,
    /// SHOW AUDIT: lists the server's audit log.
    ShowAudit {
        /// SINCE: only entries with a sequence number above this one.
        since: Option<u64>,
    },
    /// SHOW SESSIONS: lists the server's sessions.
    ShowSessions,
    /// CREATE POLICY: creates a row-level security policy.
    CreatePolicy {
        /// The policy name.
        name: String,
        /// The table.
        table: String,
        /// TO: the user the policy applies to, if given.
        user: Option<String>,
        /// The USING condition.
        using: Expression,
        /// The WITH CHECK condition, if given.
        with_check: Option<Expression>,
    },
    /// DROP POLICY: drops a row-level security policy.
    DropPolicy {
        /// The policy to drop.
        name: String,
        /// The table.
        table: String,
        /// IF EXISTS: if true, don't error if the policy doesn't exist.
        if_exists: bool,
    },
    /// CREATE MATERIALIZED VIEW: creates a materialized view.
    CreateMaterializedView {
        /// The materialized view name.
        name: String,
        /// The column names, if given.
        columns: Option<Vec<String>>,
        /// The materialized view's query, a SELECT statement.
        query: Box<Statement>,
    },
    /// REFRESH MATERIALIZED VIEW: recomputes a materialized view's rows.
    RefreshMaterializedView {
        /// The materialized view to refresh.
        name: String,
    },
    /// DROP MATERIALIZED VIEW: drops a materialized view.
    DropMaterializedView {
        /// The materialized view to drop.
        name: String,
        /// IF EXISTS: if true, don't error if the materialized view doesn't
        /// exist.
        if_exists: bool,
    },
}

impl Statement {
    /// Desugars the statement, rewriting syntactic constructs that the planner
    /// doesn't need to distinguish into their canonical form (see
    /// Expression::desugar). The parser preserves them for statement digests.
    pub fn desugar(self) -> Self {
        self.transform_expressions(&Expression::desugar)
    }

    /// Transforms the statement by applying the given closure to each of its
    /// top-level expressions and those of the statements it contains.
    pub fn transform_expressions(self, f: &impl Fn(Expression) -> Expression) -> Self {
        let opt = |e: Option<Expression>| e.map(f);
        let stmt = |statement: Box<Statement>| Box::new(statement.transform_expressions(f));
        match self {
            Self::Explain { statement, analyze } => {
                Self::Explain { statement: stmt(statement), analyze }
            }
            Self::CreateTable { name, columns, checks } => Self::CreateTable {
                name,
                columns: columns
                    .into_iter()
                    .map(|column| Column {
                        default: opt(column.default),
                        checks: column.checks.into_iter().map(f).collect(),
                        ..column
                    })
                    .collect(),
                checks: checks
                    .into_iter()
                    .map(|check| Check { condition: f(check.condition), ..check })
                    .collect(),
            },
            Self::Delete { table, r#where } => Self::Delete { table, r#where: opt(r#where) },
            Self::Insert { table, columns, values } => Self::Insert {
                table,
                columns,
                values: values.into_iter().map(|row| row.into_iter().map(f).collect()).collect(),
            },
            Self::Update { table, set, r#where } => Self::Update {
                table,
                set: set.into_iter().map(|(column, value)| (column, opt(value))).collect(),
                r#where: opt(r#where),
            },
            Self::Select { select, from, r#where, group_by, having, order_by, offset, limit } => {
                Self::Select {
                    select: select.into_iter().map(|(e, alias)| (f(e), alias)).collect(),
                    from: from.into_iter().map(|from| from.transform_expressions(f)).collect(),
                    r#where: opt(r#where),
                    group_by: group_by.into_iter().map(f).collect(),
                    having: opt(having),
                    order_by: order_by.into_iter().map(|(e, dir)| (f(e), dir)).collect(),
                    offset: opt(offset),
                    limit: opt(limit),
                }
            }
            Self::Format(statement) => Self::Format(stmt(statement)),
            Self::CreateView { name, columns, query } => {
                Self::CreateView { name, columns, query: stmt(query) }
            }
            Self::CreateMaterializedView { name, columns, query } => {
                Self::CreateMaterializedView { name, columns, query: stmt(query) }
            }
            Self::Describe(Describe::Statement(statement)) => {
                Self::Describe(Describe::Statement(stmt(statement)))
            }
            Self::Estimate(statement) => Self::Estimate(stmt(statement)),
            Self::Prepare { name, types, statement } => {
                Self::Prepare { name, types, statement: stmt(statement) }
            }
            Self::Execute { name, arguments } => {
                Self::Execute { name, arguments: arguments.into_iter().map(f).collect() }
            }
            Self::Lint(statement) => Self::Lint(stmt(statement)),
            Self::CreatePolicy { name, table, user, using, with_check } => Self::CreatePolicy {
                name,
                table,
                user,
                using: f(using),
                with_check: opt(with_check),
            },
            statement @ (Self::Begin { .. }
            | Self::Commit
            | Self::Rollback
            | Self::DropTable { .. }
            | Self::ShowStatements
            | Self::Analyze { .. }
            | Self::ShowStatistics { .. }
            | Self::Export { .. }
            | Self::Import { .. }
            | Self::DropView { .. }
            | Self::ShowViews
            | Self::Describe(Describe::Name(_))
            | Self::Deallocate { .. }
            | Self::ShowPrepared
            | Self::ShowChanges { .. }
            | Self::CreateUser { .. }
            | Self::DropUser { .. }
            | Self::Grant(_)
            | Self::Revoke(_)
            | Self::SetRole { .. }
            | Self::ResetRole
            | Self::ShowUsers
            | Self::ShowGrants
            | Self::ShowAudit { .. }
            | Self::ShowSessions
            | Self::DropPolicy { .. }
            | Self::RefreshMaterializedView { .. }
            | Self::DropMaterializedView { .. }) => statement,
        }
    }

    /// Returns the top-level expressions of the statement and the statements
    /// it contains.
    pub fn expressions(&self) -> Vec<&Expression> {
        let mut exprs = Vec::new();
        self.collect_expressions(&mut exprs);
        exprs
    }

    /// Adds the expressions of the statement to the given vector.
    fn collect_expressions<'a>(&'a self, exprs: &mut Vec<&'a Expression>) {
        match self {
            Self::Explain { statement, .. }
            | Self::Format(statement)
            | Self::Estimate(statement)
            | Self::Lint(statement)
            | Self::Describe(Describe::Statement(statement))
            | Self::Prepare { statement, .. } => statement.collect_expressions(exprs),
            Self::CreateView { query, .. } | Self::CreateMaterializedView { query, .. } => {
                query.collect_expressions(exprs)
            }
            Self::CreateTable { columns, checks, .. } => {
                for item in create_table_items(columns, checks) {
                    match item {
                        Either::Left(column) => {
                            exprs.extend(&column.default);
                            exprs.extend(&column.checks);
                        }
                        Either::Right(check) => exprs.push(&check.condition),
                    }
                }
            }
            Self::Delete { r#where, .. } => exprs.extend(r#where),
            Self::Insert { values, .. } => exprs.extend(values.iter().flatten()),
            Self::Update { set, r#where, .. } => {
                exprs.extend(set.values().flatten());
                exprs.extend(r#where);
            }
            Self::Select { select, from, r#where, group_by, having, order_by, offset, limit } => {
                exprs.extend(select.iter().map(|(expr, _)| expr));
                from.iter().for_each(|from| from.collect_expressions(exprs));
                exprs.extend(r#where);
                exprs.extend(group_by);
                exprs.extend(having);
                exprs.extend(order_by.iter().map(|(expr, _)| expr));
                exprs.extend(limit);
                exprs.extend(offset);
            }
            Self::Execute { arguments, .. } => exprs.extend(arguments),
            Self::CreatePolicy { using, with_check, .. } => {
                exprs.push(using);
                exprs.extend(with_check);
            }
            Self::Begin { .. }
            | Self::Commit
            | Self::Rollback
            | Self::DropTable { .. }
            | Self::ShowStatements
            | Self::Analyze { .. }
            | Self::ShowStatistics { .. }
            | Self::Export { .. }
            | Self::Import { .. }
            | Self::DropView { .. }
            | Self::ShowViews
            | Self::Describe(Describe::Name(_))
            | Self::Deallocate { .. }
            | Self::ShowPrepared
            | Self::ShowChanges { .. }
            | Self::CreateUser { .. }
            | Self::DropUser { .. }
            | Self::Grant(_)
            | Self::Revoke(_)
            | Self::SetRole { .. }
            | Self::ResetRole
            | Self::ShowUsers
            | Self::ShowGrants
            | Self::ShowAudit { .. }
            | Self::ShowSessions
            | Self::DropPolicy { .. }
            | Self::RefreshMaterializedView { .. }
            | Self::DropMaterializedView { .. } => {}
        }
    }

    /// Returns the numbers of the parameters in the statement and the
    /// statements it contains, in increasing order.
    pub fn parameters(&self) -> BTreeSet<u64> {
        let mut parameters = Vec::new();
        for expr in self.expressions() {
            expr.collect(&|expr| matches!(expr, Expression::Parameter(_)), &mut parameters);
        }
        parameters
            .into_iter()
            .filter_map(|expr| match expr {
                Expression::Parameter(n) => Some(n),
                _ => None,
            })
            .collect()
    }
}

/// The subject of a DESCRIBE statement.
#[derive(Clone, Debug)]
pub enum Describe {
    /// A table or view, by name.
    Name(String),
    /// A statement.
    Statement(Box<Statement>),
}

/// The subject of a GRANT or REVOKE statement.
#[derive(Clone, Debug)]
pub enum Grant {
    /// Privileges on a table or view, granted to or revoked from a user.
    Privileges {
        /// The privileges, as written.
        privileges: Vec<Privilege>,
        /// The table or view.
        object: String,
        /// The user.
        user: String,
    },
    /// A membership of a user in another user.
    Membership {
        /// The user that the member acts for.
        group: String,
        /// The member.
        user: String,
    },
}

/// EXPORT formats.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub enum ExportFormat {
    Csv,
    Json,
    Sql,
}

/// IMPORT formats.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ImportFormat {
    Csv,
    Json,
}

/// A FROM item.
#[derive(Clone, Debug)]
pub enum From {
    /// A table.
    Table {
        /// The table name.
        name: String,
        /// An optional alias for the table.
        alias: Option<String>,
    },
    /// A join of two or more tables (may be nested).
    Join {
        /// The left table to join,
        left: Box<From>,
        /// The right table to join.
        right: Box<From>,
        /// The join type.
        r#type: JoinType,
        /// The join condition. None for a cross join.
        predicate: Option<Expression>,
    },
}

impl From {
    /// Adds the join predicates of the FROM item to the given vector.
    fn collect_expressions<'a>(&'a self, exprs: &mut Vec<&'a Expression>) {
        match self {
            Self::Table { .. } => {}
            Self::Join { left, right, predicate, .. } => {
                left.collect_expressions(exprs);
                right.collect_expressions(exprs);
                exprs.extend(predicate);
            }
        }
    }

    /// Transforms the join predicates of the FROM item with the given closure.
    fn transform_expressions(self, f: &impl Fn(Expression) -> Expression) -> Self {
        match self {
            table @ Self::Table { .. } => table,
            Self::Join { left, right, r#type, predicate } => Self::Join {
                left: Box::new(left.transform_expressions(f)),
                right: Box::new(right.transform_expressions(f)),
                r#type,
                predicate: predicate.map(f),
            },
        }
    }
}

/// A CREATE TABLE column definition.
#[derive(Clone, Debug)]
pub struct Column {
    pub name: String,
    pub datatype: DataType,
    pub primary_key: bool,
    pub nullable: Option<bool>,
    pub default: Option<Expression>,
    pub unique: bool,
    pub index: bool,
    pub references: Option<String>,
    /// The column's CHECK constraints, in the order written.
    pub checks: Vec<Expression>,
}

/// A CHECK constraint: its name, if given, and its condition.
#[derive(Clone, Debug)]
pub struct Check {
    /// The name given with CONSTRAINT, if any.
    pub name: Option<String>,
    /// The condition.
    pub condition: Expression,
    /// The number of column definitions written before the constraint.
    pub position: usize,
}

/// Returns the column definitions and table constraints of a CREATE TABLE
/// statement, in the order written.
pub fn create_table_items<'a>(
    columns: &'a [Column],
    checks: &'a [Check],
) -> Vec<Either<&'a Column, &'a Check>> {
    let mut items = Vec::with_capacity(columns.len() + checks.len());
    let mut checks = checks.iter().peekable();
    for (i, column) in columns.iter().enumerate() {
        while let Some(check) = checks.next_if(|check| check.position <= i) {
            items.push(Either::Right(check));
        }
        items.push(Either::Left(column));
    }
    items.extend(checks.map(Either::Right));
    items
}

/// JOIN types.
#[derive(Clone, Debug, PartialEq)]
pub enum JoinType {
    Cross,
    Inner,
    Left,
    Right,
}

impl JoinType {
    // If true, the join is an outer join, where rows with no join matches are
    // emitted with a NULL match.
    pub fn is_outer(&self) -> bool {
        match self {
            Self::Left | Self::Right => true,
            Self::Cross | Self::Inner => false,
        }
    }
}

/// ORDER BY direction.
#[derive(Clone, Debug, Default)]
pub enum Direction {
    #[default]
    Ascending,
    Descending,
}

/// SQL expressions, e.g. `a + 7 > b`. Can be nested.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum Expression {
    /// All columns, i.e. *.
    All,
    /// A column reference, optionally qualified with a table name.
    Column(Option<String>, String),
    /// A literal value.
    Literal(Literal),
    /// A function call (name and parameters).
    Function(String, Vec<Expression>),
    /// An operator.
    Operator(Operator),
    /// A parameter $n of a prepared statement.
    Parameter(u64),
}

/// Expression literal values.
#[derive(Clone, Debug)]
pub enum Literal {
    Null,
    Boolean(bool),
    Integer(i64),
    Float(f64),
    String(String),
}

/// To allow using expressions and literals in e.g. hashmaps, implement simple
/// equality by value for all types, including Null and f64::NAN. This only
/// checks that the values are the same, and ignores SQL semantics for e.g. NULL
/// and NaN (which is handled by SQL expression evaluation).
impl PartialEq for Literal {
    fn eq(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Null, Self::Null) => true,
            (Self::Boolean(l), Self::Boolean(r)) => l == r,
            (Self::Integer(l), Self::Integer(r)) => l == r,
            (Self::Float(l), Self::Float(r)) => l.to_bits() == r.to_bits(),
            (Self::String(l), Self::String(r)) => l == r,
            (_, _) => false,
        }
    }
}

impl Eq for Literal {}

impl Hash for Literal {
    fn hash<H: Hasher>(&self, state: &mut H) {
        core::mem::discriminant(self).hash(state);
        match self {
            Self::Null => {}
            Self::Boolean(v) => v.hash(state),
            Self::Integer(v) => v.hash(state),
            Self::Float(v) => v.to_bits().hash(state),
            Self::String(v) => v.hash(state),
        }
    }
}

/// Expression operators.
///
/// Since this is a recursive data structure, we have to box each child
/// expression, which incurs a heap allocation. There are clever ways to get
/// around this, but we keep it simple.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum Operator {
    And(Box<Expression>, Box<Expression>), // a AND b
    Not(Box<Expression>),                  // NOT a
    Or(Box<Expression>, Box<Expression>),  // a OR b

    Equal(Box<Expression>, Box<Expression>),       // a = b
    GreaterThan(Box<Expression>, Box<Expression>), // a > b
    GreaterThanOrEqual(Box<Expression>, Box<Expression>), // a >= b
    Is(Box<Expression>, Literal),                  // IS NULL or IS NAN
    IsNot(Box<Expression>, Literal),               // IS NOT NULL or IS NOT NAN
    LessThan(Box<Expression>, Box<Expression>),    // a < b
    LessThanOrEqual(Box<Expression>, Box<Expression>), // a <= b
    NotEqual(Box<Expression>, Box<Expression>),    // a != b

    Add(Box<Expression>, Box<Expression>),          // a + b
    Divide(Box<Expression>, Box<Expression>),       // a / b
    Exponentiate(Box<Expression>, Box<Expression>), // a ^ b
    Factorial(Box<Expression>),                     // a!
    Identity(Box<Expression>),                      // +a
    Multiply(Box<Expression>, Box<Expression>),     // a * b
    Negate(Box<Expression>),                        // -a
    Remainder(Box<Expression>, Box<Expression>),    // a % b
    Subtract(Box<Expression>, Box<Expression>),     // a - b

    Like(Box<Expression>, Box<Expression>), // a LIKE b
}

impl Expression {
    /// Walks the expression tree depth-first, calling a closure for every node.
    /// Halts and returns false if the closure returns false.
    pub fn walk(&self, visitor: &mut impl FnMut(&Expression) -> bool) -> bool {
        use Operator::*;

        if !visitor(self) {
            return false;
        }

        match self {
            Self::Operator(op) => match op {
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
                | Subtract(lhs, rhs) => lhs.walk(visitor) && rhs.walk(visitor),

                Factorial(expr)
                | Identity(expr)
                | Is(expr, _)
                | IsNot(expr, _)
                | Negate(expr)
                | Not(expr) => expr.walk(visitor),
            },

            Self::Function(_, exprs) => exprs.iter().any(|expr| expr.walk(visitor)),

            Self::All | Self::Column(_, _) | Self::Literal(_) | Self::Parameter(_) => true,
        }
    }

    /// Walks the expression tree depth-first while calling a closure until it
    /// returns true. This is the inverse of walk().
    pub fn contains(&self, visitor: &impl Fn(&Expression) -> bool) -> bool {
        !self.walk(&mut |expr| !visitor(expr))
    }

    /// Find and collects expressions for which the given closure returns true,
    /// adding them to c. Does not recurse into matching expressions.
    pub fn collect(&self, visitor: &impl Fn(&Expression) -> bool, exprs: &mut Vec<Expression>) {
        use Operator::*;

        if visitor(self) {
            exprs.push(self.clone());
            return;
        }

        match self {
            Self::Operator(op) => match op {
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
                | Subtract(lhs, rhs) => {
                    lhs.collect(visitor, exprs);
                    rhs.collect(visitor, exprs);
                }
                Factorial(expr)
                | Identity(expr)
                | Is(expr, _)
                | IsNot(expr, _)
                | Negate(expr)
                | Not(expr) => expr.collect(visitor, exprs),
            },

            Self::Function(_, args) => args.iter().for_each(|arg| arg.collect(visitor, exprs)),

            Self::All | Self::Column(_, _) | Self::Literal(_) | Self::Parameter(_) => {}
        }
    }

    /// Transforms the expression by recursively applying the given closure
    /// depth-first to each node after descending into its children.
    pub fn transform(self, f: &impl Fn(Expression) -> Expression) -> Expression {
        use Operator::*;

        let xform = |expr: Box<Expression>| Box::new(expr.transform(f));
        let expr = match self {
            Self::Operator(op) => Self::Operator(match op {
                And(lhs, rhs) => And(xform(lhs), xform(rhs)),
                Not(expr) => Not(xform(expr)),
                Or(lhs, rhs) => Or(xform(lhs), xform(rhs)),

                Equal(lhs, rhs) => Equal(xform(lhs), xform(rhs)),
                GreaterThan(lhs, rhs) => GreaterThan(xform(lhs), xform(rhs)),
                GreaterThanOrEqual(lhs, rhs) => GreaterThanOrEqual(xform(lhs), xform(rhs)),
                Is(expr, literal) => Is(xform(expr), literal),
                IsNot(expr, literal) => IsNot(xform(expr), literal),
                LessThan(lhs, rhs) => LessThan(xform(lhs), xform(rhs)),
                LessThanOrEqual(lhs, rhs) => LessThanOrEqual(xform(lhs), xform(rhs)),
                NotEqual(lhs, rhs) => NotEqual(xform(lhs), xform(rhs)),

                Add(lhs, rhs) => Add(xform(lhs), xform(rhs)),
                Divide(lhs, rhs) => Divide(xform(lhs), xform(rhs)),
                Exponentiate(lhs, rhs) => Exponentiate(xform(lhs), xform(rhs)),
                Factorial(expr) => Factorial(xform(expr)),
                Identity(expr) => Identity(xform(expr)),
                Multiply(lhs, rhs) => Multiply(xform(lhs), xform(rhs)),
                Negate(expr) => Negate(xform(expr)),
                Remainder(lhs, rhs) => Remainder(xform(lhs), xform(rhs)),
                Subtract(lhs, rhs) => Subtract(xform(lhs), xform(rhs)),

                Like(lhs, rhs) => Like(xform(lhs), xform(rhs)),
            }),
            Self::Function(name, args) => {
                Self::Function(name, args.into_iter().map(|arg| arg.transform(f)).collect())
            }
            expr @ (Self::All | Self::Column(_, _) | Self::Literal(_) | Self::Parameter(_)) => {
                expr
            }
        };
        f(expr)
    }

    /// Desugars the expression: a IS NOT NULL/NAN becomes NOT (a IS NULL/NAN).
    /// The parser keeps IS NOT for statement digests, but the planner only
    /// knows IS, so that e.g. GROUP BY and aggregate expressions match
    /// regardless of how they were written.
    pub fn desugar(self) -> Expression {
        self.transform(&|expr| match expr {
            Self::Operator(Operator::IsNot(expr, literal)) => {
                Operator::Not(Operator::Is(expr, literal).into()).into()
            }
            expr => expr,
        })
    }
}

impl core::convert::From<Literal> for Expression {
    fn from(literal: Literal) -> Self {
        Self::Literal(literal)
    }
}

impl core::convert::From<Operator> for Expression {
    fn from(op: Operator) -> Self {
        Self::Operator(op)
    }
}

impl core::convert::From<Operator> for Box<Expression> {
    fn from(value: Operator) -> Self {
        Box::new(value.into())
    }
}
