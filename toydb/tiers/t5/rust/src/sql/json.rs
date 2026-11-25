//! JSON output: JSON encodings of SQL values, statement results and query
//! plans, each written as one line of compact JSON (see `encoding::json`). Used
//! by the `toysql` client (`!format json`) and the test runners (`[json]`), and
//! for values by `EXPORT ... AS JSON`.
//!
//! * Values are written natively where JSON can represent them (NULL, booleans,
//!   integers, finite floats as displayed in query results, and strings). Other
//!   values (NaN and infinite floats) use a typed form with the data type and the
//!   value as displayed: {"type":"FLOAT","value":"NaN"}.
//!
//! * Statement results are objects with a "result" member naming the kind of
//!   result, followed by its contents, e.g. {"result":"insert","count":1}.
//!
//! * Plans have the structure of the plan tree, and contain exactly what the plan
//!   display shows. Nodes are objects whose first member is "node" (the node name
//!   in snake case), followed by the node's parameters and then its inputs.
//!   Expressions are constants {"constant":<value>}, column references
//!   {"column":<index>,"label":<label or null>} with labels resolved as by the
//!   plan display, or operators and functions {"op":"_ + _","args":[...]}, where
//!   "op" is the expression's plan display with each operand replaced by _.

use crate::encoding::json::Json;
use crate::sql::execution::StatementResult;
use crate::sql::parser::ast;
use crate::sql::planner::{Aggregate, Direction, Node, Plan, invert_remap};
use crate::sql::types::{Expression, Label, Value};

/// Encodes a value as JSON. Values without a native JSON form (NaN and
/// infinite floats) use the typed form {"type":<datatype>,"value":<display>}.
pub fn value(value: &Value) -> Json {
    match value {
        Value::Null => Json::Null,
        Value::Boolean(b) => Json::Bool(*b),
        Value::Integer(i) => (*i).into(),
        Value::Float(f) if f.is_finite() => Json::Number(value.to_string()),
        Value::Float(_) => typed(value),
        Value::String(s) => s.into(),
    }
}

/// Encodes a value in typed form, for values without a native JSON form.
fn typed(value: &Value) -> Json {
    let datatype = value.datatype().expect("NULL has no typed form").to_string();
    Json::object([("type", datatype.into()), ("value", value.to_string().into())])
}

/// Encodes a statement result as JSON.
pub fn result(result: &StatementResult) -> Json {
    use StatementResult::*;

    let kind = |kind: &str| ("result", kind.into());
    match result {
        Begin(state) => Json::object([
            kind("begin"),
            ("version", state.version.into()),
            ("read_only", state.read_only.into()),
        ]),
        Commit { version } => Json::object([kind("commit"), ("version", (*version).into())]),
        Rollback { version } => Json::object([kind("rollback"), ("version", (*version).into())]),
        Explain(explained) => Json::object([kind("explain"), ("plan", plan(explained))]),
        CreateTable { name } => Json::object([kind("create_table"), ("table", name.into())]),
        DropTable { name, existed } => Json::object([
            kind("drop_table"),
            ("table", name.into()),
            ("existed", (*existed).into()),
        ]),
        Delete { count } => Json::object([kind("delete"), ("count", (*count).into())]),
        Insert { count } => Json::object([kind("insert"), ("count", (*count).into())]),
        Update { count } => Json::object([kind("update"), ("count", (*count).into())]),
        Select { columns, rows } => Json::object([
            kind("select"),
            ("columns", Json::array(columns.iter().map(label))),
            ("rows", Json::array(rows.iter().map(|row| Json::array(row.iter().map(value))))),
        ]),
        Analyze { tables } => Json::object([kind("analyze"), ("tables", Json::array(tables))]),
        Export { format, lines } => {
            let format = match format {
                ast::ExportFormat::Csv => "csv",
                ast::ExportFormat::Json => "json",
                ast::ExportFormat::Sql => "sql",
            };
            Json::object([kind("export"), ("format", format.into()), ("lines", Json::array(lines))])
        }
        Format { lines } => Json::object([kind("format"), ("lines", Json::array(lines))]),
        CreateView { name } => Json::object([kind("create_view"), ("view", name.into())]),
        DropView { name, existed } => Json::object([
            kind("drop_view"),
            ("view", name.into()),
            ("existed", (*existed).into()),
        ]),
        Prepare { name } => Json::object([kind("prepare"), ("name", name.into())]),
        Deallocate { name } => Json::object([kind("deallocate"), ("name", name.into())]),
        CreateUser { name } => Json::object([kind("create_user"), ("user", name.into())]),
        DropUser { name, existed } => Json::object([
            kind("drop_user"),
            ("user", name.into()),
            ("existed", (*existed).into()),
        ]),
        Grant { table: Some(table) } => Json::object([kind("grant"), ("table", table.into())]),
        Grant { table: None } => Json::object([kind("grant")]),
        Revoke { table: Some(table) } => Json::object([kind("revoke"), ("table", table.into())]),
        Revoke { table: None } => Json::object([kind("revoke")]),
        SetRole { user } => Json::object([kind("set_role"), ("user", user.into())]),
        ResetRole { user } => Json::object([kind("reset_role"), ("user", user.into())]),
        CreatePolicy { table, name } => Json::object([
            kind("create_policy"),
            ("table", table.into()),
            ("policy", name.into()),
        ]),
        DropPolicy { table, name, existed } => Json::object([
            kind("drop_policy"),
            ("table", table.into()),
            ("policy", name.into()),
            ("existed", (*existed).into()),
        ]),
        CreateMaterializedView { name, count } => Json::object([
            kind("create_materialized_view"),
            ("view", name.into()),
            ("count", (*count).into()),
        ]),
        RefreshMaterializedView { name, count } => Json::object([
            kind("refresh_materialized_view"),
            ("view", name.into()),
            ("count", (*count).into()),
        ]),
        DropMaterializedView { name, existed } => Json::object([
            kind("drop_materialized_view"),
            ("view", name.into()),
            ("existed", (*existed).into()),
        ]),
    }
}

