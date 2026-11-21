from __future__ import annotations

from typing import Dict, Iterable, Iterator, List, Sequence

from ..errors import SchemaError
from .types import Column


class Schema:
    def __init__(self, columns: Iterable[Column], name: str | None = None):
        self.name = name
        self.columns = tuple(columns)
        if not self.columns:
            raise SchemaError("a table needs at least one column")
        self._index: Dict[str, int] = {}
        for i, c in enumerate(self.columns):
            if c.name in self._index:
                raise SchemaError(f"duplicate column {c.name!r}")
            self._index[c.name] = i

    def __len__(self) -> int:
        return len(self.columns)

    def __iter__(self) -> Iterator[Column]:
        return iter(self.columns)

    @property
    def names(self) -> List[str]:
        return [c.name for c in self.columns]

    def index(self, name: str) -> int:
        try:
            return self._index[name]
        except KeyError:
            raise SchemaError(f"unknown column {name!r}") from None

    def column(self, name: str) -> Column:
        return self.columns[self.index(name)]

    def check_row(self, row: Sequence) -> None:
        if len(row) != len(self.columns):
            raise SchemaError(f"expected {len(self.columns)} values, got {len(row)}")
        for c, v in zip(self.columns, row):
            if v is None and not c.nullable:
                raise SchemaError(f"column {c.name!r} is NOT NULL")

    def to_dict(self) -> list:
        return [c.to_dict() for c in self.columns]

    @classmethod
    def from_dict(cls, items: list) -> "Schema":
        return cls(Column.from_dict(d) for d in items)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Schema) and self.name == other.name and self.columns == other.columns

    def __repr__(self) -> str:
        prefix = f"{self.name}, " if self.name else ""
        return f"Schema({prefix}{', '.join(f'{c.name}:{c.type.value}' for c in self.columns)})"
