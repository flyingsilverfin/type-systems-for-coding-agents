//! The messages of the PostgreSQL protocol: decoding of frontend messages and
//! encoding of backend messages.
//!
//! A message is a type byte, a 4-byte big-endian length that counts itself, and
//! the body; the startup-phase messages have no type byte. Integers are
//! big-endian and strings are zero-terminated UTF-8.

use std::io::{ErrorKind, Read};

use super::PgError;
use super::types::Format;
use crate::errdata;
use crate::error::Result;

/// The protocol version 3.0.
pub const PROTOCOL_VERSION: u32 = 3 << 16;

/// The startup-phase request codes, in place of a protocol version.
const SSL_REQUEST: u32 = 80877103;
const GSSENC_REQUEST: u32 = 80877104;
const CANCEL_REQUEST: u32 = 80877102;

/// A message from the client.
#[derive(Clone, Debug, PartialEq)]
pub enum FrontendMessage {
    /// Asks for an SSL connection.
    SslRequest,
    /// Asks for a GSSAPI-encrypted connection.
    GssEncRequest,
    /// Asks to cancel the query of another connection.
    CancelRequest { process: i32, secret: i32 },
    /// Starts the connection with a protocol version and parameters.
    Startup { version: u32, parameters: Vec<(String, String)> },
    /// A password response.
    Password(String),
    /// A simple query.
    Query(String),
    /// Prepares a statement.
    Parse { name: String, query: String, types: Vec<u32> },
    /// Binds parameter values to a prepared statement, creating a portal.
    Bind {
        portal: String,
        statement: String,
        formats: Vec<i16>,
        values: Vec<Option<Vec<u8>>>,
        results: Vec<i16>,
    },
    /// Describes a prepared statement or a portal.
    Describe { target: Target, name: String },
    /// Executes a portal.
    Execute { portal: String, max_rows: i32 },
    /// Closes a prepared statement or a portal.
    Close { target: Target, name: String },
    /// Ends an extended query.
    Sync,
    /// Asks for the pending responses.
    Flush,
    /// Closes the connection.
    Terminate,
    /// COPY data.
    CopyData(Vec<u8>),
    /// The end of the COPY data.
    CopyDone,
    /// Aborts a COPY FROM STDIN with a message.
    CopyFail(String),
}

/// The object of a Describe or Close message.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Target {
    Statement,
    Portal,
}

/// A message to the client.
#[derive(Clone, Debug, PartialEq)]
pub enum BackendMessage {
    AuthenticationOk,
    AuthenticationMd5Password { salt: [u8; 4] },
    ParameterStatus { name: String, value: String },
    BackendKeyData { process: i32, secret: i32 },
    ReadyForQuery(TransactionStatus),
    RowDescription(Vec<Field>),
    DataRow(Vec<Option<Vec<u8>>>),
    CommandComplete(String),
    EmptyQueryResponse,
    ErrorResponse { severity: String, code: String, message: String },
    ParseComplete,
    BindComplete,
    CloseComplete,
    NoData,
    PortalSuspended,
    ParameterDescription(Vec<u32>),
    CopyInResponse { columns: usize },
    CopyOutResponse { columns: usize },
    CopyData(Vec<u8>),
    CopyDone,
}

/// The transaction status of ReadyForQuery.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TransactionStatus {
    Idle,
    Transaction,
    Failed,
}

/// A column of a RowDescription.
#[derive(Clone, Debug, PartialEq)]
pub struct Field {
    pub name: String,
    pub table_oid: u32,
    pub column: i16,
    pub type_oid: u32,
    pub size: i16,
    pub modifier: i32,
    pub format: Format,
}

/// Reads a message's type byte and body, or None at the end of the stream.
pub fn read(reader: &mut impl Read) -> Result<Option<(u8, Vec<u8>)>> {
    let mut kind = [0];
    match reader.read_exact(&mut kind) {
        Ok(()) => {}
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error.into()),
    }
    let mut length = [0; 4];
    reader.read_exact(&mut length)?;
    Ok(Some((kind[0], read_body(reader, length)?)))
}

