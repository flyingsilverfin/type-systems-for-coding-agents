"""CSV lines, as written by EXPORT and read by IMPORT.

CSV text is handled as a list of lines, one per record: line breaks never
separate records, and a field containing one is quoted and stays within its
line. Fields are separated by `,`. A field is quoted (between `"`, with any `"`
doubled) if it contains `,`, `"`, `\\n` or `\\r`, or if it is empty but must not
be read as a missing value. An unquoted empty field is a missing value
(None), which the SQL extensions use for NULL.

The standard library `csv` module can't distinguish an empty quoted field from
an empty unquoted one, so this is a small implementation of its own.
"""

_SPECIAL = frozenset(',"\n\r')


class CSVError(ValueError):
    """An invalid CSV line."""


def format_field(field: str | None) -> str:
    """Formats a field, quoting it if necessary. None is an empty unquoted
    field."""
    if field is None:
        return ""
    if not field or any(c in _SPECIAL for c in field):
        return '"{}"'.format(field.replace('"', '""'))
    return field


def format_line(fields: list[str | None]) -> str:
    """Formats a line of fields."""
    return ",".join(format_field(field) for field in fields)


def parse_line(line: str) -> list[str | None]:
    """Parses a line into fields. An unquoted empty field is None. Raises
    CSVError if the line is invalid: an unterminated quoted field, a `"`
    inside an unquoted field, or a character after a closing quote."""
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