/// Encodes a column label: its display, or null if the column has no label.
fn label(label: &Label) -> Json {
    match label {
        Label::None => Json::Null,
        label => label.to_string().into(),
    }
}

/// Encodes an execution plan (as shown by EXPLAIN).
pub fn plan(plan: &Plan) -> Json {
    let statement = |name: &str| ("statement", name.into());
    match plan {
        Plan::CreateTable { schema, .. } => {
            Json::object([statement("create_table"), ("table", (&schema.name).into())])
        }
        Plan::DropTable { name, .. } => {
            Json::object([statement("drop_table"), ("table", name.into())])
        }
        Plan::Delete { table, source, .. } => Json::object([
            statement("delete"),
            ("table", table.into()),
            ("source", node(source)),
        ]),
        Plan::Insert { table, source, .. } => Json::object([
            statement("insert"),
            ("table", (&table.name).into()),
            ("source", node(source)),
        ]),
        Plan::Update { table, source, expressions, .. } => {
            let set = expressions.iter().map(|(i, expr)| {
                Json::object([
                    ("column", (&table.columns[*i].name).into()),
                    ("expr", expression(expr, source)),
                ])
            });
            Json::object([
                statement("update"),
                ("table", (&table.name).into()),
                ("set", Json::array(set)),
                ("source", node(source)),
            ])
        }
        Plan::Select(root) => Json::object([statement("select"), ("root", node(root))]),
    }
}

