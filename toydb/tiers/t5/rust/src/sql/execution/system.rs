//! System tables: the catalog, the server's sessions and audit log, and the SQL
//! language, as read-only tables that queries read. Their names begin with
//! `toydb_`; a table or view of the same name comes first. They are computed
//! when a `SystemTable` plan node is executed, in the statement's transaction,
//! and are not exported or backed up.
//!
//! * `toydb_tables`, `toydb_columns`, `toydb_views`, `toydb_checks`,
//!   `toydb_policies`, `toydb_users`, `toydb_members`, `toydb_grants`,
//!   `toydb_statistics`, `toydb_matviews`: the catalog. `toydb_sessions`,
//!   `toydb_audit` (superusers only), `toydb_prepared` and `toydb_statements`
//!   (the reading session's): as their SHOW statements. `pg_type`: the
//!   PostgreSQL types that the wire protocol uses, by OID.
//! * `toydb_keywords`: every reserved keyword, also those added later, in upper
//!   case, with its category, the first that applies: `statement` (a statement
//!   begins with it), `type` (it names a data type in a column definition),
//!   `literal` (it is a literal on its own), `operator` (it is a word of an
//!   operator of the precedence table, levels 1 to 10 — `AND`, `OR`, `NOT`,
//!   `LIKE`, `IS` — or of an operator added later at one of those levels),
//!   `reserved` (every other keyword).
//! * `toydb_functions`: every function and aggregate function that expressions
//!   can call, also those added later, by name in lower case, with its kind
//!   (`scalar` or `aggregate`) and its number of arguments (one row per
//!   number).
//! * `toydb_types`: every data type, also those added later, with the keywords
//!   that name it in a column definition (its spellings), the PostgreSQL type
//!   and OID the wire protocol sends it as, and its backup type tag.

use std::sync::Mutex;

use itertools::Itertools as _;

use super::StatementResult;
use super::audit::{AuditLog, SessionList};
use super::digests::Digests;
use super::export::sql_literal;
use super::prepared::PreparedStatements;
use super::{privileges, users};
use crate::backup::format::type_tag;
use crate::error::Result;
use crate::pgwire::types as pgtypes;
use crate::sql::engine::{Catalog, Transaction};
use crate::sql::parser::{Keyword, format};
use crate::sql::types::{Column, DataType, Row, Rows, Table, User, Value};
use crate::{errdata, errperm};

/// A system table.
#[derive(Clone, Copy, Debug, PartialEq)]
enum SystemTable {
    Tables,
    Columns,
    Views,
    Matviews,
    Checks,
    Policies,
    Users,
    Members,
    Grants,
    Statistics,
    Sessions,
    Audit,
    Prepared,
    Statements,
    Keywords,
    Functions,
    Types,
    PgType,
}

