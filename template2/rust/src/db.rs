//! Thin wrapper around the TypeDB driver: connections and transactions.
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
