"""Users, memberships and privileges, as stored in the catalog.

A user has a name, a superuser flag and optionally a password, stored as
PostgreSQL stores MD5 passwords: `md5` and the hex MD5 digest of the password
followed by the user name. The superuser `admin` always exists and is not
stored. A user can be a member of other users; it acts for itself, for the
users it is a member of, and for the users those act for. A table or view has
an owner, the user that created it (objects of `admin` are owned by `admin`,
which is not stored), and privileges granted to users: `SELECT`, `INSERT`,
`UPDATE`, `DELETE`. A statement that creates a table or view, also one added
later, makes the current user its owner. A statement that changes a table's
definition doesn't change its owner or its privileges, and a CREATE TABLE that
doesn't create a table doesn't change the existing table's owner.
"""

import enum
from dataclasses import dataclass

from toydb import encoding
from toydb.encoding import serde
from toydb.encoding.serde import Deserializer, Serializer

ADMIN = "admin"
"""The name of the superuser admin."""


@dataclass(frozen=True)
class User(encoding.Value):
    """A user, as stored in the catalog."""

    name: str
    """The user name."""
    superuser: bool
    """Whether the user is a superuser."""
    password: str | None
    """The user's password as an MD5 hash, if it has one."""

    @classmethod
    def admin(cls) -> "User":
        """Returns the superuser admin."""
        return cls(name=ADMIN, superuser=True, password=None)

    def __str__(self) -> str:
        """Formats the user for the catalog key display."""
        superuser = "true" if self.superuser else "false"
        password = "None" if self.password is None else self.password
        return f"{self.name} superuser={superuser} password={password}"

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.name)
        serializer.serialize_bool(self.superuser)
        serde.Option(serde.String).serialize(self.password, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "User":
        deserializer.deserialize_struct()
        return cls(
            name=deserializer.deserialize_str(),
            superuser=deserializer.deserialize_bool(),
            password=serde.Option(serde.String).deserialize(deserializer),
        )


class Privilege(enum.Enum):
    """A privilege on a table or view: SELECT, INSERT, UPDATE or DELETE."""

    Select = 0
    Insert = 1
    Update = 2
    Delete = 3

    def __str__(self) -> str:
        return self.name.upper()

    def __lt__(self, other: "Privilege") -> bool:
        return self.value < other.value

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_unit_variant(self.value)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Privilege":
        index = deserializer.deserialize_enum()
        if index >= len(cls):
            raise deserializer.invalid_variant(index, len(cls))
        return cls(index)


PRIVILEGES: serde.Serde[list[Privilege]] = serde.Vec(Privilege)
"""The encoding of a set of privileges, a `BTreeSet<Privilege>` (a sorted
`Vec<Privilege>`)."""


@dataclass(frozen=True)
class Grant(encoding.Value):
    """The privileges granted to a user on a table or view."""

    object: str
    grantee: str
    privileges: list[Privilege]
    """The granted privileges."""

    def serialize(self, serializer: Serializer) -> None:
        serializer.serialize_struct()
        serializer.serialize_str(self.object)
        serializer.serialize_str(self.grantee)
        PRIVILEGES.serialize(self.privileges, serializer)

    @classmethod
    def deserialize(cls, deserializer: Deserializer) -> "Grant":
        deserializer.deserialize_struct()
        return cls(
            object=deserializer.deserialize_str(),
            grantee=deserializer.deserialize_str(),
            privileges=PRIVILEGES.deserialize(deserializer),
        )


type Membership = tuple[str, str]
"""A membership: the member and the user it is a member of."""

MEMBERSHIP: serde.Serde[Membership] = serde.Tuple(serde.String, serde.String)
"""The encoding of a membership, a `(String, String)`."""