/// Reads the body of a startup-phase message, or None at the end of the stream.
pub fn read_startup(reader: &mut impl Read) -> Result<Option<Vec<u8>>> {
    let mut length = [0; 4];
    match reader.read_exact(&mut length) {
        Ok(()) => Ok(Some(read_body(reader, length)?)),
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => Ok(None),
        Err(error) => Err(error.into()),
    }
}

/// Reads a message body of the given length (which counts itself).
fn read_body(reader: &mut impl Read, length: [u8; 4]) -> Result<Vec<u8>> {
    let length = i32::from_be_bytes(length);
    let Some(size) = length.checked_sub(4).and_then(|size| usize::try_from(size).ok()) else {
        return errdata!("invalid message length {length}");
    };
    let mut body = vec![0; size];
    reader.read_exact(&mut body)?;
    Ok(body)
}

/// Writes a message: its type byte (if any), its length and its body.
fn write(out: &mut Vec<u8>, kind: Option<u8>, body: Vec<u8>) {
    out.extend(kind);
    out.extend((body.len() as i32 + 4).to_be_bytes());
    out.extend(body);
}

impl FrontendMessage {
    /// Decodes a startup-phase message from its body.
    pub fn decode_startup(body: &[u8]) -> std::result::Result<Self, PgError> {
        let mut body = Body(body);
        let code = body.u32().ok_or_else(invalid_format)?;
        Ok(match code {
            SSL_REQUEST => Self::SslRequest,
            GSSENC_REQUEST => Self::GssEncRequest,
            CANCEL_REQUEST => Self::CancelRequest {
                process: body.i32().ok_or_else(invalid_format)?,
                secret: body.i32().ok_or_else(invalid_format)?,
            },
            version => {
                // Only protocol 3.0 is supported.
                let parameters = match body.parameters() {
                    Some(parameters) => parameters,
                    None if version == PROTOCOL_VERSION => return Err(invalid_format()),
                    None => Vec::new(),
                };
                Self::Startup { version, parameters }
            }
        })
    }

    /// Decodes a message from its type byte and body.
    pub fn decode(kind: u8, body: &[u8]) -> std::result::Result<Self, PgError> {
        let mut body = Body(body);
        let message = match kind {
            b'p' => body.string().map(Self::Password),
            b'Q' => body.string().map(Self::Query),
            b'P' => (|| {
                let name = body.string()?;
                let query = body.string()?;
                let types = body.list(|body| body.u32())?;
                Some(Self::Parse { name, query, types })
            })(),
            b'B' => (|| {
                let portal = body.string()?;
                let statement = body.string()?;
                let formats = body.list(|body| body.i16())?;
                let values = body.list(|body| body.value())?;
                let results = body.list(|body| body.i16())?;
                Some(Self::Bind { portal, statement, formats, values, results })
            })(),
            b'D' => body.target().and_then(|target| {
                Some(Self::Describe { target, name: body.string()? })
            }),
            b'E' => (|| Some(Self::Execute { portal: body.string()?, max_rows: body.i32()? }))(),
            b'C' => {
                body.target().and_then(|target| Some(Self::Close { target, name: body.string()? }))
            }
            b'S' => Some(Self::Sync),
            b'H' => Some(Self::Flush),
            b'X' => Some(Self::Terminate),
            b'd' => Some(Self::CopyData(body.rest().to_vec())),
            b'c' => Some(Self::CopyDone),
            b'f' => body.string().map(Self::CopyFail),
            kind => {
                let kind = kind as char;
                return Err(PgError::new("08P01", format!("invalid frontend message type {kind}")));
            }
        };
        match message {
            Some(message) if body.is_empty() => Ok(message),
            _ => Err(invalid_format()),
        }
    }

