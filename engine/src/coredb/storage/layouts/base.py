"""Common interface of every storage model (layout)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterable, Iterator, List, Optional, Sequence, Tuple

from ...catalog.schema import Schema


class StorageLayout(ABC):
    """A table stored with a particular physical model.

    Record ids (rids) are opaque: NSM uses (page_id, slot), DSM uses a row position.
    """
    layout_name = "base"

    schema: Schema
    header_page_id: int

    @classmethod
    @abstractmethod
    def create(cls, pool, schema: Schema) -> "StorageLayout": ...

    @classmethod
    @abstractmethod
    def open(cls, pool, schema: Schema, header_page_id: int) -> "StorageLayout": ...

    @abstractmethod
    def insert(self, row: Sequence) -> Any: ...

    def insert_many(self, rows: Iterable[Sequence]) -> List[Any]:
        return [self.insert(r) for r in rows]

    @abstractmethod
    def get(self, rid: Any) -> Tuple: ...

    @abstractmethod
    def update(self, rid: Any, row: Sequence) -> Any:
        """Replace the row; returns the (possibly new) rid."""

    @abstractmethod
    def delete(self, rid: Any) -> None: ...

    @abstractmethod
    def scan(self, columns: Optional[Sequence[str]] = None) -> Iterator[Tuple[Any, Tuple]]:
        """Yield (rid, tuple).  `columns` projects (and reorders) the output."""

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def page_count(self) -> int: ...

    @abstractmethod
    def flush_meta(self) -> None:
        """Persist in-memory counters to the table header page."""

    @abstractmethod
    def drop(self) -> None:
        """Free every page owned by the table."""
