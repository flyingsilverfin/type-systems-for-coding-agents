//! PostgreSQL types of toydb values, and their text and binary formats.
//!
//! | toydb data type | PostgreSQL type | OID | size |
//! |---|---|---|---|
//! | BOOLEAN | `bool` | 16 | 1 |
//! | INTEGER | `int8` | 20 | 8 |
//! | FLOAT | `float8` | 701 | 8 |
//! | STRING | `text` | 25 | -1 |
//! | every data type added later, and an unknown type | `text` | 25 | -1 |
//!
//! A value is sent in its own type's format: BOOLEAN as `t`/`f` (binary: one
//! byte), INTEGER in decimal (binary: 8 bytes), FLOAT as displayed in query
//! results except `NaN`, `Infinity`, `-Infinity` (binary: 8 bytes IEEE 754),
//! STRING as itself (binary: UTF-8), a value of every data type added later as
//! displayed in query results (binary: the UTF-8 of that text).
//!
//! Parameter OIDs map to data types: 16 BOOLEAN; 20, 21, 23 INTEGER; 700, 701
//! FLOAT; 25, 1043 STRING; other OIDs are not supported. Parameter values are
//! read by their declared OID: `t`, `true`, `f`, `false` in any case; integers
//! with an optional sign within the range of the type; floats as IMPORT reads
//! them, or `NaN`, `Infinity`, `-Infinity`, `inf`, `-inf` in any case; text as
//! itself. Binary parameters have the sizes of their types.

use super::PgError;
use crate::sql::execution::parse_value;
use crate::sql::types::{DataType, Value};

/// A PostgreSQL type: its name, OID and size (-1 if variable).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Type {
    pub name: &'static str,
    pub oid: u32,
    pub size: i16,
}

pub const BOOL: Type = Type { name: "bool", oid: 16, size: 1 };
pub const INT2: Type = Type { name: "int2", oid: 21, size: 2 };
pub const INT4: Type = Type { name: "int4", oid: 23, size: 4 };
pub const INT8: Type = Type { name: "int8", oid: 20, size: 8 };
pub const FLOAT4: Type = Type { name: "float4", oid: 700, size: 4 };
pub const FLOAT8: Type = Type { name: "float8", oid: 701, size: 8 };
pub const TEXT: Type = Type { name: "text", oid: 25, size: -1 };
pub const VARCHAR: Type = Type { name: "varchar", oid: 1043, size: -1 };

/// The PostgreSQL types that toydb uses.
pub const TYPES: [Type; 8] = [BOOL, INT2, INT4, INT8, FLOAT4, FLOAT8, TEXT, VARCHAR];

impl Type {
    /// Returns the type with the given OID, if toydb uses it.
    pub fn from_oid(oid: u32) -> Option<Type> {
        TYPES.into_iter().find(|t| t.oid == oid)
    }
}

/// The format of a value: text or binary.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Format {
    #[default]
    Text,
    Binary,
}

impl Format {
    /// Returns the format of a format code.
    pub fn from_code(code: i16) -> Result<Self, PgError> {
        match code {
            0 => Ok(Self::Text),
            1 => Ok(Self::Binary),
            code => Err(PgError::new("22023", format!("unsupported format code: {code}"))),
        }
    }

    /// Returns the format code.
    pub fn code(self) -> i16 {
        match self {
            Self::Text => 0,
            Self::Binary => 1,
        }
    }
}

/// Returns the PostgreSQL type that a data type is sent as, text for an unknown
/// type.
pub fn of_datatype(datatype: Option<DataType>) -> Type {
    match datatype {
        Some(DataType::Boolean) => BOOL,
        Some(DataType::Integer) => INT8,
        Some(DataType::Float) => FLOAT8,
        Some(DataType::String) | None => TEXT,
    }
}

/// Encodes a value in the given format.
pub fn encode(value: &Value, format: Format) -> Option<Vec<u8>> {
    match format {
        Format::Text => encode_text(value).map(String::into_bytes),
        Format::Binary => encode_binary(value),
    }
}

/// Encodes a value in the text format.
pub fn encode_text(value: &Value) -> Option<String> {
    Some(match value {
        Value::Null => return None,
        Value::Boolean(true) => "t".to_string(),
        Value::Boolean(false) => "f".to_string(),
        Value::Integer(integer) => integer.to_string(),
        Value::Float(float) if float.is_nan() => "NaN".to_string(),
        Value::Float(float) if float.is_infinite() && *float > 0.0 => "Infinity".to_string(),
        Value::Float(float) if float.is_infinite() => "-Infinity".to_string(),
        Value::Float(_) => value.to_string(),
        Value::String(string) => string.clone(),
    })
}