    /// Encodes the message.
    pub fn encode(&self, out: &mut Vec<u8>) {
        let mut body = Vec::new();
        let kind = match self {
            Self::SslRequest => {
                body.put_u32(SSL_REQUEST);
                None
            }
            Self::GssEncRequest => {
                body.put_u32(GSSENC_REQUEST);
                None
            }
            Self::CancelRequest { process, secret } => {
                body.put_u32(CANCEL_REQUEST);
                body.put_i32(*process);
                body.put_i32(*secret);
                None
            }
            Self::Startup { version, parameters } => {
                body.put_u32(*version);
                for (name, value) in parameters {
                    body.put_str(name);
                    body.put_str(value);
                }
                body.push(0);
                None
            }
            Self::Password(password) => {
                body.put_str(password);
                Some(b'p')
            }
            Self::Query(query) => {
                body.put_str(query);
                Some(b'Q')
            }
            Self::Parse { name, query, types } => {
                body.put_str(name);
                body.put_str(query);
                body.put_i16(types.len() as i16);
                types.iter().for_each(|oid| body.put_u32(*oid));
                Some(b'P')
            }
            Self::Bind { portal, statement, formats, values, results } => {
                body.put_str(portal);
                body.put_str(statement);
                body.put_i16(formats.len() as i16);
                formats.iter().for_each(|format| body.put_i16(*format));
                body.put_i16(values.len() as i16);
                values.iter().for_each(|value| body.put_value(value.as_deref()));
                body.put_i16(results.len() as i16);
                results.iter().for_each(|format| body.put_i16(*format));
                Some(b'B')
            }
            Self::Describe { target, name } => {
                body.put_target(*target);
                body.put_str(name);
                Some(b'D')
            }
            Self::Execute { portal, max_rows } => {
                body.put_str(portal);
                body.put_i32(*max_rows);
                Some(b'E')
            }
            Self::Close { target, name } => {
                body.put_target(*target);
                body.put_str(name);
                Some(b'C')
            }
            Self::Sync => Some(b'S'),
            Self::Flush => Some(b'H'),
            Self::Terminate => Some(b'X'),
            Self::CopyData(data) => {
                body.extend(data);
                Some(b'd')
            }
            Self::CopyDone => Some(b'c'),
            Self::CopyFail(message) => {
                body.put_str(message);
                Some(b'f')
            }
        };
        write(out, kind, body);
    }
}

impl BackendMessage {
    /// Encodes the message.
    pub fn encode(&self, out: &mut Vec<u8>) {
        let mut body = Vec::new();
        let kind = match self {
            Self::AuthenticationOk => {
                body.put_i32(0);
                b'R'
            }
            Self::AuthenticationMd5Password { salt } => {
                body.put_i32(5);
                body.extend(salt);
                b'R'
            }
            Self::ParameterStatus { name, value } => {
                body.put_str(name);
                body.put_str(value);
                b'S'
            }
            Self::BackendKeyData { process, secret } => {
                body.put_i32(*process);
                body.put_i32(*secret);
                b'K'
            }
            Self::ReadyForQuery(status) => {
                body.push(match status {
                    TransactionStatus::Idle => b'I',
                    TransactionStatus::Transaction => b'T',
                    TransactionStatus::Failed => b'E',
                });
                b'Z'
            }
            Self::RowDescription(fields) => {
                body.put_i16(fields.len() as i16);
                for field in fields {
                    body.put_str(&field.name);
                    body.put_u32(field.table_oid);
                    body.put_i16(field.column);
                    body.put_u32(field.type_oid);
                    body.put_i16(field.size);
                    body.put_i32(field.modifier);
                    body.put_i16(field.format.code());
                }
                b'T'
            }
            Self::DataRow(values) => {
                body.put_i16(values.len() as i16);
                values.iter().for_each(|value| body.put_value(value.as_deref()));
                b'D'
            }
            Self::CommandComplete(tag) => {
                body.put_str(tag);
                b'C'
            }
            Self::EmptyQueryResponse => b'I',
            Self::ErrorResponse { severity, code, message } => {
                let fields = [(b'S', severity), (b'V', severity), (b'C', code), (b'M', message)];
                for (field, value) in fields {
                    body.push(field);
                    body.put_str(value);
                }
                body.push(0);
                b'E'
            }
            Self::ParseComplete => b'1',
            Self::BindComplete => b'2',
            Self::CloseComplete => b'3',
            Self::NoData => b'n',
            Self::PortalSuspended => b's',
            Self::ParameterDescription(types) => {
                body.put_i16(types.len() as i16);
                types.iter().for_each(|oid| body.put_u32(*oid));
                b't'
            }
            Self::CopyInResponse { columns } => {
                body.put_copy_formats(*columns);
                b'G'
            }
            Self::CopyOutResponse { columns } => {
                body.put_copy_formats(*columns);
                b'H'
            }
            Self::CopyData(data) => {
                body.extend(data);
                b'd'
            }
            Self::CopyDone => b'c',
        };
        write(out, Some(kind), body);
    }

