"""Build modified "3.13" driver variants (Rust crate + Python package) from the pristine 3.12 copies.

Breaks:  get  = ConceptRow.get errors on missing value, new try_get
         int  = try_get_integer renamed try_get_int
         date = try_get_date returns a driver Date type
usage: make_drivers.py   -> /home/user/agentbench/drivers/{full,get,date}/{rust,python}
"""
from __future__ import annotations

import glob
import shutil
import subprocess
from pathlib import Path

OUT = Path("/home/user/agentbench/drivers")
RS = Path(glob.glob(str(Path.home() / ".cargo/registry/src/*/typedb-driver-3.12.0"))[0])
PY = Path.home() / ".local/lib/python3.12/site-packages/typedb"
VARIANTS = {"full": {"get", "int", "date"}, "get": {"get"}, "date": {"date"}}


def sub(path: Path, old: str, new: str, count: int = 1):
    s = path.read_text()
    assert s.count(old) == count, (str(path), old[:80], s.count(old))
    path.write_text(s.replace(old, new))


RS_DATE = '''
/// A calendar date value, as returned by [`crate::concept::Concept::try_get_date`] since 3.13.
///
/// `Date` is a driver-owned type; convert it with [`Date::to_naive_date`] when you need `chrono`
/// functionality such as comparison or arithmetic.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Date {
    inner: NaiveDate,
}

impl Date {
    /// Creates a `Date` from a `chrono::NaiveDate`.
    pub fn from_naive_date(date: NaiveDate) -> Self {
        Self { inner: date }
    }

    /// Converts this `Date` into a `chrono::NaiveDate`.
    pub fn to_naive_date(&self) -> NaiveDate {
        self.inner
    }

    /// The year of this date.
    pub fn year(&self) -> i32 {
        chrono::Datelike::year(&self.inner)
    }

    /// The month of this date, 1 to 12.
    pub fn month(&self) -> u32 {
        chrono::Datelike::month(&self.inner)
    }

    /// The day of the month of this date, 1 to 31.
    pub fn day(&self) -> u32 {
        chrono::Datelike::day(&self.inner)
    }
}

impl fmt::Display for Date {
    /// Formats the date as ISO 8601 (`YYYY-MM-DD`).
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.inner.format("%Y-%m-%d"))
    }
}
'''

RS_NEW_GET = '''    /// Retrieves the concept for a given column name. Since 3.13 this fails with
    /// `ConceptError::MissingRowValue` if the column exists but has no value in this row
    /// (for example, a variable bound inside a `try { ... };` block that did not match).
    /// Use [`Self::try_get`] for optional columns.
    ///
    /// # Arguments
    ///
    /// * `column_name` — The variable name in the row to retrieve
    ///
    /// # Examples
    ///
    /// ```rust
    /// concept_row.get(var_name)?
    /// ```
    pub fn get(&self, column_name: &str) -> Result<&Concept> {
        self.try_get(column_name)?
            .ok_or_else(|| ConceptError::MissingRowValue { variable: column_name.to_string() }.into())
    }

'''

CHANGELOG = {
    ("rust", "get"): """## `ConceptRow::get` no longer returns an `Option`

`ConceptRow::get(column)` now returns `Result<&Concept>` and fails with `ConceptError::MissingRowValue` when the
column exists but has no value in this row (for example, a variable bound inside a `try { ... };` block that did
not match). Previously it returned `Result<Option<&Concept>>`.

The previous behaviour is available as `ConceptRow::try_get(column) -> Result<Option<&Concept>>`. Use it for
every optional column.
""",
    ("rust", "int"): """## `Concept::try_get_integer` renamed to `Concept::try_get_int`

Same signature (`Option<i64>`), new name, for consistency with the other drivers.
""",
    ("rust", "date"): """## `Concept::try_get_date` returns a driver `Date`

`Concept::try_get_date()` now returns `Option<typedb_driver::concept::Date>` instead of
`Option<chrono::NaiveDate>`. `Date` implements `Display` (ISO 8601, `YYYY-MM-DD`), `PartialEq`, `Eq`, `Hash`,
and has `year()`, `month()`, `day()` and `to_naive_date()`. It deliberately does not implement ordering or
arithmetic: convert with `to_naive_date()` for those.
""",
    ("python", "get"): """## `ConceptRow.get` no longer returns `None`

`ConceptRow.get(column)` now returns a `Concept` and raises `TypeDBDriverException` (`MISSING_ROW_VALUE`) when
the column exists but has no value in this row (for example, a variable bound inside a `try { ... };` block
that did not match). Previously it returned `None` in that case.

The previous behaviour is available as `ConceptRow.try_get(column) -> Optional[Concept]`. Use it for every
optional column.
""",
    ("python", "int"): """## `Concept.try_get_integer` renamed to `Concept.try_get_int`

Same return type (`Optional[int]`), new name, for consistency with the other drivers.
""",
    ("python", "date"): """## `Concept.try_get_date` returns a driver `Date`

`Concept.try_get_date()` now returns `Optional[typedb.driver.Date]` instead of `Optional[datetime.date]`.
`Date` has `year`, `month`, `day`, `str()` (ISO 8601, `YYYY-MM-DD`), equality and hashing, and `to_date()`.
It deliberately does not support ordering or arithmetic: convert with `to_date()` for those.
""",
}


