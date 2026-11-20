use std::env;

#[derive(Debug, Clone)]
pub struct Config {
    pub address: String,
    pub database: String,
    pub username: String,
    pub password: String,
}

impl Config {
    pub fn from_env() -> Self {
        let var = |key: &str, default: &str| env::var(key).unwrap_or_else(|_| default.to_owned());
        Config {
            address: var("LENDING_ADDRESS", "127.0.0.1:1729"),
            database: var("LENDING_DB", "lending"),
            username: var("LENDING_USER", "admin"),
            password: var("LENDING_PASSWORD", "password"),
        }
    }
}
