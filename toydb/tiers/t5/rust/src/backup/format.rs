//! The backup file format, version 1, and its listing.
//!
//! The file is `TOYDBBAK`, a u16 version, records (a u8 kind, a u32 length,
//! the payload), an END record with the number of records, and the CRC-32 of
//! everything before it. Integers are big-endian; a string is a u32 length and
//! UTF-8.
//!
//! * A data type is written as its type tag: BOOLEAN 1, INTEGER 2, FLOAT 3,
//!   STRING 4. A data type added later gets the next unused tag, in the order
//!   the types are added.
//! * A value is written as NULL (tag 0) or as its type's tag and a payload:
//!   BOOLEAN one byte, INTEGER eight bytes, FLOAT its eight IEEE 754 bytes,
//!   STRING a string, a value of a data type added later its display in query
//!   results as a string.
//! * Records: USER, MEMBER, TABLE, CHECK, ROW, VIEW, MATVIEW, GRANT, POLICY,
//!   END, written in this order (tables after the tables they reference, each
//!   with its checks and rows; views and materialized views after those they
//!   depend on). A USER holds the stored password hash.
//!
//! Reading checks the header, the version and the CRC before the records; an
//! unknown record kind, type tag or value tag is an error.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fmt::Display;

use itertools::{EitherOrBoth, Itertools as _};

use crate::errinput;
use crate::error::Result;
use crate::sql::engine::changes::schema_line;
use crate::sql::types::{Check, Column, DataType, Policy, Privilege, Table, User, Value};

/// The bytes a backup starts with.
const MAGIC: &[u8; 8] = b"TOYDBBAK";

/// The format version.
pub const VERSION: u16 = 1;

/// The value tag of NULL.
const NULL_TAG: u8 = 0;

/// The record kind of the END record.
const END: u8 = 255;

/// All privileges.
const PRIVILEGES: [Privilege; 4] =
    [Privilege::Select, Privilege::Insert, Privilege::Update, Privilege::Delete];

/// A backup record.
#[derive(Clone, Debug, PartialEq)]
pub enum Record {
    /// A user.
    User(User),
    /// A membership of a user in another user.
    Member { member: String, user: String },
    /// A table definition and its owner.
    Table { table: Table, owner: String },
    /// A CHECK constraint of a table.
    Check { table: String, check: Check },
    /// A table row.
    Row { table: String, values: Vec<Value> },
    /// A view definition and its owner.
    View { name: String, owner: String, columns: Vec<String>, query: String },
    /// A materialized view definition and its owner.
    MaterializedView { name: String, owner: String, columns: Vec<String>, query: String },
    /// The privileges granted to a user on a table or view.
    Grant { object: String, grantee: String, privileges: BTreeSet<Privilege> },
    /// A row-level security policy.
    Policy(Policy),
}

impl Record {
    /// Returns the record kind.
    fn kind(&self) -> u8 {
        match self {
            Self::User(_) => 1,
            Self::Member { .. } => 2,
            Self::Table { .. } => 3,
            Self::Check { .. } => 4,
            Self::Row { .. } => 5,
            Self::View { .. } => 6,
            Self::Grant { .. } => 7,
            Self::Policy(_) => 8,
            Self::MaterializedView { .. } => 9,
        }
    }

