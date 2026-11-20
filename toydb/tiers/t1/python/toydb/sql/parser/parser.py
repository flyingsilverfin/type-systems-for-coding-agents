"""The SQL parser.

Mirrors `src/sql/parser/parser.rs`.
"""

import enum
from abc import ABC
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import ClassVar

from toydb.error import Error, errinput
from toydb.sql.parser import ast
from toydb.sql.parser.lexer import Keyword, Lexer, Token
from toydb.sql.types.value import DataType

U64_MAX = 2**64 - 1
I64_MAX = 2**63 - 1


class Peekable[T]:
    """An iterator that can peek at the next item without consuming it, like
    Rust's `std::iter::Peekable`. If the inner iterator raises an `Error`, it
    is cached like an item: `peek()` raises it without consuming it, and
    `next()` consumes it."""

    def __init__(self, iter: Iterator[T]) -> None:
        self.iter = iter
        self.peeked: list[T | Error | None] = []
        """The peeked item, error, or None if exhausted. Empty if not peeked."""

    def _fetch(self) -> T | Error | None:
        try:
            return next(self.iter, None)
        except Error as error:
            return error

    def peek(self) -> T | None:
        """Returns the next item without consuming it, or None if exhausted."""
        if not self.peeked:
            self.peeked.append(self._fetch())
        item = self.peeked[0]
        if isinstance(item, Error):
            raise item
        return item

    def next(self) -> T | None:
        """Consumes and returns the next item, or None if exhausted."""
        item = self.peeked.pop() if self.peeked else self._fetch()
        if isinstance(item, Error):
            raise item
        return item


