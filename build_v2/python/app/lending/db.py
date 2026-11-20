"""Thin wrapper around the TypeDB driver: connection, schema setup, write and read transactions."""
from __future__ import annotations

from typedb.driver import (
    ConceptRow,
    Credentials,
    DriverOptions,
    DriverTlsConfig,
    TransactionType,
    TypeDB,
    TypeDBDriverException,
)

from lending.config import Config
from lending.errors import AppError


class Db:
    def __init__(self, config: Config) -> None:
        self._name = config.database
        try:
            self._driver = TypeDB.driver(
                config.address,
                Credentials(config.username, config.password),
                DriverOptions(DriverTlsConfig.disabled()),
            )
        except TypeDBDriverException as e:
            raise AppError(f"could not connect to TypeDB at {config.address}: {e}") from None

    @property
    def name(self) -> str:
        return self._name

    def close(self) -> None:
        self._driver.close()

    def recreate(self) -> None:
        """Delete the database if it exists and create it again, empty."""
        databases = self._driver.databases
        if databases.contains(self._name):
            databases.get(self._name).delete()
        databases.create(self._name)

    def define(self, schema: str) -> None:
        with self._driver.transaction(self._name, TransactionType.SCHEMA) as tx:
            tx.query(schema).resolve()
            tx.commit()

    def write(self, queries: list[str]) -> None:
        """Run all queries in a single write transaction and commit."""
        with self._driver.transaction(self._name, TransactionType.WRITE) as tx:
            for query in queries:
                tx.query(query).resolve()
            tx.commit()

    def read_rows(self, query: str) -> list[ConceptRow]:
        with self._driver.transaction(self._name, TransactionType.READ) as tx:
            answer = tx.query(query).resolve()
            return list(answer.as_concept_rows())


def quote(value: str) -> str:
    """Render a Python string as a TypeQL string literal."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

