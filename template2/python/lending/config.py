from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    address: str
    database: str
    username: str
    password: str

    @staticmethod
    def from_env() -> "Config":
        return Config(
            address=os.environ.get("LENDING_ADDRESS", "127.0.0.1:1729"),
            database=os.environ.get("LENDING_DB", "lending"),
            username=os.environ.get("LENDING_USER", "admin"),
            password=os.environ.get("LENDING_PASSWORD", "password"),
        )