/// A system table column: its name, data type and nullability.
type ColumnSpec = (&'static str, DataType, bool);

impl SystemTable {
    /// All system tables.
    const ALL: [SystemTable; 18] = [
        Self::Tables,
        Self::Columns,
        Self::Views,
        Self::Matviews,
        Self::Checks,
        Self::Policies,
        Self::Users,
        Self::Members,
        Self::Grants,
        Self::Statistics,
        Self::Sessions,
        Self::Audit,
        Self::Prepared,
        Self::Statements,
        Self::Keywords,
        Self::Functions,
        Self::Types,
        Self::PgType,
    ];

    /// Returns the system table of that name, if there is one.
    fn from_name(name: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|table| table.name() == name)
    }

    /// Returns the table's name.
    fn name(self) -> &'static str {
        match self {
            Self::Tables => "toydb_tables",
            Self::Columns => "toydb_columns",
            Self::Views => "toydb_views",
            Self::Matviews => "toydb_matviews",
            Self::Checks => "toydb_checks",
            Self::Policies => "toydb_policies",
            Self::Users => "toydb_users",
            Self::Members => "toydb_members",
            Self::Grants => "toydb_grants",
            Self::Statistics => "toydb_statistics",
            Self::Sessions => "toydb_sessions",
            Self::Audit => "toydb_audit",
            Self::Prepared => "toydb_prepared",
            Self::Statements => "toydb_statements",
            Self::Keywords => "toydb_keywords",
            Self::Functions => "toydb_functions",
            Self::Types => "toydb_types",
            Self::PgType => "pg_type",
        }
    }

    /// Returns the table's columns.
    fn columns(self) -> &'static [ColumnSpec] {
        use DataType::*;
        match self {
            Self::Tables => &[
                ("name", String, false),
                ("owner", String, false),
                ("primary_key", String, false),
                ("columns", Integer, false),
                ("checks", Integer, false),
                ("policies", Integer, false),
            ],
            Self::Columns => &[
                ("table_name", String, false),
                ("name", String, false),
                ("position", Integer, false),
                ("type", String, false),
                ("nullable", Boolean, false),
                ("default_value", String, true),
                ("primary_key", Boolean, false),
                ("is_unique", Boolean, false),
                ("has_index", Boolean, false),
                ("references_table", String, true),
            ],
            Self::Views => &[
                ("name", String, false),
                ("owner", String, false),
                ("columns", String, false),
                ("query", String, false),
                ("dependencies", String, false),
            ],
            Self::Matviews => &[
                ("name", String, false),
                ("owner", String, false),
                ("columns", String, false),
                ("query", String, false),
                ("dependencies", String, false),
                ("rows", Integer, false),
            ],
            Self::Checks => &[
                ("table_name", String, false),
                ("name", String, false),
                ("condition", String, false),
            ],
            Self::Policies => &[
                ("table_name", String, false),
                ("name", String, false),
                ("grantee", String, true),
                ("condition", String, false),
                ("check_condition", String, true),
            ],
            Self::Users => &[
                ("name", String, false),
                ("is_superuser", Boolean, false),
                ("has_password", Boolean, false),
            ],
            Self::Members => &[("member", String, false), ("member_of", String, false)],
            Self::Grants => &[
                ("object", String, false),
                ("grantee", String, false),
                ("privilege", String, false),
            ],
            Self::Statistics => &[
                ("table_name", String, false),
                ("column_name", String, false),
                ("rows", Integer, false),
                ("nulls", Integer, false),
                ("distinct_values", Integer, false),
                ("min_value", String, true),
                ("max_value", String, true),
            ],
            Self::Sessions => &[
                ("session", Integer, false),
                ("username", String, false),
                ("login", String, false),
                ("txn", Integer, true),
                ("executed", Integer, false),
            ],
            Self::Audit => &[
                ("seq", Integer, false),
                ("username", String, false),
                ("class", String, false),
                ("statement", String, false),
                ("objects", String, false),
                ("error", String, true),
            ],
            Self::Prepared => &[
                ("name", String, false),
                ("parameters", String, false),
                ("statement", String, false),
                ("executions", Integer, false),
                ("plans", Integer, false),
            ],
            Self::Statements => &[
                ("digest", String, false),
                ("calls", Integer, false),
                ("errors", Integer, false),
                ("rows", Integer, false),
            ],
            Self::Keywords => &[("word", String, false), ("category", String, false)],
            Self::Functions => &[
                ("name", String, false),
                ("kind", String, false),
                ("arguments", Integer, false),
            ],
            Self::Types => &[
                ("name", String, false),
                ("spellings", String, false),
                ("pg_type", String, false),
                ("pg_oid", Integer, false),
                ("backup_tag", Integer, false),
            ],
            Self::PgType => &[
                ("oid", Integer, false),
                ("typname", String, false),
                ("typlen", Integer, false),
            ],
        }
    }
}

/// The functions and aggregate functions that expressions can call, with their
/// kind and number of arguments.
pub const FUNCTIONS: &[(&str, &str, usize)] = &[
    ("avg", "aggregate", 1),
    ("count", "aggregate", 1),
    ("max", "aggregate", 1),
    ("min", "aggregate", 1),
    ("sqrt", "scalar", 1),
    ("sum", "aggregate", 1),
];

