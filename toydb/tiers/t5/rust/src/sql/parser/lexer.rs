use std::fmt::Display;
use std::iter::Peekable;
use std::str::Chars;

use crate::errinput;
use crate::error::Result;
use crate::sql::types::DataType;

/// A lexical token.
///
/// These carry owned String clones rather than &str references into the
/// original input string, because the lexer may need to modify the string (e.g.
/// to parse escaped quotes in strings, lowercase identifiers, etc). We could
/// use `Cow<str>` to avoid this in the common case, but we'll end up using
/// owned strings in the final parsed AST anyway to avoid propagating these
/// lifetimes throughout the entire SQL execution engine, so we keep it simple.
#[derive(Clone, Debug, PartialEq)]
pub enum Token {
    /// A numeric string, with digits, decimal points, and/or exponents. Leading
    /// signs (e.g. -) are separate tokens.
    Number(String),
    /// A Unicode string, with quotes stripped and escape sequences resolved.
    String(String),
    /// An identifier, with any quotes stripped. Lowercased if not quoted.
    Ident(String),
    /// A SQL keyword.
    Keyword(Keyword),
    /// A parameter $n of a prepared statement.
    Parameter(u64),
    Period,             // .
    Equal,              // =
    NotEqual,           // !=
    GreaterThan,        // >
    GreaterThanOrEqual, // >=
    LessThan,           // <
    LessThanOrEqual,    // <=
    LessOrGreaterThan,  // <>
    Plus,               // +
    Minus,              // -
    Asterisk,           // *
    Slash,              // /
    Caret,              // ^
    Percent,            // %
    Exclamation,        // !
    Question,           // ?
    Comma,              // ,
    Semicolon,          // ;
    OpenParen,          // (
    CloseParen,         // )
}

impl Display for Token {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str(match self {
            Self::Number(n) => n,
            Self::String(s) => s,
            Self::Ident(s) => s,
            Self::Keyword(k) => return k.fmt(f),
            Self::Parameter(n) => return write!(f, "${n}"),
            Self::Period => ".",
            Self::Equal => "=",
            Self::NotEqual => "!=",
            Self::GreaterThan => ">",
            Self::GreaterThanOrEqual => ">=",
            Self::LessThan => "<",
            Self::LessThanOrEqual => "<=",
            Self::LessOrGreaterThan => "<>",
            Self::Plus => "+",
            Self::Minus => "-",
            Self::Asterisk => "*",
            Self::Slash => "/",
            Self::Caret => "^",
            Self::Percent => "%",
            Self::Exclamation => "!",
            Self::Question => "?",
            Self::Comma => ",",
            Self::Semicolon => ";",
            Self::OpenParen => "(",
            Self::CloseParen => ")",
        })
    }
}

impl From<Keyword> for Token {
    fn from(keyword: Keyword) -> Self {
        Self::Keyword(keyword)
    }
}

/// Reserved SQL keywords.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Keyword {
    Analyze,
    And,
    As,
    Asc,
    Audit,
    Begin,
    Bool,
    Boolean,
    By,
    Changes,
    Check,
    Commit,
    Constraint,
    Create,
    Cross,
    Csv,
    Deallocate,
    Default,
    Delete,
    Desc,
    Describe,
    Double,
    Drop,
    Estimate,
    Execute,
    Exists,
    Explain,
    Export,
    False,
    Float,
    Format,
    From,
    Grant,
    Grants,
    Group,
    Having,
    If,
    Import,
    Index,
    Infinity,
    Inner,
    Insert,
    Int,
    Integer,
    Into,
    Is,
    Join,
    Json,
    Key,
    Left,
    Like,
    Limit,
    Lint,
    Materialized,
    NaN,
    Not,
    Null,
    Of,
    Offset,
    On,
    Only,
    Or,
    Order,
    Outer,
    Password,
    Policy,
    Prepare,
    Prepared,
    Primary,
    Read,
    References,
    Refresh,
    Reset,
    Revoke,
    Right,
    Role,
    Rollback,
    Select,
    Sessions,
    Set,
    Show,
    Since,
    Sql,
    Statements,
    Statistics,
    String,
    Superuser,
    System,
    Table,
    Text,
    Time,
    To,
    Transaction,
    True,
    Unique,
    Update,
    User,
    Users,
    Using,
    Values,
    Varchar,
    View,
    Views,
    Where,
    With,
    Write,
}