/// Encodes a plan node and its inputs. Expressions resolve column labels
/// against the same nodes as in the plan display.
pub fn node(node: &Node) -> Json {
    let name = |name: &str| ("node", name.into());
    let input = |name, input: &Node| (name, self::node(input));
    match node {
        Node::Aggregate { source, group_by, aggregates } => Json::object([
            name("aggregate"),
            ("group_by", Json::array(group_by.iter().map(|expr| expression(expr, source)))),
            ("aggregates", Json::array(aggregates.iter().map(|agg| aggregate(agg, source)))),
            input("source", source),
        ]),
        Node::Filter { source, predicate } => Json::object([
            name("filter"),
            ("predicate", expression(predicate, source)),
            input("source", source),
        ]),
        Node::HashJoin { left, left_column, right, right_column, outer } => Json::object([
            name("hash_join"),
            ("join", join(*outer)),
            ("left_column", column(left, *left_column)),
            ("right_column", column(right, *right_column)),
            input("left", left),
            input("right", right),
        ]),
        Node::IndexLookup { table, column, values, alias } => Json::object([
            name("index_lookup"),
            ("table", (&table.name).into()),
            ("alias", alias.as_ref().into()),
            ("column", (&table.columns[*column].name).into()),
            ("values", Json::array(values.iter().map(value))),
        ]),
        Node::KeyLookup { table, keys, alias } => Json::object([
            name("key_lookup"),
            ("table", (&table.name).into()),
            ("alias", alias.as_ref().into()),
            ("keys", Json::array(keys.iter().map(value))),
        ]),
        Node::Limit { source, limit } => {
            Json::object([name("limit"), ("limit", (*limit).into()), input("source", source)])
        }
        Node::MaterializedView { view, alias, .. } => Json::object([
            name("materialized_view"),
            ("view", view.into()),
            ("alias", alias.as_ref().into()),
        ]),
        Node::NestedLoopJoin { left, right, predicate, outer } => Json::object([
            name("nested_loop_join"),
            ("join", join(*outer)),
            ("predicate", predicate.as_ref().map(|p| expression(p, node)).into()),
            input("left", left),
            input("right", right),
        ]),
        Node::Nothing { .. } => Json::object([name("nothing")]),
        Node::Offset { source, offset } => {
            Json::object([name("offset"), ("offset", (*offset).into()), input("source", source)])
        }
        Node::Order { source, key } => {
            let key = key.iter().map(|(expr, direction)| {
                let direction = match direction {
                    Direction::Ascending => "asc",
                    Direction::Descending => "desc",
                };
                Json::object([("expr", expression(expr, source)), ("direction", direction.into())])
            });
            Json::object([name("order"), ("key", Json::array(key)), input("source", source)])
        }
        Node::Projection { source, expressions, aliases } => {
            let expressions = expressions.iter().enumerate().map(|(i, expr)| {
                let alias = match aliases.get(i) {
                    Some(Label::None) | None => Json::Null,
                    Some(alias) => alias.to_string().into(),
                };
                Json::object([("expr", expression(expr, source)), ("alias", alias)])
            });
            Json::object([
                name("projection"),
                ("expressions", Json::array(expressions)),
                input("source", source),
            ])
        }
        Node::Remap { source, targets } => {
            let columns = invert_remap(targets)
                .into_iter()
                .map(|from| from.map(|from| column(source, from)).into());
            let dropped = targets
                .iter()
                .enumerate()
                .filter(|(_, target)| target.is_none())
                .map(|(i, _)| column(source, i));
            Json::object([
                name("remap"),
                ("columns", Json::Array(columns.collect())),
                ("dropped", Json::array(dropped)),
                input("source", source),
            ])
        }
        Node::Scan { table, filter, alias } => Json::object([
            name("scan"),
            ("table", (&table.name).into()),
            ("alias", alias.as_ref().into()),
            ("filter", filter.as_ref().map(|f| expression(f, node)).into()),
        ]),
        Node::SystemTable { table, alias } => Json::object([
            name("system_table"),
            ("table", (&table.name).into()),
            ("alias", alias.as_ref().into()),
        ]),
        Node::Values { rows } => {
            let rows = rows.iter().map(|row| Json::array(row.iter().map(|e| expression(e, node))));
            Json::object([name("values"), ("rows", Json::array(rows))])
        }
        Node::View { view, alias, source, .. } => Json::object([
            name("view"),
            ("view", view.into()),
            ("alias", alias.as_ref().into()),
            input("source", source),
        ]),
    }
}

/// Encodes a join type.
fn join(outer: bool) -> Json {
    match outer {
        true => "outer".into(),
        false => "inner".into(),
    }
}

/// Encodes a reference to a column of the given node, with its label as shown
/// by the plan display (or null where it shows #index).
fn column(node: &Node, index: usize) -> Json {
    Json::object([("column", index.into()), ("label", label(&node.column_label(index)))])
}