class Parser:
    """The SQL parser takes tokens from the lexer and parses the SQL syntax
    into an Abstract Syntax Tree (AST).

    The AST represents the syntactic structure of a SQL query (e.g. the SELECT
    and FROM clauses, values, arithmetic expressions, etc.). However, it only
    ensures the syntax is well-formed, and does not know whether e.g. a given
    table or column exists or which kind of join to use -- that is the job of
    the planner.
    """

    def __init__(self, input: str) -> None:
        """Creates a new parser for the given raw SQL string."""
        self.lexer: Peekable[Token] = Peekable(Lexer(input))

    @staticmethod
    def parse(statement: str) -> ast.Statement:
        """Parses the input string into a SQL statement AST. The entire string
        must be parsed as a single statement, ending with an optional
        semicolon."""
        parser = Parser(statement)
        result = parser.parse_statement()
        parser.skip(Token.Semicolon())
        if (token := parser.lexer.next()) is not None:
            raise errinput(f"unexpected token {token}")
        return result

    @staticmethod
    def parse_expr(expr: str) -> ast.Expression:
        """Parse the input string into a SQL expression AST. The entire string
        must be parsed as a single expression. Only used in tests."""
        parser = Parser(expr)
        expression = parser.parse_expression()
        if (token := parser.lexer.next()) is not None:
            raise errinput(f"unexpected token {token}")
        return expression

    def next(self) -> Token:
        """Fetches the next lexer token, or errors if none is found."""
        token = self.lexer.next()
        if token is None:
            raise errinput("unexpected end of input")
        return token

    def next_ident(self) -> str:
        """Returns the next identifier, or errors if not found."""
        match self.next():
            case Token.Ident(ident):
                return ident
            case token:
                raise errinput(f"expected identifier, got {token}")

    def next_if(self, predicate: Callable[[Token], bool]) -> Token | None:
        """Returns the next lexer token if it satisfies the predicate."""
        try:
            token = self.peek()
        except Error:
            return None
        if token is None or not predicate(token):
            return None
        return self.lexer.next()

    def next_if_map[T](self, f: Callable[[Token], T | None]) -> T | None:
        """Passes the next lexer token through the closure, consuming it if
        the closure returns a value. Returns the result of the closure."""
        try:
            token = self.peek()
        except Error:
            return None
        if token is None or (value := f(token)) is None:
            return None
        self.lexer.next()
        return value

    def next_if_keyword(self) -> Keyword | None:
        """Returns the next keyword if there is one."""

        def keyword(token: Token) -> Keyword | None:
            match token:
                case Token.Keyword(keyword):
                    return keyword
                case _:
                    return None

        return self.next_if_map(keyword)

    def next_is(self, token: Token | Keyword) -> bool:
        """Consumes the next lexer token if it is the given token (or keyword),
        returning true."""
        token = _token(token)
        return self.next_if(lambda t: t == token) is not None

    def expect(self, expect: Token | Keyword) -> None:
        """Consumes the next lexer token if it's the expected token (or
        keyword), or errors."""
        expect = _token(expect)
        token = self.next()
        if token != expect:
            raise errinput(f"expected token {expect}, found {token}")

    def skip(self, token: Token | Keyword) -> None:
        """Consumes the next lexer token if it is the given token. Equivalent
        to next_is(), but expresses intent better."""
        self.next_is(token)

    def peek(self) -> Token | None:
        """Peeks the next lexer token if any."""
        return self.lexer.peek()

    def parse_statement(self) -> ast.Statement:
        """Parses a SQL statement."""
        token = self.peek()
        if token is None:
            raise errinput("unexpected end of input")
        match token:
            case Token.Keyword(Keyword.Begin):
                return self.parse_begin()
            case Token.Keyword(Keyword.Commit):
                return self.parse_commit()
            case Token.Keyword(Keyword.Rollback):
                return self.parse_rollback()
            case Token.Keyword(Keyword.Explain):
                return self.parse_explain()

            case Token.Keyword(Keyword.Create):
                return self.parse_create_table()
            case Token.Keyword(Keyword.Drop):
                return self.parse_drop_table()

            case Token.Keyword(Keyword.Delete):
                return self.parse_delete()
            case Token.Keyword(Keyword.Insert):
                return self.parse_insert()
            case Token.Keyword(Keyword.Select):
                return self.parse_select()
            case Token.Keyword(Keyword.Update):
                return self.parse_update()

            case token:
                raise errinput(f"unexpected token {token}")

    def parse_begin(self) -> ast.Statement:
        """Parses a BEGIN statement."""
        self.expect(Keyword.Begin)
        self.skip(Keyword.Transaction)

        read_only = False
        if self.next_is(Keyword.Read):
            match self.next():
                case Token.Keyword(Keyword.Only):
                    read_only = True
                case Token.Keyword(Keyword.Write):
                    pass
                case token:
                    raise errinput(f"unexpected token {token}")

        as_of = None
        if self.next_is(Keyword.As):
            self.expect(Keyword.Of)
            self.expect(Keyword.System)
            self.expect(Keyword.Time)
            match self.next():
                case Token.Number(n):
                    as_of = _parse_int(n, U64_MAX)
                case token:
                    raise errinput(f"unexpected token {token}, wanted number")
        return ast.Statement.Begin(read_only=read_only, as_of=as_of)

    def parse_commit(self) -> ast.Statement:
        """Parses a COMMIT statement."""
        self.expect(Keyword.Commit)
        return ast.Statement.Commit()

    def parse_rollback(self) -> ast.Statement:
        """Parses a ROLLBACK statement."""
        self.expect(Keyword.Rollback)
        return ast.Statement.Rollback()

    def parse_explain(self) -> ast.Statement:
        """Parses an EXPLAIN statement."""
        self.expect(Keyword.Explain)
        if self.next_is(Keyword.Explain):
            raise errinput("cannot nest EXPLAIN statements")
        return ast.Statement.Explain(self.parse_statement())

    def parse_create_table(self) -> ast.Statement:
        """Parses a CREATE TABLE statement."""
        self.expect(Keyword.Create)
        self.expect(Keyword.Table)
        name = self.next_ident()
        self.expect(Token.OpenParen())
        columns: list[ast.Column] = []
        while True:
            columns.append(self.parse_create_table_column())
            if not self.next_is(Token.Comma()):
                break
        self.expect(Token.CloseParen())
        return ast.Statement.CreateTable(name=name, columns=columns)

    def parse_create_table_column(self) -> ast.Column:
        """Parses a CREATE TABLE column definition."""
        name = self.next_ident()
        match self.next():
            case Token.Keyword(Keyword.Bool | Keyword.Boolean):
                datatype = DataType.Boolean
            case Token.Keyword(Keyword.Float | Keyword.Double):
                datatype = DataType.Float
            case Token.Keyword(Keyword.Int | Keyword.Integer):
                datatype = DataType.Integer
            case Token.Keyword(Keyword.String | Keyword.Text | Keyword.Varchar):
                datatype = DataType.String
            case token:
                raise errinput(f"unexpected token {token}")
        column = ast.Column(
            name=name,
            datatype=datatype,
            primary_key=False,
            nullable=None,
            default=None,
            unique=False,
            index=False,
            references=None,
        )
        while (keyword := self.next_if_keyword()) is not None:
            match keyword:
                case Keyword.Primary:
                    self.expect(Keyword.Key)
                    column.primary_key = True
                case Keyword.Null:
                    if column.nullable is not None:
                        raise errinput(f"nullability already set for column {column.name}")
                    column.nullable = True
                case Keyword.Not:
                    self.expect(Keyword.Null)
                    if column.nullable is not None:
                        raise errinput(f"nullability already set for column {column.name}")
                    column.nullable = False
                case Keyword.Default:
                    column.default = self.parse_expression()
                case Keyword.Unique:
                    column.unique = True
                case Keyword.Index:
                    column.index = True
                case Keyword.References:
                    column.references = self.next_ident()
                case keyword:
                    raise errinput(f"unexpected keyword {keyword}")
        return column

    def parse_drop_table(self) -> ast.Statement:
        """Parses a DROP TABLE statement."""
        self.expect(Keyword.Drop)
        self.expect(Keyword.Table)
        if_exists = False
        if self.next_is(Keyword.If):
            self.expect(Keyword.Exists)
            if_exists = True
        name = self.next_ident()
        return ast.Statement.DropTable(name=name, if_exists=if_exists)

    def parse_delete(self) -> ast.Statement:
        """Parses a DELETE statement."""
        self.expect(Keyword.Delete)
        self.expect(Keyword.From)
        table = self.next_ident()
        return ast.Statement.Delete(table=table, where=self.parse_where_clause())

    def parse_insert(self) -> ast.Statement:
        """Parses an INSERT statement."""
        self.expect(Keyword.Insert)
        self.expect(Keyword.Into)
        table = self.next_ident()

        columns: list[str] | None = None
        if self.next_is(Token.OpenParen()):
            columns = []
            while True:
                columns.append(self.next_ident())
                if not self.next_is(Token.Comma()):
                    break
            self.expect(Token.CloseParen())

        self.expect(Keyword.Values)

        values: list[list[ast.Expression]] = []
        while True:
            row: list[ast.Expression] = []
            self.expect(Token.OpenParen())
            while True:
                row.append(self.parse_expression())
                if not self.next_is(Token.Comma()):
                    break
            self.expect(Token.CloseParen())
            values.append(row)
            if not self.next_is(Token.Comma()):
                break

        return ast.Statement.Insert(table=table, columns=columns, values=values)

    def parse_update(self) -> ast.Statement:
        """Parses an UPDATE statement."""
        self.expect(Keyword.Update)
        table = self.next_ident()
        self.expect(Keyword.Set)
        set: dict[str, ast.Expression | None] = {}
        while True:
            column = self.next_ident()
            self.expect(Token.Equal())
            expr = None if self.next_is(Keyword.Default) else self.parse_expression()
            if column in set:
                raise errinput(f"column {column} set multiple times")
            set[column] = expr
            if not self.next_is(Token.Comma()):
                break
        return ast.Statement.Update(
            table=table,
            set=dict(sorted(set.items())),  # ordered by column, like a BTreeMap
            where=self.parse_where_clause(),
        )

    def parse_select(self) -> ast.Statement:
        """Parses a SELECT statement."""
        # NB: the clauses are parsed in this order, i.e. LIMIT before OFFSET.
        select = self.parse_select_clause()
        from_ = self.parse_from_clause()
        where = self.parse_where_clause()
        group_by = self.parse_group_by_clause()
        having = self.parse_having_clause()
        order_by = self.parse_order_by_clause()
        limit = self.parse_limit_clause()
        offset = self.parse_offset_clause()
        return ast.Statement.Select(
            select=select,
            from_=from_,
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            offset=offset,
            limit=limit,
        )

    def parse_select_clause(self) -> list[tuple[ast.Expression, str | None]]:
        """Parses a SELECT clause, if present."""
        if not self.next_is(Keyword.Select):
            return []
        select: list[tuple[ast.Expression, str | None]] = []
        while True:
            expr = self.parse_expression()
            alias = None
            if self.next_is(Keyword.As) or isinstance(self.peek(), Token.Ident):
                if expr == ast.Expression.All():
                    raise errinput("can't alias *")
                alias = self.next_ident()
            select.append((expr, alias))
            if not self.next_is(Token.Comma()):
                break
        return select

    def parse_from_clause(self) -> list[ast.From]:
        """Parses a FROM clause, if present."""
        if not self.next_is(Keyword.From):
            return []
        from_: list[ast.From] = []
        while True:
            from_item = self.parse_from_table()
            while (type := self.parse_from_join()) is not None:
                left = from_item
                right = self.parse_from_table()
                predicate = None
                if type != ast.JoinType.Cross:
                    self.expect(Keyword.On)
                    predicate = self.parse_expression()
                from_item = ast.From.Join(left=left, right=right, type=type, predicate=predicate)
            from_.append(from_item)
            if not self.next_is(Token.Comma()):
                break
        return from_

    def parse_from_table(self) -> ast.From:
        """Parses a FROM table."""
        name = self.next_ident()
        alias = None
        if self.next_is(Keyword.As) or isinstance(self.peek(), Token.Ident):
            alias = self.next_ident()
        return ast.From.Table(name=name, alias=alias)

    def parse_from_join(self) -> ast.JoinType | None:
        """Parses a FROM JOIN type, if present."""
        if self.next_is(Keyword.Join):
            return ast.JoinType.Inner
        if self.next_is(Keyword.Cross):
            self.expect(Keyword.Join)
            return ast.JoinType.Cross
        if self.next_is(Keyword.Inner):
            self.expect(Keyword.Join)
            return ast.JoinType.Inner
        if self.next_is(Keyword.Left):
            self.skip(Keyword.Outer)
            self.expect(Keyword.Join)
            return ast.JoinType.Left
        if self.next_is(Keyword.Right):
            self.skip(Keyword.Outer)
            self.expect(Keyword.Join)
            return ast.JoinType.Right
        return None

    def parse_where_clause(self) -> ast.Expression | None:
        """Parses a WHERE clause, if present."""
        if not self.next_is(Keyword.Where):
            return None
        return self.parse_expression()

    def parse_group_by_clause(self) -> list[ast.Expression]:
        """Parses a GROUP BY clause, if present."""
        if not self.next_is(Keyword.Group):
            return []
        group_by: list[ast.Expression] = []
        self.expect(Keyword.By)
        while True:
            group_by.append(self.parse_expression())
            if not self.next_is(Token.Comma()):
                break
        return group_by

    def parse_having_clause(self) -> ast.Expression | None:
        """Parses a HAVING clause, if present."""
        if not self.next_is(Keyword.Having):
            return None
        return self.parse_expression()

    def parse_order_by_clause(self) -> list[tuple[ast.Expression, ast.Direction]]:
        """Parses an ORDER BY clause, if present."""
        if not self.next_is(Keyword.Order):
            return []

        def direction(token: Token) -> ast.Direction | None:
            match token:
                case Token.Keyword(Keyword.Asc):
                    return ast.Direction.Ascending
                case Token.Keyword(Keyword.Desc):
                    return ast.Direction.Descending
                case _:
                    return None

        order_by: list[tuple[ast.Expression, ast.Direction]] = []
        self.expect(Keyword.By)
        while True:
            expr = self.parse_expression()
            order = self.next_if_map(direction) or ast.Direction.Ascending
            order_by.append((expr, order))
            if not self.next_is(Token.Comma()):
                break
        return order_by

    def parse_limit_clause(self) -> ast.Expression | None:
        """Parses a LIMIT clause, if present."""
        if not self.next_is(Keyword.Limit):
            return None
        return self.parse_expression()

    def parse_offset_clause(self) -> ast.Expression | None:
        """Parses an OFFSET clause, if present."""
        if not self.next_is(Keyword.Offset):
            return None
        return self.parse_expression()

    def parse_expression(self) -> ast.Expression:
        """Parses an expression using the precedence climbing algorithm. See:

        <https://en.wikipedia.org/wiki/Operator-precedence_parser#Precedence_climbing_method>
        <https://eli.thegreenplace.net/2012/08/02/parsing-expressions-by-precedence-climbing>

        Expressions are made up of two main entities:

        * Atoms: values, variables, functions, and parenthesized expressions.
        * Operators: performs operations on atoms and sub-expressions.
          * Prefix operators: e.g. `-a` or `NOT a`.
          * Infix operators: e.g. `a + b`  or `a AND b`.
          * Postfix operators: e.g. `a!` or `a IS NULL`.

        During parsing, we have to respect the mathematical precedence and
        associativity of operators. Consider e.g.:

        2 ^ 3 ^ 2 - 4 * 3

        By the rules of precedence and associativity, this expression should
        be interpreted as:

        (2 ^ (3 ^ 2)) - (4 * 3)

        Specifically, the exponentiation operator ^ is right-associative, so it
        should be 2 ^ (3 ^ 2) = 512, not (2 ^ 3) ^ 2 = 64. Similarly,
        exponentiation and multiplication have higher precedence than
        subtraction, so it should be (2 ^ 3 ^ 2) - (4 * 3) = 500, not
        2 ^ 3 ^ (2 - 4) * 3 = -3.24.

        To use precedence climbing, we first need to specify the relative
        precedence of operators as a number, where 1 is the lowest precedence:

        * 1: OR
        * 2: AND
        * 3: NOT
        * 4: =, !=, LIKE, IS
        * 5: <, <=, >, >=
        * 6: +, -
        * 7: *, /, %
        * 8: ^
        * 9: !
        * 10: +, - (prefix)

        We also have to specify the associativity of operators:

        * Right-associative: ^ and all prefix operators.
        * Left-associative: all other operators.

        Left-associative operators get a +1 to their precedence, so that they
        bind tighter to their left operand than right-associative operators.

        The precedence climbing algorithm works by recursively parsing the
        left-hand side of an expression (including any prefix operators), any
        infix operators and recursive right-hand side expressions, and finally
        any postfix operators.

        The grouping is determined by where the right-hand side recursion
        terminates. The algorithm will greedily consume as many operators as
        possible, but only as long as their precedence is greater than or equal
        to the precedence of the previous operator (hence the name "climbing").
        When we find an operator with lower precedence, we return the current
        expression up the recursion stack and resume parsing the operator at a
        lower precedence.

        The precedence levels for the previous example are as follows:

            -----          Precedence 9: ^ right-associativity
        ---------          Precedence 9: ^
                    -----  Precedence 7: *
        -----------------  Precedence 6: -
        2 ^ 3 ^ 2 - 4 * 3

        Let's walk through the recursive parsing of this expression:

        parse_expression_at(prec=0)
          lhs = parse_expression_atom() = 2
          op = parse_infix_operator(prec=0) = ^ (prec=9)
          rhs = parse_expression_at(prec=9)
            lhs = parse_expression_atom() = 3
            op = parse_infix_operator(prec=9) = ^ (prec=9)
            rhs = parse_expression_at(prec=9)
              lhs = parse_expression_atom() = 2
              op = parse_infix_operator(prec=9) = None (reject - at prec=6)
              return lhs = 2
            lhs = (lhs op rhs) = (3 ^ 2)
            op = parse_infix_operator(prec=9) = None (reject - at prec=6)
            return lhs = (3 ^ 2)
          lhs = (lhs op rhs) = (2 ^ (3 ^ 2))
          op = parse_infix_operator(prec=0) = - (prec=6)
          rhs = parse_expression_at(prec=6)
            lhs = parse_expression_atom() = 4
            op = parse_infix_operator(prec=6) = * (prec=7)
            rhs = parse_expression_at(prec=7)
              lhs = parse_expression_atom() = 3
              op = parse_infix_operator(prec=7) = None (end of expression)
              return lhs = 3
            lhs = (lhs op rhs) = (4 * 3)
            op = parse_infix_operator(prec=6) = None (end of expression)
            return lhs = (4 * 3)
          lhs = (lhs op rhs) = ((2 ^ (3 ^ 2)) - (4 * 3))
          op = parse_infix_operator(prec=0) = None (end of expression)
          return lhs = ((2 ^ (3 ^ 2)) - (4 * 3))
        """
        return self.parse_expression_at(0)

    def parse_expression_at(self, min_precedence: "Precedence") -> ast.Expression:
        """Parses an expression at the given minimum precedence."""
        # If the left-hand side is a prefix operator, recursively parse it and
        # its operand. Otherwise, parse the left-hand side as an atom.
        if (prefix := self.parse_prefix_operator_at(min_precedence)) is not None:
            next_precedence = prefix.precedence() + prefix.associativity()
            rhs = self.parse_expression_at(next_precedence)
            lhs = prefix.into_expression(rhs)
        else:
            lhs = self.parse_expression_atom()

        # Apply any postfix operators to the left-hand side.
        while (postfix := self.parse_postfix_operator_at(min_precedence)) is not None:
            lhs = postfix.into_expression(lhs)

        # Repeatedly apply any infix operators to the left-hand side as long as
        # their precedence is greater than or equal to the current minimum
        # precedence (i.e. that of the upstack operator).
        #
        # The right-hand side expression parsing will recursively apply any
        # infix operators at or above this operator's precedence to the
        # right-hand side.
        while (infix := self.parse_infix_operator_at(min_precedence)) is not None:
            next_precedence = infix.precedence() + infix.associativity()
            rhs = self.parse_expression_at(next_precedence)
            lhs = infix.into_expression(lhs, rhs)

        # Apply any postfix operators after the binary operator. Consider e.g.
        # 1 + NULL IS NULL.
        while (postfix := self.parse_postfix_operator_at(min_precedence)) is not None:
            lhs = postfix.into_expression(lhs)

        return lhs

    def parse_expression_atom(self) -> ast.Expression:
        """Parses an expression atom. This is either:

        * A literal value.
        * A column name.
        * A function call.
        * A parenthesized expression.
        """
        literal = ast.Expression.from_literal
        match self.next():
            # All columns.
            case Token.Asterisk():
                return ast.Expression.All()

            # Literal value.
            case Token.Number(n) if all("0" <= c <= "9" for c in n):
                return literal(ast.Literal.Integer(_parse_int(n, I64_MAX)))
            case Token.Number(n):
                return literal(ast.Literal.Float(_parse_float(n)))
            case Token.String(s):
                return literal(ast.Literal.String(s))
            case Token.Keyword(Keyword.True_):
                return literal(ast.Literal.Boolean(True))
            case Token.Keyword(Keyword.False_):
                return literal(ast.Literal.Boolean(False))
            case Token.Keyword(Keyword.Infinity):
                return literal(ast.Literal.Float(float("inf")))
            case Token.Keyword(Keyword.NaN):
                return literal(ast.Literal.Float(float("nan")))
            case Token.Keyword(Keyword.Null):
                return literal(ast.Literal.Null())

            # Function call.
            case Token.Ident(name) if self.next_is(Token.OpenParen()):
                args: list[ast.Expression] = []
                while not self.next_is(Token.CloseParen()):
                    if args:
                        self.expect(Token.Comma())
                    args.append(self.parse_expression())
                return ast.Expression.Function(name, tuple(args))

            # Column name, either qualified as table.column or unqualified.
            case Token.Ident(table) if self.next_is(Token.Period()):
                return ast.Expression.Column(table, self.next_ident())
            case Token.Ident(column):
                return ast.Expression.Column(None, column)

            # Parenthesized expression.
            case Token.OpenParen():
                expr = self.parse_expression()
                self.expect(Token.CloseParen())
                return expr

            case token:
                raise errinput(f"expected expression atom, found {token}")

    def parse_prefix_operator_at(self, min_precedence: "Precedence") -> "PrefixOperator | None":
        """Parses a prefix operator, if there is one and its precedence is at
        least min_precedence."""

        def prefix(token: Token) -> PrefixOperator | None:
            match token:
                case Token.Keyword(Keyword.Not):
                    operator = PrefixOperator.Not
                case Token.Minus():
                    operator = PrefixOperator.Minus
                case Token.Plus():
                    operator = PrefixOperator.Plus
                case _:
                    return None
            return operator if operator.precedence() >= min_precedence else None

        return self.next_if_map(prefix)

    def parse_infix_operator_at(self, min_precedence: "Precedence") -> "InfixOperator | None":
        """Parses an infix operator, if there is one and its precedence is at
        least min_precedence."""

        def infix(token: Token) -> InfixOperator | None:
            match token:
                case Token.Asterisk():
                    operator = InfixOperator.Multiply
                case Token.Caret():
                    operator = InfixOperator.Exponentiate
                case Token.Equal():
                    operator = InfixOperator.Equal
                case Token.GreaterThan():
                    operator = InfixOperator.GreaterThan
                case Token.GreaterThanOrEqual():
                    operator = InfixOperator.GreaterThanOrEqual
                case Token.Keyword(Keyword.And):
                    operator = InfixOperator.And
                case Token.Keyword(Keyword.Like):
                    operator = InfixOperator.Like
                case Token.Keyword(Keyword.Or):
                    operator = InfixOperator.Or
                case Token.LessOrGreaterThan():
                    operator = InfixOperator.NotEqual
                case Token.LessThan():
                    operator = InfixOperator.LessThan
                case Token.LessThanOrEqual():
                    operator = InfixOperator.LessThanOrEqual
                case Token.Minus():
                    operator = InfixOperator.Subtract
                case Token.NotEqual():
                    operator = InfixOperator.NotEqual
                case Token.Percent():
                    operator = InfixOperator.Remainder
                case Token.Plus():
                    operator = InfixOperator.Add
                case Token.Slash():
                    operator = InfixOperator.Divide
                case _:
                    return None
            return operator if operator.precedence() >= min_precedence else None

        return self.next_if_map(infix)

    def parse_postfix_operator_at(self, min_precedence: "Precedence") -> "PostfixOperator | None":
        """Parses a postfix operator, if there is one and its precedence is at
        least min_precedence."""
        # Handle IS (NOT) NULL/NAN separately, since it's multiple tokens.
        if self.peek() == Token.Keyword(Keyword.Is):
            # We can't consume tokens unless the precedence is satisfied, so we
            # assume IS NULL (they all have the same precedence).
            if PostfixOperator.Is(ast.Literal.Null()).precedence() < min_precedence:
                return None
            self.expect(Keyword.Is)
            not_ = self.next_is(Keyword.Not)
            match self.next():
                case Token.Keyword(Keyword.NaN):
                    value: ast.Literal = ast.Literal.Float(float("nan"))
                case Token.Keyword(Keyword.Null):
                    value = ast.Literal.Null()
                case token:
                    raise errinput(f"unexpected token {token}")
            return PostfixOperator.IsNot(value) if not_ else PostfixOperator.Is(value)

        def postfix(token: Token) -> PostfixOperator | None:
            match token:
                case Token.Exclamation():
                    operator = PostfixOperator.Factorial()
                case _:
                    return None
            return operator if operator.precedence() >= min_precedence else None

        return self.next_if_map(postfix)