/// The state of a session and its server that the system tables show.
pub struct SessionState<'a> {
    /// The server's audit log.
    pub audit: &'a Mutex<AuditLog>,
    /// The server's open sessions.
    pub sessions: &'a Mutex<SessionList>,
    /// The session's prepared statements.
    pub prepared: &'a PreparedStatements,
    /// The session's statement statistics.
    pub digests: &'a Digests,
}

/// Returns the schema of the system table of that name, if there is one.
pub fn schema(name: &str) -> Option<Table> {
    let table = SystemTable::from_name(name)?;
    let columns = table
        .columns()
        .iter()
        .map(|&(name, datatype, nullable)| Column {
            name: name.to_string(),
            datatype,
            nullable,
            default: None,
            unique: false,
            index: false,
            references: None,
        })
        .collect();
    // System tables have no primary key; the field is unused.
    Some(Table { name: table.name().to_string(), primary_key: 0, columns })
}

/// Returns true if a name in a FROM clause refers to a system table.
pub fn is_system_table(catalog: &impl Catalog, name: &str) -> Result<bool> {
    Ok(SystemTable::from_name(name).is_some()
        && catalog.get_table(name)?.is_none()
        && catalog.get_view(name)?.is_none()
        && catalog.get_materialized_view(name)?.is_none())
}

/// Computes the rows of a system table in the transaction.
pub fn rows(
    txn: &impl Transaction,
    user: &str,
    session: Option<&SessionState>,
    name: &str,
) -> Result<Vec<Row>> {
    let Some(table) = SystemTable::from_name(name) else {
        return errdata!("unknown system table {name}");
    };
    let session = || -> Result<&SessionState> {
        session.ok_or_else(|| errdata!("system table {name} requires a session"))
    };
    Ok(match table {
        SystemTable::Tables => tables(txn)?,
        SystemTable::Columns => columns(txn)?,
        SystemTable::Views => views(txn)?,
        SystemTable::Matviews => matviews(txn)?,
        SystemTable::Checks => checks(txn)?,
        SystemTable::Policies => policies(txn)?,
        SystemTable::Users => users(txn)?,
        SystemTable::Members => txn
            .list_memberships()?
            .into_iter()
            .sorted()
            .map(|(member, user)| vec![Value::String(member), Value::String(user)])
            .collect(),
        SystemTable::Grants => result_rows(users::show_grants(txn)?)?,
        SystemTable::Statistics => statistics(txn)?,
        SystemTable::Sessions => result_rows(session()?.sessions.lock()?.result())?,
        SystemTable::Audit => {
            if !privileges::is_superuser(txn, user)? {
                return errperm!("user {user} is not a superuser");
            }
            result_rows(session()?.audit.lock()?.result(0))?
        }
        SystemTable::Prepared => result_rows(session()?.prepared.show())?,
        SystemTable::Statements => result_rows(session()?.digests.to_result())?,
        SystemTable::Keywords => keywords(),
        SystemTable::Functions => functions(),
        SystemTable::Types => types(),
        SystemTable::PgType => pg_types(),
    })
}

/// Returns the category of a keyword, the first that applies: statement, type,
/// literal, operator, reserved.
pub fn keyword_category(keyword: Keyword) -> &'static str {
    use Keyword::*;
    match keyword {
        Analyze | Begin | Commit | Create | Deallocate | Delete | Describe | Drop | Estimate
        | Execute | Explain | Export | Format | Grant | Import | Insert | Lint | Prepare
        | Refresh | Reset | Revoke | Rollback | Select | Set | Show | Update => "statement",
        keyword if keyword.datatype().is_some() => "type",
        True | False | Null | NaN | Infinity => "literal",
        And | Or | Not | Like | Is => "operator",
        _ => "reserved",
    }
}