def rust(dst: Path, breaks: set[str]):
    shutil.copytree(RS, dst)
    sub(dst / "Cargo.toml", 'version = "3.12.0"', 'version = "3.13.0"')
    lib = dst / "src/lib.rs"
    sub(lib, "#![deny(elided_lifetimes_in_paths)]", "#![allow(warnings)]\n#![deny(elided_lifetimes_in_paths)]")
    if "get" in breaks:
        sub(dst / "src/common/error.rs", '''        3: "Could not parse the value '{unparsed}' as decimal: {reason}.",
''', '''        3: "Could not parse the value '{unparsed}' as decimal: {reason}.",
    MissingRowValue { variable: String } =
        4: "The column '{variable}' has no value in this row. Use `ConceptRow::try_get` for optional columns.",
''')
        row = dst / "src/answer/concept_row.rs"
        sub(row, '''    /// concept_row.get(var_name)
    /// ```
    pub fn get(&self, column_name: &str) -> Result<Option<&Concept>> {''', '''    /// concept_row.try_get(var_name)
    /// ```
    pub fn try_get(&self, column_name: &str) -> Result<Option<&Concept>> {''')
        sub(row, "    /// Retrieves a concept for a given column index.", RS_NEW_GET + "    /// Retrieves a concept for a given column index.")
    if "int" in breaks:
        sub(dst / "src/concept/mod.rs", "pub fn try_get_integer(&self) -> Option<i64> {", "pub fn try_get_int(&self) -> Option<i64> {")
    if "date" in breaks:
        m = dst / "src/concept/mod.rs"
        sub(m, "pub fn try_get_date(&self) -> Option<NaiveDate> {", "pub fn try_get_date(&self) -> Option<Date> {")
        sub(m, "        self.try_get_value().and_then(|value| value.get_date())",
            "        self.try_get_value().and_then(|value| value.get_date()).map(Date::from_naive_date)")
        sub(m, "    value::{Value, ValueType},", "    value::{Date, Value, ValueType},")
        v = dst / "src/concept/value.rs"
        v.write_text(v.read_text() + RS_DATE)
    (dst / "CHANGELOG.md").write_text("# TypeDB Rust Driver 3.13.0 — breaking changes\n\n" +
                                      "\n".join(CHANGELOG[("rust", b)] for b in ("get", "int", "date") if b in breaks))


PY_DATE = '''from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date


@dataclass(frozen=True)
class Date:
    """
    A calendar date value, as returned by ``Concept.try_get_date()`` since 3.13.

    ``Date`` is a driver-owned type. ``str(d)`` is ISO 8601 (``YYYY-MM-DD``) and equality/hashing are supported,
    but it deliberately does not support ordering or arithmetic: convert with ``to_date()`` for those.
    """
    year: int
    month: int
    day: int

    @staticmethod
    def from_date(value: _date) -> "Date":
        return Date(value.year, value.month, value.day)

    def to_date(self) -> _date:
        return _date(self.year, self.month, self.day)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"
'''


