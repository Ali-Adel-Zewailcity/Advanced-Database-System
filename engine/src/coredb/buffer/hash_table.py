"""Fixed-size hash table (separate chaining) mapping page id -> frame id.

The bucket array is allocated once, sized from the number of frames, and never
resizes: the pool can never hold more entries than it has frames, so chains stay short.
"""
from __future__ import annotations

from typing import Iterator, List, Optional, Tuple


def _next_prime(n: int) -> int:
    def is_prime(x: int) -> bool:
        if x < 2:
            return False
        i = 2
        while i * i <= x:
            if x % i == 0:
                return False
            i += 1
        return True
    while not is_prime(n):
        n += 1
    return n


class FixedSizeHashTable:
    __slots__ = ("_buckets", "_n", "_size")

    def __init__(self, capacity: int):
        self._n = _next_prime(max(3, 2 * capacity))      # load factor <= 0.5
        self._buckets: List[Optional[list]] = [None] * self._n
        self._size = 0

    def _find(self, key: int) -> Tuple[Optional[list], int]:
        b = self._buckets[key % self._n]
        if b is not None:
            for i, kv in enumerate(b):
                if kv[0] == key:
                    return b, i
        return b, -1

    def get(self, key: int, default=None):
        b, i = self._find(key)
        return default if i < 0 else b[i][1]

    def __getitem__(self, key: int):
        b, i = self._find(key)
        if i < 0:
            raise KeyError(key)
        return b[i][1]

    def __contains__(self, key: int) -> bool:
        return self._find(key)[1] >= 0

    def __setitem__(self, key: int, value: int) -> None:
        b, i = self._find(key)
        if i >= 0:
            b[i][1] = value
            return
        if b is None:
            b = self._buckets[key % self._n] = []
        b.append([key, value])
        self._size += 1

    def pop(self, key: int, *default):
        b, i = self._find(key)
        if i < 0:
            if default:
                return default[0]
            raise KeyError(key)
        _, v = b.pop(i)
        self._size -= 1
        return v

    def __delitem__(self, key: int) -> None:
        self.pop(key)

    def __len__(self) -> int:
        return self._size

    def __iter__(self) -> Iterator[int]:
        for b in self._buckets:
            if b:
                for k, _ in b:
                    yield k

    def items(self):
        for b in self._buckets:
            if b:
                for k, v in b:
                    yield k, v

    def values(self):
        for _, v in self.items():
            yield v
