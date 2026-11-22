//! Canonical SQL text of statements and expressions, written from the AST. It
//! is the output of `FORMAT`, the text that views store for their query, the
//! statement text of `SHOW PREPARED`, and the text of expressions in lint
//! messages and of views in the change log.
//!
//! * Keywords are upper case. Names are written so that they read back as the
//!   same name: unquoted only if they are a lower-case identifier that isn't a
//!   keyword, otherwise between double quotes with `"` doubled. (Statement
//!   digests and the schema display quote names differently.)
//! * Literals are written as SQL text that reads back as the same literal:
//!   floats as displayed in query results, `NAN` and `INFINITY`, strings
//!   between single quotes with `'` doubled, literals of other kinds as the SQL
//!   text of their value. A minus sign is an operator, not part of a literal.
//!   Parameters are written `$n`.
//! * Synonyms are written in one form, optional words are left out, aliases
//!   use `AS`, and operands are parenthesized exactly where the parser needs
//!   it, as in statement digests. Every construct is written as it was written.
//! * A statement is a list of lines: each clause on a line of its own (`FROM`,
//!   `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT`, `OFFSET`, `SET`), joins
//!   on lines indented by two spaces, the rows of `VALUES` and the columns of
//!   `CREATE TABLE` one per indented line, and a statement contained in another
//!   one on the following lines. The one-line form joins the lines with single
//!   spaces.

use itertools::Itertools as _;

use super::ast::{
    Column, Describe, Direction, ExportFormat, Expression, From, ImportFormat, JoinType, Literal,
    Operator, Statement,
};
use super::digest::{is_value, precedence};
use super::{Lexer, Token};
use crate::sql::types::Value;

/// Returns the canonical text of a statement, as lines.
pub fn lines(statement: &Statement) -> Vec<String> {
    let mut lines = Vec::new();
    write_statement(statement, &mut lines);
    lines
}

/// Returns the canonical text of a statement on one line: its lines without
/// indentation, joined by single spaces (without spaces inside the parentheses
/// of a column list).
pub fn one_line(statement: &Statement) -> String {
    let mut text = String::new();
    for line in lines(statement) {
        let line = line.trim_start();
        if !text.is_empty() && !text.ends_with('(') && line != ")" {
            text.push(' ');
        }
        text.push_str(line);
    }
    text
}

/// Returns the canonical text of an expression.
pub fn expression(expr: &Expression) -> String {
    use Operator::*;

    let op = match expr {
        Expression::All => return "*".to_string(),
        Expression::Column(Some(table), column) => {
            return format!("{}.{}", ident(table), ident(column));
        }
        Expression::Column(None, column) => return ident(column),
        Expression::Literal(value) => return literal(value),
        Expression::Function(name, args) => {
            return format!("{}({})", ident(name), expressions(args));
        }
        Expression::Parameter(n) => return format!("${n}"),
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
        Is(expr, value) => format!("{} IS {}", operand(expr, precedence), is_value(value)),
        IsNot(expr, value) => format!("{} IS NOT {}", operand(expr, precedence), is_value(value)),
        LessThan(lhs, rhs) => infix(lhs, "<", rhs, precedence),
        LessThanOrEqual(lhs, rhs) => infix(lhs, "<=", rhs, precedence),
        NotEqual(lhs, rhs) => infix(lhs, "!=", rhs, precedence),

        Add(lhs, rhs) => infix(lhs, "+", rhs, precedence),
        Divide(lhs, rhs) => infix(lhs, "/", rhs, precedence),
        // ^ is right-associative.
        Exponentiate(lhs, rhs) => {
            format!("{} ^ {}", operand(lhs, precedence + 1), operand(rhs, precedence))
        }
        Factorial(expr) => format!("{}!", operand(expr, precedence)),
        Identity(expr) => format!("+{}", operand(expr, precedence)),
        Multiply(lhs, rhs) => infix(lhs, "*", rhs, precedence),
        Negate(expr) => format!("-{}", operand(expr, precedence)),
        Remainder(lhs, rhs) => infix(lhs, "%", rhs, precedence),
        Subtract(lhs, rhs) => infix(lhs, "-", rhs, precedence),

        Like(lhs, rhs) => infix(lhs, "LIKE", rhs, precedence),
    }
}

/// Writes a name so that the lexer reads it back as the same name, quoting it
/// if necessary.
pub fn ident(name: &str) -> String {
    let mut lexer = Lexer::new(name);
    match (lexer.next(), lexer.next()) {
        (Some(Ok(Token::Ident(ident))), None) if ident == name => ident,
        _ => format!("\"{}\"", name.replace('"', "\"\"")),
    }
}

