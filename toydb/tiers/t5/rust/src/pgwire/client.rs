//! A PostgreSQL client for the messages the server supports: startup with the
//! MD5 password method, simple and extended query, COPY. `toysql --pg` and the
//! test runners use it.

use std::collections::{HashMap, VecDeque};
use std::io::{BufReader, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};

use itertools::Itertools as _;

use super::md5_response;
use super::message::{self, BackendMessage, FrontendMessage, TransactionStatus};
use super::types::{Format, Type};
use crate::sql::execution::users::hash_password;
use crate::sql::types::Value;
use crate::{errdata, error::Result};

/// A PostgreSQL client on a stream.
pub struct Client<S: Read + Write> {
    /// The stream, read through a buffer.
    reader: BufReader<S>,
    /// The queued messages.
    output: Vec<u8>,
    /// Whether the connection is closed.
    closed: bool,
    /// The result format codes of the portals bound since the last Sync.
    portals: HashMap<String, Vec<i16>>,
    /// The result format codes of the sent Executes that have no result yet.
    executes: VecDeque<Vec<i16>>,
}

/// How a client answers a CopyInResponse.
#[derive(Clone, Debug, PartialEq)]
pub enum CopyIn {
    /// CopyData with the data, if any, then CopyDone.
    Data(Option<Vec<u8>>),
    /// CopyFail with a message.
    Fail(String),
}

impl Client<TcpStream> {
    /// Connects to a server's PostgreSQL port.
    pub fn connect(addr: impl ToSocketAddrs) -> Result<Self> {
        Ok(Self::new(TcpStream::connect(addr)?))
    }
}

impl<S: Read + Write> Client<S> {
    /// Creates a client on a stream.
    pub fn new(stream: S) -> Self {
        Self {
            reader: BufReader::new(stream),
            output: Vec::new(),
            closed: false,
            portals: HashMap::new(),
            executes: VecDeque::new(),
        }
    }

    /// Returns true if the connection is closed.
    pub fn is_closed(&self) -> bool {
        self.closed
    }

    /// Queues a message.
    pub fn send(&mut self, message: &FrontendMessage) {
        match message {
            FrontendMessage::Bind { portal, results, .. } => {
                self.portals.insert(portal.clone(), results.clone());
            }
            FrontendMessage::Execute { portal, .. } => {
                let results = self.portals.get(portal).cloned().unwrap_or_default();
                self.executes.push_back(results);
            }
            FrontendMessage::Sync => self.portals.clear(),
            _ => {}
        }
        message.encode(&mut self.output);
    }

    /// Sends the queued messages.
    pub fn flush(&mut self) -> Result<()> {
        let stream = self.reader.get_mut();
        stream.write_all(&self.output)?;
        stream.flush()?;
        self.output.clear();
        Ok(())
    }

    /// Receives a message, or None if the server closed the connection.
    pub fn receive(&mut self) -> Result<Option<BackendMessage>> {
        if self.closed {
            return Ok(None);
        }
        match message::read(&mut self.reader)? {
            Some((kind, body)) => Ok(Some(BackendMessage::decode(kind, &body)?)),
            None => {
                self.closed = true;
                Ok(None)
            }
        }
    }

    /// Receives messages until ReadyForQuery.
    pub fn receive_until_ready(&mut self) -> Result<Vec<BackendMessage>> {
        let mut messages = Vec::new();
        while let Some(message) = self.receive()? {
            let ready = matches!(message, BackendMessage::ReadyForQuery(_));
            messages.push(message);
            if ready {
                break;
            }
        }
        Ok(messages)
    }

    /// Asks the server for an SSL connection, and returns its answer.
    pub fn request_ssl(&mut self) -> Result<u8> {
        self.send(&FrontendMessage::SslRequest);
        self.flush()?;
        let mut answer = [0];
        self.reader.read_exact(&mut answer)?;
        Ok(answer[0])
    }

    /// Starts the connection, and returns the messages received until ReadyForQuery.
    pub fn startup(
        &mut self,
        version: u32,
        parameters: Vec<(String, String)>,
        password: Option<&str>,
    ) -> Result<Vec<BackendMessage>> {
        let user = parameters
            .iter()
            .find(|(name, _)| name == "user")
            .map(|(_, user)| user.clone())
            .unwrap_or_default();
        self.send(&FrontendMessage::Startup { version, parameters });
        self.flush()?;
        let mut messages = Vec::new();
        while let Some(message) = self.receive()? {
            if let BackendMessage::AuthenticationMd5Password { salt } = message {
                messages.push(message);
                let Some(password) = password else {
                    self.closed = true;
                    break;
                };
                let response = md5_response(&hash_password(&user, password), salt);
                self.send(&FrontendMessage::Password(response));
                self.flush()?;
                continue;
            }
            let ready = matches!(message, BackendMessage::ReadyForQuery(_));
            messages.push(message);
            if ready {
                break;
            }
        }
        Ok(messages)
    }

