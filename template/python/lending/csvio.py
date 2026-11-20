"""Minimal CSV reader: header row, comma separated, double-quoted fields may contain commas and "" escapes."""
from __future__ import annotations

from pathlib import Path

from lending.errors import CsvError

Record = dict[str, str]


def parse_line(line: str, file: str, line_no: int) -> list[str]:
    fields: list[str] = []
    current = []
    in_quotes = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quotes:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    current.append('"')
                    i += 1
                else:
                    in_quotes = False
            else:
                current.append(ch)
        elif ch == '"':
            in_quotes = True
        elif ch == ",":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if in_quotes:
        raise CsvError(file, line_no, "unterminated quoted field")
    fields.append("".join(current))
    return fields


def read_csv(path: Path) -> list[tuple[int, Record]]:
    """Return (line number, record) pairs. Empty fields are returned as empty strings."""
    if not path.exists():
        raise CsvError(path.name, 0, "file not found")
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise CsvError(path.name, 0, "missing header row")
    header = parse_line(lines[0], path.name, 1)
    records: list[tuple[int, Record]] = []
    for index, line in enumerate(lines[1:], start=2):
        if line.strip() == "":
            continue
        fields = parse_line(line, path.name, index)
        if len(fields) != len(header):
            raise CsvError(path.name, index, f"expected {len(header)} fields, found {len(fields)}")
        records.append((index, dict(zip(header, fields))))
    return records