/// Returns the rows of a SELECT result.
fn result_rows(result: StatementResult) -> Result<Vec<Row>> {
    Rows::try_from(result)?.try_collect()
}

/// Returns an optional string value, NULL if None.
fn optional(value: Option<String>) -> Value {
    value.map_or(Value::Null, Value::String)
}

/// Returns the rows of toydb_tables.
fn tables(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    for table in catalog.list_tables()? {
        rows.push(vec![
            Value::String(table.name.clone()),
            Value::String(catalog.get_owner(&table.name)?),
            Value::String(table.columns[table.primary_key].name.clone()),
            Value::Integer(table.columns.len() as i64),
            Value::Integer(catalog.get_checks(&table.name)?.len() as i64),
            Value::Integer(catalog.list_policies(&table.name)?.len() as i64),
        ]);
    }
    Ok(rows)
}

/// Returns the rows of toydb_columns.
fn columns(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    for table in catalog.list_tables()? {
        for (i, column) in table.columns.iter().enumerate() {
            rows.push(vec![
                Value::String(table.name.clone()),
                Value::String(column.name.clone()),
                Value::Integer(i as i64 + 1),
                Value::String(column.datatype.to_string()),
                Value::Boolean(column.nullable),
                optional(column.default.as_ref().map(sql_literal)),
                Value::Boolean(i == table.primary_key),
                Value::Boolean(column.unique),
                Value::Boolean(column.index),
                optional(column.references.clone()),
            ]);
        }
    }
    Ok(rows)
}

/// Returns the rows of toydb_views.
fn views(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    for view in catalog.list_views()? {
        rows.push(vec![
            Value::String(view.name.clone()),
            Value::String(catalog.get_owner(&view.name)?),
            Value::String(view.columns.iter().map(|c| format::ident(c)).join(", ")),
            Value::String(view.query),
            Value::String(view.dependencies.join(", ")),
        ]);
    }
    Ok(rows)
}

/// Returns the rows of toydb_matviews.
fn matviews(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    for view in catalog.list_materialized_views()? {
        let count = catalog.get_materialized_view_rows(&view.name)?.len();
        rows.push(vec![
            Value::String(view.name.clone()),
            Value::String(catalog.get_owner(&view.name)?),
            Value::String(view.columns.iter().map(|c| format::ident(c)).join(", ")),
            Value::String(view.query),
            Value::String(view.dependencies.join(", ")),
            Value::Integer(count as i64),
        ]);
    }
    Ok(rows)
}

/// Returns the rows of toydb_checks.
fn checks(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    for table in catalog.list_tables()? {
        for check in catalog.get_checks(&table.name)? {
            rows.push(vec![
                Value::String(table.name.clone()),
                Value::String(check.name),
                Value::String(check.condition),
            ]);
        }
    }
    Ok(rows)
}

/// Returns the rows of toydb_policies.
fn policies(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    for table in catalog.list_tables()? {
        for policy in catalog.list_policies(&table.name)? {
            rows.push(vec![
                Value::String(policy.table),
                Value::String(policy.name),
                optional(policy.user),
                Value::String(policy.using),
                optional(policy.with_check),
            ]);
        }
    }
    Ok(rows)
}

/// Returns the rows of toydb_users.
fn users(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let users = std::iter::once(User::admin()).chain(catalog.list_users()?);
    Ok(users
        .sorted_by(|a, b| a.name.cmp(&b.name))
        .map(|user| {
            vec![
                Value::String(user.name),
                Value::Boolean(user.superuser),
                Value::Boolean(user.password.is_some()),
            ]
        })
        .collect())
}