    /// Sends a simple query, and returns the messages received until ReadyForQuery.
    pub fn query(&mut self, query: &str, copy: CopyIn) -> Result<Vec<BackendMessage>> {
        self.send(&FrontendMessage::Query(query.to_string()));
        self.flush()?;
        let mut messages = Vec::new();
        loop {
            messages.extend(self.receive_until_ready_or_copy()?);
            match messages.last() {
                Some(BackendMessage::CopyInResponse { .. }) => {
                    match &copy {
                        CopyIn::Data(data) => {
                            if let Some(data) = data {
                                self.send(&FrontendMessage::CopyData(data.clone()));
                            }
                            self.send(&FrontendMessage::CopyDone);
                        }
                        CopyIn::Fail(message) => {
                            self.send(&FrontendMessage::CopyFail(message.clone()))
                        }
                    }
                    self.flush()?;
                }
                _ => return Ok(messages),
            }
        }
    }

    /// Receives messages until ReadyForQuery or CopyInResponse.
    fn receive_until_ready_or_copy(&mut self) -> Result<Vec<BackendMessage>> {
        let mut messages = Vec::new();
        while let Some(message) = self.receive()? {
            let end = matches!(
                message,
                BackendMessage::ReadyForQuery(_) | BackendMessage::CopyInResponse { .. }
            );
            messages.push(message);
            if end {
                break;
            }
        }
        Ok(messages)
    }

    /// Sends the queued messages and Sync, and returns the messages received.
    pub fn sync(&mut self) -> Result<Vec<BackendMessage>> {
        self.send(&FrontendMessage::Sync);
        self.flush()?;
        self.receive_until_ready()
    }

    /// Closes the connection with Terminate, and waits for the server to close it.
    pub fn terminate(&mut self) -> Result<()> {
        self.send(&FrontendMessage::Terminate);
        self.flush()?;
        match self.receive()? {
            Some(message) => errdata!("unexpected message {message:?} after Terminate"),
            None => Ok(()),
        }
    }

    /// Formats received messages as the test runners print them. A DataRow of
    /// an Execute has the values in binary that the portal's Bind asked for.
    pub fn display(&mut self, messages: &[BackendMessage]) -> Vec<String> {
        let mut lines: Vec<String> = Vec::new();
        let mut parameters = false;
        for message in messages {
            let line = match message {
                BackendMessage::AuthenticationOk => "AuthenticationOk".to_string(),
                BackendMessage::AuthenticationMd5Password { .. } => {
                    "AuthenticationMD5Password".to_string()
                }
                BackendMessage::ParameterStatus { name, value } => {
                    let parameter = format!("{name}={value}");
                    if parameters && let Some(line) = lines.last_mut() {
                        *line = format!("{line} {parameter}");
                        continue;
                    }
                    parameters = true;
                    lines.push(format!("ParameterStatus {parameter}"));
                    continue;
                }
                BackendMessage::BackendKeyData { .. } => "BackendKeyData".to_string(),
                BackendMessage::ReadyForQuery(status) => {
                    let status = match status {
                        TransactionStatus::Idle => "idle",
                        TransactionStatus::Transaction => "transaction",
                        TransactionStatus::Failed => "failed",
                    };
                    format!("ReadyForQuery {status}")
                }
                BackendMessage::RowDescription(fields) => {
                    let fields = fields
                        .iter()
                        .map(|field| {
                            let (oid, size) = (field.type_oid, field.size);
                            let name = type_name(oid);
                            let binary = match field.format {
                                Format::Binary => " binary",
                                Format::Text => "",
                            };
                            format!("{} {name}({oid},{size}){binary}", field.name)
                        })
                        .join(", ");
                    format!("RowDescription {fields}")
                }
                BackendMessage::DataRow(values) => {
                    let formats = self.executes.front().map(Vec::as_slice).unwrap_or_default();
                    let binary = |i: usize| match formats {
                        [] => false,
                        [format] => *format == Format::Binary.code(),
                        formats => formats.get(i) == Some(&Format::Binary.code()),
                    };
                    let values = values
                        .iter()
                        .enumerate()
                        .map(|(i, value)| match value {
                            None => "NULL".to_string(),
                            Some(bytes) if binary(i) => format!("\\x{}", hex(bytes)),
                            Some(bytes) => text(bytes),
                        })
                        .join(", ");
                    format!("DataRow {values}")
                }
                BackendMessage::CommandComplete(tag) => {
                    self.executes.pop_front();
                    format!("CommandComplete {tag}")
                }
                BackendMessage::EmptyQueryResponse => {
                    self.executes.pop_front();
                    "EmptyQueryResponse".to_string()
                }
                BackendMessage::ErrorResponse { severity, code, message } => {
                    // The server skips the messages after an error until Sync.
                    self.executes.clear();
                    format!("ErrorResponse {severity} {code} {message}")
                }
                BackendMessage::ParseComplete => "ParseComplete".to_string(),
                BackendMessage::BindComplete => "BindComplete".to_string(),
                BackendMessage::CloseComplete => "CloseComplete".to_string(),
                BackendMessage::NoData => "NoData".to_string(),
                BackendMessage::PortalSuspended => {
                    self.executes.pop_front();
                    "PortalSuspended".to_string()
                }
                BackendMessage::ParameterDescription(types) => {
                    let types = types.iter().map(|oid| format!(" {}({oid})", type_name(*oid)));
                    format!("ParameterDescription{}", types.collect_vec().join(","))
                }
                BackendMessage::CopyInResponse { columns } => {
                    format!("CopyInResponse {columns} columns")
                }
                BackendMessage::CopyOutResponse { columns } => {
                    format!("CopyOutResponse {columns} columns")
                }
                BackendMessage::CopyData(data) => format!("CopyData {}", text(data)),
                BackendMessage::CopyDone => "CopyDone".to_string(),
            };
            parameters = false;
            lines.push(line);
        }
        lines
    }
}