def python(dst: Path, breaks: set[str]):
    pkg = dst / "typedb"
    shutil.copytree(PY, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    if "get" in breaks:
        sub(pkg / "common/exception.py", "UNSUPPORTED_VALUE_CONVERSION = ConceptErrorMessage(4,",
            "MISSING_ROW_VALUE = ConceptErrorMessage(5, \"The column '%s' has no value in this row. Use ConceptRow.try_get() for optional columns.\")\nUNSUPPORTED_VALUE_CONVERSION = ConceptErrorMessage(4,")
        row = pkg / "concept/answer/concept_row.py"
        sub(row, '''    def get(self, column_name: str) -> Optional[Concept]:
        require_non_null(column_name, "column_name")''', '''    def get(self, column_name: str) -> Concept:
        concept = self.try_get(column_name)
        if concept is None:
            raise TypeDBDriverException(MISSING_ROW_VALUE, column_name)
        return concept

    def try_get(self, column_name: str) -> Optional[Concept]:
        require_non_null(column_name, "column_name")''')
        sub(row, "from typedb.common.exception import TypeDBDriverException, ILLEGAL_STATE, NULL_NATIVE_OBJECT",
            "from typedb.common.exception import TypeDBDriverException, ILLEGAL_STATE, NULL_NATIVE_OBJECT, MISSING_ROW_VALUE")
        api = pkg / "api/answer/concept_row.py"
        sub(api, '''    def get(self, column_name: str) -> Optional[Concept]:
        """
        Retrieves a concept for a given column name (variable). Returns ``None`` if the variable has an empty answer.
        Throws an exception if the variable is not present.''', '''    def get(self, column_name: str) -> Concept:
        """
        Retrieves the concept for a given column name (variable).
        Since 3.13, raises ``TypeDBDriverException`` (MISSING_ROW_VALUE) if the column has no value in this row,
        e.g. a variable bound inside a ``try { ... };`` block that did not match. Use ``try_get`` for optional columns.
        Throws an exception if the variable is not present.''')
        sub(api, "    @abstractmethod\n    def get_index(", '''    @abstractmethod
    def try_get(self, column_name: str) -> Optional[Concept]:
        """
        Retrieves a concept for a given column name (variable). Returns ``None`` if the variable has an empty answer.
        Throws an exception if the variable is not present.

        :param column_name: The string representation of a variable (column name from ``column_names``)

        Examples
        --------
        ::

          concept_row.try_get(column_name)
        """
        pass

    @abstractmethod
    def get_index(''')
    if "int" in breaks:
        for f in ("concept/value/value.py", "concept/concept.py", "api/concept/concept.py"):
            p = pkg / f
            p.write_text(p.read_text().replace("try_get_integer", "try_get_int"))
    if "date" in breaks:
        (pkg / "common/date.py").write_text(PY_DATE)
        c = pkg / "concept/concept.py"
        sub(c, '''    def try_get_date(self) -> Optional[date]:
        if self.is_type() or not self.is_date():
            return None
        return Datetime.utcfromtimestamp(concept_get_date_as_seconds(self.native_object), 0).date''', '''    def try_get_date(self) -> Optional[Date]:
        if self.is_type() or not self.is_date():
            return None
        return Date.from_date(Datetime.utcfromtimestamp(concept_get_date_as_seconds(self.native_object), 0).date)''')
        sub(c, "from typedb.common.datetime import Datetime\n", "from typedb.common.date import Date\nfrom typedb.common.datetime import Datetime\n")
        a = pkg / "api/concept/concept.py"
        sub(a, '''    def try_get_date(self) -> Optional[date]:
        """
        Returns a timezone naive ``date`` value of this ``Concept``.''', '''    def try_get_date(self) -> Optional[Date]:
        """
        Returns the date value of this ``Concept`` as a driver ``typedb.driver.Date`` (since 3.13; previously a
        ``datetime.date``). ``Date`` supports ``str()`` (ISO 8601) and equality, but not ordering or arithmetic:
        use ``Date.to_date()`` to get a ``datetime.date``.''')
        s = a.read_text()
        a.write_text(s.replace("from __future__ import annotations\n", "from __future__ import annotations\n\nfrom typedb.common.date import Date\n", 1))
        sub(pkg / "concept/value/value.py", "    def get_date(self) -> date:", '    def get_date(self) -> "Date":')
        d = pkg / "driver.py"
        sub(d, "from typedb.api.answer.concept_document_iterator import *",
            "from typedb.common.date import Date  # noqa # pylint: disable=unused-import\nfrom typedb.api.answer.concept_document_iterator import *")
    (dst / "CHANGELOG.md").write_text("# TypeDB Python Driver 3.13.0 — breaking changes\n\n" +
                                      "\n".join(CHANGELOG[("python", b)] for b in ("get", "int", "date") if b in breaks))


if __name__ == "__main__":
    for name, breaks in VARIANTS.items():
        base = OUT / name
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True)
        rust(base / "rust", breaks)
        python(base / "python", breaks)
        print(name, sorted(breaks))
    shutil.rmtree(OUT / "rust-3.13", ignore_errors=True)
    shutil.rmtree(OUT / "python-3.13", ignore_errors=True)
