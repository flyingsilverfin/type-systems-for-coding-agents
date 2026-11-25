"""An ordered map, an ordered set and double-ended iterators, standing in for
Rust's `std::collections::{BTreeMap, BTreeSet}` and `DoubleEndedIterator`,
which the storage and SQL engines are built on. There is no Rust module corresponding to this one.
"""

from abc import ABC, abstractmethod
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator
from typing import Any, Generic, TypeVar

from toydb.error import Panic
from toydb.ops import Bound, Range

T = TypeVar("T")
K = TypeVar("K", bound=Any)  # must be orderable
V = TypeVar("V")


class DoubleEndedIterator(Iterator[T], ABC):
    """An iterator that can also yield items from the back, like Rust's
    `DoubleEndedIterator`. Both ends work on the same range, and the iterator
    is exhausted when they meet."""

    @abstractmethod
    def __next__(self) -> T:
        """Returns the next item from the front."""

    @abstractmethod
    def next_back(self) -> T:
        """Returns the next item from the back, or raises `StopIteration` when
        the iterator is exhausted."""

    def rev(self) -> "Rev[T]":
        """Reverses the iterator's direction."""
        return Rev(self)


class Rev(Iterator[T]):
    """A double-ended iterator with the direction inverted, like Rust's
    `std::iter::Rev`."""

    def __init__(self, iter: DoubleEndedIterator[T]) -> None:
        self.iter = iter

    def __next__(self) -> T:
        return self.iter.next_back()


class BTreeMap(Generic[K, V]):
    """An ordered map. Keys are kept in a sorted list, which makes lookups
    and range scans cheap but inserts and removals linear in the map size.

    Like the Rust `BTreeMap`, keys are only compared by their ordering (`<`),
    never by `==` or `hash()`. This matters for keys whose equality differs
    from their ordering, e.g. SQL float values (where `-0.0 == 0.0`, but
    `-0.0 < 0.0`)."""

    def __init__(self) -> None:
        self._keys: list[K] = []
        self._values: list[V] = []

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key: K) -> bool:
        return self._find(key) is not None

    def __iter__(self) -> Iterator[tuple[K, V]]:
        return self.iter()

    def _find(self, key: K) -> int | None:
        """Returns the index of a key, if it exists."""
        index = bisect_left(self._keys, key)
        if index < len(self._keys) and not key < self._keys[index]:
            return index
        return None

    def is_empty(self) -> bool:
        """Returns true if the map has no entries."""
        return not self._keys

    def get(self, key: K) -> V | None:
        """Returns the value for a key, if it exists."""
        index = self._find(key)
        return None if index is None else self._values[index]

    def insert(self, key: K, value: V) -> V | None:
        """Inserts a key/value pair, returning the old value if any."""
        index = bisect_left(self._keys, key)
        if index < len(self._keys) and not key < self._keys[index]:
            old = self._values[index]
            self._values[index] = value
            return old
        self._keys.insert(index, key)
        self._values.insert(index, value)
        return None

    def remove(self, key: K) -> V | None:
        """Removes a key, returning its value if it existed."""
        index = self._find(key)
        if index is None:
            return None
        del self._keys[index]
        return self._values.pop(index)

    def iter(self) -> "MapRange[K, V]":
        """Iterates over all key/value pairs in key order."""
        return MapRange(self._keys, self._values, 0, len(self._keys))

    def range(self, range: Range[K]) -> "MapRange[K, V]":
        """Iterates over the key/value pairs in a key range, in key order.
        Panics if the range start is after the range end. The map must not be
        modified while iterating."""
        start, end = range
        match (start, end):
            case (Bound.Excluded(s), Bound.Excluded(e)) if s == e:
                raise Panic("range start and end are equal and excluded in BTreeMap")
            case (
                (Bound.Included(s) | Bound.Excluded(s)),
                (Bound.Included(e) | Bound.Excluded(e)),
            ) if s > e:
                raise Panic("range start is greater than range end in BTreeMap")
            case _:
                pass
        match start:
            case Bound.Included(key):
                lo = bisect_left(self._keys, key)
            case Bound.Excluded(key):
                lo = bisect_right(self._keys, key)
            case _:
                lo = 0
        match end:
            case Bound.Included(key):
                hi = bisect_right(self._keys, key)
            case Bound.Excluded(key):
                hi = bisect_left(self._keys, key)
            case _:
                hi = len(self._keys)
        return MapRange(self._keys, self._values, lo, hi)


class MapRange(DoubleEndedIterator[tuple[K, V]]):
    """An iterator over a range of a `BTreeMap`, like Rust's
    `std::collections::btree_map::Range`."""

    def __init__(self, keys: list[K], values: list[V], lo: int, hi: int) -> None:
        self._keys = keys
        self._values = values
        self._lo = lo
        self._hi = hi

    def __next__(self) -> tuple[K, V]:
        if self._lo >= self._hi:
            raise StopIteration
        index = self._lo
        self._lo += 1
        return self._keys[index], self._values[index]

    def next_back(self) -> tuple[K, V]:
        if self._lo >= self._hi:
            raise StopIteration
        self._hi -= 1
        return self._keys[self._hi], self._values[self._hi]


class BTreeSet(Generic[K]):
    """An ordered set, kept as a sorted list. Like the Rust `BTreeSet`, members
    are only compared by their ordering (`<`), never by `==` or `hash()`."""

    def __init__(self, items: Iterable[K] = ()) -> None:
        self._items: list[K] = []
        for item in items:
            self.insert(item)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, item: K) -> bool:
        return self._find(item) is not None

    def __iter__(self) -> Iterator[K]:
        """Iterates over the members in order."""
        return iter(list(self._items))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, BTreeSet) and self._items == other._items

    def __repr__(self) -> str:
        return f"BTreeSet({self._items!r})"

    def _find(self, item: K) -> int | None:
        """Returns the index of an item, if it exists."""
        index = bisect_left(self._items, item)
        if index < len(self._items) and not item < self._items[index]:
            return index
        return None

    def is_empty(self) -> bool:
        """Returns true if the set has no members."""
        return not self._items

    def first(self) -> K | None:
        """Returns the smallest member, if any."""
        return self._items[0] if self._items else None

    def insert(self, item: K) -> bool:
        """Adds an item, returning false if it already existed."""
        index = bisect_left(self._items, item)
        if index < len(self._items) and not item < self._items[index]:
            return False
        self._items.insert(index, item)
        return True

    def remove(self, item: K) -> bool:
        """Removes an item, returning whether it existed."""
        index = self._find(item)
        if index is None:
            return False
        del self._items[index]
        return True

    def extend(self, items: Iterable[K]) -> None:
        """Adds all of the given items."""
        for item in items:
            self.insert(item)