impl TryFrom<&str> for Keyword {
    // Use a cheap static error string. This just indicates it's not a keyword.
    type Error = &'static str;

    fn try_from(value: &str) -> std::result::Result<Self, Self::Error> {
        // Only compare lowercase, which is enforced by the lexer. This avoids
        // allocating a string to change the case. Assert this.
        debug_assert!(value.chars().all(|c| !c.is_uppercase()), "keyword must be lowercase");
        Ok(match value {
            "analyze" => Self::Analyze,
            "as" => Self::As,
            "asc" => Self::Asc,
            "audit" => Self::Audit,
            "and" => Self::And,
            "begin" => Self::Begin,
            "bool" => Self::Bool,
            "boolean" => Self::Boolean,
            "by" => Self::By,
            "changes" => Self::Changes,
            "check" => Self::Check,
            "commit" => Self::Commit,
            "constraint" => Self::Constraint,
            "create" => Self::Create,
            "cross" => Self::Cross,
            "csv" => Self::Csv,
            "deallocate" => Self::Deallocate,
            "default" => Self::Default,
            "delete" => Self::Delete,
            "desc" => Self::Desc,
            "describe" => Self::Describe,
            "double" => Self::Double,
            "drop" => Self::Drop,
            "estimate" => Self::Estimate,
            "execute" => Self::Execute,
            "exists" => Self::Exists,
            "explain" => Self::Explain,
            "export" => Self::Export,
            "false" => Self::False,
            "float" => Self::Float,
            "format" => Self::Format,
            "from" => Self::From,
            "grant" => Self::Grant,
            "grants" => Self::Grants,
            "group" => Self::Group,
            "having" => Self::Having,
            "if" => Self::If,
            "import" => Self::Import,
            "index" => Self::Index,
            "infinity" => Self::Infinity,
            "inner" => Self::Inner,
            "insert" => Self::Insert,
            "int" => Self::Int,
            "integer" => Self::Integer,
            "into" => Self::Into,
            "is" => Self::Is,
            "join" => Self::Join,
            "json" => Self::Json,
            "key" => Self::Key,
            "left" => Self::Left,
            "like" => Self::Like,
            "limit" => Self::Limit,
            "lint" => Self::Lint,
            "materialized" => Self::Materialized,
            "nan" => Self::NaN,
            "not" => Self::Not,
            "null" => Self::Null,
            "of" => Self::Of,
            "offset" => Self::Offset,
            "on" => Self::On,
            "only" => Self::Only,
            "or" => Self::Or,
            "order" => Self::Order,
            "outer" => Self::Outer,
            "password" => Self::Password,
            "policy" => Self::Policy,
            "prepare" => Self::Prepare,
            "prepared" => Self::Prepared,
            "primary" => Self::Primary,
            "read" => Self::Read,
            "references" => Self::References,
            "refresh" => Self::Refresh,
            "reset" => Self::Reset,
            "revoke" => Self::Revoke,
            "right" => Self::Right,
            "role" => Self::Role,
            "rollback" => Self::Rollback,
            "select" => Self::Select,
            "sessions" => Self::Sessions,
            "set" => Self::Set,
            "show" => Self::Show,
            "since" => Self::Since,
            "sql" => Self::Sql,
            "statements" => Self::Statements,
            "statistics" => Self::Statistics,
            "string" => Self::String,
            "superuser" => Self::Superuser,
            "system" => Self::System,
            "table" => Self::Table,
            "text" => Self::Text,
            "time" => Self::Time,
            "to" => Self::To,
            "transaction" => Self::Transaction,
            "true" => Self::True,
            "unique" => Self::Unique,
            "update" => Self::Update,
            "user" => Self::User,
            "users" => Self::Users,
            "using" => Self::Using,
            "values" => Self::Values,
            "varchar" => Self::Varchar,
            "view" => Self::View,
            "views" => Self::Views,
            "where" => Self::Where,
            "with" => Self::With,
            "write" => Self::Write,
            _ => return Err("not a keyword"),
        })
    }
}

