"""NSM (N-ary Storage Model): row-oriented heap file of slotted pages."""
from __future__ import annotations

import struct
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from ...catalog.codec import decode_row, encode_row
from ...catalog.schema import Schema
from ...errors import CorruptDatabaseError, RecordNotFoundError
from ..page import INVALID_PAGE_ID
from .base import StorageLayout
from .slotted_page import SLOT_SIZE, SlottedPage

_HDR = struct.Struct("<4sIIQQ")   # magic, first_page, last_page, row_count, page_count
_MAGIC = b"NSMH"

RID = Tuple[int, int]             # (page_id, slot)


class NSMTable(StorageLayout):
    layout_name = "nsm"

    def __init__(self, pool, schema: Schema, header_page_id: int, first: int, last: int,
                 rows: int, pages: int):
        self.pool, self.schema, self.header_page_id = pool, schema, header_page_id
        self._first, self._last, self._rows, self._pages = first, last, rows, pages
        self._fsm: Dict[int, int] = {}      # page id -> free bytes (pages that lost data)

    # ---- construction ------------------------------------------------------
    @classmethod
    def create(cls, pool, schema: Schema) -> "NSMTable":
        with pool.new() as h:
            hid = h.page_id
        with pool.new() as d:
            SlottedPage.init(d.data)
            first = d.page_id
        t = cls(pool, schema, hid, first, first, 0, 1)
        t.flush_meta()
        return t

    @classmethod
    def open(cls, pool, schema: Schema, header_page_id: int) -> "NSMTable":
        with pool.pin(header_page_id) as h:
            magic, first, last, rows, pages = _HDR.unpack_from(h.data, 0)
        if magic != _MAGIC:
            raise CorruptDatabaseError("bad NSM table header")
        return cls(pool, schema, header_page_id, first, last, rows, pages)

    def flush_meta(self) -> None:
        with self.pool.pin(self.header_page_id, write=True) as h:
            _HDR.pack_into(h.data, 0, _MAGIC, self._first, self._last, self._rows, self._pages)

    # ---- helpers -------------------------------------------------------------
    def _try_page(self, pid: int, rec: bytes) -> Optional[int]:
        f = self.pool.fetch_page(pid)
        slot = None
        try:
            sp = SlottedPage(f.data)
            slot = sp.insert(rec)
            if pid in self._fsm:                      # keep the free-space map accurate
                self._fsm[pid] = sp.free_space()
        finally:
            self.pool.unpin_page(pid, slot is not None)
        return slot

    def _append_page(self) -> int:
        with self.pool.new() as d:
            SlottedPage.init(d.data)
            new = d.page_id
        with self.pool.pin(self._last, write=True) as last:
            SlottedPage(last.data).next_page = new
        self._last = new
        self._pages += 1
        self.flush_meta()
        return new

    # ---- API ---------------------------------------------------------------------
    def insert(self, row: Sequence) -> RID:
        rec = encode_row(self.schema, row)
        need = len(rec) + SLOT_SIZE
        slot = self._try_page(self._last, rec)
        pid = self._last
        if slot is None:
            for cand, free in list(self._fsm.items()):
                if free >= need and cand != self._last:
                    slot = self._try_page(cand, rec)
                    if slot is not None:
                        pid = cand
                        break
        if slot is None:
            pid = self._append_page()
            slot = self._try_page(pid, rec)
        self._rows += 1
        return (pid, slot)

    def get(self, rid: RID) -> Tuple:
        pid, slot = rid
        with self.pool.pin(pid) as f:
            rec = SlottedPage(f.data).get(slot)
        if rec is None:
            raise RecordNotFoundError(f"no record at {rid}")
        return decode_row(self.schema, rec)

    def delete(self, rid: RID) -> None:
        pid, slot = rid
        with self.pool.pin(pid, write=True) as f:
            sp = SlottedPage(f.data)
            ok = sp.delete(slot)
            free = sp.free_space()
        if not ok:
            raise RecordNotFoundError(f"no record at {rid}")
        self._fsm[pid] = free
        self._rows -= 1

    def update(self, rid: RID, row: Sequence) -> RID:
        pid, slot = rid
        rec = encode_row(self.schema, row)
        with self.pool.pin(pid, write=True) as f:
            sp = SlottedPage(f.data)
            if sp.get(slot) is None:
                raise RecordNotFoundError(f"no record at {rid}")
            if sp.update(slot, rec):
                return rid
        self.delete(rid)          # does not fit in its page any more: relocate
        return self.insert(row)

    def scan(self, columns: Optional[Sequence[str]] = None) -> Iterator[Tuple[RID, Tuple]]:
        idx = None if columns is None else [self.schema.index(c) for c in columns]
        pid = self._first
        while pid != INVALID_PAGE_ID:
            with self.pool.pin(pid) as f:         # materialise the page, then release the pin
                sp = SlottedPage(f.data)
                recs = list(sp.records())
                nxt = sp.next_page
            for slot, rec in recs:
                row = decode_row(self.schema, rec)
                yield (pid, slot), (row if idx is None else tuple(row[i] for i in idx))
            pid = nxt

    def count(self) -> int:
        return self._rows

    def page_count(self) -> int:
        return self._pages + 1       # + header page

    def _chain(self) -> List[int]:
        out, pid = [], self._first
        while pid != INVALID_PAGE_ID:
            out.append(pid)
            with self.pool.pin(pid) as f:
                pid = SlottedPage(f.data).next_page
        return out

    def rebuild_free_space_map(self) -> None:
        """Walk the heap once and remember every page with usable free space."""
        self._fsm.clear()
        for pid in self._chain():
            with self.pool.pin(pid) as f:
                free = SlottedPage(f.data).free_space()
            if free > 32:
                self._fsm[pid] = free

    def drop(self) -> None:
        for pid in self._chain():
            self.pool.delete_page(pid)
        self.pool.delete_page(self.header_page_id)