    /// Decodes a message from its type byte and body.
    pub fn decode(kind: u8, body: &[u8]) -> Result<Self> {
        let mut body = Body(body);
        let message = match kind {
            b'R' => match body.i32() {
                Some(0) => Some(Self::AuthenticationOk),
                Some(5) => body.bytes(4).map(|salt| Self::AuthenticationMd5Password {
                    salt: salt.try_into().expect("4 bytes"),
                }),
                _ => None,
            },
            b'S' => (|| {
                Some(Self::ParameterStatus { name: body.string()?, value: body.string()? })
            })(),
            b'K' => (|| Some(Self::BackendKeyData { process: body.i32()?, secret: body.i32()? }))(),
            b'Z' => match body.u8() {
                Some(b'I') => Some(Self::ReadyForQuery(TransactionStatus::Idle)),
                Some(b'T') => Some(Self::ReadyForQuery(TransactionStatus::Transaction)),
                Some(b'E') => Some(Self::ReadyForQuery(TransactionStatus::Failed)),
                _ => None,
            },
            b'T' => body.list(|body| body.field()).map(Self::RowDescription),
            b'D' => body.list(|body| body.value()).map(Self::DataRow),
            b'C' => body.string().map(Self::CommandComplete),
            b'I' => Some(Self::EmptyQueryResponse),
            b'E' => body.error_fields(),
            b'1' => Some(Self::ParseComplete),
            b'2' => Some(Self::BindComplete),
            b'3' => Some(Self::CloseComplete),
            b'n' => Some(Self::NoData),
            b's' => Some(Self::PortalSuspended),
            b't' => body.list(|body| body.u32()).map(Self::ParameterDescription),
            b'G' | b'H' => (|| {
                body.u8()?;
                let columns = body.list(|body| body.i16())?.len();
                match kind {
                    b'G' => Some(Self::CopyInResponse { columns }),
                    _ => Some(Self::CopyOutResponse { columns }),
                }
            })(),
            b'd' => Some(Self::CopyData(body.rest().to_vec())),
            b'c' => Some(Self::CopyDone),
            kind => return errdata!("invalid backend message type {}", kind as char),
        };
        match message {
            Some(message) if body.is_empty() => Ok(message),
            _ => errdata!("invalid backend message {}", kind as char),
        }
    }
}

/// The error for a message that can't be decoded.
fn invalid_format() -> PgError {
    PgError::new("08P01", "invalid message format")
}

/// A message body being decoded.
struct Body<'a>(&'a [u8]);

impl<'a> Body<'a> {
    fn is_empty(&self) -> bool {
        self.0.is_empty()
    }