    /// Writes the record's payload.
    fn write_payload(&self, buf: &mut Vec<u8>) {
        match self {
            Self::User(user) => {
                write_string(buf, &user.name);
                write_bool(buf, user.superuser);
                write_optional_string(buf, user.password.as_deref());
            }
            Self::Member { member, user } => {
                write_string(buf, member);
                write_string(buf, user);
            }
            Self::Table { table, owner } => {
                write_string(buf, &table.name);
                write_string(buf, owner);
                write_u32(buf, table.primary_key as u32);
                write_u32(buf, table.columns.len() as u32);
                for column in &table.columns {
                    write_string(buf, &column.name);
                    write_type(buf, column.datatype);
                    write_bool(buf, column.nullable);
                    write_bool(buf, column.default.is_some());
                    if let Some(default) = &column.default {
                        write_value(buf, default);
                    }
                    write_bool(buf, column.unique);
                    write_bool(buf, column.index);
                    write_optional_string(buf, column.references.as_deref());
                }
            }
            Self::Check { table, check } => {
                write_string(buf, table);
                write_string(buf, &check.name);
                write_string(buf, &check.condition);
            }
            Self::Row { table, values } => {
                write_string(buf, table);
                write_u32(buf, values.len() as u32);
                for value in values {
                    write_value(buf, value);
                }
            }
            Self::View { name, owner, columns, query }
            | Self::MaterializedView { name, owner, columns, query } => {
                write_string(buf, name);
                write_string(buf, owner);
                write_u32(buf, columns.len() as u32);
                for column in columns {
                    write_string(buf, column);
                }
                write_string(buf, query);
            }
            Self::Grant { object, grantee, privileges } => {
                write_string(buf, object);
                write_string(buf, grantee);
                buf.push(privileges.iter().map(|privilege| privilege_bit(*privilege)).sum());
            }
            Self::Policy(policy) => {
                write_string(buf, &policy.table);
                write_string(buf, &policy.name);
                write_optional_string(buf, policy.user.as_deref());
                write_string(buf, &policy.using);
                write_optional_string(buf, policy.with_check.as_deref());
            }
        }
    }

    /// Reads a record of the given kind from its payload.
    fn read_payload(kind: u8, input: &mut &[u8]) -> Result<Self> {
        Ok(match kind {
            1 => Self::User(User {
                name: read_string(input)?,
                superuser: read_bool(input)?,
                password: read_optional_string(input)?,
            }),
            2 => Self::Member { member: read_string(input)?, user: read_string(input)? },
            3 => {
                let name = read_string(input)?;
                let owner = read_string(input)?;
                let primary_key = read_u32(input)? as usize;
                let count = read_u32(input)?;
                let mut columns = Vec::new();
                for _ in 0..count {
                    let name = read_string(input)?;
                    let datatype = read_type(input)?;
                    let nullable = read_bool(input)?;
                    let default = match read_bool(input)? {
                        true => Some(read_value(input)?),
                        false => None,
                    };
                    columns.push(Column {
                        name,
                        datatype,
                        nullable,
                        default,
                        unique: read_bool(input)?,
                        index: read_bool(input)?,
                        references: read_optional_string(input)?,
                    });
                }
                Self::Table { table: Table { name, primary_key, columns }, owner }
            }
            4 => Self::Check {
                table: read_string(input)?,
                check: Check { name: read_string(input)?, condition: read_string(input)? },
            },
            5 => {
                let table = read_string(input)?;
                let count = read_u32(input)?;
                let values = (0..count).map(|_| read_value(input)).try_collect()?;
                Self::Row { table, values }
            }
            6 => {
                let (name, owner, columns, query) = read_view(input)?;
                Self::View { name, owner, columns, query }
            }
            7 => {
                let object = read_string(input)?;
                let grantee = read_string(input)?;
                let bits = read_u8(input)?;
                let privileges = PRIVILEGES
                    .into_iter()
                    .filter(|privilege| bits & privilege_bit(*privilege) != 0)
                    .collect();
                Self::Grant { object, grantee, privileges }
            }
            8 => Self::Policy(Policy {
                table: read_string(input)?,
                name: read_string(input)?,
                user: read_optional_string(input)?,
                using: read_string(input)?,
                with_check: read_optional_string(input)?,
            }),
            9 => {
                let (name, owner, columns, query) = read_view(input)?;
                Self::MaterializedView { name, owner, columns, query }
            }
            kind => return invalid(format!("unknown record kind {kind}")),
        })
    }

