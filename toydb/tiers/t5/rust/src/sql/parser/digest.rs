//! Statement digests: the canonical SQL text of a parsed statement, with every
//! literal replaced by ?. Statements that only differ in literal values, letter
//! case, whitespace, redundant parentheses, optional words, or synonymous
//! spellings (e.g. <> and !=, INT and INTEGER) have the same digest. They are
//! used to group statement statistics (see `SHOW STATEMENTS`).
//!
//! The canonical text is written from the AST:
//!
//! * Keywords are upper case, names are written as parsed (quoted where
//!   necessary), and tokens are separated by single spaces, except around
//!   parentheses, commas, periods, function calls, and prefix/postfix
//!   arithmetic operators.
//!
//! * Optional words are left out (e.g. INNER, OUTER, ASC, TRANSACTION) and
//!   aliases always use AS.
//!
//! * Operands are parenthesized exactly when the parser needs it, according to
//!   operator precedence and associativity. Constructs are never rewritten
//!   into equivalent ones: a >= b is not written as a > b OR a = b, a != b not
//!   as NOT a = b, and a IS NOT NULL not as NOT a IS NULL.

use itertools::{Either, Itertools as _};

use super::ast::{
    Check, Column, Describe, Direction, ExportFormat, Expression, From, Grant, ImportFormat,
    JoinType, Literal, Operator, Statement, create_table_items,
};
use super::format_ident;

