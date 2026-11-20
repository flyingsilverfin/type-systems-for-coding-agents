"""Range bounds, standing in for Rust's `std::ops::Bound`.

Rust's `impl RangeBounds<Vec<u8>>` parameters are ported as a
`(start, end)` tuple of bounds, see `Range`.
"""

from abc import ABC
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

T = TypeVar("T")


class Bound(ABC, Generic[T]):
    """An endpoint of a range of keys."""

    Included: ClassVar[type["Included"]]
    Excluded: ClassVar[type["Excluded"]]
    Unbounded: ClassVar[type["Unbounded"]]


@dataclass(frozen=True)
class Included(Bound[T]):
    """An inclusive bound."""

    value: T

    def fmt_debug(self, pretty: bool = False) -> str:
        from toydb.fmt import debug_tuple

        return debug_tuple("Included", [self.value], pretty)


@dataclass(frozen=True)
class Excluded(Bound[T]):
    """An exclusive bound."""

    value: T

    def fmt_debug(self, pretty: bool = False) -> str:
        from toydb.fmt import debug_tuple

        return debug_tuple("Excluded", [self.value], pretty)


@dataclass(frozen=True)
class Unbounded(Bound[T]):
    """An infinite endpoint. Indicates that there is no bound in this direction."""


Bound.Included = Included
Bound.Excluded = Excluded
Bound.Unbounded = Unbounded

type Range[T] = tuple[Bound[T], Bound[T]]
"""A `(start, end)` range, like Rust's `(Bound<T>, Bound<T>)`."""
