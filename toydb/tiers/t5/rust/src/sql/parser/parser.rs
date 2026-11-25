use std::iter::Peekable;
use std::ops::Add;

use super::{Keyword, Lexer, Token, ast};
use crate::errinput;
use crate::error::Result;
use crate::sql::types::{DataType, Privilege};

/// The SQL parser takes tokens from the lexer and parses the SQL syntax into an
/// Abstract Syntax Tree (AST).
///
/// The AST represents the syntactic structure of a SQL query (e.g. the SELECT
/// and FROM clauses, values, arithmetic expressions, etc.). However, it only
/// ensures the syntax is well-formed, and does not know whether e.g. a given
/// table or column exists or which kind of join to use -- that is the job of
/// the planner.
pub struct Parser<'a> {
    pub lexer: Peekable<Lexer<'a>>,
}

impl Parser<'_> {
    /// Parses the input string into a SQL statement AST. The entire string must
    /// be parsed as a single statement, ending with an optional semicolon.
    pub fn parse(statement: &str) -> Result<ast::Statement> {
        let mut parser = Self::new(statement);
        let statement = parser.parse_statement()?;
        parser.skip(Token::Semicolon);
        if let Some(token) = parser.lexer.next().transpose()? {
            return errinput!("unexpected token {token}");
        }
        Ok(statement)
    }

    /// Parse the input string into a SQL expression AST. The entire string must
    /// be parsed as a single expression.
    pub fn parse_expr(expr: &str) -> Result<ast::Expression> {
        let mut parser = Self::new(expr);
        let expression = parser.parse_expression()?;
        if let Some(token) = parser.lexer.next().transpose()? {
            return errinput!("unexpected token {token}");
        }
        Ok(expression)
    }

    /// Creates a new parser for the given raw SQL string.
    fn new(input: &str) -> Parser<'_> {
        Parser { lexer: Lexer::new(input).peekable() }
    }

    /// Fetches the next lexer token, or errors if none is found.
    fn next(&mut self) -> Result<Token> {
        self.lexer.next().transpose()?.ok_or_else(|| errinput!("unexpected end of input"))
    }

    /// Returns the next identifier, or errors if not found.
    fn next_ident(&mut self) -> Result<String> {
        match self.next()? {
            Token::Ident(ident) => Ok(ident),
            token => errinput!("expected identifier, got {token}"),
        }
    }

    /// Returns the next string literal, or errors if not found.
    fn next_string(&mut self) -> Result<String> {
        match self.next()? {
            Token::String(string) => Ok(string),
            token => errinput!("expected string, found {token}"),
        }
    }

    /// Returns the next identifier if there is one.
    fn next_if_ident(&mut self) -> Option<String> {
        self.next_if_map(|token| match token {
            Token::Ident(ident) => Some(ident.clone()),
            _ => None,
        })
    }

    /// Returns the next lexer token if it satisfies the predicate.
    fn next_if(&mut self, predicate: impl Fn(&Token) -> bool) -> Option<Token> {
        self.peek().ok()?.filter(|t| predicate(t))?;
        self.next().ok()
    }

    /// Passes the next lexer token through the closure, consuming it if the
    /// closure returns Some. Returns the result of the closure.
    fn next_if_map<T>(&mut self, f: impl Fn(&Token) -> Option<T>) -> Option<T> {
        self.peek().ok()?.map(f)?.inspect(|_| drop(self.next()))
    }

    /// Returns the next keyword if there is one.
    fn next_if_keyword(&mut self) -> Option<Keyword> {
        self.next_if_map(|token| match token {
            Token::Keyword(keyword) => Some(*keyword),
            _ => None,
        })
    }

    /// Consumes the next lexer token if it is the given token, returning true.
    fn next_is(&mut self, token: Token) -> bool {
        self.next_if(|t| t == &token).is_some()
    }

    /// Consumes the next lexer token if it's the expected token, or errors.
    fn expect(&mut self, expect: Token) -> Result<()> {
        let token = self.next()?;
        if token != expect {
            return errinput!("expected token {expect}, found {token}");
        }
        Ok(())
    }

    /// Consumes the next lexer token if it is the given token. Equivalent to
    /// next_is(), but expresses intent better.
    fn skip(&mut self, token: Token) {
        self.next_is(token);
    }

    /// Peeks the next lexer token if any, but transposes it for convenience.
    fn peek(&mut self) -> Result<Option<&Token>> {
        self.lexer.peek().map(|r| r.as_ref().map_err(|err| err.clone())).transpose()
    }

    /// Parses a SQL statement.
    fn parse_statement(&mut self) -> Result<ast::Statement> {
        let Some(token) = self.peek()? else {
            return errinput!("unexpected end of input");
        };
        match token {
            Token::Keyword(Keyword::Begin) => self.parse_begin(),
            Token::Keyword(Keyword::Commit) => self.parse_commit(),
            Token::Keyword(Keyword::Rollback) => self.parse_rollback(),
            Token::Keyword(Keyword::Explain) => self.parse_explain(),

            Token::Keyword(Keyword::Create) => self.parse_create(),
            Token::Keyword(Keyword::Drop) => self.parse_drop(),

            Token::Keyword(Keyword::Delete) => self.parse_delete(),
            Token::Keyword(Keyword::Insert) => self.parse_insert(),
            Token::Keyword(Keyword::Select) => self.parse_select(),
            Token::Keyword(Keyword::Update) => self.parse_update(),

            Token::Keyword(Keyword::Show) => self.parse_show(),
            Token::Keyword(Keyword::Analyze) => self.parse_analyze(),
            Token::Keyword(Keyword::Export) => self.parse_export(),
            Token::Keyword(Keyword::Import) => self.parse_import(),

            Token::Keyword(Keyword::Format) => self.parse_format(),
            Token::Keyword(Keyword::Describe) => self.parse_describe(),
            Token::Keyword(Keyword::Estimate) => self.parse_estimate(),
            Token::Keyword(Keyword::Prepare) => self.parse_prepare(),
            Token::Keyword(Keyword::Execute) => self.parse_execute(),
            Token::Keyword(Keyword::Deallocate) => self.parse_deallocate(),
            Token::Keyword(Keyword::Lint) => self.parse_lint(),

            Token::Keyword(Keyword::Grant) => self.parse_grant(),
            Token::Keyword(Keyword::Revoke) => self.parse_revoke(),
            Token::Keyword(Keyword::Set) => self.parse_set_role(),
            Token::Keyword(Keyword::Reset) => self.parse_reset_role(),
            Token::Keyword(Keyword::Refresh) => self.parse_refresh(),

            token => errinput!("unexpected token {token}"),
        }
    }

    /// Parses a BEGIN statement.
    fn parse_begin(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Begin.into())?;
        self.skip(Keyword::Transaction.into());

        let mut read_only = false;
        if self.next_is(Keyword::Read.into()) {
            match self.next()? {
                Token::Keyword(Keyword::Only) => read_only = true,
                Token::Keyword(Keyword::Write) => {}
                token => return errinput!("unexpected token {token}"),
            }
        }

        let mut as_of = None;
        if self.next_is(Keyword::As.into()) {
            self.expect(Keyword::Of.into())?;
            self.expect(Keyword::System.into())?;
            self.expect(Keyword::Time.into())?;
            match self.next()? {
                Token::Number(n) => as_of = Some(n.parse()?),
                token => return errinput!("unexpected token {token}, wanted number"),
            }
        }
        Ok(ast::Statement::Begin { read_only, as_of })
    }

    /// Parses a COMMIT statement.
    fn parse_commit(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Commit.into())?;
        Ok(ast::Statement::Commit)
    }

    /// Parses a ROLLBACK statement.
    fn parse_rollback(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Rollback.into())?;
        Ok(ast::Statement::Rollback)
    }

    /// Parses an EXPLAIN or EXPLAIN (ANALYZE) statement.
    fn parse_explain(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Explain.into())?;
        if self.next_is(Keyword::Explain.into()) {
            return errinput!("cannot nest EXPLAIN statements");
        }
        let analyze = self.next_is(Token::OpenParen);
        if analyze {
            self.expect(Keyword::Analyze.into())?;
            self.expect(Token::CloseParen)?;
        }
        Ok(ast::Statement::Explain { statement: Box::new(self.parse_statement()?), analyze })
    }

    /// Parses a CREATE TABLE, CREATE VIEW, CREATE MATERIALIZED VIEW, CREATE
    /// USER or CREATE POLICY statement.
    fn parse_create(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Create.into())?;
        if self.next_is(Keyword::View.into()) {
            return self.parse_create_view();
        }
        if self.next_is(Keyword::Materialized.into()) {
            self.expect(Keyword::View.into())?;
            let (name, columns, query) = self.parse_view_definition()?;
            return Ok(ast::Statement::CreateMaterializedView { name, columns, query });
        }
        if self.next_is(Keyword::User.into()) {
            return self.parse_create_user();
        }
        if self.next_is(Keyword::Policy.into()) {
            return self.parse_create_policy();
        }
        self.expect(Keyword::Table.into())?;
        self.parse_create_table()
    }

    /// Parses a CREATE TABLE statement, after CREATE TABLE.
    fn parse_create_table(&mut self) -> Result<ast::Statement> {
        let name = self.next_ident()?;
        self.expect(Token::OpenParen)?;
        let mut columns = Vec::new();
        let mut checks = Vec::new();
        loop {
            if matches!(self.peek()?, Some(Token::Keyword(Keyword::Check | Keyword::Constraint))) {
                checks.push(self.parse_create_table_check(columns.len())?);
            } else {
                columns.push(self.parse_create_table_column()?);
            }
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        self.expect(Token::CloseParen)?;
        Ok(ast::Statement::CreateTable { name, columns, checks })
    }

    /// Parses a CREATE TABLE table constraint, after the given number of
    /// column definitions.
    fn parse_create_table_check(&mut self, position: usize) -> Result<ast::Check> {
        let mut name = None;
        if self.next_is(Keyword::Constraint.into()) {
            name = Some(self.next_ident()?);
        }
        self.expect(Keyword::Check.into())?;
        let condition = self.parse_check_condition()?;
        Ok(ast::Check { name, condition, position })
    }

    /// Parses the parenthesized condition of a CHECK constraint or a policy.
    fn parse_check_condition(&mut self) -> Result<ast::Expression> {
        self.expect(Token::OpenParen)?;
        let condition = self.parse_expression()?;
        self.expect(Token::CloseParen)?;
        Ok(condition)
    }

    /// Parses a CREATE TABLE column definition.
    fn parse_create_table_column(&mut self) -> Result<ast::Column> {
        let name = self.next_ident()?;
        let datatype = self.parse_datatype()?;
        let mut column = ast::Column {
            name,
            datatype,
            primary_key: false,
            nullable: None,
            default: None,
            unique: false,
            index: false,
            references: None,
            checks: Vec::new(),
        };
        while let Some(keyword) = self.next_if_keyword() {
            match keyword {
                Keyword::Primary => {
                    self.expect(Keyword::Key.into())?;
                    column.primary_key = true;
                }
                Keyword::Null => {
                    if column.nullable.is_some() {
                        return errinput!("nullability already set for column {}", column.name);
                    }
                    column.nullable = Some(true)
                }
                Keyword::Not => {
                    self.expect(Keyword::Null.into())?;
                    if column.nullable.is_some() {
                        return errinput!("nullability already set for column {}", column.name);
                    }
                    column.nullable = Some(false)
                }
                Keyword::Default => column.default = Some(self.parse_expression()?),
                Keyword::Unique => column.unique = true,
                Keyword::Index => column.index = true,
                Keyword::References => column.references = Some(self.next_ident()?),
                Keyword::Check => column.checks.push(self.parse_check_condition()?),
                keyword => return errinput!("unexpected keyword {keyword}"),
            }
        }
        Ok(column)
    }

    /// Parses a data type.
    fn parse_datatype(&mut self) -> Result<DataType> {
        match self.next()? {
            Token::Keyword(keyword) if let Some(datatype) = keyword.datatype() => Ok(datatype),
            token => errinput!("unexpected token {token}"),
        }
    }

    /// Parses a DROP TABLE, DROP VIEW, DROP MATERIALIZED VIEW, DROP USER or
    /// DROP POLICY statement.
    fn parse_drop(&mut self) -> Result<ast::Statement> {
        self.expect(Token::Keyword(Keyword::Drop))?;
        if self.next_is(Keyword::Policy.into()) {
            return self.parse_drop_policy();
        }
        if self.next_is(Keyword::Materialized.into()) {
            self.expect(Keyword::View.into())?;
            let if_exists = self.parse_if_exists()?;
            let name = self.next_ident()?;
            return Ok(ast::Statement::DropMaterializedView { name, if_exists });
        }
        let view = self.next_is(Keyword::View.into());
        let user = !view && self.next_is(Keyword::User.into());
        if !view && !user {
            self.expect(Token::Keyword(Keyword::Table))?;
        }
        let if_exists = self.parse_if_exists()?;
        let name = self.next_ident()?;
        Ok(match (view, user) {
            (true, _) => ast::Statement::DropView { name, if_exists },
            (false, true) => ast::Statement::DropUser { name, if_exists },
            (false, false) => ast::Statement::DropTable { name, if_exists },
        })
    }

    /// Parses an optional IF EXISTS clause, returning true if given.
    fn parse_if_exists(&mut self) -> Result<bool> {
        if !self.next_is(Keyword::If.into()) {
            return Ok(false);
        }
        self.expect(Token::Keyword(Keyword::Exists))?;
        Ok(true)
    }

    /// Parses a CREATE POLICY statement, after CREATE POLICY.
    fn parse_create_policy(&mut self) -> Result<ast::Statement> {
        let name = self.next_ident()?;
        self.expect(Keyword::On.into())?;
        let table = self.next_ident()?;
        let mut user = None;
        if self.next_is(Keyword::To.into()) {
            user = Some(self.next_ident()?);
        }
        self.expect(Keyword::Using.into())?;
        let using = self.parse_check_condition()?;
        let mut with_check = None;
        if self.next_is(Keyword::With.into()) {
            self.expect(Keyword::Check.into())?;
            with_check = Some(self.parse_check_condition()?);
        }
        Ok(ast::Statement::CreatePolicy { name, table, user, using, with_check })
    }

    /// Parses a DROP POLICY statement, after DROP POLICY.
    fn parse_drop_policy(&mut self) -> Result<ast::Statement> {
        let if_exists = self.parse_if_exists()?;
        let name = self.next_ident()?;
        self.expect(Keyword::On.into())?;
        let table = self.next_ident()?;
        Ok(ast::Statement::DropPolicy { name, table, if_exists })
    }

    /// Parses a CREATE USER statement, after CREATE USER.
    fn parse_create_user(&mut self) -> Result<ast::Statement> {
        let name = self.next_ident()?;
        let mut password = None;
        if self.next_is(Keyword::Password.into()) {
            password = Some(self.next_string()?);
        }
        let superuser = self.next_is(Keyword::Superuser.into());
        Ok(ast::Statement::CreateUser { name, password, superuser })
    }

    /// Parses a CREATE VIEW statement, after CREATE VIEW.
    fn parse_create_view(&mut self) -> Result<ast::Statement> {
        let (name, columns, query) = self.parse_view_definition()?;
        Ok(ast::Statement::CreateView { name, columns, query })
    }

    /// Parses the name, column names and query of a view or materialized view,
    /// after CREATE VIEW or CREATE MATERIALIZED VIEW.
    fn parse_view_definition(
        &mut self,
    ) -> Result<(String, Option<Vec<String>>, Box<ast::Statement>)> {
        let name = self.next_ident()?;
        let mut columns = None;
        if self.next_is(Token::OpenParen) {
            columns = Some(self.parse_ident_list()?);
            self.expect(Token::CloseParen)?;
        }
        self.expect(Keyword::As.into())?;
        let query = Box::new(self.parse_select()?);
        Ok((name, columns, query))
    }

    /// Parses a REFRESH MATERIALIZED VIEW statement.
    fn parse_refresh(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Refresh.into())?;
        self.expect(Keyword::Materialized.into())?;
        self.expect(Keyword::View.into())?;
        Ok(ast::Statement::RefreshMaterializedView { name: self.next_ident()? })
    }

    /// Parses a DELETE statement.
    fn parse_delete(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Delete.into())?;
        self.expect(Keyword::From.into())?;
        let table = self.next_ident()?;
        Ok(ast::Statement::Delete { table, r#where: self.parse_where_clause()? })
    }

    /// Parses an INSERT statement.
    fn parse_insert(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Insert.into())?;
        self.expect(Keyword::Into.into())?;
        let table = self.next_ident()?;

        let mut columns = None;
        if self.next_is(Token::OpenParen) {
            columns = Some(self.parse_ident_list()?);
            self.expect(Token::CloseParen)?;
        }

        self.expect(Keyword::Values.into())?;

        let mut values = Vec::new();
        loop {
            let mut row = Vec::new();
            self.expect(Token::OpenParen)?;
            loop {
                row.push(self.parse_expression()?);
                if !self.next_is(Token::Comma) {
                    break;
                }
            }
            self.expect(Token::CloseParen)?;
            values.push(row);
            if !self.next_is(Token::Comma) {
                break;
            }
        }

        Ok(ast::Statement::Insert { table, columns, values })
    }

    /// Parses an UPDATE statement.
    fn parse_update(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Update.into())?;
        let table = self.next_ident()?;
        self.expect(Keyword::Set.into())?;
        let mut set = std::collections::BTreeMap::new();
        loop {
            let column = self.next_ident()?;
            self.expect(Token::Equal)?;
            let expr = (!self.next_is(Keyword::Default.into()))
                .then(|| self.parse_expression())
                .transpose()?;
            if set.contains_key(&column) {
                return errinput!("column {column} set multiple times");
            }
            set.insert(column, expr);
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        Ok(ast::Statement::Update { table, set, r#where: self.parse_where_clause()? })
    }

    /// Parses a SELECT statement.
    fn parse_select(&mut self) -> Result<ast::Statement> {
        Ok(ast::Statement::Select {
            select: self.parse_select_clause()?,
            from: self.parse_from_clause()?,
            r#where: self.parse_where_clause()?,
            group_by: self.parse_group_by_clause()?,
            having: self.parse_having_clause()?,
            order_by: self.parse_order_by_clause()?,
            limit: self.parse_limit_clause()?,
            offset: self.parse_offset_clause()?,
        })
    }

    /// Parses a SHOW statement: SHOW STATEMENTS, STATISTICS, VIEWS, PREPARED,
    /// CHANGES, USERS, GRANTS, AUDIT or SESSIONS.
    fn parse_show(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Show.into())?;
        match self.next()? {
            Token::Keyword(Keyword::Statements) => Ok(ast::Statement::ShowStatements),
            Token::Keyword(Keyword::Statistics) => {
                Ok(ast::Statement::ShowStatistics { table: self.next_ident()? })
            }
            Token::Keyword(Keyword::Views) => Ok(ast::Statement::ShowViews),
            Token::Keyword(Keyword::Prepared) => Ok(ast::Statement::ShowPrepared),
            Token::Keyword(Keyword::Changes) => {
                Ok(ast::Statement::ShowChanges { since: self.parse_since()? })
            }
            Token::Keyword(Keyword::Users) => Ok(ast::Statement::ShowUsers),
            Token::Keyword(Keyword::Grants) => Ok(ast::Statement::ShowGrants),
            Token::Keyword(Keyword::Audit) => {
                Ok(ast::Statement::ShowAudit { since: self.parse_since()? })
            }
            Token::Keyword(Keyword::Sessions) => Ok(ast::Statement::ShowSessions),
            token => errinput!("unexpected token {token}"),
        }
    }

    /// Parses an optional SINCE clause of a SHOW statement.
    fn parse_since(&mut self) -> Result<Option<u64>> {
        if !self.next_is(Keyword::Since.into()) {
            return Ok(None);
        }
        match self.next()? {
            Token::Number(n) => Ok(Some(n.parse()?)),
            token => errinput!("unexpected token {token}, wanted number"),
        }
    }

    /// Parses an ANALYZE statement.
    fn parse_analyze(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Analyze.into())?;
        Ok(ast::Statement::Analyze { table: self.next_if_ident() })
    }

    /// Parses an EXPORT statement.
    fn parse_export(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Export.into())?;
        let table = self.next_if_ident();
        self.expect(Keyword::As.into())?;
        let format = match self.next()? {
            Token::Keyword(Keyword::Csv) => ast::ExportFormat::Csv,
            Token::Keyword(Keyword::Json) => ast::ExportFormat::Json,
            Token::Keyword(Keyword::Sql) => ast::ExportFormat::Sql,
            token => return errinput!("unexpected token {token}"),
        };
        Ok(ast::Statement::Export { table, format })
    }

    /// Parses an IMPORT statement.
    fn parse_import(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Import.into())?;
        self.expect(Keyword::Into.into())?;
        let table = self.next_ident()?;
        self.expect(Keyword::From.into())?;
        let format = match self.next()? {
            Token::Keyword(Keyword::Csv) => ast::ImportFormat::Csv,
            Token::Keyword(Keyword::Json) => ast::ImportFormat::Json,
            token => return errinput!("unexpected token {token}"),
        };
        let mut lines = Vec::new();
        loop {
            lines.push(self.next_string()?);
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        Ok(ast::Statement::Import { table, format, lines })
    }

    /// Parses a FORMAT statement.
    fn parse_format(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Format.into())?;
        Ok(ast::Statement::Format(Box::new(self.parse_statement()?)))
    }

    /// Parses a DESCRIBE statement, of a table or view name or a statement.
    fn parse_describe(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Describe.into())?;
        if let Some(name) = self.next_if_ident() {
            return Ok(ast::Statement::Describe(ast::Describe::Name(name)));
        }
        let statement = Box::new(self.parse_statement()?);
        Ok(ast::Statement::Describe(ast::Describe::Statement(statement)))
    }

    /// Parses an ESTIMATE statement.
    fn parse_estimate(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Estimate.into())?;
        Ok(ast::Statement::Estimate(Box::new(self.parse_statement()?)))
    }

    /// Parses a PREPARE statement.
    fn parse_prepare(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Prepare.into())?;
        let name = self.next_ident()?;
        let mut types = Vec::new();
        if self.next_is(Token::OpenParen) {
            loop {
                types.push(self.parse_datatype()?);
                if !self.next_is(Token::Comma) {
                    break;
                }
            }
            self.expect(Token::CloseParen)?;
        }
        self.expect(Keyword::As.into())?;
        let statement = Box::new(self.parse_statement()?);
        Ok(ast::Statement::Prepare { name, types, statement })
    }

    /// Parses an EXECUTE statement.
    fn parse_execute(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Execute.into())?;
        let name = self.next_ident()?;
        let mut arguments = Vec::new();
        if self.next_is(Token::OpenParen) {
            loop {
                arguments.push(self.parse_expression()?);
                if !self.next_is(Token::Comma) {
                    break;
                }
            }
            self.expect(Token::CloseParen)?;
        }
        Ok(ast::Statement::Execute { name, arguments })
    }

    /// Parses a DEALLOCATE statement.
    fn parse_deallocate(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Deallocate.into())?;
        Ok(ast::Statement::Deallocate { name: self.next_ident()? })
    }

    /// Parses a LINT statement.
    fn parse_lint(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Lint.into())?;
        Ok(ast::Statement::Lint(Box::new(self.parse_statement()?)))
    }

    /// Parses a GRANT statement.
    fn parse_grant(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Grant.into())?;
        Ok(ast::Statement::Grant(self.parse_grant_subject(Keyword::To)?))
    }

    /// Parses a REVOKE statement.
    fn parse_revoke(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Revoke.into())?;
        Ok(ast::Statement::Revoke(self.parse_grant_subject(Keyword::From)?))
    }

    /// Parses what a GRANT or REVOKE grants or revokes, with the given keyword
    /// (TO or FROM) before the user.
    fn parse_grant_subject(&mut self, keyword: Keyword) -> Result<ast::Grant> {
        let Some(privilege) = self.next_if_map(Self::privilege) else {
            let group = self.next_ident()?;
            self.expect(keyword.into())?;
            let user = self.next_ident()?;
            return Ok(ast::Grant::Membership { group, user });
        };
        let mut privileges = vec![privilege];
        while self.next_is(Token::Comma) {
            let token = self.next()?;
            let Some(privilege) = Self::privilege(&token) else {
                return errinput!("unexpected token {token}");
            };
            privileges.push(privilege);
        }
        self.expect(Keyword::On.into())?;
        let object = self.next_ident()?;
        self.expect(keyword.into())?;
        let user = self.next_ident()?;
        Ok(ast::Grant::Privileges { privileges, object, user })
    }

    /// Returns the privilege of a privilege keyword token, if it is one.
    fn privilege(token: &Token) -> Option<Privilege> {
        match token {
            Token::Keyword(Keyword::Select) => Some(Privilege::Select),
            Token::Keyword(Keyword::Insert) => Some(Privilege::Insert),
            Token::Keyword(Keyword::Update) => Some(Privilege::Update),
            Token::Keyword(Keyword::Delete) => Some(Privilege::Delete),
            _ => None,
        }
    }

    /// Parses a SET ROLE statement.
    fn parse_set_role(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Set.into())?;
        self.expect(Keyword::Role.into())?;
        Ok(ast::Statement::SetRole { user: self.next_ident()? })
    }

    /// Parses a RESET ROLE statement.
    fn parse_reset_role(&mut self) -> Result<ast::Statement> {
        self.expect(Keyword::Reset.into())?;
        self.expect(Keyword::Role.into())?;
        Ok(ast::Statement::ResetRole)
    }

    /// Parses a comma-separated list of identifiers.
    fn parse_ident_list(&mut self) -> Result<Vec<String>> {
        let mut idents = Vec::new();
        loop {
            idents.push(self.next_ident()?);
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        Ok(idents)
    }

    /// Parses a SELECT clause.
    fn parse_select_clause(&mut self) -> Result<Vec<(ast::Expression, Option<String>)>> {
        self.expect(Keyword::Select.into())?;
        let mut select = Vec::new();
        loop {
            let expr = self.parse_expression()?;
            let mut alias = None;
            if self.next_is(Keyword::As.into()) || matches!(self.peek()?, Some(Token::Ident(_))) {
                if expr == ast::Expression::All {
                    return errinput!("can't alias *");
                }
                alias = Some(self.next_ident()?);
            }
            select.push((expr, alias));
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        Ok(select)
    }

    /// Parses a FROM clause, if present.
    fn parse_from_clause(&mut self) -> Result<Vec<ast::From>> {
        if !self.next_is(Keyword::From.into()) {
            return Ok(Vec::new());
        }
        let mut from = Vec::new();
        loop {
            let mut from_item = self.parse_from_table()?;
            while let Some(r#type) = self.parse_from_join()? {
                let left = Box::new(from_item);
                let right = Box::new(self.parse_from_table()?);
                let mut predicate = None;
                if r#type != ast::JoinType::Cross {
                    self.expect(Keyword::On.into())?;
                    predicate = Some(self.parse_expression()?)
                }
                from_item = ast::From::Join { left, right, r#type, predicate };
            }
            from.push(from_item);
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        Ok(from)
    }

    // Parses a FROM table.
    fn parse_from_table(&mut self) -> Result<ast::From> {
        let name = self.next_ident()?;
        let mut alias = None;
        if self.next_is(Keyword::As.into()) || matches!(self.peek()?, Some(Token::Ident(_))) {
            alias = Some(self.next_ident()?)
        };
        Ok(ast::From::Table { name, alias })
    }

    // Parses a FROM JOIN type, if present.
    fn parse_from_join(&mut self) -> Result<Option<ast::JoinType>> {
        if self.next_is(Keyword::Join.into()) {
            return Ok(Some(ast::JoinType::Inner));
        }
        if self.next_is(Keyword::Cross.into()) {
            self.expect(Keyword::Join.into())?;
            return Ok(Some(ast::JoinType::Cross));
        }
        if self.next_is(Keyword::Inner.into()) {
            self.expect(Keyword::Join.into())?;
            return Ok(Some(ast::JoinType::Inner));
        }
        if self.next_is(Keyword::Left.into()) {
            self.skip(Keyword::Outer.into());
            self.expect(Keyword::Join.into())?;
            return Ok(Some(ast::JoinType::Left));
        }
        if self.next_is(Keyword::Right.into()) {
            self.skip(Keyword::Outer.into());
            self.expect(Keyword::Join.into())?;
            return Ok(Some(ast::JoinType::Right));
        }
        Ok(None)
    }

    /// Parses a WHERE clause, if present.
    fn parse_where_clause(&mut self) -> Result<Option<ast::Expression>> {
        if !self.next_is(Keyword::Where.into()) {
            return Ok(None);
        }
        Ok(Some(self.parse_expression()?))
    }

    /// Parses a GROUP BY clause, if present.
    fn parse_group_by_clause(&mut self) -> Result<Vec<ast::Expression>> {
        if !self.next_is(Keyword::Group.into()) {
            return Ok(Vec::new());
        }
        let mut group_by = Vec::new();
        self.expect(Keyword::By.into())?;
        loop {
            group_by.push(self.parse_expression()?);
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        Ok(group_by)
    }

    /// Parses a HAVING clause, if present.
    fn parse_having_clause(&mut self) -> Result<Option<ast::Expression>> {
        if !self.next_is(Keyword::Having.into()) {
            return Ok(None);
        }
        Ok(Some(self.parse_expression()?))
    }

    /// Parses an ORDER BY clause, if present.
    fn parse_order_by_clause(&mut self) -> Result<Vec<(ast::Expression, ast::Direction)>> {
        if !self.next_is(Keyword::Order.into()) {
            return Ok(Vec::new());
        }
        let mut order_by = Vec::new();
        self.expect(Keyword::By.into())?;
        loop {
            let expr = self.parse_expression()?;
            let order = self
                .next_if_map(|token| match token {
                    Token::Keyword(Keyword::Asc) => Some(ast::Direction::Ascending),
                    Token::Keyword(Keyword::Desc) => Some(ast::Direction::Descending),
                    _ => None,
                })
                .unwrap_or_default();
            order_by.push((expr, order));
            if !self.next_is(Token::Comma) {
                break;
            }
        }
        Ok(order_by)
    }

    /// Parses a LIMIT clause, if present.
    fn parse_limit_clause(&mut self) -> Result<Option<ast::Expression>> {
        if !self.next_is(Keyword::Limit.into()) {
            return Ok(None);
        }
        Ok(Some(self.parse_expression()?))
    }

    /// Parses an OFFSET clause, if present.
    fn parse_offset_clause(&mut self) -> Result<Option<ast::Expression>> {
        if !self.next_is(Keyword::Offset.into()) {
            return Ok(None);
        }
        Ok(Some(self.parse_expression()?))
    }

    /// Parses an expression using the precedence climbing algorithm. See:
    ///
    /// <https://en.wikipedia.org/wiki/Operator-precedence_parser#Precedence_climbing_method>
    /// <https://eli.thegreenplace.net/2012/08/02/parsing-expressions-by-precedence-climbing>
    ///
    /// Expressions are made up of two main entities:
    ///
    /// * Atoms: values, variables, functions, and parenthesized expressions.
    /// * Operators: performs operations on atoms and sub-expressions.
    ///   * Prefix operators: e.g. `-a` or `NOT a`.
    ///   * Infix operators: e.g. `a + b`  or `a AND b`.
    ///   * Postfix operators: e.g. `a!` or `a IS NULL`.
    ///
    /// During parsing, we have to respect the mathematical precedence and
    /// associativity of operators. Consider e.g.:
    ///
    /// 2 ^ 3 ^ 2 - 4 * 3
    ///
    /// By the rules of precedence and associativity, this expression should
    /// be interpreted as:
    ///
    /// (2 ^ (3 ^ 2)) - (4 * 3)
    ///
    /// Specifically, the exponentiation operator ^ is right-associative, so it
    /// should be 2 ^ (3 ^ 2) = 512, not (2 ^ 3) ^ 2 = 64. Similarly,
    /// exponentiation and multiplication have higher precedence than
    /// subtraction, so it should be (2 ^ 3 ^ 2) - (4 * 3) = 500, not
    /// 2 ^ 3 ^ (2 - 4) * 3 = -3.24.
    ///
    /// To use precedence climbing, we first need to specify the relative
    /// precedence of operators as a number, where 1 is the lowest precedence:
    ///
    /// * 1: OR
    /// * 2: AND
    /// * 3: NOT
    /// * 4: =, !=, LIKE, IS
    /// * 5: <, <=, >, >=
    /// * 6: +, -
    /// * 7: *, /, %
    /// * 8: ^
    /// * 9: !
    /// * 10: +, - (prefix)
    ///
    /// We also have to specify the associativity of operators:
    ///
    /// * Right-associative: ^ and all prefix operators.
    /// * Left-associative: all other operators.
    ///
    /// Left-associative operators get a +1 to their precedence, so that they
    /// bind tighter to their left operand than right-associative operators.
    ///
    /// The precedence climbing algorithm works by recursively parsing the
    /// left-hand side of an expression (including any prefix operators), any
    /// infix operators and recursive right-hand side expressions, and finally
    /// any postfix operators.
    ///
    /// The grouping is determined by where the right-hand side recursion
    /// terminates. The algorithm will greedily consume as many operators as
    /// possible, but only as long as their precedence is greater than or equal
    /// to the precedence of the previous operator (hence the name "climbing").
    /// When we find an operator with lower precedence, we return the current
    /// expression up the recursion stack and resume parsing the operator at a
    /// lower precedence.
    ///
    /// The precedence levels for the previous example are as follows:
    ///
    ///     -----          Precedence 9: ^ right-associativity
    /// ---------          Precedence 9: ^
    ///             -----  Precedence 7: *
    /// -----------------  Precedence 6: -
    /// 2 ^ 3 ^ 2 - 4 * 3
    ///
    /// Let's walk through the recursive parsing of this expression:
    ///
    /// parse_expression_at(prec=0)
    ///   lhs = parse_expression_atom() = 2
    ///   op = parse_infix_operator(prec=0) = ^ (prec=9)
    ///   rhs = parse_expression_at(prec=9)
    ///     lhs = parse_expression_atom() = 3
    ///     op = parse_infix_operator(prec=9) = ^ (prec=9)
    ///     rhs = parse_expression_at(prec=9)
    ///       lhs = parse_expression_atom() = 2
    ///       op = parse_infix_operator(prec=9) = None (reject - at prec=6)
    ///       return lhs = 2
    ///     lhs = (lhs op rhs) = (3 ^ 2)
    ///     op = parse_infix_operator(prec=9) = None (reject - at prec=6)
    ///     return lhs = (3 ^ 2)
    ///   lhs = (lhs op rhs) = (2 ^ (3 ^ 2))
    ///   op = parse_infix_operator(prec=0) = - (prec=6)
    ///   rhs = parse_expression_at(prec=6)
    ///     lhs = parse_expression_atom() = 4
    ///     op = parse_infix_operator(prec=6) = * (prec=7)
    ///     rhs = parse_expression_at(prec=7)
    ///       lhs = parse_expression_atom() = 3
    ///       op = parse_infix_operator(prec=7) = None (end of expression)
    ///       return lhs = 3
    ///     lhs = (lhs op rhs) = (4 * 3)
    ///     op = parse_infix_operator(prec=6) = None (end of expression)
    ///     return lhs = (4 * 3)
    ///   lhs = (lhs op rhs) = ((2 ^ (3 ^ 2)) - (4 * 3))
    ///   op = parse_infix_operator(prec=0) = None (end of expression)
    ///   return lhs = ((2 ^ (3 ^ 2)) - (4 * 3))
    fn parse_expression(&mut self) -> Result<ast::Expression> {
        self.parse_expression_at(0)
    }

    /// Parses an expression at the given minimum precedence.
    fn parse_expression_at(&mut self, min_precedence: Precedence) -> Result<ast::Expression> {
        // If the left-hand side is a prefix operator, recursively parse it and
        // its operand. Otherwise, parse the left-hand side as an atom.
        let mut lhs = if let Some(prefix) = self.parse_prefix_operator_at(min_precedence) {
            let next_precedence = prefix.precedence() + prefix.associativity();
            let rhs = self.parse_expression_at(next_precedence)?;
            prefix.into_expression(rhs)
        } else {
            self.parse_expression_atom()?
        };

        // Apply any postfix operators to the left-hand side.
        while let Some(postfix) = self.parse_postfix_operator_at(min_precedence)? {
            lhs = postfix.into_expression(lhs)
        }

        // Repeatedly apply any infix operators to the left-hand side as long as
        // their precedence is greater than or equal to the current minimum
        // precedence (i.e. that of the upstack operator).
        //
        // The right-hand side expression parsing will recursively apply any
        // infix operators at or above this operator's precedence to the
        // right-hand side.
        while let Some(infix) = self.parse_infix_operator_at(min_precedence) {
            let next_precedence = infix.precedence() + infix.associativity();
            let rhs = self.parse_expression_at(next_precedence)?;
            lhs = infix.into_expression(lhs, rhs);
        }

        // Apply any postfix operators after the binary operator. Consider e.g.
        // 1 + NULL IS NULL.
        while let Some(postfix) = self.parse_postfix_operator_at(min_precedence)? {
            lhs = postfix.into_expression(lhs)
        }

        Ok(lhs)
    }

    /// Parses an expression atom. This is either:
    ///
    /// * A literal value.
    /// * A column name.
    /// * A function call.
    /// * A parenthesized expression.
    fn parse_expression_atom(&mut self) -> Result<ast::Expression> {
        Ok(match self.next()? {
            // All columns.
            Token::Asterisk => ast::Expression::All,

            // Literal value.
            Token::Number(n) if n.chars().all(|c| c.is_ascii_digit()) => {
                ast::Literal::Integer(n.parse()?).into()
            }
            Token::Number(n) => ast::Literal::Float(n.parse()?).into(),
            Token::String(s) => ast::Literal::String(s).into(),

            // Parameter.
            Token::Parameter(n) => ast::Expression::Parameter(n),
            Token::Keyword(Keyword::True) => ast::Literal::Boolean(true).into(),
            Token::Keyword(Keyword::False) => ast::Literal::Boolean(false).into(),
            Token::Keyword(Keyword::Infinity) => ast::Literal::Float(f64::INFINITY).into(),
            Token::Keyword(Keyword::NaN) => ast::Literal::Float(f64::NAN).into(),
            Token::Keyword(Keyword::Null) => ast::Literal::Null.into(),

            // Function call.
            Token::Ident(name) if self.next_is(Token::OpenParen) => {
                let mut args = Vec::new();
                while !self.next_is(Token::CloseParen) {
                    if !args.is_empty() {
                        self.expect(Token::Comma)?;
                    }
                    args.push(self.parse_expression()?);
                }
                ast::Expression::Function(name, args)
            }

            // Column name, either qualified as table.column or unqualified.
            Token::Ident(table) if self.next_is(Token::Period) => {
                ast::Expression::Column(Some(table), self.next_ident()?)
            }
            Token::Ident(column) => ast::Expression::Column(None, column),

            // Parenthesized expression.
            Token::OpenParen => {
                let expr = self.parse_expression()?;
                self.expect(Token::CloseParen)?;
                expr
            }

            token => return errinput!("expected expression atom, found {token}"),
        })
    }

    /// Parses a prefix operator, if there is one and its precedence is at least
    /// min_precedence.
    fn parse_prefix_operator_at(&mut self, min_precedence: Precedence) -> Option<PrefixOperator> {
        self.next_if_map(|token| {
            let operator = match token {
                Token::Keyword(Keyword::Not) => PrefixOperator::Not,
                Token::Minus => PrefixOperator::Minus,
                Token::Plus => PrefixOperator::Plus,
                _ => return None,
            };
            Some(operator).filter(|op| op.precedence() >= min_precedence)
        })
    }

    /// Parses an infix operator, if there is one and its precedence is at least
    /// min_precedence.
    fn parse_infix_operator_at(&mut self, min_precedence: Precedence) -> Option<InfixOperator> {
        self.next_if_map(|token| {
            let operator = match token {
                Token::Asterisk => InfixOperator::Multiply,
                Token::Caret => InfixOperator::Exponentiate,
                Token::Equal => InfixOperator::Equal,
                Token::GreaterThan => InfixOperator::GreaterThan,
                Token::GreaterThanOrEqual => InfixOperator::GreaterThanOrEqual,
                Token::Keyword(Keyword::And) => InfixOperator::And,
                Token::Keyword(Keyword::Like) => InfixOperator::Like,
                Token::Keyword(Keyword::Or) => InfixOperator::Or,
                Token::LessOrGreaterThan => InfixOperator::NotEqual,
                Token::LessThan => InfixOperator::LessThan,
                Token::LessThanOrEqual => InfixOperator::LessThanOrEqual,
                Token::Minus => InfixOperator::Subtract,
                Token::NotEqual => InfixOperator::NotEqual,
                Token::Percent => InfixOperator::Remainder,
                Token::Plus => InfixOperator::Add,
                Token::Slash => InfixOperator::Divide,
                _ => return None,
            };
            Some(operator).filter(|op| op.precedence() >= min_precedence)
        })
    }

    /// Parses a postfix operator, if there is one and its precedence is at
    /// least min_precedence.
    fn parse_postfix_operator_at(
        &mut self,
        min_precedence: Precedence,
    ) -> Result<Option<PostfixOperator>> {
        // Handle IS (NOT) NULL/NAN separately, since it's multiple tokens.
        if self.peek()? == Some(&Token::Keyword(Keyword::Is)) {
            // We can't consume tokens unless the precedence is satisfied, so we
            // assume IS NULL (they all have the same precedence).
            if PostfixOperator::Is(ast::Literal::Null).precedence() < min_precedence {
                return Ok(None);
            }
            self.expect(Keyword::Is.into())?;
            let not = self.next_is(Keyword::Not.into());
            let value = match self.next()? {
                Token::Keyword(Keyword::NaN) => ast::Literal::Float(f64::NAN),
                Token::Keyword(Keyword::Null) => ast::Literal::Null,
                token => return errinput!("unexpected token {token}"),
            };
            let operator = match not {
                false => PostfixOperator::Is(value),
                true => PostfixOperator::IsNot(value),
            };
            return Ok(Some(operator));
        }

        Ok(self.next_if_map(|token| {
            let operator = match token {
                Token::Exclamation => PostfixOperator::Factorial,
                _ => return None,
            };
            Some(operator).filter(|op| op.precedence() >= min_precedence)
        }))
    }
}

/// Operator precedence.
type Precedence = u8;

/// Operator associativity.
enum Associativity {
    Left,
    Right,
}

impl Add<Associativity> for Precedence {
    type Output = Self;

    fn add(self, rhs: Associativity) -> Self {
        // Left-associative operators have increased precedence, so they bind
        // tighter to their left-hand side.
        self + match rhs {
            Associativity::Left => 1,
            Associativity::Right => 0,
        }
    }
}

/// Prefix operators.
enum PrefixOperator {
    Minus, // -a
    Not,   // NOT a
    Plus,  // +a
}

impl PrefixOperator {
    /// The operator precedence.
    fn precedence(&self) -> Precedence {
        match self {
            Self::Not => 3,
            Self::Minus | Self::Plus => 10,
        }
    }

    // The operator associativity. Prefix operators are right-associative by
    // definition.
    fn associativity(&self) -> Associativity {
        Associativity::Right
    }

    /// Builds an AST expression for the operator.
    fn into_expression(self, rhs: ast::Expression) -> ast::Expression {
        let rhs = Box::new(rhs);
        match self {
            Self::Plus => ast::Operator::Identity(rhs).into(),
            Self::Minus => ast::Operator::Negate(rhs).into(),
            Self::Not => ast::Operator::Not(rhs).into(),
        }
    }
}

/// Infix operators.
enum InfixOperator {
    Add,                // a + b
    And,                // a AND b
    Divide,             // a / b
    Equal,              // a = b
    Exponentiate,       // a ^ b
    GreaterThan,        // a > b
    GreaterThanOrEqual, // a >= b
    LessThan,           // a < b
    LessThanOrEqual,    // a <= b
    Like,               // a LIKE b
    Multiply,           // a * b
    NotEqual,           // a != b
    Or,                 // a OR b
    Remainder,          // a % b
    Subtract,           // a - b
}

impl InfixOperator {
    /// The operator precedence.
    ///
    /// Mostly follows Postgres, except IS and LIKE having same precedence as =.
    /// This is similar to SQLite and MySQL.
    fn precedence(&self) -> Precedence {
        match self {
            Self::Or => 1,
            Self::And => 2,
            // Self::Not => 3
            Self::Equal | Self::NotEqual | Self::Like => 4, // also Self::Is
            Self::GreaterThan
            | Self::GreaterThanOrEqual
            | Self::LessThan
            | Self::LessThanOrEqual => 5,
            Self::Add | Self::Subtract => 6,
            Self::Multiply | Self::Divide | Self::Remainder => 7,
            Self::Exponentiate => 8,
        }
    }

    /// The operator associativity.
    fn associativity(&self) -> Associativity {
        match self {
            Self::Exponentiate => Associativity::Right,
            _ => Associativity::Left,
        }
    }

    /// Builds an AST expression for the infix operator.
    fn into_expression(self, lhs: ast::Expression, rhs: ast::Expression) -> ast::Expression {
        let (lhs, rhs) = (Box::new(lhs), Box::new(rhs));
        match self {
            Self::Add => ast::Operator::Add(lhs, rhs).into(),
            Self::And => ast::Operator::And(lhs, rhs).into(),
            Self::Divide => ast::Operator::Divide(lhs, rhs).into(),
            Self::Equal => ast::Operator::Equal(lhs, rhs).into(),
            Self::Exponentiate => ast::Operator::Exponentiate(lhs, rhs).into(),
            Self::GreaterThan => ast::Operator::GreaterThan(lhs, rhs).into(),
            Self::GreaterThanOrEqual => ast::Operator::GreaterThanOrEqual(lhs, rhs).into(),
            Self::LessThan => ast::Operator::LessThan(lhs, rhs).into(),
            Self::LessThanOrEqual => ast::Operator::LessThanOrEqual(lhs, rhs).into(),
            Self::Like => ast::Operator::Like(lhs, rhs).into(),
            Self::Multiply => ast::Operator::Multiply(lhs, rhs).into(),
            Self::NotEqual => ast::Operator::NotEqual(lhs, rhs).into(),
            Self::Or => ast::Operator::Or(lhs, rhs).into(),
            Self::Remainder => ast::Operator::Remainder(lhs, rhs).into(),
            Self::Subtract => ast::Operator::Subtract(lhs, rhs).into(),
        }
    }
}

/// Postfix operators.
enum PostfixOperator {
    Factorial,           // a!
    Is(ast::Literal),    // a IS NULL | NAN
    IsNot(ast::Literal), // a IS NOT NULL | NAN
}

impl PostfixOperator {
    // The operator precedence.
    fn precedence(&self) -> Precedence {
        match self {
            Self::Is(_) | Self::IsNot(_) => 4,
            Self::Factorial => 9,
        }
    }

    /// Builds an AST expression for the operator.
    fn into_expression(self, lhs: ast::Expression) -> ast::Expression {
        let lhs = Box::new(lhs);
        match self {
            Self::Factorial => ast::Operator::Factorial(lhs).into(),
            Self::Is(v) => ast::Operator::Is(lhs, v).into(),
            Self::IsNot(v) => ast::Operator::IsNot(lhs, v).into(),
        }
    }
}
