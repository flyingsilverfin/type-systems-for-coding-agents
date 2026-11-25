# Addendum: tests that pin the keywords or data types

This addendum replaces the paragraph "If the code base has unit tests that pin the exact number or set of reserved
keywords or of data types (and their encodings), ..." in the section "`DATE` is a reserved word" of the task
description. Everything else in the task description stays as it is.

If the code base has tests — unit tests or golden scripts — that pin the exact number or set of reserved keywords or
of data types (and their encodings), update them for the one new keyword and the one new data type; those are the
only existing test expectations that may change.
