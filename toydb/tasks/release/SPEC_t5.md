# Addendum: tests that pin the functions

This addendum extends the paragraph "If the code base has unit tests that pin the exact internal form of parsed
statements or of expressions ..." at the start of the task description. Everything else in the task description stays
as it is.

If the code base has tests — unit tests or golden scripts — that pin the exact set of functions and aggregate
functions that expressions can call, update them for the four new functions and the two new aggregate functions.
Together with the tests named in that paragraph, those are the only existing test expectations that may change.