    /// Returns the record's identity, given the primary key column indexes of
    /// the backup's tables.
    fn identity(&self, primary_keys: &HashMap<String, usize>) -> Identity {
        match self {
            Self::User(user) => Identity::User(user.name.clone()),
            Self::Member { member, user } => Identity::Member(member.clone(), user.clone()),
            Self::Table { table, .. } => Identity::Table(table.name.clone()),
            Self::Check { table, check } => Identity::Check(table.clone(), check.name.clone()),
            Self::Row { table, values } => {
                let primary_key = primary_keys.get(table).copied().unwrap_or_default();
                let id = values.get(primary_key).cloned().unwrap_or(Value::Null);
                Identity::Row(table.clone(), id)
            }
            Self::View { name, .. } => Identity::View(name.clone()),
            Self::MaterializedView { name, .. } => Identity::MaterializedView(name.clone()),
            Self::Grant { object, grantee, .. } => Identity::Grant(object.clone(), grantee.clone()),
            Self::Policy(policy) => Identity::Policy(policy.table.clone(), policy.name.clone()),
        }
    }
}

/// Formats the record as its line of toybackup list.
impl Display for Record {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::User(user) => {
                write!(f, "user {}", user.name)?;
                if user.superuser {
                    write!(f, " superuser")?;
                }
                if user.password.is_some() {
                    write!(f, " password")?;
                }
                Ok(())
            }
            Self::Member { member, user } => write!(f, "member {member} of {user}"),
            Self::Table { table, owner } => {
                write!(f, "table {} owner {owner}: {}", table.name, schema_line(table))
            }
            Self::Check { table, check } => {
                write!(f, "check {table}.{}: {}", check.name, check.condition)
            }
            Self::Row { table, values } => write!(f, "row {table}: {}", values.iter().join(", ")),
            Self::View { name, owner, columns, query } => {
                write!(f, "view {name} owner {owner} ({}): {query}", columns.join(", "))
            }
            Self::MaterializedView { name, owner, columns, query } => {
                let columns = columns.join(", ");
                write!(f, "materialized view {name} owner {owner} ({columns}): {query}")
            }
            Self::Grant { object, grantee, privileges } => {
                write!(f, "grant {object} to {grantee}: {}", privileges.iter().join(", "))
            }
            Self::Policy(policy) => {
                write!(f, "policy {}.{}", policy.table, policy.name)?;
                if let Some(user) = &policy.user {
                    write!(f, " to {user}")?;
                }
                write!(f, ": {}", policy.using)?;
                if let Some(with_check) = &policy.with_check {
                    write!(f, " with check {with_check}")?;
                }
                Ok(())
            }
        }
    }
}

/// The identity of a record.
#[derive(Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Identity {
    User(String),
    Member(String, String),
    Table(String),
    Check(String, String),
    Row(String, Value),
    View(String),
    MaterializedView(String),
    Grant(String, String),
    Policy(String, String),
}

/// Writes a backup holding the given records.
pub fn write(records: &[Record]) -> Vec<u8> {
    let mut data = MAGIC.to_vec();
    data.extend(VERSION.to_be_bytes());
    for record in records {
        let mut payload = Vec::new();
        record.write_payload(&mut payload);
        write_record(&mut data, record.kind(), &payload);
    }
    write_record(&mut data, END, &(records.len() as u32).to_be_bytes());
    let checksum = crc32(&data);
    write_u32(&mut data, checksum);
    data
}

/// Reads the records of a backup, after checking the whole file.
pub fn read(data: &[u8]) -> Result<Vec<Record>> {
    let mut reader = Reader::new(data)?;
    let mut records = Vec::new();
    while let Some(record) = reader.next_record()? {
        records.push(record);
    }
    Ok(records)
}

/// Returns the lines of toybackup list: one per record, values displayed as in
/// query results.
pub fn list(data: &[u8]) -> Vec<String> {
    let mut lines = Vec::new();
    if let Err(error) = list_into(data, &mut lines) {
        lines.push(format!("Error: {error}"));
    }
    lines
}

