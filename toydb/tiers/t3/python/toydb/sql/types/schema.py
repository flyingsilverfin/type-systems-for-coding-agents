"""SQL table schemas.

Mirrors `src/sql/types/schema.rs`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer
from toydb.error import errinput
from toydb.sql.parser.lexer import format_ident
from toydb.sql.types.value import DataType, Row, Value

if TYPE_CHECKING:
    from toydb.sql.engine import Catalog, Transaction


@dataclass
class Column:
    """A table column."""

    name: str
    """Column name. Can't be empty."""
    datatype: DataType
    """Column datatype."""
    nullable: bool
    """Whether the column allows null values. Not legal for primary keys."""
    default: Value | None
    """The column's default value. If None, the user must specify an explicit
    value. Must match the column datatype. Nullable columns require a default
    (often Null). Null is only a valid default when nullable."""
    unique: bool
    """Whether the column should only allow unique values (ignoring NULLs).
    Must be true for a primary key column. Requires index."""
    index: bool
    """Whether the column should have a secondary index. Must be false for
    primary keys, which are the primary index. Must be true for unique or
    reference columns."""
    references: str | None
    """If set, this column is a foreign key reference to the given table's
    primary key. Must be of the same type as the target primary key. Requires
    index."""

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.name)
        self.datatype.serialize(serializer)
        serializer.serialize_bool(self.nullable)
        serde.Option(Value).serialize(self.default, serializer)
        serializer.serialize_bool(self.unique)
        serializer.serialize_bool(self.index)
        serde.Option(serde.String).serialize(self.references, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Column":
        deserializer.deserialize_struct()
        return cls(
            name=deserializer.deserialize_str(),
            datatype=DataType.deserialize(deserializer),
            nullable=deserializer.deserialize_bool(),
            default=serde.Option(Value).deserialize(deserializer),
            unique=deserializer.deserialize_bool(),
            index=deserializer.deserialize_bool(),
            references=serde.Option(serde.String).deserialize(deserializer),
        )


@dataclass
class Table(encoding.Value):
    """A table schema, which specifies its data structure and constraints.

    Tables can't change after they are created. There is no ALTER TABLE nor
    CREATE/DROP INDEX, only CREATE TABLE and DROP TABLE.
    """

    name: str
    """The table name. Unique identifier for the table. Can't be empty."""
    primary_key: int
    """The primary key column index. A table must have a primary key, and it
    can only be a single column."""
    columns: list[Column]
    """The table's columns. Must have at least one."""

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.name)
        serializer.serialize_u64(self.primary_key)
        serde.Vec(Column).serialize(self.columns, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Table":
        deserializer.deserialize_struct()
        return cls(
            name=deserializer.deserialize_str(),
            primary_key=deserializer.deserialize_u64(),
            columns=serde.Vec(Column).deserialize(deserializer),
        )

    def __str__(self) -> str:
        """Formats the table as a SQL CREATE TABLE statement."""
        return self.format(str)

    def format(self, format_value: Callable[[Value], str]) -> str:
        """Formats the table as a SQL CREATE TABLE statement, using the given
        function to format default values (the display uses the value
        display, SQL export uses SQL literals)."""
        output = f"CREATE TABLE {format_ident(self.name)} (\n"
        for i, column in enumerate(self.columns):
            output += f"  {format_ident(column.name)} {column.datatype}"
            if i == self.primary_key:
                output += " PRIMARY KEY"
            elif not column.nullable:
                output += " NOT NULL"
            if column.default is not None:
                output += f" DEFAULT {format_value(column.default)}"
            if i != self.primary_key:
                if column.unique:
                    output += " UNIQUE"
                if column.index:
                    output += " INDEX"
            if column.references is not None:
                output += f" REFERENCES {column.references}"
            if i < len(self.columns) - 1:
                output += ","
            output += "\n"
        return output + ")"

    def validate(self, catalog: "Catalog") -> None:
        """Validates the table schema, using the catalog to validate foreign
        key references."""
        if not self.name:
            raise errinput("table name can't be empty")
        if not self.columns:
            raise errinput("table has no columns")
        if not 0 <= self.primary_key < len(self.columns):
            raise errinput("invalid primary key index")

        for i, column in enumerate(self.columns):
            if not column.name:
                raise errinput("column name can't be empty")
            cname, ctype = column.name, column.datatype  # for formatting convenience

            # Validate primary key.
            is_primary_key = i == self.primary_key
            if is_primary_key:
                if column.nullable:
                    raise errinput(f"primary key {cname} cannot be nullable")
                if not column.unique:
                    raise errinput(f"primary key {cname} must be unique")
                if column.index:
                    raise errinput(f"primary key {cname} can't have an index")

            # Validate default value.
            if column.default is None:
                if column.nullable:
                    raise errinput(f"nullable column {cname} must have a default value")
            else:
                vtype = column.default.datatype()
                if vtype is None and not column.nullable:
                    raise errinput(f"invalid NULL default for non-nullable column {cname}")
                if vtype is not None and vtype != column.datatype:
                    raise errinput(f"invalid default type {vtype} for {ctype} column {cname}")

            # Validate unique index.
            if column.unique and not column.index and not is_primary_key:
                raise errinput(f"unique column {cname} must have a secondary index")

            # Validate references.
            if (reference := column.references) is not None:
                if not column.index and not is_primary_key:
                    raise errinput(f"reference column {cname} must have a secondary index")
                if reference == self.name:
                    reftype = self.columns[self.primary_key].datatype
                elif (target := catalog.get_table(reference)) is not None:
                    reftype = target.columns[target.primary_key].datatype
                else:
                    raise errinput(f"unknown table {reference} referenced by column {cname}")
                if column.datatype != reftype:
                    raise errinput(
                        f"can't reference {reftype} primary key of {reference} "
                        f"from {ctype} column {cname}"
                    )

    def validate_row(self, row: Row, update: bool, txn: "Transaction") -> None:
        """Validates a row, including uniqueness and reference checks using
        the given transaction.

        If update is true, the row replaces an existing entry with the same
        primary key. Otherwise, it is an insert. Primary key changes are
        implemented as a delete+insert.

        Validating uniqueness and references individually for each row is not
        performant, but it's fine for our purposes.
        """
        if len(row) != len(self.columns):
            raise errinput(f"invalid row size for table {self.name}")

        # Validate primary key.
        id = row[self.primary_key]
        idslice = [id]
        if id.is_undefined():
            raise errinput(f"invalid primary key {id}")
        if not update and txn.get(self.name, idslice):
            raise errinput(f"primary key {id} already exists")

        for i, (column, value) in enumerate(zip(self.columns, row)):
            cname, ctype = column.name, column.datatype
            valueslice = [value]

            # Validate datatype.
            if (vtype := value.datatype()) is not None and vtype != ctype:
                raise errinput(f"invalid datatype {vtype} for {ctype} column {cname}")
            if value == Value.Null() and not column.nullable:
                raise errinput(f"NULL value not allowed for column {cname}")

            # Validate outgoing references.
            if (target := column.references) is not None:
                match value:
                    # NB: NaN is not a valid primary key, and not valid as a
                    # missing foreign key marker.
                    case Value.Null():
                        pass
                    case v if target == self.name and v == id:
                        pass
                    case v if not txn.get(target, valueslice):
                        raise errinput(f"reference {v} not in table {target}")
                    case _:
                        pass

            # Validate uniqueness constraints. Unique columns are indexed.
            if column.unique and i != self.primary_key and not value.is_undefined():
                index = txn.lookup_index(self.name, column.name, valueslice)
                if update:
                    index.remove(id)  # ignore existing version of this row
                if not index.is_empty():
                    raise errinput(f"value {value} already in unique column {cname}")
