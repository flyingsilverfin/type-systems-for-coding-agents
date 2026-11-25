"""System tables: the catalog, the server's sessions and audit log, and the SQL
language, as read-only tables that queries read. Their names begin with
`toydb_`; a table or view of the same name comes first. They are computed when
a `SystemTable` plan node is executed, in the statement's transaction, and are
not exported or backed up.

* `toydb_tables`, `toydb_columns`, `toydb_views`, `toydb_checks`,
  `toydb_policies`, `toydb_users`, `toydb_members`, `toydb_grants`,
  `toydb_statistics`, `toydb_matviews`: the catalog. `toydb_sessions`,
  `toydb_audit` (superusers only), `toydb_prepared` and `toydb_statements`
  (the reading session's): as their SHOW statements. `pg_type`: the
  PostgreSQL types that the wire protocol uses, by OID.
* `toydb_keywords`: every reserved keyword, also those added later, in upper
  case, with its category, the first that applies: `statement` (a statement
  begins with it), `type` (it names a data type in a column definition),
  `literal` (it is a literal on its own), `operator` (it is a word of an
  operator of the precedence table, levels 1 to 10 — `AND`, `OR`, `NOT`,
  `LIKE`, `IS` — or of an operator added later at one of those levels),
  `reserved` (every other keyword).
* `toydb_functions`: every function and aggregate function that expressions
  can call, also those added later, by name in lower case, with its kind
  (`scalar` or `aggregate`) and its number of arguments (one row per number).
* `toydb_types`: every data type, also those added later, with the keywords
  that name it in a column definition (its spellings), the PostgreSQL type and
  OID the wire protocol sends it as, and its backup type tag.
"""

from typing import TYPE_CHECKING

from toydb.error import panic
from toydb.sql.engine import Catalog, Transaction
from toydb.sql.parser.format import format_name
from toydb.sql.parser.lexer import Keyword
from toydb.sql.types import Column, DataType, Row, Table, Value
from toydb.sql.types.formats import to_sql_literal

if TYPE_CHECKING:
    from toydb.sql.execution.session import Session

_BOOLEAN, _INTEGER, _STRING = DataType.Boolean, DataType.Integer, DataType.String


def column(name: str, datatype: DataType, nullable: bool = False) -> Column:
    """Returns a system table column."""
    return Column(
        name=name,
        datatype=datatype,
        nullable=nullable,
        default=None,
        unique=False,
        index=False,
        references=None,
    )


TABLES: dict[str, list[Column]] = {
    "toydb_tables": [
        column("name", _STRING),
        column("owner", _STRING),
        column("primary_key", _STRING),
        column("columns", _INTEGER),
        column("checks", _INTEGER),
        column("policies", _INTEGER),
    ],
    "toydb_columns": [
        column("table_name", _STRING),
        column("name", _STRING),
        column("position", _INTEGER),
        column("type", _STRING),
        column("nullable", _BOOLEAN),
        column("default_value", _STRING, nullable=True),
        column("primary_key", _BOOLEAN),
        column("is_unique", _BOOLEAN),
        column("has_index", _BOOLEAN),
        column("references_table", _STRING, nullable=True),
    ],
    "toydb_views": [
        column("name", _STRING),
        column("owner", _STRING),
        column("columns", _STRING),
        column("query", _STRING),
        column("dependencies", _STRING),
    ],
    "toydb_matviews": [
        column("name", _STRING),
        column("owner", _STRING),
        column("columns", _STRING),
        column("query", _STRING),
        column("dependencies", _STRING),
        column("rows", _INTEGER),
    ],
    "toydb_checks": [
        column("table_name", _STRING),
        column("name", _STRING),
        column("condition", _STRING),
    ],
    "toydb_policies": [
        column("table_name", _STRING),
        column("name", _STRING),
        column("grantee", _STRING, nullable=True),
        column("condition", _STRING),
        column("check_condition", _STRING, nullable=True),
    ],
    "toydb_users": [
        column("name", _STRING),
        column("is_superuser", _BOOLEAN),
        column("has_password", _BOOLEAN),
    ],
    "toydb_members": [column("member", _STRING), column("member_of", _STRING)],
    "toydb_grants": [
        column("object", _STRING),
        column("grantee", _STRING),
        column("privilege", _STRING),
    ],
    "toydb_statistics": [
        column("table_name", _STRING),
        column("column_name", _STRING),
        column("rows", _INTEGER),
        column("nulls", _INTEGER),
        column("distinct_values", _INTEGER),
        column("min_value", _STRING, nullable=True),
        column("max_value", _STRING, nullable=True),
    ],
    "toydb_sessions": [
        column("session", _INTEGER),
        column("username", _STRING),
        column("login", _STRING),
        column("txn", _INTEGER, nullable=True),
        column("executed", _INTEGER),
    ],
    "toydb_audit": [
        column("seq", _INTEGER),
        column("username", _STRING),
        column("class", _STRING),
        column("statement", _STRING),
        column("objects", _STRING),
        column("error", _STRING, nullable=True),
    ],
    "toydb_prepared": [
        column("name", _STRING),
        column("parameters", _STRING),
        column("statement", _STRING),
        column("executions", _INTEGER),
        column("plans", _INTEGER),
    ],
    "toydb_statements": [
        column("digest", _STRING),
        column("calls", _INTEGER),
        column("errors", _INTEGER),
        column("rows", _INTEGER),
    ],
    "toydb_keywords": [column("word", _STRING), column("category", _STRING)],
    "toydb_functions": [
        column("name", _STRING),
        column("kind", _STRING),
        column("arguments", _INTEGER),
    ],
    "toydb_types": [
        column("name", _STRING),
        column("spellings", _STRING),
        column("pg_type", _STRING),
        column("pg_oid", _INTEGER),
        column("backup_tag", _INTEGER),
    ],
    "pg_type": [column("oid", _INTEGER), column("typname", _STRING), column("typlen", _INTEGER)],
}
"""The columns of the system tables, by name."""