def _token(token: Token | Keyword) -> Token:
    """Converts a keyword into a token, like the Rust `Keyword::into()`."""
    return Token.from_keyword(token) if isinstance(token, Keyword) else token


def _parse_int(number: str, max: int) -> int:
    """Parses an unsigned decimal integer no larger than max, like Rust's
    `str::parse` for integers (with the same errors)."""
    if not number:
        raise errinput("cannot parse integer from empty string")
    result = 0
    for c in number:
        if not "0" <= c <= "9":
            raise errinput("invalid digit found in string")
        result = result * 10 + int(c)
        if result > max:
            raise errinput("number too large to fit in target type")
    return result


def _parse_float(number: str) -> float:
    """Parses a float, like Rust's `str::parse` for `f64` (with the same
    error)."""
    try:
        return float(number)
    except ValueError:
        raise errinput("invalid float literal") from None


type Precedence = int
"""Operator precedence."""


class Associativity(enum.Enum):
    """Operator associativity."""

    Left = enum.auto()
    Right = enum.auto()

    def __radd__(self, precedence: Precedence) -> Precedence:
        """Adds the associativity to a precedence: left-associative operators
        have increased precedence, so they bind tighter to their left-hand
        side."""
        match self:
            case Associativity.Left:
                return precedence + 1
            case Associativity.Right:
                return precedence


