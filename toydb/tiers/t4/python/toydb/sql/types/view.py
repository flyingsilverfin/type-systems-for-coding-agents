"""View definitions. A view is a named query, stored in the catalog as the
canonical one-line text of its query together with its column names and the
names of the tables and views that its query reads (its dependencies). The
query is parsed and planned again every time a statement uses the view.
"""

from dataclasses import dataclass

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.sql.parser.format import format_name


@dataclass
class View(encoding.Value):
    """A view definition, as stored in the catalog."""

    name: str
    """The view name. Views and tables share one namespace."""
    columns: list[str]
    """The column names, in query column order."""
    query: str
    """The canonical one-line text of the view's query."""
    dependencies: list[str]
    """The tables and views that the query's FROM clause reads, by name."""

    def __str__(self) -> str:
        """Formats the view as a CREATE VIEW statement."""
        columns = ", ".join(format_name(column) for column in self.columns)
        return f"CREATE VIEW {format_name(self.name)} ({columns}) AS {self.query}"

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.name)
        _NAMES.serialize(self.columns, serializer)
        serializer.serialize_str(self.query)
        _NAMES.serialize(self.dependencies, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "View":
        deserializer.deserialize_struct()
        return cls(
            name=deserializer.deserialize_str(),
            columns=_NAMES.deserialize(deserializer),
            query=deserializer.deserialize_str(),
            dependencies=_NAMES.deserialize(deserializer),
        )


_NAMES: serde.Serde[list[str]] = serde.Vec(serde.String)
"""The encoding of the column and dependency names, a `Vec<String>`."""