STATEMENT_KEYWORDS: frozenset[Keyword] = frozenset(
    {
        Keyword.Analyze,
        Keyword.Begin,
        Keyword.Commit,
        Keyword.Create,
        Keyword.Deallocate,
        Keyword.Delete,
        Keyword.Describe,
        Keyword.Drop,
        Keyword.Estimate,
        Keyword.Execute,
        Keyword.Explain,
        Keyword.Export,
        Keyword.Format,
        Keyword.Grant,
        Keyword.Import,
        Keyword.Insert,
        Keyword.Lint,
        Keyword.Prepare,
        Keyword.Refresh,
        Keyword.Reset,
        Keyword.Revoke,
        Keyword.Rollback,
        Keyword.Select,
        Keyword.Set,
        Keyword.Show,
        Keyword.Update,
    }
)
"""The keywords that statements begin with."""

LITERAL_KEYWORDS: frozenset[Keyword] = frozenset(
    {Keyword.True_, Keyword.False_, Keyword.Null, Keyword.NaN, Keyword.Infinity}
)
"""The keywords that are literals on their own."""

OPERATOR_KEYWORDS: frozenset[Keyword] = frozenset(
    {Keyword.And, Keyword.Or, Keyword.Not, Keyword.Like, Keyword.Is}
)
"""The keywords of the operators of the precedence table."""

FUNCTIONS: list[tuple[str, str, int]] = [
    ("avg", "aggregate", 1),
    ("count", "aggregate", 1),
    ("max", "aggregate", 1),
    ("min", "aggregate", 1),
    ("sqrt", "scalar", 1),
    ("sum", "aggregate", 1),
]
"""The functions and aggregate functions that expressions can call, with
their kind and number of arguments."""


def schema(name: str) -> Table | None:
    """Returns the schema of the system table of that name, if there is
    one."""
    if (columns := TABLES.get(name)) is None:
        return None
    # System tables have no primary key; the field is unused.
    return Table(name=name, primary_key=0, columns=columns)


def is_system_table(catalog: Catalog, name: str) -> bool:
    """Returns true if a name in a FROM clause refers to a system table."""
    return (
        name in TABLES
        and catalog.get_table(name) is None
        and catalog.get_view(name) is None
        and catalog.get_matview(name) is None
    )


def rows(name: str, txn: Transaction, session: "Session") -> list[Row]:
    """Computes the rows of a system table in the transaction."""
    # Imported here, since these modules import the planner, which imports
    # this module.
    from toydb.sql.execution import users

    match name:
        case "toydb_tables":
            return table_rows(txn)
        case "toydb_columns":
            return column_rows(txn)
        case "toydb_views":
            return view_rows(txn)
        case "toydb_matviews":
            return matview_rows(txn)
        case "toydb_checks":
            return [
                [Value.String(table.name), Value.String(check.name), Value.String(check.condition)]
                for table in tables(txn)
                for check in txn.get_checks(table.name)
            ]
        case "toydb_policies":
            return [
                [
                    Value.String(table.name),
                    Value.String(policy.name),
                    optional(policy.user),
                    Value.String(policy.condition),
                    optional(policy.check),
                ]
                for table in tables(txn)
                for policy in txn.list_policies(table.name)
            ]
        case "toydb_users":
            return [
                [
                    Value.String(user.name),
                    Value.Boolean(user.superuser),
                    Value.Boolean(user.password is not None),
                ]
                for user in sorted(txn.list_users(), key=lambda user: user.name)
            ]
        case "toydb_members":
            return [
                [Value.String(member), Value.String(group)]
                for member, group in sorted(txn.list_memberships())
            ]
        case "toydb_grants":
            return users.show_grants(txn)
        case "toydb_statistics":
            return statistics_rows(txn)
        case "toydb_sessions":
            with session.engine.sessions.lock() as sessions:
                return sessions.rows()
        case "toydb_audit":
            session.check_superuser(txn)
            with session.engine.audit.lock() as log:
                return [entry.row() for entry in log.since(0)]
        case "toydb_prepared":
            return session.prepared.rows()
        case "toydb_statements":
            return session.digests.rows()
        case "toydb_keywords":
            return [
                [Value.String(str(keyword)), Value.String(keyword_category(keyword))]
                for keyword in sorted(Keyword, key=str)
            ]
        case "toydb_functions":
            return [
                [Value.String(name), Value.String(kind), Value.Integer(arguments)]
                for name, kind, arguments in sorted(FUNCTIONS)
            ]
        case "toydb_types":
            return type_rows()
        case "pg_type":
            return pg_type_rows()
        case _:
            panic(f"unknown system table {name}")


