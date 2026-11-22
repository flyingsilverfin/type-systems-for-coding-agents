"""CSV lines, as written by EXPORT and read by IMPORT.

CSV text is handled one line (record) at a time, so a line break never
separates records. Fields are separated by commas. A field is quoted with " if
it contains a comma, a quote or a line break (\\n or \\r), with any " doubled.
An unquoted empty field is distinct from a quoted empty field "": the former
represents a missing value (SQL NULL), the latter an empty string.

The standard library `csv` module can't distinguish an empty quoted field from
an empty unquoted one, so this is a small implementation of its own.
"""

_SPECIAL = frozenset(',"\n\r')


class CSVError(ValueError):
    """An invalid CSV line."""


def format_field(field: str | None) -> str:
    """Formats a single CSV field, quoting it if necessary. None is an
    unquoted empty field."""
    if field is None:
        return ""
    if not field or any(c in _SPECIAL for c in field):
        return '"{}"'.format(field.replace('"', '""'))
    return field


def format_line(fields: list[str | None]) -> str:
    """Formats CSV fields as a line."""
    return ",".join(format_field(field) for field in fields)


def parse_line(line: str) -> list[str | None]:
    """Parses a CSV line into fields. An unquoted empty field is None. A line
    always has one more field than it has separating commas, so an empty
    line is a single None field.

    A quoted field extends to the matching closing quote (where "" stands for
    one "), which must be followed by a comma or the end of the line. Any
    other field extends to the next comma and can't contain quotes.

    Raises CSVError if the line is not valid CSV.
    """
    fields: list[str | None] = []
    pos = 0
    while True:
        if line.startswith('"', pos):
            # A quoted field, up to the matching closing quote.
            field = ""
            pos += 1
            while True:
                end = line.find('"', pos)
                if end < 0:
                    raise CSVError("unterminated quoted field")
                field += line[pos:end]
                pos = end + 1
                if line.startswith('"', pos):  # "" is an escaped "
                    field += '"'
                    pos += 1
                    continue
                break
            if pos < len(line) and line[pos] != ",":
                raise CSVError("unexpected character after quoted field")
            fields.append(field)
        else:
            # An unquoted field, up to the next comma.
            end = line.find(",", pos)
            if end < 0:
                end = len(line)
            field = line[pos:end]
            if '"' in field:
                raise CSVError("quote in unquoted field")
            fields.append(field if field else None)
            pos = end
        # pos is now at a comma or the end of the line.
        if pos >= len(line):
            return fields
        pos += 1