/// Formats text bytes as a STRING value is displayed in query results.
fn text(bytes: &[u8]) -> String {
    Value::String(String::from_utf8_lossy(bytes).into_owned()).to_string()
}

/// Returns the name of a type OID.
fn type_name(oid: u32) -> &'static str {
    Type::from_oid(oid).map_or("unknown", |pgtype| pgtype.name)
}

/// Formats bytes as lower-case hex.
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pgwire::message::Field;

    #[test]
    fn display_messages() {
        let field = |name: &str, format| Field {
            name: name.to_string(),
            table_oid: 0,
            column: 0,
            type_oid: 20,
            size: 8,
            modifier: -1,
            format,
        };
        let parameter = |name: &str, value: &str| BackendMessage::ParameterStatus {
            name: name.to_string(),
            value: value.to_string(),
        };
        let mut client = Client::new(std::io::Cursor::new(Vec::new()));
        client.send(&FrontendMessage::Bind {
            portal: "p".into(),
            statement: String::new(),
            formats: Vec::new(),
            values: Vec::new(),
            results: vec![0, 1],
        });
        client.send(&FrontendMessage::Execute { portal: "p".into(), max_rows: 0 });
        let messages = [
            parameter("a", "1"),
            parameter("b", ""),
            BackendMessage::BackendKeyData { process: 1, secret: 2 },
            BackendMessage::RowDescription(vec![
                field("x", Format::Text),
                field("y", Format::Binary),
            ]),
            BackendMessage::DataRow(vec![Some(b"it's".to_vec()), Some(vec![0, 255])]),
            BackendMessage::DataRow(vec![None, None]),
            BackendMessage::CommandComplete("SELECT 2".into()),
            BackendMessage::ReadyForQuery(TransactionStatus::Idle),
            BackendMessage::DataRow(vec![Some(vec![0x61])]),
            BackendMessage::ParameterDescription(vec![]),
            BackendMessage::ParameterDescription(vec![21, 1043]),
            BackendMessage::ErrorResponse {
                severity: "FATAL".into(),
                code: "28000".into(),
                message: "no user name given".into(),
            },
            BackendMessage::CopyData(b"1\ta\n".to_vec()),
        ];
        assert_eq!(
            client.display(&messages),
            vec![
                "ParameterStatus a=1 b=",
                "BackendKeyData",
                "RowDescription x int8(20,8), y int8(20,8) binary",
                "DataRow 'it\\'s', \\x00ff",
                "DataRow NULL, NULL",
                "CommandComplete SELECT 2",
                "ReadyForQuery idle",
                "DataRow 'a'",
                "ParameterDescription",
                "ParameterDescription int2(21), varchar(1043)",
                "ErrorResponse FATAL 28000 no user name given",
                "CopyData '1\\ta\\n'",
            ]
        );
    }
}