/// Returns the rows of toydb_statistics.
fn statistics(catalog: &impl Catalog) -> Result<Vec<Row>> {
    let mut rows = Vec::new();
    for table in catalog.list_tables()? {
        let Some(statistics) = catalog.get_table_statistics(&table.name)? else {
            continue;
        };
        for column in &table.columns {
            let Some(column_statistics) = statistics.columns.get(&column.name) else {
                continue;
            };
            let display = |value: &Option<Value>| optional(value.as_ref().map(Value::to_string));
            rows.push(vec![
                Value::String(table.name.clone()),
                Value::String(column.name.clone()),
                Value::Integer(statistics.rows as i64),
                Value::Integer(column_statistics.nulls as i64),
                Value::Integer(column_statistics.distinct as i64),
                display(&column_statistics.min),
                display(&column_statistics.max),
            ]);
        }
    }
    Ok(rows)
}

/// Returns the rows of toydb_keywords.
fn keywords() -> Vec<Row> {
    Keyword::ALL
        .into_iter()
        .map(|keyword| (keyword.to_string(), keyword_category(keyword)))
        .sorted()
        .map(|(word, category)| vec![Value::String(word), Value::String(category.to_string())])
        .collect()
}

/// Returns the rows of toydb_functions.
fn functions() -> Vec<Row> {
    FUNCTIONS
        .iter()
        .sorted()
        .map(|&(name, kind, arguments)| {
            vec![
                Value::String(name.to_string()),
                Value::String(kind.to_string()),
                Value::Integer(arguments as i64),
            ]
        })
        .collect()
}

/// Returns the rows of toydb_types.
fn types() -> Vec<Row> {
    DataType::ALL
        .into_iter()
        .sorted_by_key(|datatype| type_tag(*datatype))
        .map(|datatype| {
            let spellings = Keyword::ALL
                .into_iter()
                .filter(|keyword| keyword.datatype() == Some(datatype))
                .map(|keyword| keyword.to_string())
                .sorted()
                .join(", ");
            let pg_type = pgtypes::of_datatype(Some(datatype));
            vec![
                Value::String(datatype.to_string()),
                Value::String(spellings),
                Value::String(pg_type.name.to_string()),
                Value::Integer(pg_type.oid as i64),
                Value::Integer(type_tag(datatype) as i64),
            ]
        })
        .collect()
}