/// Encodes a value in the binary format.
pub fn encode_binary(value: &Value) -> Option<Vec<u8>> {
    Some(match value {
        Value::Null => return None,
        Value::Boolean(boolean) => vec![*boolean as u8],
        Value::Integer(integer) => integer.to_be_bytes().to_vec(),
        Value::Float(float) => float.to_be_bytes().to_vec(),
        Value::String(string) => string.as_bytes().to_vec(),
    })
}

/// Returns the data type of a parameter OID, or an error if it isn't
/// supported.
pub fn parameter_datatype(oid: u32) -> Result<DataType, PgError> {
    match oid {
        16 => Ok(DataType::Boolean),
        20 | 21 | 23 => Ok(DataType::Integer),
        700 | 701 => Ok(DataType::Float),
        25 | 1043 => Ok(DataType::String),
        oid => Err(unsupported(oid)),
    }
}

/// Returns the error for an unsupported parameter OID.
fn unsupported(oid: u32) -> PgError {
    PgError::new("0A000", format!("unsupported parameter type {oid}"))
}

/// Returns the error for text that isn't UTF-8.
pub fn invalid_encoding() -> PgError {
    PgError::new("22021", "invalid byte sequence for encoding UTF8")
}

/// Decodes a parameter value of the given OID and format.
pub fn decode_parameter(
    oid: u32,
    format: Format,
    data: &[u8],
    number: usize,
) -> Result<Value, PgError> {
    let Some(pgtype) = Type::from_oid(oid) else {
        return Err(unsupported(oid));
    };
    match format {
        Format::Text => match std::str::from_utf8(data) {
            Ok(text) => decode_text(pgtype, text),
            Err(_) => Err(invalid_encoding()),
        },
        Format::Binary => decode_binary(pgtype, data).ok_or_else(|| {
            let message = format!("incorrect binary data format in bind parameter {number}");
            PgError::new("22P03", message)
        }),
    }
}

/// Decodes a value of the given type from the text format.
pub fn decode_text(pgtype: Type, text: &str) -> Result<Value, PgError> {
    let name = pgtype.name;
    let invalid =
        || PgError::new("22P02", format!("invalid input syntax for type {name}: \"{text}\""));
    let out_of_range =
        || PgError::new("22003", format!("value \"{text}\" is out of range for type {name}"));
    match pgtype.oid {
        16 => match text.to_lowercase().as_str() {
            "t" | "true" => Ok(Value::Boolean(true)),
            "f" | "false" => Ok(Value::Boolean(false)),
            _ => Err(invalid()),
        },
        20 | 21 | 23 => {
            let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                return Err(invalid());
            }
            // The text is a valid integer, so parsing can only fail by overflow.
            let integer: i64 = text.parse().map_err(|_| out_of_range())?;
            let in_range = match pgtype.oid {
                21 => i16::try_from(integer).is_ok(),
                23 => i32::try_from(integer).is_ok(),
                _ => true,
            };
            match in_range {
                true => Ok(Value::Integer(integer)),
                false => Err(out_of_range()),
            }
        }
        700 | 701 => match text.to_lowercase().as_str() {
            "nan" => Ok(Value::Float(f64::NAN)),
            "infinity" | "inf" => Ok(Value::Float(f64::INFINITY)),
            "-infinity" | "-inf" => Ok(Value::Float(f64::NEG_INFINITY)),
            _ => parse_value(text, DataType::Float).ok_or_else(invalid),
        },
        25 | 1043 => Ok(Value::String(text.to_string())),
        oid => Err(unsupported(oid)),
    }
}