/// Appends the lines of toybackup list, up to the first error.
fn list_into(data: &[u8], lines: &mut Vec<String>) -> Result<()> {
    check_header(data)?;
    lines.push(format!("toydb backup version {VERSION}"));
    let mut reader = Reader::new(data)?;
    while let Some(record) = reader.next_record()? {
        lines.push(record.to_string());
    }
    lines.push(format!("end {} records, checksum {:08x}", reader.count, reader.checksum));
    Ok(())
}

/// Returns the lines of toybackup diff: the records of each identity that is in
/// one backup only, or differs, by record kind and identity.
pub fn diff(a: &[u8], b: &[u8]) -> Result<Vec<String>> {
    let (a, b) = (identities(read(a)?), identities(read(b)?));
    let mut lines = Vec::new();
    for entry in a.into_iter().merge_join_by(b, |(a, _), (b, _)| a.cmp(b)) {
        match entry {
            EitherOrBoth::Left((_, a)) => lines.push(format!("- {a}")),
            EitherOrBoth::Right((_, b)) => lines.push(format!("+ {b}")),
            EitherOrBoth::Both((_, a), (_, b)) if a != b => {
                lines.push(format!("- {a}"));
                lines.push(format!("+ {b}"));
            }
            EitherOrBoth::Both(..) => {}
        }
    }
    Ok(lines)
}

/// Returns the records of a backup by identity.
fn identities(records: Vec<Record>) -> BTreeMap<Identity, Record> {
    let mut primary_keys = HashMap::new();
    let mut identities = BTreeMap::new();
    for record in records {
        if let Record::Table { table, .. } = &record {
            primary_keys.insert(table.name.clone(), table.primary_key);
        }
        identities.insert(record.identity(&primary_keys), record);
    }
    identities
}

/// Reads the records of a backup whose header, version and CRC are valid.
struct Reader<'a> {
    /// The remaining bytes of the records.
    input: &'a [u8],
    /// The number of records read so far.
    count: u32,
    /// The backup's CRC.
    checksum: u32,
}

impl<'a> Reader<'a> {
    /// Checks the header, the version and the CRC of a backup.
    fn new(data: &'a [u8]) -> Result<Self> {
        check_header(data)?;
        let (content, checksum) = data.split_at(data.len() - 4);
        let checksum = u32::from_be_bytes(checksum.try_into().expect("checksum is 4 bytes"));
        if crc32(content) != checksum {
            return invalid("checksum mismatch");
        }
        Ok(Self { input: &content[MAGIC.len() + 2..], count: 0, checksum })
    }

    /// Reads the next record, or returns None after the END record.
    fn next_record(&mut self) -> Result<Option<Record>> {
        let kind = read_u8(&mut self.input)?;
        let length = read_u32(&mut self.input)?;
        let mut payload = read_bytes(&mut self.input, length as usize)?;
        if kind == END {
            let count = read_u32(&mut payload)?;
            if !self.input.is_empty() {
                return invalid("data after end record");
            }
            if count != self.count {
                return invalid("wrong record count");
            }
            return Ok(None);
        }
        let record = Record::read_payload(kind, &mut payload)?;
        self.count += 1;
        Ok(Some(record))
    }
}

/// Checks the header and the version of a backup.
fn check_header(data: &[u8]) -> Result<()> {
    if data.len() < MAGIC.len() + 2 + 4 || !data.starts_with(MAGIC) {
        return invalid("not a toydb backup");
    }
    let version = u16::from_be_bytes([data[8], data[9]]);
    if version != VERSION {
        return invalid(format!("unsupported version {version}"));
    }
    Ok(())
}

/// Returns an invalid backup error.
fn invalid<T>(reason: impl Display) -> Result<T> {
    errinput!("invalid backup: {reason}")
}

/// Returns the type tag of a data type.
pub fn type_tag(datatype: DataType) -> u8 {
    match datatype {
        DataType::Boolean => 1,
        DataType::Integer => 2,
        DataType::Float => 3,
        DataType::String => 4,
    }
}

