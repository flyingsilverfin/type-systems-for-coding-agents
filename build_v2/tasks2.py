"""v2 task definitions and case lists."""
from __future__ import annotations

V2_VISIBLE = [
    ["books"], ["book", "9787717592285"], ["members"], ["member", "M001"], ["history", "M003"],
    ["overdue", "2024-05-01"], ["fees"], ["stats"],
]

V2_HIDDEN = [
    ["books"], ["book", "9781000000001"], ["book", "9781000000002"], ["book", "0000"],
    ["members"], ["member", "M001"], ["member", "M005"], ["member", "M900"], ["member", "M003"], ["member", "NOPE"],
    ["history", "M003"], ["history", "M016"], ["history", "M900"],
    ["overdue", "2024-05-01"], ["overdue", "2024-07-15"],
    ["due-soon", "2024-04-20", "14"], ["due-soon", "2024-06-01", "0"],
    ["returned-late"], ["lost"], ["fees"], ["member-fees", "M004"], ["member-fees", "M900"],
    ["stats"], ["decades"], ["genre", "fiction"], ["genre", "(none)"], ["never-borrowed"],
    ["available", "9781000000001"], ["available", "9781000000003"], ["top", "5"], ["top", "x"],
    ["search", "ATLAS"], ["search", "with, commas"], ["monthly"], ["inactive", "2024-05-01"],
    ["contact", "M001"], ["contact", "M005"], ["contact", "M900"], ["contact", "M009"], ["contact", "M002"], ["contact", "XX"],
]