/// Decodes a value of the given type from the binary format, if it's valid.
fn decode_binary(pgtype: Type, data: &[u8]) -> Option<Value> {
    Some(match pgtype.oid {
        16 => match data {
            [0] => Value::Boolean(false),
            [1] => Value::Boolean(true),
            _ => return None,
        },
        21 => Value::Integer(i16::from_be_bytes(data.try_into().ok()?).into()),
        23 => Value::Integer(i32::from_be_bytes(data.try_into().ok()?).into()),
        20 => Value::Integer(i64::from_be_bytes(data.try_into().ok()?)),
        700 => Value::Float(f32::from_be_bytes(data.try_into().ok()?).into()),
        701 => Value::Float(f64::from_be_bytes(data.try_into().ok()?)),
        25 | 1043 => Value::String(String::from_utf8(data.to_vec()).ok()?),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;

    #[test_case(Value::Null => None; "null")]
    #[test_case(Value::Boolean(true) => Some("t".to_string()); "boolean")]
    #[test_case(Value::Integer(-7) => Some("-7".to_string()); "integer")]
    #[test_case(Value::Float(1.0) => Some("1.0".to_string()); "float")]
    #[test_case(Value::Float(-0.0) => Some("-0.0".to_string()); "negative zero")]
    #[test_case(Value::Float(1e100) => Some("1e100".to_string()); "exponent")]
    #[test_case(Value::Float(f64::NAN) => Some("NaN".to_string()); "nan")]
    #[test_case(Value::Float(f64::INFINITY) => Some("Infinity".to_string()); "infinity")]
    #[test_case(Value::Float(f64::NEG_INFINITY) => Some("-Infinity".to_string()); "neg infinity")]
    #[test_case(Value::String("it's".into()) => Some("it's".to_string()); "string")]
    fn text(value: Value) -> Option<String> {
        encode_text(&value)
    }

    #[test_case(Value::Boolean(false) => Some(vec![0]); "boolean")]
    #[test_case(Value::Integer(1) => Some(vec![0, 0, 0, 0, 0, 0, 0, 1]); "integer")]
    #[test_case(Value::Float(1.5) => Some(vec![0x3f, 0xf8, 0, 0, 0, 0, 0, 0]); "float")]
    #[test_case(Value::String("hi".into()) => Some(b"hi".to_vec()); "string")]
    #[test_case(Value::Null => None; "null")]
    fn binary(value: Value) -> Option<Vec<u8>> {
        encode_binary(&value)
    }

    #[test]
    fn datatypes() {
        assert_eq!(of_datatype(Some(DataType::Boolean)), BOOL);
        assert_eq!(of_datatype(Some(DataType::Integer)), INT8);
        assert_eq!(of_datatype(Some(DataType::Float)), FLOAT8);
        assert_eq!(of_datatype(Some(DataType::String)), TEXT);
        assert_eq!(of_datatype(None), TEXT);
        assert_eq!(Type::from_oid(1043), Some(VARCHAR));
        assert_eq!(Type::from_oid(114), None);
        assert_eq!(parameter_datatype(21), Ok(DataType::Integer));
        assert_eq!(
            parameter_datatype(114),
            Err(PgError::new("0A000", "unsupported parameter type 114"))
        );
    }

    #[test_case(16, "TRUE" => Ok(Value::Boolean(true)); "bool upper")]
    #[test_case(16, "f" => Ok(Value::Boolean(false)); "bool short")]
    #[test_case(16, "yes" => Err("22P02 invalid input syntax for type bool: \"yes\"".into()); "bool invalid")]
    #[test_case(21, "-32768" => Ok(Value::Integer(-32768)); "int2 min")]
    #[test_case(21, "32768" => Err("22003 value \"32768\" is out of range for type int2".into()); "int2 range")]
    #[test_case(23, "+7" => Ok(Value::Integer(7)); "int4 plus")]
    #[test_case(20, "9223372036854775808" => Err("22003 value \"9223372036854775808\" is out of range for type int8".into()); "int8 range")]
    #[test_case(20, "1.0" => Err("22P02 invalid input syntax for type int8: \"1.0\"".into()); "int8 fraction")]
    #[test_case(20, "-" => Err("22P02 invalid input syntax for type int8: \"-\"".into()); "int8 sign")]
    #[test_case(701, "2.5e-3" => Ok(Value::Float(0.0025)); "float8")]
    #[test_case(701, "-INF" => Ok(Value::Float(f64::NEG_INFINITY)); "float8 inf")]
    #[test_case(700, ".5" => Err("22P02 invalid input syntax for type float4: \".5\"".into()); "float4 invalid")]
    #[test_case(1043, "a b" => Ok(Value::String("a b".into())); "varchar")]
    fn decode_text_parameter(oid: u32, text: &str) -> Result<Value, String> {
        decode_parameter(oid, Format::Text, text.as_bytes(), 1)
            .map_err(|error| format!("{} {}", error.code, error.message))
    }

    #[test_case(16, &[1] => Ok(Value::Boolean(true)); "bool")]
    #[test_case(16, &[2] => Err(()); "bool invalid")]
    #[test_case(21, &[1, 2] => Ok(Value::Integer(258)); "int2")]
    #[test_case(23, &[0xff, 0xff, 0xff, 0xfe] => Ok(Value::Integer(-2)); "int4")]
    #[test_case(20, &[1] => Err(()); "int8 short")]
    #[test_case(700, &[0x3f, 0xc0, 0, 0] => Ok(Value::Float(1.5)); "float4")]
    #[test_case(25, &[0xff] => Err(()); "text invalid")]
    fn decode_binary_parameter(oid: u32, data: &[u8]) -> Result<Value, ()> {
        decode_parameter(oid, Format::Binary, data, 2).map_err(|error| {
            assert_eq!(error.code, "22P03");
            assert_eq!(error.message, "incorrect binary data format in bind parameter 2");
        })
    }
}