impl Display for Keyword {
    fn fmt(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        // Display keywords as uppercase.
        f.write_str(match self {
            Self::Analyze => "ANALYZE",
            Self::As => "AS",
            Self::Asc => "ASC",
            Self::Audit => "AUDIT",
            Self::And => "AND",
            Self::Begin => "BEGIN",
            Self::Bool => "BOOL",
            Self::Boolean => "BOOLEAN",
            Self::By => "BY",
            Self::Changes => "CHANGES",
            Self::Check => "CHECK",
            Self::Commit => "COMMIT",
            Self::Constraint => "CONSTRAINT",
            Self::Create => "CREATE",
            Self::Cross => "CROSS",
            Self::Csv => "CSV",
            Self::Deallocate => "DEALLOCATE",
            Self::Default => "DEFAULT",
            Self::Delete => "DELETE",
            Self::Desc => "DESC",
            Self::Describe => "DESCRIBE",
            Self::Double => "DOUBLE",
            Self::Drop => "DROP",
            Self::Estimate => "ESTIMATE",
            Self::Execute => "EXECUTE",
            Self::Exists => "EXISTS",
            Self::Explain => "EXPLAIN",
            Self::Export => "EXPORT",
            Self::False => "FALSE",
            Self::Float => "FLOAT",
            Self::Format => "FORMAT",
            Self::From => "FROM",
            Self::Grant => "GRANT",
            Self::Grants => "GRANTS",
            Self::Group => "GROUP",
            Self::Having => "HAVING",
            Self::If => "IF",
            Self::Import => "IMPORT",
            Self::Index => "INDEX",
            Self::Infinity => "INFINITY",
            Self::Inner => "INNER",
            Self::Insert => "INSERT",
            Self::Int => "INT",
            Self::Integer => "INTEGER",
            Self::Into => "INTO",
            Self::Is => "IS",
            Self::Join => "JOIN",
            Self::Json => "JSON",
            Self::Key => "KEY",
            Self::Left => "LEFT",
            Self::Like => "LIKE",
            Self::Limit => "LIMIT",
            Self::Lint => "LINT",
            Self::Materialized => "MATERIALIZED",
            Self::NaN => "NAN",
            Self::Not => "NOT",
            Self::Null => "NULL",
            Self::Of => "OF",
            Self::Offset => "OFFSET",
            Self::On => "ON",
            Self::Only => "ONLY",
            Self::Outer => "OUTER",
            Self::Or => "OR",
            Self::Order => "ORDER",
            Self::Password => "PASSWORD",
            Self::Policy => "POLICY",
            Self::Prepare => "PREPARE",
            Self::Prepared => "PREPARED",
            Self::Primary => "PRIMARY",
            Self::Read => "READ",
            Self::References => "REFERENCES",
            Self::Refresh => "REFRESH",
            Self::Reset => "RESET",
            Self::Revoke => "REVOKE",
            Self::Right => "RIGHT",
            Self::Role => "ROLE",
            Self::Rollback => "ROLLBACK",
            Self::Select => "SELECT",
            Self::Sessions => "SESSIONS",
            Self::Set => "SET",
            Self::Show => "SHOW",
            Self::Since => "SINCE",
            Self::Sql => "SQL",
            Self::Statements => "STATEMENTS",
            Self::Statistics => "STATISTICS",
            Self::String => "STRING",
            Self::Superuser => "SUPERUSER",
            Self::System => "SYSTEM",
            Self::Table => "TABLE",
            Self::Text => "TEXT",
            Self::Time => "TIME",
            Self::To => "TO",
            Self::Transaction => "TRANSACTION",
            Self::True => "TRUE",
            Self::Unique => "UNIQUE",
            Self::Update => "UPDATE",
            Self::User => "USER",
            Self::Users => "USERS",
            Self::Using => "USING",
            Self::Values => "VALUES",
            Self::Varchar => "VARCHAR",
            Self::View => "VIEW",
            Self::Views => "VIEWS",
            Self::Where => "WHERE",
            Self::With => "WITH",
            Self::Write => "WRITE",
        })
    }
}

