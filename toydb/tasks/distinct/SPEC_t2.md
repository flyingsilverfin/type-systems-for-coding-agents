# Addendum: DISTINCT in query plans

This addendum replaces the section "Query plans" at the end of the task description. Everything else in the
task description stays as it is.

`EXPLAIN` shows DISTINCT as follows. The plans of statements without the word DISTINCT don't change.

* **DISTINCT aggregates** are shown like the same aggregate without DISTINCT, with `distinct` and one space
  before the argument, wherever plans show aggregates today. For example, the `Aggregate` line of
  `SELECT COUNT(DISTINCT g), COUNT(g) FROM t` is `Aggregate: count(distinct t.g), count(t.g)`.
* **SELECT DISTINCT** is shown as a plan node of its own: a line that consists of the word `Distinct` only. Its
  position is specified for SELECT DISTINCT statements whose ORDER BY and HAVING clauses (if any) refer only to
  result columns by name and to constants, and whose plan without the DISTINCT after SELECT is not just
  `Nothing`: the plan is the plan of the same statement without the DISTINCT after SELECT, with the `Distinct`
  node inserted directly underneath the `Order` node; if there is no ORDER BY, directly underneath the lower of
  the `Offset` and `Limit` nodes; if there are none of these, at the top of the plan. The rest of the plan is
  unchanged. For other SELECT DISTINCT statements, the plan display is not specified.

For example, for `t (id INT PRIMARY KEY, g STRING, v INT)`:

```
> EXPLAIN SELECT DISTINCT g, v FROM t WHERE id > 1 ORDER BY g DESC LIMIT 2
---
Limit: 2
└─ Order: t.g desc
   └─ Distinct
      └─ Projection: t.g, t.v
         └─ Scan: t (t.id > 1)

> EXPLAIN SELECT DISTINCT * FROM t
---
Distinct
└─ Scan: t
```