/// Returns the data type of a type tag, if any.
fn tag_type(tag: u8) -> Option<DataType> {
    match tag {
        1 => Some(DataType::Boolean),
        2 => Some(DataType::Integer),
        3 => Some(DataType::Float),
        4 => Some(DataType::String),
        _ => None,
    }
}

/// Returns the bit of a privilege in a GRANT record.
fn privilege_bit(privilege: Privilege) -> u8 {
    match privilege {
        Privilege::Select => 1,
        Privilege::Insert => 2,
        Privilege::Update => 4,
        Privilege::Delete => 8,
    }
}

/// Writes the type tag of a data type.
pub fn write_type(buf: &mut Vec<u8>, datatype: DataType) {
    buf.push(type_tag(datatype));
}

/// Writes a value: its type tag and payload, or the NULL tag.
pub fn write_value(buf: &mut Vec<u8>, value: &Value) {
    match value {
        Value::Null => buf.push(NULL_TAG),
        Value::Boolean(boolean) => {
            write_type(buf, DataType::Boolean);
            write_bool(buf, *boolean);
        }
        Value::Integer(integer) => {
            write_type(buf, DataType::Integer);
            buf.extend(integer.to_be_bytes());
        }
        Value::Float(float) => {
            write_type(buf, DataType::Float);
            buf.extend(float.to_be_bytes());
        }
        Value::String(string) => {
            write_type(buf, DataType::String);
            write_string(buf, string);
        }
    }
}

/// Reads a type tag, erroring on an unknown tag.
pub fn read_type(input: &mut &[u8]) -> Result<DataType> {
    let tag = read_u8(input)?;
    match tag_type(tag) {
        Some(datatype) => Ok(datatype),
        None => invalid(format!("unknown type tag {tag}")),
    }
}

/// Reads a value, erroring on an unknown tag.
pub fn read_value(input: &mut &[u8]) -> Result<Value> {
    let tag = read_u8(input)?;
    if tag == NULL_TAG {
        return Ok(Value::Null);
    }
    let Some(datatype) = tag_type(tag) else {
        return invalid(format!("unknown value tag {tag}"));
    };
    Ok(match datatype {
        DataType::Boolean => Value::Boolean(read_bool(input)?),
        DataType::Integer => Value::Integer(i64::from_be_bytes(read_array(input)?)),
        DataType::Float => Value::Float(f64::from_be_bytes(read_array(input)?)),
        DataType::String => Value::String(read_string(input)?),
    })
}

/// Writes a record: its kind, payload length and payload.
fn write_record(buf: &mut Vec<u8>, kind: u8, payload: &[u8]) {
    buf.push(kind);
    write_u32(buf, payload.len() as u32);
    buf.extend(payload);
}

/// Writes a u32.
fn write_u32(buf: &mut Vec<u8>, value: u32) {
    buf.extend(value.to_be_bytes());
}

/// Writes a bool.
fn write_bool(buf: &mut Vec<u8>, value: bool) {
    buf.push(value as u8);
}

/// Writes a string.
fn write_string(buf: &mut Vec<u8>, string: &str) {
    write_u32(buf, string.len() as u32);
    buf.extend(string.as_bytes());
}

/// Writes an optional string.
fn write_optional_string(buf: &mut Vec<u8>, string: Option<&str>) {
    write_bool(buf, string.is_some());
    if let Some(string) = string {
        write_string(buf, string);
    }
}

/// Reads the given number of bytes.
fn read_bytes<'a>(input: &mut &'a [u8], length: usize) -> Result<&'a [u8]> {
    if input.len() < length {
        return invalid("truncated");
    }
    let (bytes, rest) = input.split_at(length);
    *input = rest;
    Ok(bytes)
}

/// Reads a fixed number of bytes.
fn read_array<const N: usize>(input: &mut &[u8]) -> Result<[u8; N]> {
    Ok(read_bytes(input, N)?.try_into().expect("read N bytes"))
}