    fn bytes(&mut self, n: usize) -> Option<&'a [u8]> {
        let (bytes, rest) = self.0.split_at_checked(n)?;
        self.0 = rest;
        Some(bytes)
    }

    fn rest(&mut self) -> &'a [u8] {
        std::mem::take(&mut self.0)
    }

    fn u8(&mut self) -> Option<u8> {
        Some(self.bytes(1)?[0])
    }

    fn i16(&mut self) -> Option<i16> {
        Some(i16::from_be_bytes(self.bytes(2)?.try_into().ok()?))
    }

    fn i32(&mut self) -> Option<i32> {
        Some(i32::from_be_bytes(self.bytes(4)?.try_into().ok()?))
    }

    fn u32(&mut self) -> Option<u32> {
        Some(u32::from_be_bytes(self.bytes(4)?.try_into().ok()?))
    }

    /// A zero-terminated UTF-8 string.
    fn string(&mut self) -> Option<String> {
        let end = self.0.iter().position(|b| *b == 0)?;
        let string = String::from_utf8(self.bytes(end)?.to_vec()).ok()?;
        self.bytes(1)?;
        Some(string)
    }

    /// A list: its 16-bit length and its items.
    fn list<T>(&mut self, item: impl Fn(&mut Self) -> Option<T>) -> Option<Vec<T>> {
        let length = usize::try_from(self.i16()?).ok()?;
        (0..length).map(|_| item(self)).collect()
    }

    /// A value: its 32-bit length (-1 for NULL) and its bytes.
    fn value(&mut self) -> Option<Option<Vec<u8>>> {
        match self.i32()? {
            -1 => Some(None),
            length => Some(Some(self.bytes(usize::try_from(length).ok()?)?.to_vec())),
        }
    }

    /// The target of a Describe or Close: S or P.
    fn target(&mut self) -> Option<Target> {
        match self.u8()? {
            b'S' => Some(Target::Statement),
            b'P' => Some(Target::Portal),
            _ => None,
        }
    }

    /// Name and value pairs, ended by an empty name.
    fn parameters(&mut self) -> Option<Vec<(String, String)>> {
        let mut parameters = Vec::new();
        loop {
            match self.string()? {
                name if name.is_empty() => return Some(parameters),
                name => parameters.push((name, self.string()?)),
            }
        }
    }

    /// A RowDescription field.
    fn field(&mut self) -> Option<Field> {
        Some(Field {
            name: self.string()?,
            table_oid: self.u32()?,
            column: self.i16()?,
            type_oid: self.u32()?,
            size: self.i16()?,
            modifier: self.i32()?,
            format: Format::from_code(self.i16()?).ok()?,
        })
    }

    /// The fields of an ErrorResponse, as the message.
    fn error_fields(&mut self) -> Option<BackendMessage> {
        let (mut severity, mut code, mut message) = (String::new(), String::new(), String::new());
        loop {
            match self.u8()? {
                0 => return Some(BackendMessage::ErrorResponse { severity, code, message }),
                b'V' => severity = self.string()?,
                b'C' => code = self.string()?,
                b'M' => message = self.string()?,
                _ => {
                    self.string()?;
                }
            }
        }
    }
}

/// Encodes the items of message bodies.
trait Put {
    fn put_i16(&mut self, value: i16);
    fn put_i32(&mut self, value: i32);
    fn put_u32(&mut self, value: u32);
    fn put_str(&mut self, value: &str);
    fn put_value(&mut self, value: Option<&[u8]>);
    fn put_target(&mut self, target: Target);
    fn put_copy_formats(&mut self, columns: usize);
}

impl Put for Vec<u8> {
    fn put_i16(&mut self, value: i16) {
        self.extend(value.to_be_bytes());
    }

    fn put_i32(&mut self, value: i32) {
        self.extend(value.to_be_bytes());
    }

    fn put_u32(&mut self, value: u32) {
        self.extend(value.to_be_bytes());
    }

    /// A zero-terminated string.
    fn put_str(&mut self, value: &str) {
        self.extend(value.as_bytes());
        self.push(0);
    }

    /// A value: its length (-1 for NULL) and its bytes.
    fn put_value(&mut self, value: Option<&[u8]>) {
        match value {
            Some(bytes) => {
                self.put_i32(bytes.len() as i32);
                self.extend(bytes);
            }
            None => self.put_i32(-1),
        }
    }

    fn put_target(&mut self, target: Target) {
        self.push(match target {
            Target::Statement => b'S',
            Target::Portal => b'P',
        });
    }

