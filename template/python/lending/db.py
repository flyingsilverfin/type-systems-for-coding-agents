"""Thin wrapper around the TypeDB driver: connections, transactions and typed column access."""
from __future__ import annotations

from datetime import date
from typing import Optional

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
from lending.errors import AppError, DataError


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


# Column accessors. Each returns None when the variable is absent from the row
# (e.g. it was bound inside a `try { ... };` block that did not match).

def string(row: ConceptRow, var: str) -> Optional[str]:
    concept = row.get(var)
    if concept is None:
        return None
    value = concept.try_get_string()
    if value is None:
        raise DataError(f"${var} is not a string attribute")
    return value


def integer(row: ConceptRow, var: str) -> Optional[int]:
    concept = row.get(var)
    if concept is None:
        return None
    value = concept.try_get_integer()
    if value is None:
        raise DataError(f"${var} is not an integer attribute")
    return value


def date_value(row: ConceptRow, var: str) -> Optional[date]:
    concept = row.get(var)
    if concept is None:
        return None
    value = concept.try_get_date()
    if value is None:
        raise DataError(f"${var} is not a date attribute")
    return value


def req_string(row: ConceptRow, var: str) -> str:
    value = string(row, var)
    if value is None:
        raise DataError(f"required column ${var} missing from row")
    return value


def req_integer(row: ConceptRow, var: str) -> int:
    value = integer(row, var)
    if value is None:
        raise DataError(f"required column ${var} missing from row")
    return value


def req_date(row: ConceptRow, var: str) -> date:
    value = date_value(row, var)
    if value is None:
        raise DataError(f"required column ${var} missing from row")
    return value