/// Reads a u8.
fn read_u8(input: &mut &[u8]) -> Result<u8> {
    Ok(u8::from_be_bytes(read_array(input)?))
}

/// Reads a u32.
fn read_u32(input: &mut &[u8]) -> Result<u32> {
    Ok(u32::from_be_bytes(read_array(input)?))
}

/// Reads a bool.
fn read_bool(input: &mut &[u8]) -> Result<bool> {
    Ok(read_u8(input)? != 0)
}

/// Reads a string.
fn read_string(input: &mut &[u8]) -> Result<String> {
    let length = read_u32(input)? as usize;
    match std::str::from_utf8(read_bytes(input, length)?) {
        Ok(string) => Ok(string.to_string()),
        Err(_) => invalid("invalid string"),
    }
}

/// Reads an optional string.
fn read_optional_string(input: &mut &[u8]) -> Result<Option<String>> {
    match read_bool(input)? {
        true => Ok(Some(read_string(input)?)),
        false => Ok(None),
    }
}

/// Reads the name, owner, column names and query of a VIEW or MATVIEW record.
fn read_view(input: &mut &[u8]) -> Result<(String, String, Vec<String>, String)> {
    let name = read_string(input)?;
    let owner = read_string(input)?;
    let count = read_u32(input)?;
    let columns = (0..count).map(|_| read_string(input)).try_collect()?;
    Ok((name, owner, columns, read_string(input)?))
}

