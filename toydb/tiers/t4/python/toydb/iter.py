"""Clonable iterators, standing in for Rust's `std::iter` adapters.

Rust iterator adapters such as `map`, `filter_map`, `take` and `skip` are
`Clone` when the underlying iterator is, which the SQL executor relies on
(`sql.types.Rows` must be clonable, e.g. to rewind the right source of a nested
loop join). Python generators can't be cloned, so the adapters are ported as
small iterator classes with a `clone()` method. There is no Rust module
corresponding to this one.

A Rust iterator over `Result<T>` is ported as an iterator that raises `Error`
from `__next__`. Unlike a generator, these iterators can still be used after
raising: the failed item has been consumed, and the next call moves on to the
next item, just like a Rust iterator that yielded an `Err`.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from typing import Generic, Protocol, Self, TypeVar

from toydb.error import Error

T = TypeVar("T")
U = TypeVar("U")
T_co = TypeVar("T_co", covariant=True)


class Clonable(Protocol[T_co]):
    """An iterator that can be cloned in its current state, like a Rust
    `Iterator + Clone`."""

    def __iter__(self) -> Iterator[T_co]: ...

    def __next__(self) -> T_co: ...

    def clone(self) -> Self: ...


class CloneIterator(Iterator[T], ABC):
    """Base class for clonable iterators."""

    @abstractmethod
    def clone(self) -> Self:
        """Clones the iterator in its current state. The clone and the
        original are advanced independently."""


class Empty(CloneIterator[T]):
    """An iterator that yields nothing, like `std::iter::empty`."""

    def __next__(self) -> T:
        raise StopIteration

    def clone(self) -> "Empty[T]":
        return Empty()


class IntoIter(CloneIterator[T]):
    """Iterates over the items of a sequence, like `Vec::into_iter`. The
    sequence must not be modified while iterating."""

    def __init__(self, items: Sequence[T], position: int = 0) -> None:
        self.items = items
        self.position = position

    def __next__(self) -> T:
        if self.position >= len(self.items):
            raise StopIteration
        item = self.items[self.position]
        self.position += 1
        return item

    def clone(self) -> "IntoIter[T]":
        return IntoIter(self.items, self.position)


class Map(CloneIterator[U], Generic[T, U]):
    """Maps each item through a function, like `Iterator::map`."""

    def __init__(self, iter: Clonable[T], f: Callable[[T], U]) -> None:
        self.iter = iter
        self.f = f

    def __next__(self) -> U:
        return self.f(next(self.iter))

    def clone(self) -> "Map[T, U]":
        return Map(self.iter.clone(), self.f)


class FilterMap(CloneIterator[U], Generic[T, U]):
    """Maps each item through a function, and skips items for which it
    returns None, like `Iterator::filter_map`."""

    def __init__(self, iter: Clonable[T], f: Callable[[T], U | None]) -> None:
        self.iter = iter
        self.f = f

    def __next__(self) -> U:
        while True:
            if (item := self.f(next(self.iter))) is not None:
                return item

    def clone(self) -> "FilterMap[T, U]":
        return FilterMap(self.iter.clone(), self.f)


class Take(CloneIterator[T]):
    """Yields at most the first n items, like `Iterator::take`. An item that
    fails with an error counts towards the limit."""

    def __init__(self, iter: Clonable[T], n: int) -> None:
        self.iter = iter
        self.n = n

    def __next__(self) -> T:
        if self.n == 0:
            raise StopIteration
        self.n -= 1
        return next(self.iter)

    def clone(self) -> "Take[T]":
        return Take(self.iter.clone(), self.n)


class Skip(CloneIterator[T]):
    """Discards the first n items, like `Iterator::skip`. Items that fail with
    an error are discarded too, along with the error."""

    def __init__(self, iter: Clonable[T], n: int) -> None:
        self.iter = iter
        self.n = n

    def __next__(self) -> T:
        while self.n > 0:
            self.n -= 1
            try:
                next(self.iter)
            except Error:
                pass
        return next(self.iter)

    def clone(self) -> "Skip[T]":
        return Skip(self.iter.clone(), self.n)


class Peekable(CloneIterator[T]):
    """An iterator that can peek at the next item without consuming it, like
    `std::iter::Peekable`. If fetching the peeked item failed, the error is
    retained and raised again by every `peek()` until consumed by `next()`."""

    def __init__(self, iter: Clonable[T]) -> None:
        self.iter = iter
        self.peeked: tuple[T] | Error | None = None
        """The peeked item (in a tuple) or error, if any."""
        self.done = False
        """True if the peeked item was the end of the iterator."""

    def peek(self) -> T | None:
        """Returns the next item without consuming it, or None at the end of
        the iterator. Raises the item's error, if any."""
        if self.peeked is None and not self.done:
            try:
                self.peeked = (next(self.iter),)
            except StopIteration:
                self.done = True
            except Error as error:
                self.peeked = error
        match self.peeked:
            case None:
                return None
            case Error() as error:
                raise error
            case (item,):
                return item

    def __next__(self) -> T:
        peeked, self.peeked = self.peeked, None
        match peeked:
            case None:
                if self.done:
                    self.done = False
                    raise StopIteration
                return next(self.iter)
            case Error() as error:
                raise error
            case (item,):
                return item

    def clone(self) -> "Peekable[T]":
        clone = Peekable(self.iter.clone())
        clone.peeked = self.peeked
        clone.done = self.done
        return clone
