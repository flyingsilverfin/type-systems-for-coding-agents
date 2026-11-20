from __future__ import annotations


class AppError(Exception):
    """Base class for all errors reported to the user by the CLI."""


class UsageError(AppError):
    pass


class NotFoundError(AppError):
    pass


class CsvError(AppError):
    def __init__(self, file: str, line: int, message: str) -> None:
        super().__init__(f"{file}:{line}: {message}")
        self.file = file
        self.line = line


class DataError(AppError):
    """The database returned data that does not match what the application expects."""