def tables(catalog: Catalog) -> list[Table]:
    """Returns the tables."""
    return sorted(catalog.list_tables(), key=lambda table: table.name)


def optional(value: str | None) -> Value:
    """Returns a string value, or NULL."""
    return Value.String(value) if value is not None else Value.Null()


def table_rows(txn: Transaction) -> list[Row]:
    """Returns the rows of toydb_tables."""
    return [
        [
            Value.String(table.name),
            Value.String(txn.get_owner(table.name)),
            Value.String(table.columns[table.primary_key].name),
            Value.Integer(len(table.columns)),
            Value.Integer(len(txn.get_checks(table.name))),
            Value.Integer(len(txn.list_policies(table.name))),
        ]
        for table in tables(txn)
    ]


def column_rows(txn: Transaction) -> list[Row]:
    """Returns the rows of toydb_columns."""
    rows: list[Row] = []
    for table in tables(txn):
        for i, column in enumerate(table.columns):
            default = to_sql_literal(column.default) if column.default is not None else None
            rows.append(
                [
                    Value.String(table.name),
                    Value.String(column.name),
                    Value.Integer(i + 1),
                    Value.String(str(column.datatype)),
                    Value.Boolean(column.nullable),
                    optional(default),
                    Value.Boolean(i == table.primary_key),
                    Value.Boolean(column.unique),
                    Value.Boolean(column.index),
                    optional(column.references),
                ]
            )
    return rows


def view_rows(txn: Transaction) -> list[Row]:
    """Returns the rows of toydb_views."""
    return [
        [
            Value.String(view.name),
            Value.String(txn.get_owner(view.name)),
            Value.String(", ".join(format_name(column) for column in view.columns)),
            Value.String(view.query),
            Value.String(", ".join(view.dependencies)),
        ]
        for view in sorted(txn.list_views(), key=lambda view: view.name)
    ]


def matview_rows(txn: Transaction) -> list[Row]:
    """Returns the rows of toydb_matviews."""
    return [
        [
            Value.String(matview.name),
            Value.String(txn.get_owner(matview.name)),
            Value.String(", ".join(format_name(column) for column in matview.columns)),
            Value.String(matview.query),
            Value.String(", ".join(matview.dependencies)),
            Value.Integer(len(txn.get_matview_rows(matview.name))),
        ]
        for matview in sorted(txn.list_matviews(), key=lambda matview: matview.name)
    ]


def statistics_rows(txn: Transaction) -> list[Row]:
    """Returns the rows of toydb_statistics."""
    rows: list[Row] = []
    for table in tables(txn):
        if (statistics := txn.get_statistics(table.name)) is None:
            continue
        for column in table.columns:
            if (stats := statistics.columns.get(column.name)) is None:
                continue
            rows.append(
                [
                    Value.String(table.name),
                    Value.String(column.name),
                    Value.Integer(statistics.rows),
                    Value.Integer(stats.nulls),
                    Value.Integer(stats.distinct),
                    optional(str(stats.min) if stats.min is not None else None),
                    optional(str(stats.max) if stats.max is not None else None),
                ]
            )
    return rows


def keyword_category(keyword: Keyword) -> str:
    """Returns the category of a keyword, the first that applies: statement,
    type, literal, operator, reserved."""
    if keyword in STATEMENT_KEYWORDS:
        return "statement"
    if keyword.datatype() is not None:
        return "type"
    if keyword in LITERAL_KEYWORDS:
        return "literal"
    if keyword in OPERATOR_KEYWORDS:
        return "operator"
    return "reserved"


def spellings(datatype: DataType) -> list[str]:
    """Returns the spellings of a data type."""
    return sorted(str(keyword) for keyword in Keyword if keyword.datatype() == datatype)


def type_rows() -> list[Row]:
    """Returns the rows of toydb_types."""
    # Imported here, since these packages import the session, which imports
    # this module.
    from toydb.backup.format import TYPE_TAGS
    from toydb.pgwire.types import pg_type

    rows: list[Row] = []
    for datatype in sorted(DataType, key=lambda datatype: TYPE_TAGS[datatype]):
        pgtype = pg_type(datatype)
        rows.append(
            [
                Value.String(str(datatype)),
                Value.String(", ".join(spellings(datatype))),
                Value.String(pgtype.name),
                Value.Integer(pgtype.oid),
                Value.Integer(TYPE_TAGS[datatype]),
            ]
        )
    return rows


def pg_type_rows() -> list[Row]:
    """Returns the rows of pg_type."""
    from toydb.pgwire.types import TYPES

    return [
        [Value.Integer(oid), Value.String(pgtype.name), Value.Integer(pgtype.size)]
        for oid, pgtype in sorted(TYPES.items())
    ]
