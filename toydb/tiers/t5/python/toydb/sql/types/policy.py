"""Row-level security policies, as stored in the catalog: a policy of a table
has a name, optionally the user it applies to, and the canonical one-line text
of its USING condition and, if it has one, of its WITH CHECK condition.
"""

from dataclasses import dataclass

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.sql.parser.format import format_name


@dataclass(frozen=True)
class Policy(encoding.Value):
    """A row-level security policy: its table, name, user (none for all
    users), USING condition and WITH CHECK condition (if any)."""

    table: str
    """The table name."""
    name: str
    """The policy name."""
    user: str | None
    """The user the policy applies to, or None for all users."""
    condition: str
    """The canonical one-line text of the USING condition."""
    check: str | None
    """The canonical one-line text of the WITH CHECK condition, if any."""

    def __str__(self) -> str:
        """Formats the policy as a CREATE POLICY statement."""
        output = f"CREATE POLICY {format_name(self.name)} ON {format_name(self.table)}"
        if self.user is not None:
            output += f" TO {format_name(self.user)}"
        output += f" USING ({self.condition})"
        if self.check is not None:
            output += f" WITH CHECK ({self.check})"
        return output

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.table)
        serializer.serialize_str(self.name)
        serde.Option(serde.String).serialize(self.user, serializer)
        serializer.serialize_str(self.condition)
        serde.Option(serde.String).serialize(self.check, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Policy":
        deserializer.deserialize_struct()
        return cls(
            table=deserializer.deserialize_str(),
            name=deserializer.deserialize_str(),
            user=serde.Option(serde.String).deserialize(deserializer),
            condition=deserializer.deserialize_str(),
            check=serde.Option(serde.String).deserialize(deserializer),
        )


POLICIES: serde.Serde[list[Policy]] = serde.Vec(Policy)
"""The encoding of a list of policies, a `Vec<Policy>`."""
