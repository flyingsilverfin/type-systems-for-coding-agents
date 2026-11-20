//! Thin wrapper around the TypeDB driver: connections, transactions and typed column access.
use chrono::NaiveDate;
use typedb_driver::{
    answer::ConceptRow, Addresses, Credentials, DriverOptions, DriverTlsConfig, Promise, TransactionType, TypeDBDriver,
};

use crate::{
    config::Config,
    error::{AppError, Result},
};

pub struct Db {
    driver: TypeDBDriver,
    name: String,
}

impl Db {
    pub fn connect(config: &Config) -> Result<Self> {
        let addresses = Addresses::try_from_address_str(&config.address)
            .map_err(|e| AppError::Usage(format!("invalid address '{}': {e}", config.address)))?;
        let driver = TypeDBDriver::new(
            addresses,
            Credentials::new(&config.username, &config.password),
            DriverOptions::new(DriverTlsConfig::disabled()),
        )
        .map_err(|e| AppError::Usage(format!("could not connect to TypeDB at {}: {e}", config.address)))?;
        Ok(Db { driver, name: config.database.clone() })
    }

    pub fn name(&self) -> &str {
        &self.name
    }

    /// Delete the database if it exists and create it again, empty.
    pub fn recreate(&self) -> Result<()> {
        let databases = self.driver.databases();
        if databases.contains(&self.name)? {
            databases.get(&self.name)?.delete()?;
        }
        databases.create(&self.name)?;
        Ok(())
    }

    pub fn define(&self, schema: &str) -> Result<()> {
        let tx = self.driver.transaction(&self.name, TransactionType::Schema)?;
        tx.query(schema).resolve()?;
        tx.commit().resolve()?;
        Ok(())
    }

    /// Run all queries in a single write transaction and commit.
    pub fn write(&self, queries: &[String]) -> Result<()> {
        let tx = self.driver.transaction(&self.name, TransactionType::Write)?;
        for query in queries {
            tx.query(query).resolve()?;
        }
        tx.commit().resolve()?;
        Ok(())
    }

    pub fn read_rows(&self, query: &str) -> Result<Vec<ConceptRow>> {
        let tx = self.driver.transaction(&self.name, TransactionType::Read)?;
        let answer = tx.query(query).resolve()?;
        let rows = answer.into_rows().collect::<std::result::Result<Vec<_>, _>>()?;
        Ok(rows)
    }
}

/// Render a string as a TypeQL string literal.
pub fn quote(value: &str) -> String {
    format!("\"{}\"", value.replace('\\', "\\\\").replace('"', "\\\""))
}

// Column accessors. Each returns Ok(None) when the variable is absent from the row
// (e.g. it was bound inside a `try { ... };` block that did not match).

pub fn string(row: &ConceptRow, var: &str) -> Result<Option<String>> {
    match row.get(var)? {
        None => Ok(None),
        Some(concept) => match concept.try_get_string() {
            Some(value) => Ok(Some(value.to_owned())),
            None => Err(AppError::Data(format!("${var} is not a string attribute"))),
        },
    }
}

pub fn integer(row: &ConceptRow, var: &str) -> Result<Option<i64>> {
    match row.get(var)? {
        None => Ok(None),
        Some(concept) => match concept.try_get_integer() {
            Some(value) => Ok(Some(value)),
            None => Err(AppError::Data(format!("${var} is not an integer attribute"))),
        },
    }
}

pub fn date(row: &ConceptRow, var: &str) -> Result<Option<NaiveDate>> {
    match row.get(var)? {
        None => Ok(None),
        Some(concept) => match concept.try_get_date() {
            Some(value) => Ok(Some(value)),
            None => Err(AppError::Data(format!("${var} is not a date attribute"))),
        },
    }
}

fn required<T>(value: Option<T>, var: &str) -> Result<T> {
    value.ok_or_else(|| AppError::Data(format!("required column ${var} missing from row")))
}

pub fn req_string(row: &ConceptRow, var: &str) -> Result<String> {
    required(string(row, var)?, var)
}

pub fn req_integer(row: &ConceptRow, var: &str) -> Result<i64> {
    required(integer(row, var)?, var)
}

pub fn req_date(row: &ConceptRow, var: &str) -> Result<NaiveDate> {
    required(date(row, var)?, var)
}