/// Returns the digest of a statement.
pub fn digest(statement: &Statement) -> String {
    match statement {
        Statement::Begin { read_only, as_of } => {
            let mut digest = "BEGIN".to_string();
            if *read_only {
                digest += " READ ONLY";
            }
            if as_of.is_some() {
                digest += " AS OF SYSTEM TIME ?";
            }
            digest
        }
        Statement::Commit => "COMMIT".to_string(),
        Statement::Rollback => "ROLLBACK".to_string(),
        Statement::Explain { statement, analyze: false } => {
            format!("EXPLAIN {}", digest(statement))
        }
        Statement::Explain { statement, analyze: true } => {
            format!("EXPLAIN (ANALYZE) {}", digest(statement))
        }

        Statement::CreateTable { name, columns, checks } => {
            let items = create_table_items(columns, checks)
                .into_iter()
                .map(|item| match item {
                    Either::Left(c) => column(c),
                    Either::Right(c) => check(c),
                })
                .join(", ");
            format!("CREATE TABLE {} ({items})", format_ident(name))
        }
        Statement::DropTable { name, if_exists } => match if_exists {
            true => format!("DROP TABLE IF EXISTS {}", format_ident(name)),
            false => format!("DROP TABLE {}", format_ident(name)),
        },

        Statement::Delete { table, r#where } => {
            format!("DELETE FROM {}{}", format_ident(table), where_clause(r#where))
        }
        Statement::Insert { table, columns, values } => {
            let mut digest = format!("INSERT INTO {}", format_ident(table));
            if let Some(columns) = columns {
                digest += &format!(" ({})", columns.iter().map(|c| format_ident(c)).join(", "));
            }
            let rows = values.iter().map(|row| format!("({})", expressions(row))).join(", ");
            format!("{digest} VALUES {rows}")
        }
        Statement::Update { table, set, r#where } => {
            // The assignments are ordered by column name.
            let set = set
                .iter()
                .map(|(column, value)| match value {
                    Some(value) => format!("{} = {}", format_ident(column), expression(value)),
                    None => format!("{} = DEFAULT", format_ident(column)),
                })
                .join(", ");
            format!("UPDATE {} SET {set}{}", format_ident(table), where_clause(r#where))
        }
        Statement::Select { select, from, r#where, group_by, having, order_by, offset, limit } => {
            let mut digest = format!("SELECT {}", select.iter().map(select_item).join(", "));
            if !from.is_empty() {
                digest += &format!(" FROM {}", from.iter().map(from_item).join(", "));
            }
            digest += &where_clause(r#where);
            if !group_by.is_empty() {
                digest += &format!(" GROUP BY {}", expressions(group_by));
            }
            if let Some(having) = having {
                digest += &format!(" HAVING {}", expression(having));
            }
            if !order_by.is_empty() {
                let order_by = order_by
                    .iter()
                    .map(|(expr, direction)| match direction {
                        Direction::Ascending => expression(expr),
                        Direction::Descending => format!("{} DESC", expression(expr)),
                    })
                    .join(", ");
                digest += &format!(" ORDER BY {order_by}");
            }
            if let Some(limit) = limit {
                digest += &format!(" LIMIT {}", expression(limit));
            }
            if let Some(offset) = offset {
                digest += &format!(" OFFSET {}", expression(offset));
            }
            digest
        }

        Statement::ShowStatements => "SHOW STATEMENTS".to_string(),
        Statement::Analyze { table: Some(table) } => format!("ANALYZE {}", format_ident(table)),
        Statement::Analyze { table: None } => "ANALYZE".to_string(),
        Statement::ShowStatistics { table } => format!("SHOW STATISTICS {}", format_ident(table)),
        Statement::Export { table, format } => {
            let format = match format {
                ExportFormat::Csv => "CSV",
                ExportFormat::Json => "JSON",
                ExportFormat::Sql => "SQL",
            };
            match table {
                Some(table) => format!("EXPORT {} AS {format}", format_ident(table)),
                None => format!("EXPORT AS {format}"),
            }
        }
        Statement::Import { table, format, lines } => {
            let format = match format {
                ImportFormat::Csv => "CSV",
                ImportFormat::Json => "JSON",
            };
            let lines = lines.iter().map(|_| "?").join(", ");
            format!("IMPORT INTO {} FROM {format} {lines}", format_ident(table))
        }

        Statement::Format(statement) => format!("FORMAT {}", digest(statement)),
        Statement::CreateView { name, columns, query } => {
            let columns = match columns {
                Some(columns) => {
                    format!(" ({})", columns.iter().map(|c| format_ident(c)).join(", "))
                }
                None => String::new(),
            };
            format!("CREATE VIEW {}{columns} AS {}", format_ident(name), digest(query))
        }
        Statement::DropView { name, if_exists } => match if_exists {
            true => format!("DROP VIEW IF EXISTS {}", format_ident(name)),
            false => format!("DROP VIEW {}", format_ident(name)),
        },
        Statement::ShowViews => "SHOW VIEWS".to_string(),
        Statement::Describe(Describe::Name(name)) => format!("DESCRIBE {}", format_ident(name)),
        Statement::Describe(Describe::Statement(statement)) => {
            format!("DESCRIBE {}", digest(statement))
        }
        Statement::Estimate(statement) => format!("ESTIMATE {}", digest(statement)),
        Statement::Prepare { name, types, statement } => {
            let types = match types.is_empty() {
                true => String::new(),
                false => format!(" ({})", types.iter().join(", ")),
            };
            format!("PREPARE {}{types} AS {}", format_ident(name), digest(statement))
        }
        Statement::Execute { name, arguments } => match arguments.is_empty() {
            true => format!("EXECUTE {}", format_ident(name)),
            false => format!("EXECUTE {} ({})", format_ident(name), expressions(arguments)),
        },
        Statement::Deallocate { name } => format!("DEALLOCATE {}", format_ident(name)),
        Statement::ShowPrepared => "SHOW PREPARED".to_string(),
        Statement::Lint(statement) => format!("LINT {}", digest(statement)),
        Statement::ShowChanges { since: Some(_) } => "SHOW CHANGES SINCE ?".to_string(),
        Statement::ShowChanges { since: None } => "SHOW CHANGES".to_string(),
        Statement::CreateUser { name, password, superuser } => {
            let mut digest = format!("CREATE USER {}", format_ident(name));
            if password.is_some() {
                digest += " PASSWORD ?";
            }
            if *superuser {
                digest += " SUPERUSER";
            }
            digest
        }
        Statement::DropUser { name, if_exists } => match if_exists {
            true => format!("DROP USER IF EXISTS {}", format_ident(name)),
            false => format!("DROP USER {}", format_ident(name)),
        },
        Statement::Grant(grant) => format!("GRANT {}", grant_subject(grant, "TO")),
        Statement::Revoke(grant) => format!("REVOKE {}", grant_subject(grant, "FROM")),
        Statement::SetRole { user } => format!("SET ROLE {}", format_ident(user)),
        Statement::ResetRole => "RESET ROLE".to_string(),
        Statement::ShowUsers => "SHOW USERS".to_string(),
        Statement::ShowGrants => "SHOW GRANTS".to_string(),
        Statement::ShowAudit { since: Some(_) } => "SHOW AUDIT SINCE ?".to_string(),
        Statement::ShowAudit { since: None } => "SHOW AUDIT".to_string(),
        Statement::ShowSessions => "SHOW SESSIONS".to_string(),
        Statement::CreatePolicy { name, table, user, using, with_check } => {
            let mut digest =
                format!("CREATE POLICY {} ON {}", format_ident(name), format_ident(table));
            if let Some(user) = user {
                digest += &format!(" TO {}", format_ident(user));
            }
            digest += &format!(" USING ({})", expression(using));
            if let Some(with_check) = with_check {
                digest += &format!(" WITH CHECK ({})", expression(with_check));
            }
            digest
        }
        Statement::DropPolicy { name, table, if_exists } => match if_exists {
            true => {
                format!("DROP POLICY IF EXISTS {} ON {}", format_ident(name), format_ident(table))
            }
            false => format!("DROP POLICY {} ON {}", format_ident(name), format_ident(table)),
        },
        Statement::CreateMaterializedView { name, columns, query } => {
            let columns = match columns {
                Some(columns) => {
                    format!(" ({})", columns.iter().map(|c| format_ident(c)).join(", "))
                }
                None => String::new(),
            };
            format!("CREATE MATERIALIZED VIEW {}{columns} AS {}", format_ident(name), digest(query))
        }
        Statement::RefreshMaterializedView { name } => {
            format!("REFRESH MATERIALIZED VIEW {}", format_ident(name))
        }
        Statement::DropMaterializedView { name, if_exists } => match if_exists {
            true => format!("DROP MATERIALIZED VIEW IF EXISTS {}", format_ident(name)),
            false => format!("DROP MATERIALIZED VIEW {}", format_ident(name)),
        },
    }
}

/// Returns the digest of what a GRANT or REVOKE grants or revokes, with the
/// given keyword (TO or FROM) before the user.
fn grant_subject(grant: &Grant, keyword: &str) -> String {
    match grant {
        Grant::Privileges { privileges, object, user } => format!(
            "{} ON {} {keyword} {}",
            privileges.iter().join(", "),
            format_ident(object),
            format_ident(user)
        ),
        Grant::Membership { group, user } => {
            format!("{} {keyword} {}", format_ident(group), format_ident(user))
        }
    }
}

/// Formats a CREATE TABLE column definition, with constraints in a fixed
/// order regardless of how they were written.
fn column(column: &Column) -> String {
    let mut digest = format!("{} {}", format_ident(&column.name), column.datatype);
    if column.primary_key {
        digest += " PRIMARY KEY";
    }
    match column.nullable {
        Some(true) => digest += " NULL",
        Some(false) => digest += " NOT NULL",
        None => {}
    }
    if let Some(default) = &column.default {
        digest += &format!(" DEFAULT {}", expression(default));
    }
    if column.unique {
        digest += " UNIQUE";
    }
    if column.index {
        digest += " INDEX";
    }
    if let Some(references) = &column.references {
        digest += &format!(" REFERENCES {}", format_ident(references));
    }
    for condition in &column.checks {
        digest += &format!(" CHECK ({})", expression(condition));
    }
    digest
}

/// Formats a CREATE TABLE table constraint.
fn check(check: &Check) -> String {
    let condition = expression(&check.condition);
    match &check.name {
        Some(name) => format!("CONSTRAINT {} CHECK ({condition})", format_ident(name)),
        None => format!("CHECK ({condition})"),
    }
}

/// Formats an optional WHERE clause, with a leading space.
fn where_clause(r#where: &Option<Expression>) -> String {
    match r#where {
        Some(expr) => format!(" WHERE {}", expression(expr)),
        None => String::new(),
    }
}

/// Formats a SELECT item.
fn select_item((expr, alias): &(Expression, Option<String>)) -> String {
    match alias {
        Some(alias) => format!("{} AS {}", expression(expr), format_ident(alias)),
        None => expression(expr),
    }
}

/// Formats a FROM item. Joins are written left to right, as parsed.
fn from_item(from: &From) -> String {
    match from {
        From::Table { name, alias: Some(alias) } => {
            format!("{} AS {}", format_ident(name), format_ident(alias))
        }
        From::Table { name, alias: None } => format_ident(name),
        From::Join { left, right, r#type, predicate } => {
            let join = match r#type {
                JoinType::Cross => "CROSS JOIN",
                JoinType::Inner => "JOIN",
                JoinType::Left => "LEFT JOIN",
                JoinType::Right => "RIGHT JOIN",
            };
            let mut digest = format!("{} {join} {}", from_item(left), from_item(right));
            if let Some(predicate) = predicate {
                digest += &format!(" ON {}", expression(predicate));
            }
            digest
        }
    }
}

/// Formats a comma-separated list of expressions.
fn expressions(exprs: &[Expression]) -> String {
    exprs.iter().map(expression).join(", ")
}

/// Formats an expression, with literals as ? and parentheses only where the
/// parser needs them.
fn expression(expr: &Expression) -> String {
    use Operator::*;

    let op = match expr {
        Expression::All => return "*".to_string(),
        Expression::Column(Some(table), column) => {
            return format!("{}.{}", format_ident(table), format_ident(column));
        }
        Expression::Column(None, column) => return format_ident(column),
        Expression::Literal(_) => return "?".to_string(),
        Expression::Parameter(n) => return format!("${n}"),
        Expression::Function(name, args) => {
            return format!("{}({})", format_ident(name), expressions(args));
        }
        Expression::Operator(op) => op,
    };

    let precedence = precedence(expr);
    match op {
        And(lhs, rhs) => infix(lhs, "AND", rhs, precedence),
        Not(expr) => format!("NOT {}", operand(expr, precedence)),
        Or(lhs, rhs) => infix(lhs, "OR", rhs, precedence),

        Equal(lhs, rhs) => infix(lhs, "=", rhs, precedence),
        GreaterThan(lhs, rhs) => infix(lhs, ">", rhs, precedence),
        GreaterThanOrEqual(lhs, rhs) => infix(lhs, ">=", rhs, precedence),
        Is(expr, literal) => format!("{} IS {}", operand(expr, precedence), is_value(literal)),
        IsNot(expr, literal) => {
            format!("{} IS NOT {}", operand(expr, precedence), is_value(literal))
        }
        LessThan(lhs, rhs) => infix(lhs, "<", rhs, precedence),
        LessThanOrEqual(lhs, rhs) => infix(lhs, "<=", rhs, precedence),
        NotEqual(lhs, rhs) => infix(lhs, "!=", rhs, precedence),

        Add(lhs, rhs) => infix(lhs, "+", rhs, precedence),
        Divide(lhs, rhs) => infix(lhs, "/", rhs, precedence),
        // ^ is right-associative.
        Exponentiate(lhs, rhs) => format!(
            "{} ^ {}",
            operand(lhs, precedence + 1),
            operand(rhs, precedence)
        ),
        Factorial(expr) => format!("{}!", operand(expr, precedence)),
        Identity(expr) => format!("+{}", operand(expr, precedence)),
        Multiply(lhs, rhs) => infix(lhs, "*", rhs, precedence),
        Negate(expr) => format!("-{}", operand(expr, precedence)),
        Remainder(lhs, rhs) => infix(lhs, "%", rhs, precedence),
        Subtract(lhs, rhs) => infix(lhs, "-", rhs, precedence),

        Like(lhs, rhs) => infix(lhs, "LIKE", rhs, precedence),
    }
}

/// Formats a left-associative infix operator.
fn infix(lhs: &Expression, op: &str, rhs: &Expression, precedence: u8) -> String {
    format!("{} {op} {}", operand(lhs, precedence), operand(rhs, precedence + 1))
}

/// Formats an operand, parenthesized if its precedence is below the given
/// minimum precedence.
fn operand(expr: &Expression, min_precedence: u8) -> String {
    match precedence(expr) < min_precedence {
        true => format!("({})", expression(expr)),
        false => expression(expr),
    }
}

/// Formats the NULL or NAN of IS [NOT] NULL/NAN, which is part of the
/// operator, not a literal.
pub(super) fn is_value(literal: &Literal) -> &'static str {
    match literal {
        Literal::Null => "NULL",
        Literal::Float(f) if f.is_nan() => "NAN",
        literal => panic!("invalid IS value {literal:?}"), // enforced by parser
    }
}

/// Returns the precedence of an expression, matching the parser: from 1
/// (loosest) to 11 (atoms).
pub(super) fn precedence(expr: &Expression) -> u8 {
    use Operator::*;
    match expr {
        Expression::All
        | Expression::Column(_, _)
        | Expression::Literal(_)
        | Expression::Parameter(_)
        | Expression::Function(_, _) => 11,
        Expression::Operator(op) => match op {
            Or(_, _) => 1,
            And(_, _) => 2,
            Not(_) => 3,
            Equal(_, _) | NotEqual(_, _) | Like(_, _) | Is(_, _) | IsNot(_, _) => 4,
            GreaterThan(_, _)
            | GreaterThanOrEqual(_, _)
            | LessThan(_, _)
            | LessThanOrEqual(_, _) => 5,
            Add(_, _) | Subtract(_, _) => 6,
            Multiply(_, _) | Divide(_, _) | Remainder(_, _) => 7,
            Exponentiate(_, _) => 8,
            Factorial(_) => 9,
            Identity(_) | Negate(_) => 10,
        },
    }
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::parser::Parser;

    #[test_case("select ID, v as \"the name\" from T x where x.id >= 2 and not (v <> 'a' or v is not null) order by 1 asc, v desc limit 5 offset 1"
        => "SELECT id, v AS \"the name\" FROM t AS x WHERE x.id >= ? AND NOT (v != ? OR v IS NOT NULL) ORDER BY ?, v DESC LIMIT ? OFFSET ?"; "select clauses")]
    #[test_case("SELECT t.v, u.w FROM t INNER JOIN u ON u.t_id = t.id LEFT OUTER JOIN t t2 ON t2.id = u.id"
        => "SELECT t.v, u.w FROM t JOIN u ON u.t_id = t.id LEFT JOIN t AS t2 ON t2.id = u.id"; "joins")]
    #[test_case("SELECT * FROM a RIGHT OUTER JOIN b ON a.x = b.y CROSS JOIN c, d"
        => "SELECT * FROM a RIGHT JOIN b ON a.x = b.y CROSS JOIN c, d"; "right and cross joins")]
    #[test_case("SELECT COUNT(*), Sum(n) FROM t GROUP BY n % 2 HAVING count(*) > 1"
        => "SELECT count(*), sum(n) FROM t GROUP BY n % ? HAVING count(*) > ?"; "aggregates")]
    #[test_case("insert into T values (1, 'x', 15), (2, NULL, -1)" => "INSERT INTO t VALUES (?, ?, ?), (?, ?, -?)"; "insert")]
    #[test_case("INSERT INTO t (a, \"B\", \"c d\") VALUES (TRUE)" => "INSERT INTO t (a, B, \"c d\") VALUES (?)"; "insert columns")]
    #[test_case("update t set v = default, n = n + 1 where id = 3" => "UPDATE t SET n = n + ?, v = DEFAULT WHERE id = ?"; "update")]
    #[test_case("DELETE FROM t" => "DELETE FROM t"; "delete")]
    #[test_case("CREATE TABLE u (k INT NOT NULL DEFAULT 1 PRIMARY KEY, r INT INDEX REFERENCES u, b BOOL NULL UNIQUE, s VARCHAR, f DOUBLE)"
        => "CREATE TABLE u (k INTEGER PRIMARY KEY NOT NULL DEFAULT ?, r INTEGER INDEX REFERENCES u, b BOOLEAN NULL UNIQUE, s STRING, f FLOAT)"; "create table")]
    #[test_case("create table t (constraint \"Big\" check (n < 100), id int check (id > 0) primary key, n int, check (n != 5))"
        => "CREATE TABLE t (CONSTRAINT Big CHECK (n < ?), id INTEGER PRIMARY KEY CHECK (id > ?), n INTEGER, CHECK (n != ?))"; "create table checks")]
    #[test_case("drop table if exists \"show\"" => "DROP TABLE IF EXISTS \"show\""; "drop table")]
    #[test_case("BEGIN TRANSACTION READ ONLY AS OF SYSTEM TIME 3" => "BEGIN READ ONLY AS OF SYSTEM TIME ?"; "begin")]
    #[test_case("begin read write" => "BEGIN"; "begin read write")]
    #[test_case("explain select 1;" => "EXPLAIN SELECT ?"; "explain")]
    #[test_case("explain ( analyze ) select 1;" => "EXPLAIN (ANALYZE) SELECT ?"; "explain analyze")]
    #[test_case("show statements" => "SHOW STATEMENTS"; "show statements")]
    #[test_case("analyze" => "ANALYZE"; "analyze all")]
    #[test_case("show statistics \"T\"" => "SHOW STATISTICS T"; "show statistics")]
    #[test_case("export as sql" => "EXPORT AS SQL"; "export database")]
    #[test_case("export t as json" => "EXPORT t AS JSON"; "export")]
    #[test_case("import into t from csv 'a', 'b', 'c'" => "IMPORT INTO t FROM CSV ?, ?, ?"; "import")]
    #[test_case("format select 'a'" => "FORMAT SELECT ?"; "format")]
    #[test_case("create view \"V\" (a, \"b c\") as select 1" => "CREATE VIEW V (a, \"b c\") AS SELECT ?"; "create view")]
    #[test_case("drop view if exists v" => "DROP VIEW IF EXISTS v"; "drop view")]
    #[test_case("show views" => "SHOW VIEWS"; "show views")]
    #[test_case("describe T" => "DESCRIBE t"; "describe name")]
    #[test_case("describe select 1" => "DESCRIBE SELECT ?"; "describe statement")]
    #[test_case("estimate delete from t where id = 1" => "ESTIMATE DELETE FROM t WHERE id = ?"; "estimate")]
    #[test_case("prepare p (int, text) as select $2 + $01" => "PREPARE p (INTEGER, STRING) AS SELECT $2 + $1"; "prepare")]
    #[test_case("prepare p as select 1" => "PREPARE p AS SELECT ?"; "prepare without types")]
    #[test_case("execute p (1, -2, 'x')" => "EXECUTE p (?, -?, ?)"; "execute")]
    #[test_case("execute p" => "EXECUTE p"; "execute without arguments")]
    #[test_case("deallocate p" => "DEALLOCATE p"; "deallocate")]
    #[test_case("show prepared" => "SHOW PREPARED"; "show prepared")]
    #[test_case("lint update t set v = 1" => "LINT UPDATE t SET v = ?"; "lint")]
    #[test_case("show changes since 3" => "SHOW CHANGES SINCE ?"; "show changes since")]
    #[test_case("show changes" => "SHOW CHANGES"; "show changes")]
    #[test_case("show audit since 3" => "SHOW AUDIT SINCE ?"; "show audit since")]
    #[test_case("show sessions" => "SHOW SESSIONS"; "show sessions")]
    #[test_case("create policy p on t to Alice using (id > 1 or v = 'x') with check (true)"
        => "CREATE POLICY p ON t TO alice USING (id > ? OR v = ?) WITH CHECK (?)"; "create policy")]
    #[test_case("create policy p on t using ((id > 1))" => "CREATE POLICY p ON t USING (id > ?)"; "create policy for all users")]
    #[test_case("drop policy if exists p on \"T\"" => "DROP POLICY IF EXISTS p ON T"; "drop policy")]
    fn statement(input: &str) -> String {
        digest(&Parser::parse(input).expect("parse failed"))
    }

    #[test_case("a + b * c" => "a + b * c"; "mul in add")]
    #[test_case("(a + b) * c" => "(a + b) * c"; "add in mul")]
    #[test_case("a - (b - c)" => "a - (b - c)"; "right assoc sub")]
    #[test_case("(a - b) - c" => "a - b - c"; "left assoc sub")]
    #[test_case("a ^ (b ^ c)" => "a ^ b ^ c"; "right assoc exp")]
    #[test_case("(a ^ b) ^ c" => "(a ^ b) ^ c"; "left assoc exp")]
    #[test_case("(-a) ^ 2" => "-a ^ ?"; "negate in exp")]
    #[test_case("-(a ^ 2)" => "-(a ^ ?)"; "exp in negate")]
    #[test_case("- -a" => "--a"; "double negate")]
    #[test_case("+-a" => "+-a"; "identity negate")]
    #[test_case("(a!)!" => "a!!"; "double factorial")]
    #[test_case("-(a!)" => "-(a!)"; "factorial in negate")]
    #[test_case("(a + 1)!" => "(a + ?)!"; "add in factorial")]
    #[test_case("(NOT p) = q" => "(NOT p) = q"; "not in equal")]
    #[test_case("NOT (p = q)" => "NOT p = q"; "equal in not")]
    #[test_case("NOT (p AND q)" => "NOT (p AND q)"; "and in not")]
    #[test_case("(p = q) = p" => "p = q = p"; "left assoc equal")]
    #[test_case("p = (q = p)" => "p = (q = p)"; "right assoc equal")]
    #[test_case("(p = q) IS NULL" => "p = q IS NULL"; "equal in is")]
    #[test_case("p = (q IS NULL)" => "p = (q IS NULL)"; "is in equal")]
    #[test_case("(p AND q) IS NOT NAN" => "(p AND q) IS NOT NAN"; "and in is not")]
    #[test_case("NOT ((a)) IS NULL" => "NOT a IS NULL"; "not is null")]
    #[test_case("sqrt((a + b))" => "sqrt(a + b)"; "function args")]
    #[test_case("t.\"Col\" LIKE 'x%'" => "t.Col LIKE ?"; "quoted column case")]
    #[test_case("\"select\".\"a\"\"b\"" => "\"select\".\"a\"\"b\""; "quoted column")]
    #[test_case("NAN + INFINITY - 1.5e3 + 'x' + TRUE" => "? + ? - ? + ? + ?"; "literals")]
    fn expr(input: &str) -> String {
        expression(&Parser::parse_expr(input).expect("parse failed"))
    }
}
