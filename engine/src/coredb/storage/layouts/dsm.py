"""DSM (Decomposition Storage Model): one ColumnStore per attribute.

* insert  : a tuple is split and each attribute appended to its own column pages
* get/scan: attributes are re-combined; scans only touch the projected columns
* rid     : the row position (0-based).  Positions change after sort_by()/compaction.
* delete  : lazily creates a hidden boolean "deleted" column (logical delete);
            sort_by() physically removes deleted rows.
* sort_by : permutes EVERY column with the same permutation, so tuples stay intact.

Table header page: <4s 'DSMH'><I n_cols><Q row_count><Q deleted_count><I deleted_col_hdr>
                   padding to 32, then one u32 column-header page id per column.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ...catalog.schema import Schema
from ...catalog.types import Column, DataType
from ...errors import CorruptDatabaseError, RecordNotFoundError, SchemaError
from ..page import INVALID_PAGE_ID
from .base import StorageLayout
from .column_store import SORT_ASC, SORT_DESC, ColumnStore

_HDR = struct.Struct("<4sIQQI")
_MAGIC = b"DSMH"
_IDS_OFF = 32
_DELETED = Column("__deleted__", DataType.BOOLEAN, nullable=False)


class DSMTable(StorageLayout):
    layout_name = "dsm"

    def __init__(self, pool, schema: Schema, header_page_id: int, cols: List[ColumnStore],
                 rows: int, deleted: int, deleted_col: Optional[ColumnStore]):
        self.pool, self.schema, self.header_page_id = pool, schema, header_page_id
        self._cols = cols
        self._rows, self._deleted_count, self._del = rows, deleted, deleted_col

    # ---- construction -----------------------------------------------------------
    @classmethod
    def create(cls, pool, schema: Schema) -> "DSMTable":
        if len(schema) > (pool.page_size - _IDS_OFF) // 4:
            raise SchemaError("too many columns for one header page")
        with pool.new() as h:
            hid = h.page_id
        cols = [ColumnStore.create(pool, c) for c in schema.columns]
        t = cls(pool, schema, hid, cols, 0, 0, None)
        t.flush_meta()
        return t

    @classmethod
    def open(cls, pool, schema: Schema, header_page_id: int) -> "DSMTable":
        with pool.pin(header_page_id) as h:
            magic, n, rows, deleted, del_hdr = _HDR.unpack_from(h.data, 0)
            if magic != _MAGIC or n != len(schema):
                raise CorruptDatabaseError("bad DSM table header")
            ids = [struct.unpack_from("<I", h.data, _IDS_OFF + 4 * i)[0] for i in range(n)]
        cols = [ColumnStore.open(pool, c, i) for c, i in zip(schema.columns, ids)]
        dcol = None if del_hdr == INVALID_PAGE_ID else ColumnStore.open(pool, _DELETED, del_hdr)
        return cls(pool, schema, header_page_id, cols, rows, deleted, dcol)

    def flush_meta(self) -> None:
        with self.pool.pin(self.header_page_id, write=True) as h:
            _HDR.pack_into(h.data, 0, _MAGIC, len(self._cols), self._rows, self._deleted_count,
                           self._del.header_page_id if self._del else INVALID_PAGE_ID)
            for i, c in enumerate(self._cols):
                struct.pack_into("<I", h.data, _IDS_OFF + 4 * i, c.header_page_id)
        for c in self._cols:
            c.flush_meta()
        if self._del:
            self._del.flush_meta()

    # ---- conversions -------------------------------------------------------------
    def _to_raws(self, row: Sequence) -> List[Any]:
        self.schema.check_row(row)
        return [None if v is None else c.to_storage(v) for c, v in zip(self.schema.columns, row)]

    def _from_raw(self, i: int, raw: Any) -> Any:
        c = self.schema.columns[i]
        return raw if raw is None or c.identity else c.from_storage(raw)

    # ---- writes ----------------------------------------------------------------------
    def insert(self, row: Sequence) -> int:
        raws = self._to_raws(row)                     # validate everything before touching pages
        for store, r in zip(self._cols, raws):
            store.append(r)
        if self._del:
            self._del.append(False)
        rid = self._rows
        self._rows += 1
        return rid

    def insert_many(self, rows: Sequence[Sequence]) -> List[int]:
        converted = [self._to_raws(r) for r in rows]
        if not converted:
            return []
        for i, store in enumerate(self._cols):        # column-at-a-time: sequential page access
            store.append_many([r[i] for r in converted])
        if self._del:
            self._del.append_many([False] * len(converted))
        start = self._rows
        self._rows += len(converted)
        return list(range(start, self._rows))

    def _check_rid(self, rid: int) -> None:
        if not isinstance(rid, int) or rid < 0 or rid >= self._rows:
            raise RecordNotFoundError(f"no row {rid}")
        if self._del and self._del.get(rid):
            raise RecordNotFoundError(f"row {rid} was deleted")

    def update(self, rid: int, row: Sequence) -> int:
        self._check_rid(rid)
        for store, r in zip(self._cols, self._to_raws(row)):
            store.set(rid, r)
        return rid

    def update_column(self, rid: int, column: str, value: Any) -> None:
        """Touch a single attribute: only that column's pages are read/written."""
        self._check_rid(rid)
        i = self.schema.index(column)
        c = self.schema.columns[i]
        if value is None and not c.nullable:
            raise SchemaError(f"column {c.name!r} is NOT NULL")
        self._cols[i].set(rid, None if value is None else c.to_storage(value))

    def delete(self, rid: int) -> None:
        self._check_rid(rid)
        if self._del is None:
            self._del = ColumnStore.create(self.pool, _DELETED)
            self._del.append_many([False] * self._rows)
        self._del.set(rid, True)
        self._deleted_count += 1

    # ---- reads -----------------------------------------------------------------------------
    def get(self, rid: int) -> Tuple:
        self._check_rid(rid)
        return tuple(self._from_raw(i, s.get(rid)) for i, s in enumerate(self._cols))

    def scan(self, columns: Optional[Sequence[str]] = None) -> Iterator[Tuple[int, Tuple]]:
        idx = list(range(len(self._cols))) if columns is None else [self.schema.index(c) for c in columns]
        iters = [self._cols[i].iter_values() for i in idx]
        conv = [None if self.schema.columns[i].identity else self.schema.columns[i].from_storage for i in idx]
        dels = self._del.iter_values() if (self._del and self._deleted_count) else None
        rid = 0
        for vals in zip(*iters):
            if dels is not None and next(dels):
                rid += 1
                continue
            yield rid, tuple(v if (cv is None or v is None) else cv(v) for v, cv in zip(vals, conv))
            rid += 1

    def scan_column(self, column: str) -> Iterator[Any]:
        """Values of a single column (skips deleted rows) - touches only that column's pages."""
        i = self.schema.index(column)
        c = self.schema.columns[i]
        dels = self._del.iter_values() if (self._del and self._deleted_count) else None
        for v in self._cols[i].iter_values():
            if dels is not None and next(dels):
                continue
            yield v if (v is None or c.identity) else c.from_storage(v)

    def column_stats(self, column: str) -> dict:
        return self._cols[self.schema.index(column)].stats()

    def count(self) -> int:
        return self._rows - self._deleted_count

    def page_count(self) -> int:
        return 1 + sum(c.page_count() for c in self._cols) + (self._del.page_count() if self._del else 0)

    # ---- sorting -----------------------------------------------------------------------------------
    def sort_by(self, column: str, descending: bool = False) -> None:
        """Physically reorder the table by `column`; NULLs last.  Compacts deleted rows.

        The same permutation is applied to every column so each tuple's attributes
        stay together.  (The permutation and one column at a time are held in memory.)
        """
        ki = self.schema.index(column)
        keys = self._cols[ki].read_all()
        dels = self._del.read_all() if self._del else [False] * len(keys)
        live = [i for i, d in enumerate(dels) if not d]
        nn = [i for i in live if keys[i] is not None]
        nulls = [i for i in live if keys[i] is None]
        nn.sort(key=keys.__getitem__, reverse=descending)   # stable
        order = nn + nulls
        for i, store in enumerate(self._cols):
            vals = keys if i == ki else store.read_all()
            store.replace_all([vals[j] for j in order])
        if self._del:
            self._del.drop()
            self._del = None
        self._rows, self._deleted_count = len(order), 0
        self._cols[ki].sort_state = SORT_DESC if descending else SORT_ASC
        self.flush_meta()

    def drop(self) -> None:
        for c in self._cols:
            c.drop()
        if self._del:
            self._del.drop()
        self.pool.delete_page(self.header_page_id)