impl Keyword {
    /// All keywords.
    pub const ALL: [Keyword; 106] = [
        Self::Analyze,
        Self::And,
        Self::As,
        Self::Asc,
        Self::Audit,
        Self::Begin,
        Self::Bool,
        Self::Boolean,
        Self::By,
        Self::Changes,
        Self::Check,
        Self::Commit,
        Self::Constraint,
        Self::Create,
        Self::Cross,
        Self::Csv,
        Self::Deallocate,
        Self::Default,
        Self::Delete,
        Self::Desc,
        Self::Describe,
        Self::Double,
        Self::Drop,
        Self::Estimate,
        Self::Execute,
        Self::Exists,
        Self::Explain,
        Self::Export,
        Self::False,
        Self::Float,
        Self::Format,
        Self::From,
        Self::Grant,
        Self::Grants,
        Self::Group,
        Self::Having,
        Self::If,
        Self::Import,
        Self::Index,
        Self::Infinity,
        Self::Inner,
        Self::Insert,
        Self::Int,
        Self::Integer,
        Self::Into,
        Self::Is,
        Self::Join,
        Self::Json,
        Self::Key,
        Self::Left,
        Self::Like,
        Self::Limit,
        Self::Lint,
        Self::Materialized,
        Self::NaN,
        Self::Not,
        Self::Null,
        Self::Of,
        Self::Offset,
        Self::On,
        Self::Only,
        Self::Or,
        Self::Order,
        Self::Outer,
        Self::Password,
        Self::Policy,
        Self::Prepare,
        Self::Prepared,
        Self::Primary,
        Self::Read,
        Self::References,
        Self::Refresh,
        Self::Reset,
        Self::Revoke,
        Self::Right,
        Self::Role,
        Self::Rollback,
        Self::Select,
        Self::Sessions,
        Self::Set,
        Self::Show,
        Self::Since,
        Self::Sql,
        Self::Statements,
        Self::Statistics,
        Self::String,
        Self::Superuser,
        Self::System,
        Self::Table,
        Self::Text,
        Self::Time,
        Self::To,
        Self::Transaction,
        Self::True,
        Self::Unique,
        Self::Update,
        Self::User,
        Self::Users,
        Self::Using,
        Self::Values,
        Self::Varchar,
        Self::View,
        Self::Views,
        Self::Where,
        Self::With,
        Self::Write,
    ];

    /// Returns the data type that the keyword names in a column definition, if
    /// any.
    pub fn datatype(self) -> Option<DataType> {
        match self {
            Self::Bool | Self::Boolean => Some(DataType::Boolean),
            Self::Float | Self::Double => Some(DataType::Float),
            Self::Int | Self::Integer => Some(DataType::Integer),
            Self::String | Self::Text | Self::Varchar => Some(DataType::String),
            _ => None,
        }
    }
}

/// The lexer (lexical analyzer) preprocesses raw SQL strings into a sequence of
/// lexical tokens (e.g. keyword, number, string, etc), which are passed on to
/// the SQL parser. In doing so, it strips away basic syntactic noise such as
/// whitespace, case, and quotes, and performs initial symbol validation.
pub struct Lexer<'a> {
    input: &'a str,
    chars: Peekable<Chars<'a>>,
}

