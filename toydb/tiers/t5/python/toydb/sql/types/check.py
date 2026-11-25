"""CHECK constraints, as stored in the catalog: a table's constraints, each with
its name and the canonical one-line text of its condition.
"""

from dataclasses import dataclass

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.sql.parser.lexer import format_ident


@dataclass(frozen=True)
class Check(encoding.Value):
    """A CHECK constraint: its name and its condition, as canonical text."""

    name: str
    """The constraint name."""
    condition: str
    """The canonical one-line text of the condition."""

    def __str__(self) -> str:
        """Formats the constraint as a table constraint of CREATE TABLE."""
        return f"CONSTRAINT {format_ident(self.name)} CHECK ({self.condition})"

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.name)
        serializer.serialize_str(self.condition)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Check":
        deserializer.deserialize_struct()
        return cls(name=deserializer.deserialize_str(), condition=deserializer.deserialize_str())


CHECKS: serde.Serde[list[Check]] = serde.Vec(Check)
"""The encoding of a table's CHECK constraints, a `Vec<Check>`."""
