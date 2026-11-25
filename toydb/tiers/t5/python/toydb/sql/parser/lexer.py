"""The SQL lexer.

Mirrors `src/sql/parser/lexer.rs`.
"""

import enum
import unicodedata
from abc import ABC
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, ClassVar

from toydb.error import Error, errinput
from toydb.fmt import debug_tuple

if TYPE_CHECKING:
    from toydb.sql.types import DataType

_U64_MAX = 2**64 - 1
"""The maximum of a `u64`."""


class Token(ABC):
    """A lexical token.

    These carry their own strings rather than references into the original
    input string, because the lexer may need to modify the string (e.g. to
    parse escaped quotes in strings, lowercase identifiers, etc).
    """

    Number: ClassVar[type["Number"]]
    String: ClassVar[type["String"]]
    Ident: ClassVar[type["Ident"]]
    Keyword: ClassVar[type["TokenKeyword"]]
    Parameter: ClassVar[type["Parameter"]]
    Period: ClassVar[type["Period"]]
    Equal: ClassVar[type["Equal"]]
    NotEqual: ClassVar[type["NotEqual"]]
    GreaterThan: ClassVar[type["GreaterThan"]]
    GreaterThanOrEqual: ClassVar[type["GreaterThanOrEqual"]]
    LessThan: ClassVar[type["LessThan"]]
    LessThanOrEqual: ClassVar[type["LessThanOrEqual"]]
    LessOrGreaterThan: ClassVar[type["LessOrGreaterThan"]]
    Plus: ClassVar[type["Plus"]]
    Minus: ClassVar[type["Minus"]]
    Asterisk: ClassVar[type["Asterisk"]]
    Slash: ClassVar[type["Slash"]]
    Caret: ClassVar[type["Caret"]]
    Percent: ClassVar[type["Percent"]]
    Exclamation: ClassVar[type["Exclamation"]]
    Question: ClassVar[type["Question"]]
    Comma: ClassVar[type["Comma"]]
    Semicolon: ClassVar[type["Semicolon"]]
    OpenParen: ClassVar[type["OpenParen"]]
    CloseParen: ClassVar[type["CloseParen"]]

    def __str__(self) -> str:
        match self:
            case Number(n):
                return n
            case String(s):
                return s
            case Ident(s):
                return s
            case TokenKeyword(k):
                return str(k)
            case Parameter(n):
                return f"${n}"
            case Period():
                return "."
            case Equal():
                return "="
            case NotEqual():
                return "!="
            case GreaterThan():
                return ">"
            case GreaterThanOrEqual():
                return ">="
            case LessThan():
                return "<"
            case LessThanOrEqual():
                return "<="
            case LessOrGreaterThan():
                return "<>"
            case Plus():
                return "+"
            case Minus():
                return "-"
            case Asterisk():
                return "*"
            case Slash():
                return "/"
            case Caret():
                return "^"
            case Percent():
                return "%"
            case Exclamation():
                return "!"
            case Question():
                return "?"
            case Comma():
                return ","
            case Semicolon():
                return ";"
            case OpenParen():
                return "("
            case CloseParen():
                return ")"
            case _:
                raise AssertionError(f"unknown token {self!r}")

    def __repr__(self) -> str:
        return self.fmt_debug()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the token like the Rust `#[derive(Debug)]`."""
        values = [getattr(self, field.name) for field in fields(self)]  # type: ignore[arg-type]
        name = "Keyword" if isinstance(self, TokenKeyword) else type(self).__name__
        return debug_tuple(name, values, pretty)

    @staticmethod
    def from_keyword(keyword: "Keyword") -> "Token":
        """Builds a keyword token. Mirrors the Rust `From<Keyword> for Token`."""
        return TokenKeyword(keyword)


@dataclass(frozen=True, repr=False)
class Number(Token):
    """A numeric string, with digits, decimal points, and/or exponents.
    Leading signs (e.g. -) are separate tokens."""

    value: str


@dataclass(frozen=True, repr=False)
class String(Token):
    """A Unicode string, with quotes stripped and escape sequences resolved."""

    value: str


@dataclass(frozen=True, repr=False)
class Ident(Token):
    """An identifier, with any quotes stripped. Lowercased if not quoted."""

    value: str


@dataclass(frozen=True, repr=False)
class TokenKeyword(Token):
    """A SQL keyword. This is the Rust `Token::Keyword`, available as
    `Token.Keyword`."""

    keyword: "Keyword"


@dataclass(frozen=True, repr=False)
class Parameter(Token):
    """A parameter $n of a prepared statement."""

    number: int


@dataclass(frozen=True, repr=False)
class Period(Token):
    """The symbol `.`."""


@dataclass(frozen=True, repr=False)
class Equal(Token):
    """The symbol `=`."""


@dataclass(frozen=True, repr=False)
class NotEqual(Token):
    """The symbol `!=`."""


@dataclass(frozen=True, repr=False)
class GreaterThan(Token):
    """The symbol `>`."""


@dataclass(frozen=True, repr=False)
class GreaterThanOrEqual(Token):
    """The symbol `>=`."""


@dataclass(frozen=True, repr=False)
class LessThan(Token):
    """The symbol `<`."""


@dataclass(frozen=True, repr=False)
class LessThanOrEqual(Token):
    """The symbol `<=`."""


@dataclass(frozen=True, repr=False)
class LessOrGreaterThan(Token):
    """The symbol `<>`."""


@dataclass(frozen=True, repr=False)
class Plus(Token):
    """The symbol `+`."""


@dataclass(frozen=True, repr=False)
class Minus(Token):
    """The symbol `-`."""


@dataclass(frozen=True, repr=False)
class Asterisk(Token):
    """The symbol `*`."""


@dataclass(frozen=True, repr=False)
class Slash(Token):
    """The symbol `/`."""


@dataclass(frozen=True, repr=False)
class Caret(Token):
    """The symbol `^`."""


@dataclass(frozen=True, repr=False)
class Percent(Token):
    """The symbol `%`."""


@dataclass(frozen=True, repr=False)
class Exclamation(Token):
    """The symbol `!`."""


@dataclass(frozen=True, repr=False)
class Question(Token):
    """The symbol `?`."""


@dataclass(frozen=True, repr=False)
class Comma(Token):
    """The symbol `,`."""


@dataclass(frozen=True, repr=False)
class Semicolon(Token):
    """The symbol `;`."""


@dataclass(frozen=True, repr=False)
class OpenParen(Token):
    """The symbol `(`."""


@dataclass(frozen=True, repr=False)
class CloseParen(Token):
    """The symbol `)`."""


Token.Number = Number
Token.String = String
Token.Ident = Ident
Token.Keyword = TokenKeyword
Token.Parameter = Parameter
Token.Period = Period
Token.Equal = Equal
Token.NotEqual = NotEqual
Token.GreaterThan = GreaterThan
Token.GreaterThanOrEqual = GreaterThanOrEqual
Token.LessThan = LessThan
Token.LessThanOrEqual = LessThanOrEqual
Token.LessOrGreaterThan = LessOrGreaterThan
Token.Plus = Plus
Token.Minus = Minus
Token.Asterisk = Asterisk
Token.Slash = Slash
Token.Caret = Caret
Token.Percent = Percent
Token.Exclamation = Exclamation
Token.Question = Question
Token.Comma = Comma
Token.Semicolon = Semicolon
Token.OpenParen = OpenParen
Token.CloseParen = CloseParen


class Keyword(enum.Enum):
    """Reserved SQL keywords. The values are the lowercase keywords, as
    matched by the lexer.

    The Rust `Keyword::True` and `Keyword::False` are named `True_` and
    `False_`, since they are reserved words in Python."""

    Analyze = "analyze"
    And = "and"
    As = "as"
    Asc = "asc"
    Audit = "audit"
    Begin = "begin"
    Bool = "bool"
    Boolean = "boolean"
    By = "by"
    Changes = "changes"
    Check = "check"
    Commit = "commit"
    Constraint = "constraint"
    Create = "create"
    Cross = "cross"
    Csv = "csv"
    Deallocate = "deallocate"
    Default = "default"
    Delete = "delete"
    Desc = "desc"
    Describe = "describe"
    Double = "double"
    Drop = "drop"
    Estimate = "estimate"
    Execute = "execute"
    Exists = "exists"
    Explain = "explain"
    Export = "export"
    False_ = "false"
    Float = "float"
    Format = "format"
    From = "from"
    Grant = "grant"
    Grants = "grants"
    Group = "group"
    Having = "having"
    If = "if"
    Import = "import"
    Index = "index"
    Infinity = "infinity"
    Inner = "inner"
    Insert = "insert"
    Int = "int"
    Integer = "integer"
    Into = "into"
    Is = "is"
    Join = "join"
    Json = "json"
    Key = "key"
    Left = "left"
    Like = "like"
    Limit = "limit"
    Lint = "lint"
    Materialized = "materialized"
    NaN = "nan"
    Not = "not"
    Null = "null"
    Of = "of"
    Offset = "offset"
    On = "on"
    Only = "only"
    Or = "or"
    Order = "order"
    Outer = "outer"
    Password = "password"
    Policy = "policy"
    Prepare = "prepare"
    Prepared = "prepared"
    Primary = "primary"
    Read = "read"
    References = "references"
    Refresh = "refresh"
    Reset = "reset"
    Revoke = "revoke"
    Right = "right"
    Role = "role"
    Rollback = "rollback"
    Select = "select"
    Sessions = "sessions"
    Set = "set"
    Show = "show"
    Since = "since"
    Sql = "sql"
    Statements = "statements"
    Statistics = "statistics"
    String = "string"
    Superuser = "superuser"
    System = "system"
    Table = "table"
    Text = "text"
    Time = "time"
    To = "to"
    Transaction = "transaction"
    True_ = "true"
    Unique = "unique"
    Update = "update"
    User = "user"
    Users = "users"
    Using = "using"
    Values = "values"
    Varchar = "varchar"
    View = "view"
    Views = "views"
    Where = "where"
    With = "with"
    Write = "write"

    @classmethod
    def try_from(cls, value: str) -> "Keyword | None":
        """Returns the keyword for the given string, or None if it's not a
        keyword. Only compares lowercase, which is enforced by the lexer."""
        assert not any(c.isupper() for c in value), "keyword must be lowercase"
        return cls._value2member_map_.get(value)  # type: ignore[return-value]

    def __str__(self) -> str:
        """Display keywords as uppercase."""
        return self.value.upper()

    def fmt_debug(self, pretty: bool = False) -> str:
        """Formats the keyword like the Rust `#[derive(Debug)]`."""
        return self.name.removesuffix("_")

    def datatype(self) -> "DataType | None":
        """Returns the data type that the keyword names in a column definition,
        if any."""
        # Imported here, since the types import the lexer.
        from toydb.sql.types import DataType

        match self:
            case Keyword.Bool | Keyword.Boolean:
                return DataType.Boolean
            case Keyword.Float | Keyword.Double:
                return DataType.Float
            case Keyword.Int | Keyword.Integer:
                return DataType.Integer
            case Keyword.String | Keyword.Text | Keyword.Varchar:
                return DataType.String
            case _:
                return None


# The characters with the Unicode White_Space property, which is what Rust's
# `char::is_whitespace` uses. Python's `str.isspace` differs slightly (it also
# includes the U+001C to U+001F separators).
_WHITESPACE = frozenset(
    "\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def _is_whitespace(c: str) -> bool:
    """Returns true if the character is whitespace, like `char::is_whitespace`."""
    return c in _WHITESPACE


def _is_alphabetic(c: str) -> bool:
    """Returns true if the character is alphabetic, like `char::is_alphabetic`
    (the Unicode Alphabetic property). The standard library can only
    approximate this as letters and letter numbers: it does not know the
    Other_Alphabetic property (mostly combining vowel signs)."""
    return c.isalpha() or unicodedata.category(c) == "Nl"


def _is_alphanumeric(c: str) -> bool:
    """Returns true if the character is alphanumeric, like
    `char::is_alphanumeric` (Alphabetic, or a number in category Nd, Nl or No)."""
    return _is_alphabetic(c) or unicodedata.category(c) in ("Nd", "Nl", "No")


def _is_ascii_digit(c: str) -> bool:
    """Returns true if the character is 0-9, like `char::is_ascii_digit`."""
    return "0" <= c <= "9"


class Lexer(Iterator[Token]):
    """The lexer (lexical analyzer) preprocesses raw SQL strings into a
    sequence of lexical tokens (e.g. keyword, number, string, etc), which are
    passed on to the SQL parser. In doing so, it strips away basic syntactic
    noise such as whitespace, case, and quotes, and performs initial symbol
    validation.

    The lexer is used as a token iterator. It raises an `Error` on invalid
    input (the Rust iterator yields `Result<Token>`).
    """

    def __init__(self, input: str) -> None:
        """Creates a new lexer for the given string."""
        self.chars = input
        self.pos = 0
        """The position of the next character in chars."""

    def __next__(self) -> Token:
        token = self.scan()
        if token is not None:
            return token
        # If there's any remaining chars, the lexer didn't recognize them.
        if (c := self.peek()) is not None:
            raise errinput(f"unexpected character {c}")
        raise StopIteration

    def peek(self) -> str | None:
        """Returns the next character without consuming it, if any."""
        return self.chars[self.pos] if self.pos < len(self.chars) else None

    def next_char(self) -> str | None:
        """Consumes and returns the next character, if any."""
        c = self.peek()
        if c is not None:
            self.pos += 1
        return c

    def next_if(self, predicate: Callable[[str], bool]) -> str | None:
        """Returns the next character if it satisfies the predicate."""
        c = self.peek()
        if c is None or not predicate(c):
            return None
        return self.next_char()

    def next_if_map[T](self, map: Callable[[str], T | None]) -> T | None:
        """Applies a closure to the next character, returning its result and
        consuming the next character if it's not None."""
        c = self.peek()
        if c is None or (value := map(c)) is None:
            return None
        self.next_char()
        return value

    def next_is(self, c: str) -> bool:
        """Returns true if the next character is the given character,
        consuming it."""
        return self.next_if(lambda n: n == c) is not None

    def scan(self) -> Token | None:
        """Scans the next token, if any."""
        # Ignore whitespace.
        self.skip_whitespace()
        c = self.peek()
        if c is None:
            return None
        # The first character tells us the token kind. Scan it accordingly.
        match c:
            case "'":
                return self.scan_string()
            case '"':
                return self.scan_ident_quoted()
            case "$":
                return self.scan_parameter()
            case _ if _is_ascii_digit(c):
                return self.scan_number()
            case _ if _is_alphabetic(c):
                return self.scan_ident_or_keyword()
            case _:
                return self.scan_symbol()

    def scan_ident_or_keyword(self) -> Token | None:
        """Scans the next identifier or keyword, if any. It's converted to
        lowercase, by SQL convention."""
        # The first character must be alphabetic. The rest can be numeric.
        first = self.next_if(_is_alphabetic)
        if first is None:
            return None
        name = first.lower()
        while (c := self.next_if(lambda c: _is_alphanumeric(c) or c == "_")) is not None:
            name += c.lower()
        # Check if the identifier matches a keyword.
        if (keyword := Keyword.try_from(name)) is not None:
            return Token.Keyword(keyword)
        return Token.Ident(name)

    def scan_ident_quoted(self) -> Token | None:
        """Scans the next quoted identifier, if any. Case is preserved."""
        if not self.next_is('"'):
            return None
        ident = ""
        while True:
            match self.next_char():
                # "" is the escape sequence for ".
                case '"' if self.next_is('"'):
                    ident += '"'
                case '"':
                    break
                case None:
                    raise errinput("unexpected end of quoted identifier")
                case c:
                    ident += c
        return Token.Ident(ident)

    def scan_number(self) -> Token | None:
        """Scans the next number, if any."""
        # Scan the integer part. There must be at least one digit.
        first = self.next_if(_is_ascii_digit)
        if first is None:
            return None
        number = first
        while (c := self.next_if(_is_ascii_digit)) is not None:
            number += c
        # Scan the fractional part, if any.
        if self.next_is("."):
            number += "."
            while (dec := self.next_if(_is_ascii_digit)) is not None:
                number += dec
        # Scan the exponent, if any.
        if (exp := self.next_if(lambda c: c in ("e", "E"))) is not None:
            number += exp
            if (sign := self.next_if(lambda c: c in ("+", "-"))) is not None:
                number += sign
            while (c := self.next_if(_is_ascii_digit)) is not None:
                number += c
        return Token.Number(number)

    def scan_parameter(self) -> Token | None:
        """Scans the next parameter, if any."""
        start = self.pos
        if not self.next_is("$"):
            return None
        digits = ""
        while (c := self.next_if(_is_ascii_digit)) is not None:
            digits += c
        if not digits:
            # Not a parameter: leave the $ unconsumed.
            self.pos = start
            return None
        number = int(digits)
        if number == 0 or number > _U64_MAX:
            raise errinput(f"invalid parameter ${digits}")
        return Token.Parameter(number)

    def scan_string(self) -> Token | None:
        """Scans the next quoted string literal, if any."""
        if not self.next_is("'"):
            return None
        string = ""
        while True:
            match self.next_char():
                # '' is the escape sequence for '.
                case "'" if self.next_is("'"):
                    string += "'"
                case "'":
                    break
                case None:
                    raise errinput("unexpected end of string literal")
                case c:
                    string += c
        return Token.String(string)

    def scan_symbol(self) -> Token | None:
        """Scans the next symbol token, if any."""
        token = self.next_if_map(_SYMBOLS.get)
        # Handle two-character tokens, e.g. !=.
        match token:
            case Token.Exclamation() if self.next_is("="):
                return Token.NotEqual()
            case Token.GreaterThan() if self.next_is("="):
                return Token.GreaterThanOrEqual()
            case Token.LessThan() if self.next_is(">"):
                return Token.LessOrGreaterThan()
            case Token.LessThan() if self.next_is("="):
                return Token.LessThanOrEqual()
            case _:
                return token

    def skip_whitespace(self) -> None:
        """Skips any whitespace."""
        while self.next_if(_is_whitespace) is not None:
            pass