/// Adds the lines of a statement.
fn write_statement(statement: &Statement, lines: &mut Vec<String>) {
    match statement {
        Statement::Begin { read_only, as_of } => {
            let mut line = "BEGIN".to_string();
            if *read_only {
                line += " READ ONLY";
            }
            if let Some(as_of) = as_of {
                line += &format!(" AS OF SYSTEM TIME {as_of}");
            }
            lines.push(line);
        }
        Statement::Commit => lines.push("COMMIT".to_string()),
        Statement::Rollback => lines.push("ROLLBACK".to_string()),
        Statement::Explain(statement) => write_contained("EXPLAIN".to_string(), statement, lines),

        Statement::CreateTable { name, columns } => {
            lines.push(format!("CREATE TABLE {} (", ident(name)));
            write_indented_list(columns.iter().map(column), lines);
            lines.push(")".to_string());
        }
        Statement::DropTable { name, if_exists } => match if_exists {
            true => lines.push(format!("DROP TABLE IF EXISTS {}", ident(name))),
            false => lines.push(format!("DROP TABLE {}", ident(name))),
        },

        Statement::Delete { table, r#where } => {
            lines.push(format!("DELETE FROM {}", ident(table)));
            write_where(r#where, lines);
        }
        Statement::Insert { table, columns, values } => {
            let mut line = format!("INSERT INTO {}", ident(table));
            if let Some(columns) = columns {
                line += &format!(" ({})", columns.iter().map(|c| ident(c)).join(", "));
            }
            lines.push(line + " VALUES");
            write_indented_list(values.iter().map(|row| format!("({})", expressions(row))), lines);
        }
        Statement::Update { table, set, r#where } => {
            lines.push(format!("UPDATE {}", ident(table)));
            let set = set
                .iter()
                .map(|(column, value)| match value {
                    Some(value) => format!("{} = {}", ident(column), expression(value)),
                    None => format!("{} = DEFAULT", ident(column)),
                })
                .join(", ");
            lines.push(format!("SET {set}"));
            write_where(r#where, lines);
        }
        Statement::Select { select, from, r#where, group_by, having, order_by, offset, limit } => {
            lines.push(format!("SELECT {}", select.iter().map(select_item).join(", ")));
            if !from.is_empty() {
                write_from(from, lines);
            }
            write_where(r#where, lines);
            if !group_by.is_empty() {
                lines.push(format!("GROUP BY {}", expressions(group_by)));
            }
            if let Some(having) = having {
                lines.push(format!("HAVING {}", expression(having)));
            }
            if !order_by.is_empty() {
                let order_by = order_by
                    .iter()
                    .map(|(expr, direction)| match direction {
                        Direction::Ascending => expression(expr),
                        Direction::Descending => format!("{} DESC", expression(expr)),
                    })
                    .join(", ");
                lines.push(format!("ORDER BY {order_by}"));
            }
            if let Some(limit) = limit {
                lines.push(format!("LIMIT {}", expression(limit)));
            }
            if let Some(offset) = offset {
                lines.push(format!("OFFSET {}", expression(offset)));
            }
        }

        Statement::ShowStatements => lines.push("SHOW STATEMENTS".to_string()),
        Statement::Analyze { table: Some(table) } => {
            lines.push(format!("ANALYZE {}", ident(table)))
        }
        Statement::Analyze { table: None } => lines.push("ANALYZE".to_string()),
        Statement::ShowStatistics { table } => {
            lines.push(format!("SHOW STATISTICS {}", ident(table)))
        }
        Statement::Export { table, format } => {
            let format = match format {
                ExportFormat::Csv => "CSV",
                ExportFormat::Json => "JSON",
                ExportFormat::Sql => "SQL",
            };
            match table {
                Some(table) => lines.push(format!("EXPORT {} AS {format}", ident(table))),
                None => lines.push(format!("EXPORT AS {format}")),
            }
        }
        Statement::Import { table, format, lines: import_lines } => {
            let format = match format {
                ImportFormat::Csv => "CSV",
                ImportFormat::Json => "JSON",
            };
            let import_lines = import_lines.iter().map(|line| string(line)).join(", ");
            lines.push(format!("IMPORT INTO {} FROM {format} {import_lines}", ident(table)));
        }

        Statement::Format(statement) => write_contained("FORMAT".to_string(), statement, lines),
        Statement::CreateView { name, columns, query } => {
            let mut line = format!("CREATE VIEW {}", ident(name));
            if let Some(columns) = columns {
                line += &format!(" ({})", columns.iter().map(|c| ident(c)).join(", "));
            }
            write_contained(line + " AS", query, lines);
        }
        Statement::DropView { name, if_exists } => match if_exists {
            true => lines.push(format!("DROP VIEW IF EXISTS {}", ident(name))),
            false => lines.push(format!("DROP VIEW {}", ident(name))),
        },
        Statement::ShowViews => lines.push("SHOW VIEWS".to_string()),
        Statement::Describe(Describe::Name(name)) => {
            lines.push(format!("DESCRIBE {}", ident(name)))
        }
        Statement::Describe(Describe::Statement(statement)) => {
            write_contained("DESCRIBE".to_string(), statement, lines)
        }
        Statement::Estimate(statement) => write_contained("ESTIMATE".to_string(), statement, lines),
        Statement::Prepare { name, types, statement } => {
            let mut line = format!("PREPARE {}", ident(name));
            if !types.is_empty() {
                line += &format!(" ({})", types.iter().join(", "));
            }
            write_contained(line + " AS", statement, lines);
        }
        Statement::Execute { name, arguments } => match arguments.is_empty() {
            true => lines.push(format!("EXECUTE {}", ident(name))),
            false => lines.push(format!("EXECUTE {} ({})", ident(name), expressions(arguments))),
        },
        Statement::Deallocate { name } => lines.push(format!("DEALLOCATE {}", ident(name))),
        Statement::ShowPrepared => lines.push("SHOW PREPARED".to_string()),
        Statement::Lint(statement) => write_contained("LINT".to_string(), statement, lines),
        Statement::ShowChanges { since: Some(since) } => {
            lines.push(format!("SHOW CHANGES SINCE {since}"))
        }
        Statement::ShowChanges { since: None } => lines.push("SHOW CHANGES".to_string()),
    }
}

/// Adds a line, followed by the lines of a contained statement.
fn write_contained(line: String, statement: &Statement, lines: &mut Vec<String>) {
    lines.push(line);
    write_statement(statement, lines);
}

/// Adds the items of a list on indented lines.
fn write_indented_list(items: impl ExactSizeIterator<Item = String>, lines: &mut Vec<String>) {
    let last = items.len().saturating_sub(1);
    for (i, item) in items.enumerate() {
        let comma = if i < last { "," } else { "" };
        lines.push(format!("  {item}{comma}"));
    }
}

/// Adds a WHERE clause line, if any.
fn write_where(r#where: &Option<Expression>, lines: &mut Vec<String>) {
    if let Some(expr) = r#where {
        lines.push(format!("WHERE {}", expression(expr)));
    }
}

/// Adds the lines of a FROM clause.
fn write_from(from: &[From], lines: &mut Vec<String>) {
    lines.push("FROM ".to_string());
    for (i, item) in from.iter().enumerate() {
        if i > 0 {
            push_str(lines, ", ");
        }
        write_from_item(item, lines);
    }
}

/// Adds a FROM item.
fn write_from_item(item: &From, lines: &mut Vec<String>) {
    match item {
        From::Table { name, alias: Some(alias) } => {
            push_str(lines, &format!("{} AS {}", ident(name), ident(alias)))
        }
        From::Table { name, alias: None } => push_str(lines, &ident(name)),
        From::Join { left, right, r#type, predicate } => {
            write_from_item(left, lines);
            let join = match r#type {
                JoinType::Cross => "CROSS JOIN",
                JoinType::Inner => "JOIN",
                JoinType::Left => "LEFT JOIN",
                JoinType::Right => "RIGHT JOIN",
            };
            lines.push(format!("  {join} "));
            write_from_item(right, lines);
            if let Some(predicate) = predicate {
                push_str(lines, &format!(" ON {}", expression(predicate)));
            }
        }
    }
}

/// Appends text to the last line.
fn push_str(lines: &mut [String], text: &str) {
    lines.last_mut().expect("no line").push_str(text);
}

/// Formats a SELECT item.
fn select_item((expr, alias): &(Expression, Option<String>)) -> String {
    match alias {
        Some(alias) => format!("{} AS {}", expression(expr), ident(alias)),
        None => expression(expr),
    }
}

/// Formats a CREATE TABLE column definition.
fn column(column: &Column) -> String {
    let mut text = format!("{} {}", ident(&column.name), column.datatype);
    if column.primary_key {
        text += " PRIMARY KEY";
    }
    match column.nullable {
        Some(true) => text += " NULL",
        Some(false) => text += " NOT NULL",
        None => {}
    }
    if let Some(default) = &column.default {
        text += &format!(" DEFAULT {}", expression(default));
    }
    if column.unique {
        text += " UNIQUE";
    }
    if column.index {
        text += " INDEX";
    }
    if let Some(references) = &column.references {
        text += &format!(" REFERENCES {}", ident(references));
    }
    text
}

/// Formats a comma-separated list of expressions.
fn expressions(exprs: &[Expression]) -> String {
    exprs.iter().map(expression).join(", ")
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

/// Formats a literal.
fn literal(literal: &Literal) -> String {
    match literal {
        Literal::Null => "NULL".to_string(),
        Literal::Boolean(true) => "TRUE".to_string(),
        Literal::Boolean(false) => "FALSE".to_string(),
        Literal::Integer(i) => i.to_string(),
        Literal::Float(f) if f.is_nan() => "NAN".to_string(),
        Literal::Float(f) if f.is_infinite() => "INFINITY".to_string(),
        Literal::Float(f) => Value::Float(*f).to_string(),
        Literal::String(s) => string(s),
    }
}

/// Formats a string literal.
fn string(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::parser::Parser;

    #[test_case("select ID, v \"Label\" from T x left outer join u on u.id = x.id cross join w, z where x.id >= 2.50 group by 1 having count(*) > 1 order by 1 asc, v desc limit 5 offset 1"
        => vec!["SELECT id, v AS \"Label\"", "FROM t AS x", "  LEFT JOIN u ON u.id = x.id", "  CROSS JOIN w, z", "WHERE x.id >= 2.5", "GROUP BY 1", "HAVING count(*) > 1", "ORDER BY 1, v DESC", "LIMIT 5", "OFFSET 1"]; "select")]
    #[test_case("SELECT 1" => vec!["SELECT 1"]; "select without from")]
    #[test_case("insert into t (a, \"B\") values (1, 'x'), (-2, null)"
        => vec!["INSERT INTO t (a, \"B\") VALUES", "  (1, 'x'),", "  (-2, NULL)"]; "insert")]
    #[test_case("update t set z = default, a = 1 where id = 2" => vec!["UPDATE t", "SET a = 1, z = DEFAULT", "WHERE id = 2"]; "update")]
    #[test_case("delete from t" => vec!["DELETE FROM t"]; "delete")]
    #[test_case("create table t (id int primary key, v text not null default 'a' unique index, r int null references t)"
        => vec!["CREATE TABLE t (", "  id INTEGER PRIMARY KEY,", "  v STRING NOT NULL DEFAULT 'a' UNIQUE INDEX,", "  r INTEGER NULL REFERENCES t", ")"]; "create table")]
    #[test_case("drop table if exists \"T\"" => vec!["DROP TABLE IF EXISTS \"T\""]; "drop table")]
    #[test_case("begin transaction read only as of system time 07" => vec!["BEGIN READ ONLY AS OF SYSTEM TIME 7"]; "begin")]
    #[test_case("begin read write" => vec!["BEGIN"]; "begin read write")]
    #[test_case("explain delete from t where id = 1" => vec!["EXPLAIN", "DELETE FROM t", "WHERE id = 1"]; "explain")]
    #[test_case("import into t from csv 'a', 'it''s'" => vec!["IMPORT INTO t FROM CSV 'a', 'it''s'"]; "import")]
    #[test_case("export t as csv" => vec!["EXPORT t AS CSV"]; "export")]
    #[test_case("analyze \"T\"" => vec!["ANALYZE \"T\""]; "analyze")]
    #[test_case("format format select 1" => vec!["FORMAT", "FORMAT", "SELECT 1"]; "format")]
    #[test_case("create view v (a, \"B\") as select 1, 2" => vec!["CREATE VIEW v (a, \"B\") AS", "SELECT 1, 2"]; "create view")]
    #[test_case("drop view if exists v" => vec!["DROP VIEW IF EXISTS v"]; "drop view")]
    #[test_case("describe \"V\"" => vec!["DESCRIBE \"V\""]; "describe name")]
    #[test_case("describe select 1" => vec!["DESCRIBE", "SELECT 1"]; "describe statement")]
    #[test_case("estimate select * from t" => vec!["ESTIMATE", "SELECT *", "FROM t"]; "estimate")]
    #[test_case("prepare p (int, double) as select $2 from t where id = $01"
        => vec!["PREPARE p (INTEGER, FLOAT) AS", "SELECT $2", "FROM t", "WHERE id = $1"]; "prepare")]
    #[test_case("execute p (1, -1.50)" => vec!["EXECUTE p (1, -1.5)"]; "execute")]
    #[test_case("execute p" => vec!["EXECUTE p"]; "execute without arguments")]
    #[test_case("deallocate \"P\"" => vec!["DEALLOCATE \"P\""]; "deallocate")]
    #[test_case("lint show prepared" => vec!["LINT", "SHOW PREPARED"]; "lint")]
    #[test_case("show changes since 007" => vec!["SHOW CHANGES SINCE 7"]; "show changes")]
    fn statement(input: &str) -> Vec<String> {
        lines(&Parser::parse(input).expect("parse failed"))
    }

    #[test_case("CREATE TABLE t (id INT PRIMARY KEY, v STRING)"
        => "CREATE TABLE t (id INTEGER PRIMARY KEY, v STRING)"; "create table")]
    #[test_case("EXPLAIN INSERT INTO t VALUES (1, 'a'), (2, 'b')"
        => "EXPLAIN INSERT INTO t VALUES (1, 'a'), (2, 'b')"; "explain insert")]
    #[test_case("SELECT * FROM a JOIN b ON a.x = b.y, c WHERE TRUE"
        => "SELECT * FROM a JOIN b ON a.x = b.y, c WHERE TRUE"; "select")]
    #[test_case("PREPARE p AS CREATE TABLE \"(\" (\")\" INT PRIMARY KEY)"
        => "PREPARE p AS CREATE TABLE \"(\" (\")\" INTEGER PRIMARY KEY)"; "parenthesis names")]
    fn single_line(input: &str) -> String {
        one_line(&Parser::parse(input).expect("parse failed"))
    }

    /// The one-line form and the lines parse back as the same statement.
    #[test_case("select ID, v \"Label\" from T x left join u on u.id = x.id, z where not (a <> -1.5e-7 or b is not null) order by 2 desc"; "select")]
    #[test_case("create table \"T\" (\"Id\" int primary key, v double not null default -infinity index)"; "create table")]
    #[test_case("prepare p (bool) as update t set v = $1 * 2 where v is nan"; "prepare update")]
    #[test_case("lint explain insert into t values (1, 'it''s', 1e100, nan)"; "lint explain insert")]
    fn roundtrip(input: &str) {
        let statement = Parser::parse(input).expect("parse failed");
        let text = one_line(&statement);
        let reparsed = Parser::parse(&text).expect("one-line form parse failed");
        assert_eq!(one_line(&reparsed), text);
        assert_eq!(lines(&reparsed), lines(&statement));
        let reparsed = Parser::parse(&lines(&statement).join("\n")).expect("lines parse failed");
        assert_eq!(one_line(&reparsed), text);
    }

    #[test_case("1.50 + 2E3 - 1e100 * 1.5e-7" => "1.5 + 2000.0 - 1e100 * 1.5e-7"; "floats")]
    #[test_case("007 + -1" => "7 + -1"; "integers")]
    #[test_case("INFINITY + NAN" => "INFINITY + NAN"; "special floats")]
    #[test_case("'it''s' LIKE 'a\"b\\c'" => "'it''s' LIKE 'a\"b\\c'"; "strings")]
    #[test_case("true AND false OR null" => "TRUE AND FALSE OR NULL"; "keyword literals")]
    #[test_case("a <> b AND c >= d AND e IS NOT NULL" => "a != b AND c >= d AND e IS NOT NULL"; "operators as written")]
    #[test_case("(a + b) * c - (d - e) ^ (f ^ g)" => "(a + b) * c - (d - e) ^ f ^ g"; "parentheses")]
    #[test_case("NOT (a = b) AND (NOT a) = b" => "NOT a = b AND (NOT a) = b"; "not")]
    #[test_case("$01 + $2" => "$1 + $2"; "parameters")]
    #[test_case("SQRT(T.\"X\") + \"Max\"(y)" => "sqrt(t.\"X\") + \"Max\"(y)"; "names")]
    fn expr(input: &str) -> String {
        expression(&Parser::parse_expr(input).expect("parse failed"))
    }

    #[test_case("item" => "item"; "lower case")]
    #[test_case("t2_x" => "t2_x"; "digits and underscore")]
    #[test_case("été" => "été"; "non-ascii")]
    #[test_case("Item" => "\"Item\""; "upper case")]
    #[test_case("select" => "\"select\""; "keyword")]
    #[test_case("format" => "\"format\""; "extension keyword")]
    #[test_case("a b" => "\"a b\""; "space")]
    #[test_case("x\"y" => "\"x\"\"y\""; "quote")]
    #[test_case("_x" => "\"_x\""; "underscore first")]
    #[test_case("1a" => "\"1a\""; "digit first")]
    #[test_case("a$1" => "\"a$1\""; "parameter")]
    #[test_case("" => "\"\""; "empty")]
    fn name(name: &str) -> String {
        ident(name)
    }
}