class PrefixOperator(enum.Enum):
    """Prefix operators."""

    Minus = enum.auto()  # -a
    Not = enum.auto()  # NOT a
    Plus = enum.auto()  # +a

    def precedence(self) -> Precedence:
        """The operator precedence."""
        match self:
            case PrefixOperator.Not:
                return 3
            case PrefixOperator.Minus | PrefixOperator.Plus:
                return 10

    def associativity(self) -> Associativity:
        """The operator associativity. Prefix operators are right-associative
        by definition."""
        return Associativity.Right

    def into_expression(self, rhs: ast.Expression) -> ast.Expression:
        """Builds an AST expression for the operator."""
        match self:
            case PrefixOperator.Plus:
                return ast.Expression.from_operator(ast.Operator.Identity(rhs))
            case PrefixOperator.Minus:
                return ast.Expression.from_operator(ast.Operator.Negate(rhs))
            case PrefixOperator.Not:
                return ast.Expression.from_operator(ast.Operator.Not(rhs))


class InfixOperator(enum.Enum):
    """Infix operators."""

    Add = enum.auto()  # a + b
    And = enum.auto()  # a AND b
    Divide = enum.auto()  # a / b
    Equal = enum.auto()  # a = b
    Exponentiate = enum.auto()  # a ^ b
    GreaterThan = enum.auto()  # a > b
    GreaterThanOrEqual = enum.auto()  # a >= b
    LessThan = enum.auto()  # a < b
    LessThanOrEqual = enum.auto()  # a <= b
    Like = enum.auto()  # a LIKE b
    Multiply = enum.auto()  # a * b
    NotEqual = enum.auto()  # a != b
    Or = enum.auto()  # a OR b
    Remainder = enum.auto()  # a % b
    Subtract = enum.auto()  # a - b

    def precedence(self) -> Precedence:
        """The operator precedence.

        Mostly follows Postgres, except IS and LIKE having same precedence as
        =. This is similar to SQLite and MySQL.
        """
        match self:
            case InfixOperator.Or:
                return 1
            case InfixOperator.And:
                return 2
            # PrefixOperator.Not => 3
            case InfixOperator.Equal | InfixOperator.NotEqual | InfixOperator.Like:
                return 4  # also PostfixOperator.Is
            case (
                InfixOperator.GreaterThan
                | InfixOperator.GreaterThanOrEqual
                | InfixOperator.LessThan
                | InfixOperator.LessThanOrEqual
            ):
                return 5
            case InfixOperator.Add | InfixOperator.Subtract:
                return 6
            case InfixOperator.Multiply | InfixOperator.Divide | InfixOperator.Remainder:
                return 7
            case InfixOperator.Exponentiate:
                return 8

    def associativity(self) -> Associativity:
        """The operator associativity."""
        match self:
            case InfixOperator.Exponentiate:
                return Associativity.Right
            case _:
                return Associativity.Left

    def into_expression(self, lhs: ast.Expression, rhs: ast.Expression) -> ast.Expression:
        """Builds an AST expression for the infix operator."""
        match self:
            case InfixOperator.Add:
                op: ast.Operator = ast.Operator.Add(lhs, rhs)
            case InfixOperator.And:
                op = ast.Operator.And(lhs, rhs)
            case InfixOperator.Divide:
                op = ast.Operator.Divide(lhs, rhs)
            case InfixOperator.Equal:
                op = ast.Operator.Equal(lhs, rhs)
            case InfixOperator.Exponentiate:
                op = ast.Operator.Exponentiate(lhs, rhs)
            case InfixOperator.GreaterThan:
                op = ast.Operator.GreaterThan(lhs, rhs)
            case InfixOperator.GreaterThanOrEqual:
                op = ast.Operator.GreaterThanOrEqual(lhs, rhs)
            case InfixOperator.LessThan:
                op = ast.Operator.LessThan(lhs, rhs)
            case InfixOperator.LessThanOrEqual:
                op = ast.Operator.LessThanOrEqual(lhs, rhs)
            case InfixOperator.Like:
                op = ast.Operator.Like(lhs, rhs)
            case InfixOperator.Multiply:
                op = ast.Operator.Multiply(lhs, rhs)
            case InfixOperator.NotEqual:
                op = ast.Operator.NotEqual(lhs, rhs)
            case InfixOperator.Or:
                op = ast.Operator.Or(lhs, rhs)
            case InfixOperator.Remainder:
                op = ast.Operator.Remainder(lhs, rhs)
            case InfixOperator.Subtract:
                op = ast.Operator.Subtract(lhs, rhs)
        return ast.Expression.from_operator(op)