/// Returns the rows of pg_type.
fn pg_types() -> Vec<Row> {
    pgtypes::TYPES
        .into_iter()
        .sorted_by_key(|pg_type| pg_type.oid)
        .map(|pg_type| {
            vec![
                Value::Integer(pg_type.oid as i64),
                Value::String(pg_type.name.to_string()),
                Value::Integer(pg_type.size as i64),
            ]
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::engine::{Engine as _, Local};
    use crate::sql::parser::Parser;
    use crate::storage::Memory;

    /// Creates a local engine and executes the given statements in a session.
    fn setup(statements: &[&str]) -> Local<Memory> {
        let engine = Local::new(Memory::new());
        let mut session = engine.session();
        for statement in statements {
            session.execute(statement).expect("statement failed");
        }
        drop(session);
        engine
    }

    /// Computes the rows of a catalog or language system table as admin, one
    /// string each.
    fn table_rows(engine: &Local<Memory>, name: &str) -> Result<Vec<String>> {
        let txn = engine.begin_read_only()?;
        let rows = rows(&txn, "admin", None, name)?;
        Ok(rows.into_iter().map(|row| row.into_iter().join(", ")).collect())
    }

    #[test_case(Keyword::Select => "statement"; "select")]
    #[test_case(Keyword::Show => "statement"; "show")]
    #[test_case(Keyword::Set => "statement"; "set")]
    #[test_case(Keyword::Varchar => "type"; "varchar")]
    #[test_case(Keyword::Double => "type"; "double")]
    #[test_case(Keyword::NaN => "literal"; "nan")]
    #[test_case(Keyword::Null => "literal"; "null")]
    #[test_case(Keyword::Like => "operator"; "like")]
    #[test_case(Keyword::Is => "operator"; "is")]
    #[test_case(Keyword::Using => "reserved"; "using")]
    #[test_case(Keyword::Key => "reserved"; "key")]
    fn keyword_category(keyword: Keyword) -> &'static str {
        super::keyword_category(keyword)
    }

    #[test]
    fn keywords() -> Result<()> {
        // Every keyword is listed once, and lexes as itself.
        let rows = super::keywords();
        assert_eq!(rows.len(), Keyword::ALL.len());
        for keyword in Keyword::ALL {
            let word = keyword.to_string().to_lowercase();
            assert_eq!(Keyword::try_from(word.as_str()), Ok(keyword));
        }
        let words = rows.iter().map(|row| row[0].to_string()).collect_vec();
        assert!(words.iter().tuple_windows().all(|(a, b)| a < b), "keywords not ordered");
        assert_eq!(rows[0], vec![Value::from("ANALYZE"), Value::from("statement")]);
        Ok(())
    }

    #[test]
    fn schema() {
        let table = super::schema("toydb_columns").expect("no schema");
        assert_eq!(table.name, "toydb_columns");
        assert_eq!(table.columns.len(), 10);
        let column = &table.columns[5];
        let column = (column.name.as_str(), column.datatype, column.nullable);
        assert_eq!(column, ("default_value", DataType::String, true));
        assert_eq!(super::schema("pg_type").map(|table| table.columns.len()), Some(3));
        assert_eq!(super::schema("toydb_nosuch"), None);
        assert_eq!(super::schema("TOYDB_TABLES"), None);
    }

    #[test]
    fn is_system_table() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE toydb_users (id INT PRIMARY KEY)",
            "CREATE VIEW toydb_grants AS SELECT 1 AS x",
        ]);
        let txn = engine.begin_read_only()?;
        assert!(super::is_system_table(&txn, "toydb_tables")?);
        assert!(!super::is_system_table(&txn, "toydb_users")?);
        assert!(!super::is_system_table(&txn, "toydb_grants")?);
        assert!(!super::is_system_table(&txn, "nosuch")?);
        Ok(())
    }

    #[test]
    fn row_widths() -> Result<()> {
        // Every system table's rows have one value per column.
        let engine = setup(&[
            "CREATE USER alice PASSWORD 'pw'",
            "GRANT alice TO admin",
            "CREATE TABLE t (id INT PRIMARY KEY, v STRING, CHECK (id > 0))",
            "INSERT INTO t VALUES (1, 'a')",
            "ANALYZE t",
            "CREATE VIEW v AS SELECT * FROM t",
            "GRANT SELECT ON t TO alice",
            "CREATE POLICY p ON t TO alice USING (id > 1)",
        ]);
        let mut session = engine.session();
        session.execute("PREPARE q AS SELECT 1")?;
        session.execute("SELECT 1")?;
        let txn = engine.begin_read_only()?;
        let (prepared, digests) = (PreparedStatements::new(), Digests::new());
        let state = SessionState {
            audit: engine.audit(),
            sessions: engine.sessions(),
            prepared: &prepared,
            digests: &digests,
        };
        for table in SystemTable::ALL {
            let width = table.columns().len();
            let rows = rows(&txn, "admin", Some(&state), table.name())?;
            assert!(rows.iter().all(|row| row.len() == width), "wrong row width in {table:?}");
        }
        Ok(())
    }

    #[test]
    fn catalog() -> Result<()> {
        let engine = setup(&[
            "CREATE TABLE b (id STRING PRIMARY KEY, n INT DEFAULT 1 CHECK (n > 0))",
            "CREATE TABLE a (id INT PRIMARY KEY, b STRING NOT NULL REFERENCES b)",
            "CREATE USER u",
            "CREATE POLICY p ON a TO u USING (id > 1) WITH CHECK (id < 9)",
        ]);
        assert_eq!(
            table_rows(&engine, "toydb_tables")?,
            ["'a', 'admin', 'id', 2, 0, 1", "'b', 'admin', 'id', 2, 1, 0"]
        );
        assert_eq!(
            table_rows(&engine, "toydb_columns")?,
            [
                "'a', 'id', 1, 'INTEGER', FALSE, NULL, TRUE, TRUE, FALSE, NULL",
                "'a', 'b', 2, 'STRING', FALSE, NULL, FALSE, FALSE, TRUE, 'b'",
                "'b', 'id', 1, 'STRING', FALSE, NULL, TRUE, TRUE, FALSE, NULL",
                "'b', 'n', 2, 'INTEGER', TRUE, '1', FALSE, FALSE, FALSE, NULL",
            ]
        );
        assert_eq!(table_rows(&engine, "toydb_checks")?, ["'b', 'b_check1', 'n > 0'"]);
        assert_eq!(table_rows(&engine, "toydb_policies")?, ["'a', 'p', 'u', 'id > 1', 'id < 9'"]);
        let users = table_rows(&engine, "toydb_users")?;
        assert_eq!(users, ["'admin', TRUE, FALSE", "'u', FALSE, FALSE"]);
        Ok(())
    }

    #[test]
    fn language() -> Result<()> {
        let engine = setup(&[]);
        assert_eq!(
            table_rows(&engine, "toydb_types")?,
            [
                "'BOOLEAN', 'BOOL, BOOLEAN', 'bool', 16, 1",
                "'INTEGER', 'INT, INTEGER', 'int8', 20, 2",
                "'FLOAT', 'DOUBLE, FLOAT', 'float8', 701, 3",
                "'STRING', 'STRING, TEXT, VARCHAR', 'text', 25, 4",
            ]
        );
        let functions = table_rows(&engine, "toydb_functions")?;
        assert_eq!(functions.first().map(String::as_str), Some("'avg', 'aggregate', 1"));
        assert!(functions.contains(&"'sqrt', 'scalar', 1".to_string()));
        let pg_types = table_rows(&engine, "pg_type")?;
        assert_eq!(pg_types.first().map(String::as_str), Some("16, 'bool', 1"));
        assert_eq!(pg_types.last().map(String::as_str), Some("1043, 'varchar', -1"));
        Ok(())
    }

    #[test]
    fn functions_callable() -> Result<()> {
        // Every listed function can be called with its number of arguments.
        let engine = setup(&["CREATE TABLE t (id INT PRIMARY KEY)"]);
        let mut session = engine.session();
        for (name, _, arguments) in FUNCTIONS {
            let arguments = vec!["id"; *arguments].join(", ");
            session.execute(&format!("SELECT {name}({arguments}) FROM t"))?;
        }
        Ok(())
    }

    #[test]
    fn session_tables() -> Result<()> {
        let engine = setup(&["CREATE USER alice"]);
        let txn = engine.begin_read_only()?;
        let error = rows(&txn, "admin", None, "toydb_sessions").unwrap_err();
        let message = "invalid data: system table toydb_sessions requires a session";
        assert_eq!(error.to_string(), message);
        let (prepared, digests) = (PreparedStatements::new(), Digests::new());
        let state = SessionState {
            audit: engine.audit(),
            sessions: engine.sessions(),
            prepared: &prepared,
            digests: &digests,
        };
        let error = rows(&txn, "alice", Some(&state), "toydb_audit").unwrap_err();
        assert_eq!(error.to_string(), "permission denied: user alice is not a superuser");
        assert_eq!(rows(&txn, "admin", Some(&state), "toydb_audit")?.len(), 1);
        Ok(())
    }

    #[test]
    fn plan() -> Result<()> {
        let engine = setup(&[]);
        let txn = engine.begin_read_only()?;
        let statement = Parser::parse("SELECT name FROM toydb_users AS u WHERE u.is_superuser")?;
        let plan = crate::sql::planner::Plan::build(statement, &txn)?.optimize()?;
        assert_eq!(
            plan.to_string(),
            "Projection: u.name\n└─ Filter: u.is_superuser\n   └─ SystemTable: toydb_users as u"
        );
        Ok(())
    }
}