# Single-character symbol tokens.
_SYMBOLS: dict[str, Token] = {
    ".": Token.Period(),
    "=": Token.Equal(),
    ">": Token.GreaterThan(),
    "<": Token.LessThan(),
    "+": Token.Plus(),
    "-": Token.Minus(),
    "*": Token.Asterisk(),
    "/": Token.Slash(),
    "^": Token.Caret(),
    "%": Token.Percent(),
    "!": Token.Exclamation(),
    "?": Token.Question(),
    ",": Token.Comma(),
    ";": Token.Semicolon(),
    "(": Token.OpenParen(),
    ")": Token.CloseParen(),
}


def is_ident(ident: str) -> bool:
    """Returns true if the entire given string is a single valid identifier."""
    lexer = Lexer(ident)
    try:
        if not isinstance(next(lexer, None), Token.Ident):
            return False
        return next(lexer, None) is None  # if further tokens, it's not a lone identifier
    except Error:
        return False


def format_ident(ident: str) -> str:
    """Formats an identifier as valid SQL, quoting it if necessary: unquoted
    if the lexer reads it as a single identifier, otherwise between double
    quotes with any " doubled. Since unquoted identifiers are lowercased by
    the lexer, letter case is not considered (e.g. `Name` is not quoted)."""
    if is_ident(ident):
        return ident
    return '"{}"'.format(ident.replace('"', '""'))