    /// The formats of a COPY: text, for the whole copy and each column.
    fn put_copy_formats(&mut self, columns: usize) {
        self.push(0);
        self.put_i16(columns as i16);
        (0..columns).for_each(|_| self.put_i16(Format::Text.code()));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Encodes and decodes a frontend message.
    fn frontend_roundtrip(message: FrontendMessage) -> FrontendMessage {
        let mut out = Vec::new();
        message.encode(&mut out);
        let mut reader = out.as_slice();
        let decoded = match message {
            FrontendMessage::SslRequest
            | FrontendMessage::GssEncRequest
            | FrontendMessage::CancelRequest { .. }
            | FrontendMessage::Startup { .. } => {
                let body = read_startup(&mut reader).unwrap().unwrap();
                FrontendMessage::decode_startup(&body).unwrap()
            }
            _ => {
                let (kind, body) = read(&mut reader).unwrap().unwrap();
                FrontendMessage::decode(kind, &body).unwrap()
            }
        };
        assert!(reader.is_empty());
        decoded
    }

    #[test]
    fn frontend() {
        let messages = [
            FrontendMessage::SslRequest,
            FrontendMessage::CancelRequest { process: 1, secret: -2 },
            FrontendMessage::Startup {
                version: PROTOCOL_VERSION,
                parameters: vec![("user".into(), "alice".into())],
            },
            FrontendMessage::Query("SELECT 1".into()),
            FrontendMessage::Parse { name: "s".into(), query: "SELECT $1".into(), types: vec![20] },
            FrontendMessage::Bind {
                portal: "p".into(),
                statement: "s".into(),
                formats: vec![1],
                values: vec![Some(vec![0, 1]), None],
                results: vec![0, 1],
            },
            FrontendMessage::Describe { target: Target::Portal, name: "p".into() },
            FrontendMessage::Execute { portal: String::new(), max_rows: 2 },
            FrontendMessage::Close { target: Target::Statement, name: "s".into() },
            FrontendMessage::Sync,
            FrontendMessage::CopyData(b"1\ta\n".to_vec()),
            FrontendMessage::CopyFail("gave up".into()),
        ];
        for message in messages {
            assert_eq!(frontend_roundtrip(message.clone()), message);
        }
    }

    #[test]
    fn frontend_encoding() {
        let mut out = Vec::new();
        FrontendMessage::Query("x".into()).encode(&mut out);
        assert_eq!(out, b"Q\x00\x00\x00\x06x\x00");
        out.clear();
        FrontendMessage::SslRequest.encode(&mut out);
        assert_eq!(out, [0, 0, 0, 8, 4, 210, 22, 47]);
    }

    #[test]
    fn frontend_errors() {
        let error = FrontendMessage::decode(b'?', &[]).unwrap_err();
        assert_eq!(error, PgError::new("08P01", "invalid frontend message type ?"));
        let error = FrontendMessage::decode(b'Q', b"no terminator").unwrap_err();
        assert_eq!(error, invalid_format());
        let error = FrontendMessage::decode(b'S', b"extra").unwrap_err();
        assert_eq!(error, invalid_format());
        // Parameters of other protocol versions are ignored.
        let body = [0, 2, 0, 0, b'x'];
        assert_eq!(
            FrontendMessage::decode_startup(&body),
            Ok(FrontendMessage::Startup { version: 2 << 16, parameters: Vec::new() })
        );
    }

    #[test]
    fn backend() {
        let messages = [
            BackendMessage::AuthenticationOk,
            BackendMessage::AuthenticationMd5Password { salt: [1, 2, 3, 4] },
            BackendMessage::ParameterStatus { name: "a".into(), value: "b".into() },
            BackendMessage::BackendKeyData { process: 7, secret: 9 },
            BackendMessage::ReadyForQuery(TransactionStatus::Transaction),
            BackendMessage::RowDescription(vec![Field {
                name: "id".into(),
                table_oid: 0,
                column: 0,
                type_oid: 20,
                size: 8,
                modifier: -1,
                format: Format::Binary,
            }]),
            BackendMessage::DataRow(vec![Some(b"1".to_vec()), None]),
            BackendMessage::CommandComplete("SELECT 1".into()),
            BackendMessage::ErrorResponse {
                severity: "ERROR".into(),
                code: "42000".into(),
                message: "invalid input: x".into(),
            },
            BackendMessage::ParameterDescription(vec![20, 25]),
            BackendMessage::CopyInResponse { columns: 2 },
            BackendMessage::CopyOutResponse { columns: 0 },
            BackendMessage::CopyData(b"1\n".to_vec()),
            BackendMessage::CopyDone,
        ];
        for message in messages {
            let mut out = Vec::new();
            message.encode(&mut out);
            let (kind, body) = read(&mut out.as_slice()).unwrap().unwrap();
            assert_eq!(BackendMessage::decode(kind, &body).unwrap(), message);
        }
    }

    #[test]
    fn read_end() {
        assert_eq!(read(&mut [].as_slice()).unwrap(), None);
        assert_eq!(read_startup(&mut [].as_slice()).unwrap(), None);
        assert!(read(&mut b"Q\x00\x00".as_slice()).is_err());
        assert!(read(&mut b"Q\x00\x00\x00\x02".as_slice()).is_err());
    }
}