/// Computes the CRC-32 of data.
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xffff_ffff_u32;
    for byte in data {
        crc ^= *byte as u32;
        for _ in 0..8 {
            crc = match crc & 1 {
                1 => (crc >> 1) ^ 0xedb8_8320,
                _ => crc >> 1,
            };
        }
    }
    !crc
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;

    /// Returns a table t with an INTEGER primary key and a nullable STRING
    /// column v referencing u.
    fn table() -> Table {
        let column = |name: &str, datatype, nullable, default| Column {
            name: name.to_string(),
            datatype,
            nullable,
            default,
            unique: !nullable,
            index: false,
            references: None,
        };
        let mut v = column("v", DataType::String, true, Some(Value::Null));
        v.references = Some("u".into());
        Table {
            name: "t".into(),
            primary_key: 0,
            columns: vec![column("id", DataType::Integer, false, None), v],
        }
    }

    /// Returns one record of every kind.
    fn records() -> Vec<Record> {
        vec![
            Record::User(User {
                name: "alice".into(),
                superuser: true,
                password: Some("md5".into()),
            }),
            Record::Member { member: "alice".into(), user: "staff".into() },
            Record::Table { table: table(), owner: "alice".into() },
            Record::Check {
                table: "t".into(),
                check: Check { name: "c".into(), condition: "id > 0".into() },
            },
            Record::Row { table: "t".into(), values: vec![Value::Integer(1), Value::Null] },
            Record::View {
                name: "v".into(),
                owner: "admin".into(),
                columns: vec!["a".into(), "b".into()],
                query: "SELECT id, v FROM t".into(),
            },
            Record::MaterializedView {
                name: "m".into(),
                owner: "alice".into(),
                columns: vec!["a".into()],
                query: "SELECT a FROM v".into(),
            },
            Record::Grant {
                object: "t".into(),
                grantee: "staff".into(),
                privileges: [Privilege::Delete, Privilege::Select].into(),
            },
            Record::Policy(Policy {
                table: "t".into(),
                name: "p".into(),
                user: None,
                using: "TRUE".into(),
                with_check: Some("id = 1".into()),
            }),
        ]
    }

    /// Writes a backup with the given record bytes and END count, with a valid
    /// CRC.
    fn backup(records: &[u8], count: u32) -> Vec<u8> {
        let mut data = MAGIC.to_vec();
        data.extend(VERSION.to_be_bytes());
        data.extend(records);
        write_record(&mut data, END, &count.to_be_bytes());
        let checksum = crc32(&data);
        write_u32(&mut data, checksum);
        data
    }

    /// Returns the error message of reading a backup.
    fn read_error(data: &[u8]) -> String {
        read(data).expect_err("backup is valid").to_string()
    }

    #[test]
    fn crc32_check_value() {
        assert_eq!(crc32(b""), 0);
        assert_eq!(crc32(b"123456789"), 0xcbf43926);
    }

    #[test]
    fn empty() {
        let data = write(&[]);
        assert_eq!(hex::encode(&data), "544f59444242414b0001ff0000000400000000407b665b");
        assert_eq!(read(&data).unwrap(), Vec::new());
        assert_eq!(list(&data), vec!["toydb backup version 1", "end 0 records, checksum 407b665b"]);
    }

    #[test]
    fn roundtrip() {
        let records = records();
        let data = write(&records);
        assert_eq!(read(&data).unwrap(), records);
    }

    #[test_case(Value::Null => "00"; "null")]
    #[test_case(Value::Boolean(true) => "0101"; "boolean")]
    #[test_case(Value::Integer(-2) => "02fffffffffffffffe"; "integer")]
    #[test_case(Value::Float(1.5) => "033ff8000000000000"; "float")]
    #[test_case(Value::Float(f64::NAN) => "037ff8000000000000"; "float nan")]
    #[test_case(Value::String("é".into()) => "0400000002c3a9"; "string")]
    fn value(value: Value) -> String {
        let mut buf = Vec::new();
        write_value(&mut buf, &value);
        let mut input = buf.as_slice();
        assert_eq!(read_value(&mut input).unwrap(), value);
        assert!(input.is_empty());
        hex::encode(buf)
    }

    #[test]
    fn datatype() {
        for (datatype, tag) in [
            (DataType::Boolean, 1),
            (DataType::Integer, 2),
            (DataType::Float, 3),
            (DataType::String, 4),
        ] {
            let mut buf = Vec::new();
            write_type(&mut buf, datatype);
            assert_eq!(buf, vec![tag]);
            assert_eq!(read_type(&mut buf.as_slice()).unwrap(), datatype);
        }
        let error = read_type(&mut [5].as_slice()).unwrap_err().to_string();
        assert_eq!(error, "invalid input: invalid backup: unknown type tag 5");
        let error = read_value(&mut [9].as_slice()).unwrap_err().to_string();
        assert_eq!(error, "invalid input: invalid backup: unknown value tag 9");
        let error = read_value(&mut [2, 0, 0].as_slice()).unwrap_err().to_string();
        assert_eq!(error, "invalid input: invalid backup: truncated");
    }

    #[test]
    fn header_errors() {
        let data = write(&records());
        assert_eq!(read_error(&data[..13]), "invalid input: invalid backup: not a toydb backup");
        let mut magic = data.clone();
        magic[0] = b'X';
        assert_eq!(read_error(&magic), "invalid input: invalid backup: not a toydb backup");
        let mut version = data.clone();
        version[9] = 2;
        assert_eq!(read_error(&version), "invalid input: invalid backup: unsupported version 2");
        let mut crc = data.clone();
        crc[20] ^= 1;
        assert_eq!(read_error(&crc), "invalid input: invalid backup: checksum mismatch");
        assert_eq!(read_error(&data[..20]), "invalid input: invalid backup: checksum mismatch");
    }

    #[test]
    fn record_errors() {
        let mut user = Vec::new();
        write_record(&mut user, 1, &[0, 0, 0, 1, b'a', 0, 0]);
        let error = |records: &[u8], count| read_error(&backup(records, count));

        assert_eq!(error(&[1, 0, 0, 0, 9, 0], 0), "invalid input: invalid backup: truncated");
        assert_eq!(error(&[1, 0, 0, 0, 2, 0, 0], 1), "invalid input: invalid backup: truncated");
        assert_eq!(
            error(&[42, 0, 0, 0, 0], 1),
            "invalid input: invalid backup: unknown record kind 42"
        );
        assert_eq!(
            error(&[1, 0, 0, 0, 6, 0, 0, 0, 1, 0xff, 0], 1),
            "invalid input: invalid backup: invalid string"
        );
        assert_eq!(error(&user, 2), "invalid input: invalid backup: wrong record count");
        assert_eq!(read(&backup(&user, 1)).unwrap().len(), 1);

        // Bytes after the END record, and no END record.
        let mut data = backup(&user, 1);
        data.truncate(data.len() - 4);
        data.push(0);
        let checksum = crc32(&data);
        write_u32(&mut data, checksum);
        assert_eq!(read_error(&data), "invalid input: invalid backup: data after end record");
        let mut data = MAGIC.to_vec();
        data.extend(VERSION.to_be_bytes());
        data.extend(&user);
        let checksum = crc32(&data);
        write_u32(&mut data, checksum);
        assert_eq!(read_error(&data), "invalid input: invalid backup: truncated");
    }

    #[test]
    fn list_lines() {
        let data = write(&records());
        let checksum = u32::from_be_bytes(data[data.len() - 4..].try_into().unwrap());
        assert_eq!(
            list(&data),
            vec![
                "toydb backup version 1".to_string(),
                "user alice superuser password".to_string(),
                "member alice of staff".to_string(),
                "table t owner alice: CREATE TABLE t (id INTEGER PRIMARY KEY, \
                 v STRING DEFAULT NULL REFERENCES u)"
                    .to_string(),
                "check t.c: id > 0".to_string(),
                "row t: 1, NULL".to_string(),
                "view v owner admin (a, b): SELECT id, v FROM t".to_string(),
                "materialized view m owner alice (a): SELECT a FROM v".to_string(),
                "grant t to staff: SELECT, DELETE".to_string(),
                "policy t.p: TRUE with check id = 1".to_string(),
                format!("end 9 records, checksum {checksum:08x}"),
            ]
        );

        // An invalid record ends the listing with the error, after the records
        // before it. A bad header is the only line.
        let mut records = Vec::new();
        write_record(&mut records, 1, &[0, 0, 0, 1, b'a', 0, 0]);
        write_record(&mut records, 7, &[]);
        assert_eq!(
            list(&backup(&records, 2)),
            vec![
                "toydb backup version 1",
                "user a",
                "Error: invalid input: invalid backup: truncated"
            ]
        );
        assert_eq!(list(b"x"), vec!["Error: invalid input: invalid backup: not a toydb backup"]);
    }

    #[test]
    fn diff_lines() {
        let a = records();
        let mut b = records();
        b[0] = Record::User(User { name: "bob".into(), superuser: false, password: None });
        b[4] = Record::Row { table: "t".into(), values: vec![Value::Integer(1), "x".into()] };
        let row = Record::Row { table: "t".into(), values: vec![Value::Integer(0), Value::Null] };
        b.insert(5, row);
        b[7] = Record::MaterializedView {
            name: "m".into(),
            owner: "admin".into(),
            columns: vec!["a".into()],
            query: "SELECT a FROM v".into(),
        };
        b.pop();
        assert_eq!(
            diff(&write(&a), &write(&b)).unwrap(),
            vec![
                "- user alice superuser password",
                "+ user bob",
                "+ row t: 0, NULL",
                "- row t: 1, NULL",
                "+ row t: 1, 'x'",
                "- materialized view m owner alice (a): SELECT a FROM v",
                "+ materialized view m owner admin (a): SELECT a FROM v",
                "- policy t.p: TRUE with check id = 1",
            ]
        );
        assert_eq!(diff(&write(&a), &write(&a)).unwrap(), Vec::<String>::new());
        let error = diff(&write(&a), b"x").unwrap_err().to_string();
        assert_eq!(error, "invalid input: invalid backup: not a toydb backup");
    }
}