/// Encodes an expression, resolving column labels via the given node (the
/// same node the plan display uses for the expression).
pub fn expression(expr: &Expression, node: &Node) -> Json {
    use Expression::*;

    let (op, args): (&str, Vec<&Expression>) = match expr {
        Constant(constant) => return Json::object([("constant", value(constant))]),
        Column(index) => return column(node, *index),
        // A unary + is not displayed, and is written as its operand.
        Identity(expr) => return expression(expr, node),

        And(lhs, rhs) => ("_ AND _", vec![lhs, rhs]),
        Or(lhs, rhs) => ("_ OR _", vec![lhs, rhs]),
        Not(expr) => ("NOT _", vec![expr]),

        Equal(lhs, rhs) => ("_ = _", vec![lhs, rhs]),
        GreaterThan(lhs, rhs) => ("_ > _", vec![lhs, rhs]),
        LessThan(lhs, rhs) => ("_ < _", vec![lhs, rhs]),
        Is(expr, Value::Null) => ("_ IS NULL", vec![expr]),
        Is(expr, Value::Float(f)) if f.is_nan() => ("_ IS NAN", vec![expr]),
        Is(_, v) => panic!("unexpected IS value {v}"),

        Add(lhs, rhs) => ("_ + _", vec![lhs, rhs]),
        Divide(lhs, rhs) => ("_ / _", vec![lhs, rhs]),
        Exponentiate(lhs, rhs) => ("_ ^ _", vec![lhs, rhs]),
        Factorial(expr) => ("_!", vec![expr]),
        Multiply(lhs, rhs) => ("_ * _", vec![lhs, rhs]),
        Negate(expr) => ("-_", vec![expr]),
        Remainder(lhs, rhs) => ("_ % _", vec![lhs, rhs]),
        SquareRoot(expr) => ("sqrt(_)", vec![expr]),
        Subtract(lhs, rhs) => ("_ - _", vec![lhs, rhs]),

        Like(lhs, rhs) => ("_ LIKE _", vec![lhs, rhs]),
    };
    operator(op, args.into_iter(), node)
}

/// Encodes an aggregate function call, like a function call expression.
fn aggregate(aggregate: &Aggregate, node: &Node) -> Json {
    let op = match aggregate {
        Aggregate::Average(_) => "avg(_)",
        Aggregate::Count(_) => "count(_)",
        Aggregate::Max(_) => "max(_)",
        Aggregate::Min(_) => "min(_)",
        Aggregate::Sum(_) => "sum(_)",
    };
    operator(op, std::iter::once(aggregate.expr()), node)
}

/// Encodes an operator expression with the given operands.
fn operator<'a>(op: &str, args: impl Iterator<Item = &'a Expression>, node: &Node) -> Json {
    Json::object([("op", op.into()), ("args", Json::array(args.map(|arg| expression(arg, node))))])
}

#[cfg(test)]
mod tests {
    use test_case::test_case;

    use super::*;
    use crate::sql::types::{Column, DataType, Table};

    #[test_case(Value::Null => "null"; "null")]
    #[test_case(Value::Boolean(false) => "false"; "boolean")]
    #[test_case(Value::Integer(i64::MIN) => "-9223372036854775808"; "integer")]
    #[test_case(Value::Float(1.0) => "1.0"; "float")]
    #[test_case(Value::Float(-0.0) => "-0.0"; "float negative zero")]
    #[test_case(Value::Float(1e100) => "1e100"; "float exponent")]
    #[test_case(Value::Float(1.5e-7) => "1.5e-7"; "float negative exponent")]
    #[test_case(Value::Float(f64::NAN) => r#"{"type":"FLOAT","value":"NaN"}"#; "float nan")]
    #[test_case(Value::Float(f64::INFINITY) => r#"{"type":"FLOAT","value":"inf"}"#; "float infinity")]
    #[test_case(Value::Float(f64::NEG_INFINITY) => r#"{"type":"FLOAT","value":"-inf"}"#; "float negative infinity")]
    #[test_case(Value::String(r#"a"b"#.into()) => r#""a\"b""#; "string")]
    fn values(v: Value) -> String {
        value(&v).to_string()
    }