class PostfixOperator(ABC):
    """Postfix operators."""

    Factorial: ClassVar[type["Factorial"]]
    Is: ClassVar[type["Is"]]
    IsNot: ClassVar[type["IsNot"]]

    def precedence(self) -> Precedence:
        """The operator precedence."""
        match self:
            case Is() | IsNot():
                return 4
            case Factorial():
                return 9
            case _:
                raise AssertionError(f"unknown operator {self!r}")

    def into_expression(self, lhs: ast.Expression) -> ast.Expression:
        """Builds an AST expression for the operator."""
        match self:
            case Factorial():
                return ast.Expression.from_operator(ast.Operator.Factorial(lhs))
            case Is(v):
                return ast.Expression.from_operator(ast.Operator.Is(lhs, v))
            case IsNot(v):
                is_ = ast.Expression.from_operator(ast.Operator.Is(lhs, v))
                return ast.Expression.from_operator(ast.Operator.Not(is_))
            case _:
                raise AssertionError(f"unknown operator {self!r}")


@dataclass(frozen=True)
class Factorial(PostfixOperator):
    """a!"""


@dataclass(frozen=True)
class Is(PostfixOperator):
    """a IS NULL | NAN"""

    literal: ast.Literal


@dataclass(frozen=True)
class IsNot(PostfixOperator):
    """a IS NOT NULL | NAN"""

    literal: ast.Literal


PostfixOperator.Factorial = Factorial
PostfixOperator.Is = Is
PostfixOperator.IsNot = IsNot
