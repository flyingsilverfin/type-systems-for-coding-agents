//! A server for a subset of the PostgreSQL frontend/backend protocol, version
//! 3.0, so that PostgreSQL clients can use toydb. Each connection has a session
//! of its own, with the startup message's user as its login user; its
//! statements are executed exactly as those of a client connection.

pub mod client;
pub mod connection;
pub mod message;
pub mod types;

use std::fmt::Display;
use std::net::TcpListener;

use log::{debug, error};

pub use client::Client;

use crate::encoding::md5;
use crate::sql::engine::Engine;

/// Accepts PostgreSQL connections and serves each in a thread of its own.
pub fn serve<'a, E: Engine<'a> + Sync>(listener: TcpListener, engine: &'a E) {
    std::thread::scope(|s| {
        loop {
            let (socket, peer) = match listener.accept() {
                Ok((socket, peer)) => (socket, peer),
                Err(err) => {
                    error!("PostgreSQL client accept failed: {err}");
                    continue;
                }
            };
            s.spawn(move || {
                debug!("PostgreSQL client {peer} connected");
                match connection::serve(engine, socket) {
                    Ok(()) => debug!("PostgreSQL client {peer} disconnected"),
                    Err(err) => error!("PostgreSQL client {peer} error: {err}"),
                }
            });
        }
    })
}

/// Returns the response of PostgreSQL's MD5 password method for a stored
/// password hash and a salt.
pub fn md5_response(hash: &str, salt: [u8; 4]) -> String {
    let hash = hash.strip_prefix("md5").unwrap_or(hash);
    format!("md5{}", md5::hex(&[hash.as_bytes(), &salt].concat()))
}

/// An error sent to the client as ErrorResponse: a SQLSTATE code and a
/// message.
#[derive(Clone, Debug, PartialEq)]
pub struct PgError {
    pub code: &'static str,
    pub message: String,
}

impl PgError {
    /// Creates an error.
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self { code, message: message.into() }
    }
}

impl Display for PgError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
