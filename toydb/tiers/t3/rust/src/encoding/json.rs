//! A minimal JSON encoder, used for toyDB's JSON output (see `sql::json`).
//!
//! JSON values are built as a `Json` tree and displayed as compact JSON text,
//! with object members in insertion order. Numbers are given as text by the
//! caller, which controls their formatting (e.g. SQL floats are written as
//! they're displayed in query results).
//!
//! Parsing JSON (e.g. for IMPORT) uses `serde_json` instead.

use std::fmt::{Display, Write as _};

use itertools::Itertools as _;

/// A JSON value.
#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    /// null.
    Null,
    /// true or false.
    Bool(bool),
    /// A number, as JSON number text, e.g. 1, -0.5 or 1e100. The text must be
    /// a valid JSON number, which is not checked.
    Number(String),
    /// A string. It's escaped when displayed.
    String(String),
    /// An array of values.
    Array(Vec<Json>),
    /// An object, with members in the given order.
    Object(Vec<(String, Json)>),
}

impl Json {
    /// Creates an object from (name, value) pairs, in the given order.
    pub fn object<'a>(members: impl IntoIterator<Item = (&'a str, Json)>) -> Self {
        Self::Object(members.into_iter().map(|(name, value)| (name.to_string(), value)).collect())
    }

    /// Creates an array from an iterator of values.
    pub fn array<T: Into<Json>>(values: impl IntoIterator<Item = T>) -> Self {
        Self::Array(values.into_iter().map(Into::into).collect())
    }
}

/// Displays the value as compact JSON text, without any whitespace outside of
/// strings.
impl Display for Json {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Null => f.write_str("null"),
            Self::Bool(true) => f.write_str("true"),
            Self::Bool(false) => f.write_str("false"),
            Self::Number(number) => f.write_str(number),
            Self::String(string) => write_string(f, string),
            Self::Array(values) => write!(f, "[{}]", values.iter().join(",")),
            Self::Object(members) => {
                f.write_char('{')?;
                for (i, (name, value)) in members.iter().enumerate() {
                    if i > 0 {
                        f.write_char(',')?;
                    }
                    write_string(f, name)?;
                    write!(f, ":{value}")?;
                }
                f.write_char('}')
            }
        }
    }
}

/// Writes a JSON string literal. Only ", \ and control characters are
/// escaped; all other characters (including / and non-ASCII characters) are
/// written as is.
fn write_string(f: &mut std::fmt::Formatter<'_>, string: &str) -> std::fmt::Result {
    f.write_char('"')?;
    for c in string.chars() {
        match c {
            '"' => f.write_str("\\\"")?,
            '\\' => f.write_str("\\\\")?,
            '\u{8}' => f.write_str("\\b")?,
            '\u{c}' => f.write_str("\\f")?,
            '\n' => f.write_str("\\n")?,
            '\r' => f.write_str("\\r")?,
            '\t' => f.write_str("\\t")?,
            c if c < '\u{20}' => write!(f, "\\u{:04x}", c as u32)?,
            c => f.write_char(c)?,
        }
    }
    f.write_char('"')
}

impl From<bool> for Json {
    fn from(value: bool) -> Self {
        Self::Bool(value)
    }
}

impl From<i64> for Json {
    fn from(value: i64) -> Self {
        Self::Number(value.to_string())
    }
}

impl From<u64> for Json {
    fn from(value: u64) -> Self {
        Self::Number(value.to_string())
    }
}

impl From<usize> for Json {
    fn from(value: usize) -> Self {
        Self::Number(value.to_string())
    }
}

impl From<&str> for Json {
    fn from(value: &str) -> Self {
        Self::String(value.to_string())
    }
}

impl From<String> for Json {
    fn from(value: String) -> Self {
        Self::String(value)
    }
}

impl From<&String> for Json {
    fn from(value: &String) -> Self {
        Self::String(value.clone())
    }
}

impl<T: Into<Json>> From<Option<T>> for Json {
    fn from(value: Option<T>) -> Self {
        value.map_or(Self::Null, Into::into)
    }
}

impl From<Vec<Json>> for Json {
    fn from(values: Vec<Json>) -> Self {
        Self::Array(values)
    }
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;

    #[test_case(Json::Null => "null"; "null")]
    #[test_case(true.into() => "true"; "bool true")]
    #[test_case(false.into() => "false"; "bool false")]
    #[test_case(i64::MIN.into() => "-9223372036854775808"; "integer")]
    #[test_case(Json::Number("1e100".into()) => "1e100"; "number")]
    #[test_case("plain".into() => r#""plain""#; "string")]
    #[test_case(r#"a"b\c"#.into() => r#""a\"b\\c""#; "string quote backslash")]
    #[test_case("\u{8}\u{c}\n\r\t".into() => r#""\b\f\n\r\t""#; "string short escapes")]
    #[test_case("\u{1}\u{1b}\u{1f}".into() => r#""\u0001\u001b\u001f""#; "string control")]
    #[test_case("/ \u{7f} é ☃ 👋".into() => "\"/ \u{7f} é ☃ 👋\""; "string unescaped")]
    #[test_case(Json::array(Vec::<Json>::new()) => "[]"; "array empty")]
    #[test_case(Json::array([Json::Null, 1i64.into(), "x".into()]) => r#"[null,1,"x"]"#; "array")]
    #[test_case(Json::object([]) => "{}"; "object empty")]
    #[test_case(Json::object([("b", true.into()), ("a", Json::array([Json::Null]))]) => r#"{"b":true,"a":[null]}"#; "object order")]
    #[test_case(Json::object([("\"k\"", Json::object([("n", None::<i64>.into())]))]) => r#"{"\"k\"":{"n":null}}"#; "object nested")]
    fn display(json: Json) -> String {
        json.to_string()
    }
}
