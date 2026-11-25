"""Materialized view definitions, as stored in the catalog: a name, the column
names, the canonical one-line text of the query and its dependencies, as for a
view. The rows are stored separately.
"""

from dataclasses import dataclass

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.sql.parser.format import format_name


@dataclass
class MaterializedView(encoding.Value):
    """A materialized view definition, as stored in the catalog."""

    name: str
    """The materialized view name."""
    columns: list[str]
    """The column names, in query column order."""
    query: str
    """The canonical one-line text of the query."""
    dependencies: list[str]
    """The names in the query's FROM clause."""

    def __str__(self) -> str:
        """Formats the materialized view as a CREATE MATERIALIZED VIEW
        statement."""
        columns = ", ".join(format_name(column) for column in self.columns)
        return f"CREATE MATERIALIZED VIEW {format_name(self.name)} ({columns}) AS {self.query}"

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.name)
        _NAMES.serialize(self.columns, serializer)
        serializer.serialize_str(self.query)
        _NAMES.serialize(self.dependencies, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "MaterializedView":
        deserializer.deserialize_struct()
        return cls(
            name=deserializer.deserialize_str(),
            columns=_NAMES.deserialize(deserializer),
            query=deserializer.deserialize_str(),
            dependencies=_NAMES.deserialize(deserializer),
        )


_NAMES: serde.Serde[list[str]] = serde.Vec(serde.String)
"""The encoding of the column and dependency names, a `Vec<String>`."""