    #[test_case(StatementResult::Commit { version: 3 } => r#"{"result":"commit","version":3}"#; "commit")]
    #[test_case(StatementResult::DropTable { name: "t".into(), existed: false } => r#"{"result":"drop_table","table":"t","existed":false}"#; "drop table")]
    #[test_case(StatementResult::Select { columns: vec![Label::Qualified("t".into(), "a".into()), Label::None], rows: vec![vec![1.into(), Value::Null]] }
        => r#"{"result":"select","columns":["t.a",null],"rows":[[1,null]]}"#; "select")]
    #[test_case(StatementResult::Analyze { tables: vec!["a".into(), "B".into()] } => r#"{"result":"analyze","tables":["a","B"]}"#; "analyze")]
    #[test_case(StatementResult::Export { format: ast::ExportFormat::Csv, lines: vec!["a,b".into()] } => r#"{"result":"export","format":"csv","lines":["a,b"]}"#; "export")]
    #[test_case(StatementResult::Format { lines: vec!["SELECT 1".into(), "FROM t".into()] } => r#"{"result":"format","lines":["SELECT 1","FROM t"]}"#; "format")]
    #[test_case(StatementResult::CreateView { name: "v".into() } => r#"{"result":"create_view","view":"v"}"#; "create view")]
    #[test_case(StatementResult::DropView { name: "v".into(), existed: true } => r#"{"result":"drop_view","view":"v","existed":true}"#; "drop view")]
    #[test_case(StatementResult::Prepare { name: "p".into() } => r#"{"result":"prepare","name":"p"}"#; "prepare")]
    #[test_case(StatementResult::Deallocate { name: "p".into() } => r#"{"result":"deallocate","name":"p"}"#; "deallocate")]
    #[test_case(StatementResult::CreateUser { name: "u".into() } => r#"{"result":"create_user","user":"u"}"#; "create user")]
    #[test_case(StatementResult::DropUser { name: "u".into(), existed: false } => r#"{"result":"drop_user","user":"u","existed":false}"#; "drop user")]
    #[test_case(StatementResult::Grant { table: Some("t".into()) } => r#"{"result":"grant","table":"t"}"#; "grant privileges")]
    #[test_case(StatementResult::Grant { table: None } => r#"{"result":"grant"}"#; "grant membership")]
    #[test_case(StatementResult::Revoke { table: Some("t".into()) } => r#"{"result":"revoke","table":"t"}"#; "revoke privileges")]
    #[test_case(StatementResult::Revoke { table: None } => r#"{"result":"revoke"}"#; "revoke membership")]
    #[test_case(StatementResult::SetRole { user: "u".into() } => r#"{"result":"set_role","user":"u"}"#; "set role")]
    #[test_case(StatementResult::ResetRole { user: "admin".into() } => r#"{"result":"reset_role","user":"admin"}"#; "reset role")]
    #[test_case(StatementResult::CreatePolicy { table: "t".into(), name: "p".into() } => r#"{"result":"create_policy","table":"t","policy":"p"}"#; "create policy")]
    #[test_case(StatementResult::DropPolicy { table: "t".into(), name: "p".into(), existed: true } => r#"{"result":"drop_policy","table":"t","policy":"p","existed":true}"#; "drop policy")]
    #[test_case(StatementResult::CreateMaterializedView { name: "m".into(), count: 2 } => r#"{"result":"create_materialized_view","view":"m","count":2}"#; "create materialized view")]
    #[test_case(StatementResult::RefreshMaterializedView { name: "m".into(), count: 0 } => r#"{"result":"refresh_materialized_view","view":"m","count":0}"#; "refresh materialized view")]
    #[test_case(StatementResult::DropMaterializedView { name: "m".into(), existed: false } => r#"{"result":"drop_materialized_view","view":"m","existed":false}"#; "drop materialized view")]
    fn results(result: StatementResult) -> String {
        self::result(&result).to_string()
    }

    /// Operators are written with _ placeholders for their operands. Column
    /// labels are looked up in the given node.
    #[test]
    fn expressions() {
        let table = Table {
            name: "t".into(),
            primary_key: 0,
            columns: vec![Column {
                name: "a".into(),
                datatype: DataType::Integer,
                nullable: false,
                default: None,
                unique: true,
                index: false,
                references: None,
            }],
        };
        let scan = Node::Scan { table, filter: None, alias: Some("x".into()) };
        let one = Expression::Identity(Box::new(Expression::Constant(Value::Integer(1))));
        let subtract = Expression::Subtract(Box::new(Expression::Column(0)), Box::new(one));
        let expr = Expression::Not(Box::new(Expression::Is(Box::new(subtract), Value::Null)));
        assert_eq!(
            expression(&expr, &scan).to_string(),
            r#"{"op":"NOT _","args":[{"op":"_ IS NULL","args":[{"op":"_ - _","args":[{"column":0,"label":"x.a"},{"constant":1}]}]}]}"#
        );
        let unlabeled = Node::Values { rows: Vec::new() };
        assert_eq!(
            expression(&Expression::Column(2), &unlabeled).to_string(),
            r#"{"column":2,"label":null}"#
        );
    }
}