/// The lexer is used as a token iterator.
impl Iterator for Lexer<'_> {
    type Item = Result<Token>;

    fn next(&mut self) -> Option<Result<Token>> {
        match self.scan() {
            Ok(Some(token)) => Some(Ok(token)),
            // If there's any remaining chars, the lexer didn't recognize them.
            Ok(None) => self.chars.peek().map(|c| errinput!("unexpected character {c}")),
            Err(err) => Some(Err(err)),
        }
    }
}

impl<'a> Lexer<'a> {
    /// Creates a new lexer for the given string.
    pub fn new(input: &'a str) -> Lexer<'a> {
        Lexer { input, chars: input.chars().peekable() }
    }

    /// Returns the byte position of the next unscanned character in the input.
    pub fn position(&self) -> usize {
        self.input.len() - self.chars.clone().map(char::len_utf8).sum::<usize>()
    }

    /// Returns the next character if it satisfies the predicate.
    fn next_if(&mut self, predicate: impl Fn(char) -> bool) -> Option<char> {
        self.chars.peek().filter(|&&c| predicate(c))?;
        self.chars.next()
    }

    /// Applies a closure to the next character, returning its result and
    /// consuming the next character if it's Some.
    fn next_if_map<T>(&mut self, map: impl Fn(char) -> Option<T>) -> Option<T> {
        let value = self.chars.peek().copied().and_then(map)?;
        self.chars.next();
        Some(value)
    }

    /// Returns true if the next character is the given character, consuming it.
    fn next_is(&mut self, c: char) -> bool {
        self.next_if(|n| n == c).is_some()
    }

    /// Scans the next token, if any.
    fn scan(&mut self) -> Result<Option<Token>> {
        // Ignore whitespace.
        self.skip_whitespace();
        let Some(c) = self.chars.peek() else {
            return Ok(None);
        };
        // The first character tells us the token kind. Scan it accordingly.
        match c {
            '\'' => self.scan_string(),
            '"' => self.scan_ident_quoted(),
            '$' => self.scan_parameter(),
            '0'..='9' => Ok(self.scan_number()),
            c if c.is_alphabetic() => Ok(self.scan_ident_or_keyword()),
            _ => Ok(self.scan_symbol()),
        }
    }

    /// Scans the next identifier or keyword, if any. It's converted to
    /// lowercase, by SQL convention.
    fn scan_ident_or_keyword(&mut self) -> Option<Token> {
        // The first character must be alphabetic. The rest can be numeric.
        let mut name = self.next_if(|c| c.is_alphabetic())?.to_lowercase().to_string();
        while let Some(c) = self.next_if(|c| c.is_alphanumeric() || c == '_') {
            name.extend(c.to_lowercase())
        }
        // Check if the identifier matches a keyword.
        if let Ok(keyword) = Keyword::try_from(name.as_str()) {
            return Some(Token::Keyword(keyword));
        }
        Some(Token::Ident(name))
    }

    /// Scans the next quoted identifier, if any. Case is preserved.
    fn scan_ident_quoted(&mut self) -> Result<Option<Token>> {
        if !self.next_is('"') {
            return Ok(None);
        }
        let mut ident = String::new();
        loop {
            match self.chars.next() {
                // "" is the escape sequence for ".
                Some('"') if self.next_is('"') => ident.push('"'),
                Some('"') => break,
                Some(c) => ident.push(c),
                None => return errinput!("unexpected end of quoted identifier"),
            }
        }
        Ok(Some(Token::Ident(ident)))
    }

    /// Scans the next number, if any.
    fn scan_number(&mut self) -> Option<Token> {
        // Scan the integer part. There must be at least one digit.
        let mut number = self.next_if(|c| c.is_ascii_digit())?.to_string();
        while let Some(c) = self.next_if(|c| c.is_ascii_digit()) {
            number.push(c)
        }
        // Scan the fractional part, if any.
        if self.next_is('.') {
            number.push('.');
            while let Some(dec) = self.next_if(|c| c.is_ascii_digit()) {
                number.push(dec)
            }
        }
        // Scan the exponent, if any.
        if let Some(exp) = self.next_if(|c| c == 'e' || c == 'E') {
            number.push(exp);
            if let Some(sign) = self.next_if(|c| c == '+' || c == '-') {
                number.push(sign)
            }
            while let Some(c) = self.next_if(|c| c.is_ascii_digit()) {
                number.push(c)
            }
        }
        Some(Token::Number(number))
    }

    /// Scans the next parameter, if any.
    fn scan_parameter(&mut self) -> Result<Option<Token>> {
        // Look ahead, since a lone $ is not a token.
        let mut lookahead = self.chars.clone();
        if lookahead.next() != Some('$') || !lookahead.next().is_some_and(|c| c.is_ascii_digit()) {
            return Ok(None);
        }
        self.chars.next();
        let mut digits = String::new();
        while let Some(c) = self.next_if(|c| c.is_ascii_digit()) {
            digits.push(c)
        }
        match digits.parse() {
            Ok(n) if n > 0 => Ok(Some(Token::Parameter(n))),
            _ => errinput!("invalid parameter ${digits}"),
        }
    }

    /// Scans the next quoted string literal, if any.
    fn scan_string(&mut self) -> Result<Option<Token>> {
        if !self.next_is('\'') {
            return Ok(None);
        }
        let mut string = String::new();
        loop {
            match self.chars.next() {
                // '' is the escape sequence for '.
                Some('\'') if self.next_is('\'') => string.push('\''),
                Some('\'') => break,
                Some(c) => string.push(c),
                None => return errinput!("unexpected end of string literal"),
            }
        }
        Ok(Some(Token::String(string)))
    }

    /// Scans the next symbol token, if any.
    fn scan_symbol(&mut self) -> Option<Token> {
        let mut token = self.next_if_map(|c| {
            Some(match c {
                '.' => Token::Period,
                '=' => Token::Equal,
                '>' => Token::GreaterThan,
                '<' => Token::LessThan,
                '+' => Token::Plus,
                '-' => Token::Minus,
                '*' => Token::Asterisk,
                '/' => Token::Slash,
                '^' => Token::Caret,
                '%' => Token::Percent,
                '!' => Token::Exclamation,
                '?' => Token::Question,
                ',' => Token::Comma,
                ';' => Token::Semicolon,
                '(' => Token::OpenParen,
                ')' => Token::CloseParen,
                _ => return None,
            })
        })?;
        // Handle two-character tokens, e.g. !=.
        token = match token {
            Token::Exclamation if self.next_is('=') => Token::NotEqual,
            Token::GreaterThan if self.next_is('=') => Token::GreaterThanOrEqual,
            Token::LessThan if self.next_is('>') => Token::LessOrGreaterThan,
            Token::LessThan if self.next_is('=') => Token::LessThanOrEqual,
            token => token,
        };
        Some(token)
    }

    /// Skips any whitespace.
    fn skip_whitespace(&mut self) {
        while self.next_if(|c| c.is_whitespace()).is_some() {}
    }
}

/// Returns true if the entire given string is a single valid identifier.
pub fn is_ident(ident: &str) -> bool {
    let mut lexer = Lexer::new(ident);
    let Some(Ok(Token::Ident(_))) = lexer.next() else {
        return false;
    };
    lexer.next().is_none() // if further tokens, it's not a lone identifier
}

/// Formats an identifier as SQL, quoting it if it isn't a valid unquoted
/// identifier (e.g. if it contains special characters or is a keyword).
pub fn format_ident(ident: &str) -> String {
    match is_ident(ident) {
        true => ident.to_string(),
        false => format!("\"{}\"", ident.replace('"', "\"\"")),
    }
}
