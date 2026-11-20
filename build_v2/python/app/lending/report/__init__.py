"""Plain-text output, one formatting function per command. Output formats are part of the CLI contract."""
from __future__ import annotations

from typing import Optional

NONE = "-"


def or_none(value: Optional[str]) -> str:
    """Render an optional text field, using "-" when it is absent."""
    return value if value is not None else NONE
